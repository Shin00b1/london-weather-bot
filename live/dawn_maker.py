"""Dawn-maker — paper market-making instrument for the London dawn-lock edge.

Same signal as dawn_watcher (pre-06Z EGLC METAR daily-min, regime gate, rain
veto), but instead of crossing the ask it MODELS posting resting limit orders,
which is where the durable money is (see london-max-market-who-profits):

  winner bucket   : resting BID below mid  -> buy the locked winner cheap
  neighbor bucket : resting ASK above mid  -> sell the loser (short the field)
  in-play buckets : thin TWO-SIDED quotes inside the reward band to qualify
                    for Polymarket Liquidity Rewards + 25% Weather maker rebate.

PAPER ONLY — there is no CLOB private key in .env, so nothing is posted. The
script records, every poll, what a resting quote would look like, whether it
would be inside the reward band (within rewardsMaxSpread of mid, >= rewardsMinSize
shares), and an estimate of our share of that bucket's daily reward pool. That
is the one-day measurement that turns the reward projection into fact.

Modes:
    python3 dawn_maker.py --once       single poll now (smoke test)
    python3 dawn_maker.py --run        foreground loop 03Z-20Z, poll 60s
    python3 dawn_maker.py --ensure     start detached loop unless running
    python3 dawn_maker.py --status     today's reward-qualification summary

Writes (no DuckDB):
    london/dawn_maker_YYYYMM.jsonl    one record per poll
    london/dawn_maker_rewards.csv     per-bucket reward-qualification summary
"""
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dawn_watcher as dw  # reuse METAR + signal primitives

LON = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
PIDFILE = os.path.join(LONDON, ".dawn_maker.pid")

# --- maker strategy constants ---
QUOTE_HALF = 0.02          # distance from mid we rest at (2c, inside the 4.5c band)
WIN_SIZE = 100.0           # directional size (shares) on the winner bid
NEIGH_SIZE = 100.0         # directional size (shares) on the neighbor ask
REWARD_SIZE = 100.0        # two-sided size (shares) per in-play bucket (>= min size)
RANGE_THRESH = 8.0         # radiation regime gate (same as dawn_watcher)
BAND_MIN_SIZE = 100        # default rewardsMinSize if the market omits it

WIN_START, WIN_END = 3.0, 20.0   # UTC: quote 03Z-20Z (reward accrues all day)
POLL_SEC = 60
DIRECTION = "lowest"


def log(msg):
    print("[%s] %s" % (datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ"), msg), flush=True)


def discover_buckets(direction="lowest"):
    """-> {bucket_int: {label, token, kind, tick, min_size, max_spread, daily_rate}}"""
    try:
        ev = dw.http_json(dw.GAMMA + "/events?slug=" + dw.today_slug(direction))
    except Exception as e:
        log("discover failed: %r" % e)
        return {}
    if not ev:
        return {}
    out = {}
    for m in ev[0].get("markets", []):
        if m.get("closed"):
            continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except ValueError:
            continue
        if not outcomes or not toks:
            continue
        label = (m.get("slug") or "").rsplit("-", 1)[-1]
        mm = re_match_bucket(label)
        if not mm:
            continue
        n, kind = mm
        cr = m.get("clobRewards") or []
        daily = 0.0
        if cr:
            daily = float(cr[0].get("rewardsDailyRate") or 0)
        out[n] = {
            "label": label, "token": toks[0], "kind": kind,
            "tick": float(m.get("orderPriceMinTickSize") or 0.01),
            "min_size": float(m.get("rewardsMinSize") or BAND_MIN_SIZE),
            "max_spread": float(m.get("rewardsMaxSpread") or 0.045),
            "daily_rate": daily,
        }
    return out


def re_match_bucket(label):
    import re
    mm = re.match(r"^(\d+)c$", label)
    if mm:
        return int(mm.group(1)), "="
    mm = re.match(r"^(\d+)corbelow$", label)
    if mm:
        return int(mm.group(1)), "<="
    mm = re.match(r"^(\d+)corhigher$", label)
    if mm:
        return int(mm.group(1)), ">="
    return None


def in_band(price, mid, max_spread):
    return price is not None and abs(price - mid) <= max_spread


def book_mid(book):
    if not book or not book["bids"] or not book["asks"]:
        return None
    return (book["bids"][0]["p"] + book["asks"][0]["p"]) / 2.0


def other_qual_depth(book, mid, max_spread):
    """Total resting size (shares) currently inside the reward band (excl. ours)."""
    tot = 0.0
    for lvl in book["bids"]:
        if mid - lvl["p"] <= max_spread:
            tot += lvl["s"]
        else:
            break
    for lvl in book["asks"]:
        if lvl["p"] - mid <= max_spread:
            tot += lvl["s"]
        else:
            break
    return tot


def model_quotes(book, mid, bid_size, ask_size):
    """Resting bid/ask modeled around mid, clipped to tick, with fill flags."""
    if not book or mid is None:
        return None
    tick = 0.01
    bid_p = round(mid - QUOTE_HALF, 3)
    ask_p = round(mid + QUOTE_HALF, 3)
    best_bid = book["bids"][0]["p"] if book["bids"] else None
    best_ask = book["asks"][0]["p"] if book["asks"] else None
    return {
        "bid_p": bid_p, "ask_p": ask_p,
        "bid_size": bid_size, "ask_size": ask_size,
        # our resting bid would be filled if the ask has already traded through it
        "bid_filled": best_ask is not None and best_ask <= bid_p,
        "ask_filled": best_bid is not None and best_bid >= ask_p,
    }


def poll(buckets_cache):
    now = datetime.now(timezone.utc)
    today = now.date()
    hour = now.hour + now.minute / 60.0
    if hour < WIN_START or hour >= WIN_END:
        return

    # --- signal (identical to dawn_watcher) ---
    try:
        obs_all = dw.fetch_metar_aw()
        src = "aw"
    except Exception:
        try:
            obs_all = dw.fetch_metar_iowa(today)
            src = "iowa"
        except Exception as e:
            log("metar failed: %r" % e)
            return
    pre6 = sorted((dt, t, r) for dt, t, r in obs_all
                  if dt.date() == today and dt.hour < 6)
    pre6_min = min((t for _, t, _ in pre6), default=None)
    rain_pre6 = any(r for _, _, r in pre6)
    rng = dw.load_regime()
    call = round(pre6_min) if pre6_min is not None else None

    if buckets_cache.get("day") != today:
        buckets_cache["day"] = today
        buckets_cache["map"] = discover_buckets(DIRECTION)
    buckets = buckets_cache["map"]

    rows = []
    # regime gate: directional tilt only on clean radiation days (same as dawn_watcher).
    # Rain is NOT vetoed — measured 2026-08-31: rain is redundant with the rng>=8 gate
    # (rain+radiation locks 91%, identical to dry+radiation). See memory.
    regime_ok = rng is not None and rng >= RANGE_THRESH
    # in-play buckets = call-1, call, call+1 (the reward-funded ones are near call)
    targets = sorted({c for c in (call - 1, call, call + 1) if c is not None} if call is not None else [])
    for n in targets:
        b = buckets.get(n)
        if not b:
            continue
        book = dw.fetch_book(b["token"])
        if not book:
            continue
        mid = book_mid(book)
        if mid is None:
            continue
        max_spread = b["max_spread"] / 100.0 if b["max_spread"] > 1 else b["max_spread"]
        is_winner = (n == call)
        # baseline two-sided quote (always on, for reward measurement + rebate)
        bid_size = ask_size = max(REWARD_SIZE, b["min_size"])
        # directional tilt (edge overlay, radiation days only):
        #   big bid on the winner (buy the lock cheap), big ask on the neighbor (short the loser)
        if regime_ok:
            if is_winner:
                bid_size += WIN_SIZE
            else:
                ask_size += NEIGH_SIZE
        q = model_quotes(book, mid, bid_size, ask_size)
        if q is None:
            continue
        # two-sided reward qualification
        qual_bid = in_band(q["bid_p"], mid, max_spread) and q["bid_size"] >= b["min_size"]
        qual_ask = in_band(q["ask_p"], mid, max_spread) and q["ask_size"] >= b["min_size"]
        qualifying = qual_bid and qual_ask
        other = other_qual_depth(book, mid, max_spread)
        my_size = q["bid_size"] + q["ask_size"]
        share = my_size / (my_size + other) if qualifying and (my_size + other) > 0 else 0.0
        rows.append({
            "utc": now.strftime("%H:%M:%S"), "bucket": b["label"], "kind": b["kind"],
            "is_winner": is_winner, "mid": round(mid, 4),
            "bid_p": q["bid_p"], "ask_p": q["ask_p"],
            "bid_size": q["bid_size"], "ask_size": q["ask_size"],
            "qual_bid": qual_bid, "qual_ask": qual_ask, "qualifying": qualifying,
            "other_qual_depth": round(other, 2), "share": round(share, 4),
            "daily_rate": b["daily_rate"],
            "bid_filled": q["bid_filled"], "ask_filled": q["ask_filled"],
            "pre6_min": pre6_min, "regime_range": rng, "rain": int(rain_pre6),
        })

    rec = {"ts": now.isoformat(), "src": src, "hour": round(hour, 3),
           "pre6_min": pre6_min, "call": call, "rain": int(rain_pre6),
           "regime_range": rng, "quotes": rows}
    path = os.path.join(LONDON, "dawn_maker_%s.jsonl" % now.strftime("%Y%m"))
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if rows:
        qs = [r for r in rows if r["qualifying"]]
        log("poll call=%s rng=%s | %d buckets, %d qualifying | %s" % (
            call, ("%.1f" % rng) if rng is not None else "?",
            len(rows), len(qs),
            " ".join("%s:%.2f" % (r["bucket"], r["share"]) for r in qs)))
    else:
        log("poll call=%s rng=%s | no buckets/books" % (call, rng))


def status():
    d = datetime.now(timezone.utc).date()
    path = os.path.join(LONDON, "dawn_maker_%s.jsonl" % d.strftime("%Y%m"))
    if not os.path.exists(path):
        print("no dawn_maker log today")
        return
    recs = [json.loads(l) for l in open(path)]
    today = [r for r in recs if r["ts"][:10] == d.isoformat()]
    print("polls today: %d" % len(today))
    # aggregate reward-qualification per bucket
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "qual_min": 0, "share_sum": 0.0, "rate": 0.0})
    for r in today:
        for q in r.get("quotes", []):
            a = agg[q["bucket"]]
            a["n"] += 1
            if q["qualifying"]:
                a["qual_min"] += 1
                a["share_sum"] += q["share"]
            a["rate"] = q["daily_rate"]
    print("\nbucket  polls  qual_min  avg_share  daily_rate  est_daily_reward_usd")
    for b in sorted(agg):
        a = agg[b]
        # daily pool = daily_rate, split pro-rata by time-weighted share.
        # avg_share is our mean share of that bucket's qualifying depth while
        # we were quoting; est = avg_share * daily_rate (assumes all-day quoting).
        est = (a["share_sum"] / max(1, a["n"])) * a["rate"]
        print("%-7s %5d %8d %10.3f %11.0f %20.2f" % (
            b, a["n"], a["qual_min"], a["share_sum"] / max(1, a["n"]), a["rate"], est))


def main():
    args = sys.argv[1:]
    if "--status" in args:
        status()
        return
    if "--once" in args:
        poll({"day": None, "map": {}})
        return
    if "--ensure" in args:
        if os.path.exists(PIDFILE):
            try:
                pid = int(open(PIDFILE).read().strip())
                os.kill(pid, 0)
                print("dawn-maker already running (pid %d)" % pid)
                return
            except (ValueError, ProcessLookupError, OSError):
                pass
        devnull = open(os.devnull, "wb")
        subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                         stdin=devnull, stdout=devnull, stderr=devnull,
                         start_new_session=True, close_fds=True)
        print("dawn-maker started (detached)")
        return
    # foreground loop
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log("dawn-maker up; window %02dZ-%02dZ, poll %ds" % (WIN_START, WIN_END, POLL_SEC))
    bc = {"day": None, "map": {}}
    try:
        while True:
            now = datetime.now(timezone.utc)
            hour = now.hour + now.minute / 60.0
            if hour >= WIN_END:
                log("window closed — exiting")
                break
            if hour >= WIN_START:
                try:
                    poll(bc)
                except Exception as e:
                    log("poll failed: %r" % e)
            time.sleep(POLL_SEC)
    finally:
        try:
            os.remove(PIDFILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
