"""field-maker — paper two-sided market maker for the London HIGHEST market.

Rationale (measured 2026-08-28/31): the max market is the one London weather
book where MAKERS as a group profit (~$209/day across ~15 wallets; takers lose
~$48k/115d; field spread ~17c). Directional Yes has no edge; quoting does.
Min-side making already measured NEGATIVE (adverse selection) — out of scope.

Strategy (paper, no keys — nothing is posted):
  1. Quote EVERY bucket two-sided at mid ± QUOTE_HALF, clamped to
     join-or-improve (bid <= live best bid, ask >= live best ask) so the
     paper cannot manufacture instant fills by crossing its own market.
  2. A quote fills ONLY when the tape prints strictly THROUGH its price
     (bid: trade < bid_p ; ask: trade > ask_p) or the live book is seen
     crossed through it at a later poll. This undercounts fills — the
     conservative bias a paper maker needs (queue position is unknowable).
  3. Requote when mid moves >= 1 tick from the quoted mid, a side is
     consumed, or the quote is STALE_SEC old.
  4. Separate paper book: dead-bucket harvest (Jolly-Skate pattern) — once a
     single bucket is >= HARVEST_DELTA below the running max after 12Z and
     its Yes bid is still >= 0.03, buy No at 1 - yes_bid; exit at
     yes_ask <= 0.003 (sell No ~0.997) or at resolution.
  5. Cash accounting; at resolution winner Yes -> 1.00, loser -> 0.00,
     dead-bucket No -> 1.00 / winner-bucket No -> 0.00.

Modes:
    python3 field_maker.py --once      single poll (smoke test)
    python3 field_maker.py --run       foreground loop 06Z-20Z, 60s
    python3 field_maker.py --ensure    start detached loop unless running
    python3 field_maker.py --status    today's quotes/fills/PnL summary

Writes:
    london/field_maker_YYYYMM.jsonl    per-poll snapshots + fills
    london/field_maker_days.csv        one settled row per day
    london/field_maker_state.json      restart-safe paper state
"""
import csv
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import dawn_watcher as dw

HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
PIDFILE = os.path.join(LONDON, ".field_maker.pid")
STATE = os.path.join(LONDON, "field_maker_state.json")
DATA_API = "https://data-api.polymarket.com"
UA = {"User-Agent": "london-field-maker/1.0"}

# --- strategy constants (pre-registered before the first paper day) ---
QUOTE_HALF = 0.02        # our resting distance from the book mid (2c)
SIZE = 50.0              # shares per side per bucket
MAX_POS = 200.0          # per-bucket inventory cap (shares)
REQUOTE_TICKS = 1        # requote when mid moves this many ticks
STALE_SEC = 600          # cancel/replace age
WIN_START, WIN_END = 6.0, 20.0   # UTC quoting window
POLL_SEC = 60
DIRECTION = "highest"

# dead-bucket harvest (Jolly pattern, paper)
HARVEST_START_HOUR = 12.0
HARVEST_DELTA = 2.0      # bucket must be >= this far below the running max
HARVEST_MIN_BID = 0.03   # lazy Yes money still bid >= 3c
HARVEST_EXIT_ASK = 0.003
HARVEST_SIZE = 50.0
HARVEST_MAX_OPEN = 6


def log(msg):
    print("[%s] %s" % (datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ"), msg),
          flush=True)


# ---------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


# ------------------------------------------------------------ discovery

def discover():
    """-> {n: {label,kind,token,cond,tick,rate,min_size}} for today's highest."""
    try:
        ev = dw.http_json(dw.GAMMA + "/events?slug=" + dw.today_slug(DIRECTION))
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
        import re
        mm = re.match(r"^(\d+)c$", label)
        kind = "="
        if not mm:
            mm = re.match(r"^(\d+)corbelow$", label)
            kind = "<="
        if not mm:
            mm = re.match(r"^(\d+)corhigher$", label)
            kind = ">="
        if not mm:
            continue
        out[int(mm.group(1))] = {
            "label": label, "kind": kind, "token": toks[0],
            "cond": m.get("conditionId"),
            "tick": float(m.get("orderPriceMinTickSize") or 0.01),
            "rate": float((m.get("clobRewards") or [{}])[0].get("rewardsDailyRate") or 0),
            "min_size": float(m.get("rewardsMinSize") or 100),
            "max_spread": (lambda s: s / 100.0 if s > 1 else float(s or 0.045))(
                m.get("rewardsMaxSpread") or 0.045),
        }
    return out


# ------------------------------------------------------------ metar obs

def get_obs(day):
    try:
        obs = dw.fetch_metar_aw()
        src = "aw"
        if any(o[0].date() == day for o in obs):
            return obs, src
    except Exception:
        pass
    try:
        return dw.fetch_metar_iowa(day), "iowa"
    except Exception:
        return [], "none"


def day_max_from_obs(obs, day):
    ts = [t for dt, t, *_ in obs if dt.date() == day]
    return (max(ts), len(ts)) if ts else (None, 0)


# ---------------------------------------------------------------- fills

def check_fills(trades, token, bid_p, ask_p):
    """Strict-through fills: (bid_fill_shares, ask_fill_shares)."""
    bf = af = 0.0
    for ts, side, price, size in trades:
        if bid_p is not None and side == "SELL" and price < bid_p:
            bf += size
        elif ask_p is not None and side == "BUY" and price > ask_p:
            af += size
    return bf, af


def book_crossed(book, bid_p, ask_p):
    """Fill flags if the live book has traded through our resting quotes."""
    bf = af = False
    if book.get("asks") and bid_p is not None and book["asks"][0]["p"] < bid_p:
        bf = True
    if book.get("bids") and ask_p is not None and book["bids"][0]["p"] > ask_p:
        af = True
    return bf, af


def fetch_trades(cond, after_ts):
    """Recent trades for a condition -> [(ts, side, price, size, asset)]."""
    if not cond:
        return []
    try:
        rows = dw.http_json("%s/trades?market=%s&limit=100" % (DATA_API, cond))
    except Exception:
        return []
    out = []
    for r in rows or []:
        try:
            ts = int(r["timestamp"])
            if ts <= after_ts:
                continue
            out.append((ts, r.get("side"), float(r["price"]),
                        float(r["size"]), r.get("asset")))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


# ------------------------------------------------------------- settle

def settle(st, today):
    """Settle yesterday's book if METAR for it is available."""
    yday = st.get("day")
    if not yday or yday == today.isoformat():
        return
    yd = datetime.fromisoformat(yday).date()
    try:
        obs, _ = get_obs(yd)
        mx, n = day_max_from_obs(obs, yd)
    except Exception:
        mx, n = None, 0
    if mx is None or n < 16:
        return   # keep state; settle when data exists
    win = round(mx)
    cash = 0.0
    fills = 0
    bk = st.get("buckets", {})
    for n_, b in bk.items():
        cash += b.get("cash", 0.0)
        fills += b.get("n_fills", 0)
        pos = b.get("pos", 0.0)
        if int(n_) == win:
            cash += pos * 1.0
        # losers settle to 0
    hv = st.get("harvest", {})
    hcash = 0.0
    for n_, h in hv.items():
        cash += h.get("cash", 0.0)
        fills += h.get("n_fills", 0)
        if int(n_) != win:      # dead bucket: No pays 1
            cash += h.get("pos", 0.0) * 1.0
    row = {"date": yday, "metar_max": mx, "winner": win,
           "mm_fills": sum(b.get("n_fills", 0) for b in bk.values()),
           "mm_cash": round(sum(b.get("cash", 0.0) for b in bk.values()), 4),
           "harvest_fills": sum(h.get("n_fills", 0) for h in hv.values()),
           "harvest_cash": round(sum(h.get("cash", 0.0) for h in hv.values()), 4),
           "pnl": round(cash + hcash, 4)}
    path = os.path.join(LONDON, "field_maker_days.csv")
    new = not os.path.exists(path)
    with open(path, "a") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)
    log("SETTLED %s winner=%d pnl=%.2f (mm %.2f / harvest %.2f)"
        % (yday, win, row["pnl"], row["mm_cash"], row["harvest_cash"]))
    st["day"] = today.isoformat()
    st["buckets"] = {}
    st["harvest"] = {}
    st["trades_ts"] = {}
    st["quotes"] = {}


# ---------------------------------------------------------------- poll

def poll(st):
    now = datetime.now(timezone.utc)
    day = now.date()
    hour = now.hour + now.minute / 60.0
    if hour < WIN_START or hour >= WIN_END:
        return
    settle(st, day)

    if st.get("day") != day.isoformat():
        st.clear()
        st["day"] = day.isoformat()
        st["buckets"] = {}
        st["harvest"] = {}
        st["trades_ts"] = {}
        st["quotes"] = {}

    buckets = discover()
    if not buckets:
        log("no buckets discovered")
        return

    obs, src = get_obs(day)
    ts = [t for dt, t, *_ in obs if dt.date() == day]
    run_max = max(ts) if ts else None
    call = round(run_max) if run_max is not None else None

    quotes = st.setdefault("quotes", {})
    bk_state = st.setdefault("buckets", {})
    hv_state = st.setdefault("harvest", {})
    trades_ts = st.setdefault("trades_ts", {})

    # pull trades once per condition, cache, then route per bucket-asset
    conds = {b["cond"] for b in buckets.values() if b["cond"]}
    trades_by_token = defaultdict(list)
    for c in conds:
        after = trades_ts.get(c, int((now - timedelta(minutes=15)).timestamp()))
        rows = fetch_trades(c, after)
        if rows:
            trades_ts[c] = max(t for t, *_ in rows)
        for t, side, price, size, asset in rows:
            trades_by_token[asset].append((t, side, price, size))

    snap = {"ts": now.isoformat(), "run_max": run_max, "call": call,
            "quotes": [], "fills": [], "harvest": []}

    for n, b in sorted(buckets.items()):
        key = str(n)
        book = dw.fetch_book(b["token"])
        if not book or not book.get("bids") or not book.get("asks"):
            continue
        bid0, ask0 = book["bids"][0]["p"], book["asks"][0]["p"]
        mid = (bid0 + ask0) / 2.0
        bs = bk_state.setdefault(key, {"pos": 0.0, "cash": 0.0, "n_fills": 0,
                                       "avg": None})
        q = quotes.get(key)
        tk = b["tick"]

        # 1) fills on the working quote (each event capped at our size)
        if q:
            can_bid = bs["pos"] < MAX_POS
            can_ask = bs["pos"] > -MAX_POS
            t_list = trades_by_token.get(b["token"], [])
            bf, af = check_fills(t_list, b["token"], q["bid_p"], q["ask_p"])
            cbf, caf = book_crossed(book, q["bid_p"], q["ask_p"])
            if cbf:
                bf = max(bf, SIZE)
            if caf:
                af = max(af, SIZE)
            if bf and can_bid:
                fill = min(bf, SIZE)
                bs["pos"] += fill
                bs["cash"] -= q["bid_p"] * fill
                bs["n_fills"] += 1
                snap["fills"].append({"t": now.isoformat(), "bucket": b["label"],
                                      "side": "BUY", "p": q["bid_p"], "sz": fill})
                q = None
            if af and can_ask:
                fill = min(af, SIZE)
                bs["pos"] -= fill
                bs["cash"] += q["ask_p"] * fill
                bs["n_fills"] += 1
                snap["fills"].append({"t": now.isoformat(), "bucket": b["label"],
                                      "side": "SELL", "p": q["ask_p"], "sz": fill})
                q = None

        # 2) requote decision (sides gated by the inventory cap)
        need = q is None
        if q:
            if abs(mid - q["mid"]) >= REQUOTE_TICKS * tk:
                need = True
            if now.timestamp() - q["ts"] > STALE_SEC:
                need = True
        if need:
            bid_p = min(round(mid - QUOTE_HALF, 2), bid0)   # join-or-improve
            ask_p = max(round(mid + QUOTE_HALF, 2), ask0)
            bid_p = max(bid_p, tk)
            ask_p = min(ask_p, 1 - tk)
            if bs["pos"] >= MAX_POS:
                bid_p = None
            if bs["pos"] <= -MAX_POS:
                ask_p = None
            quotes[key] = {"bid_p": bid_p, "ask_p": ask_p,
                           "mid": mid, "ts": now.timestamp(),
                           "gen": quotes.get(key, {}).get("gen", 0) + 1}
            q = quotes[key]

        mtm = bs["cash"] + bs["pos"] * mid
        in_band = bool(
            q["bid_p"] is not None and q["ask_p"] is not None
            and abs(q["bid_p"] - mid) <= b["max_spread"]
            and abs(q["ask_p"] - mid) <= b["max_spread"]
            and SIZE >= b["min_size"])
        snap["quotes"].append({
            "bucket": b["label"], "kind": b["kind"], "mid": round(mid, 4),
            "bid_p": q["bid_p"], "ask_p": q["ask_p"],
            "pos": round(bs["pos"], 1), "fills": bs["n_fills"],
            "mtm": round(mtm, 4), "rate": b["rate"],
            "qual_rewards": in_band,
        })

        # 3) dead-bucket harvest (singles only, clearly dead)
        if (b["kind"] == "=" and call is not None and hour >= HARVEST_START_HOUR
                and (call - n) >= HARVEST_DELTA and bid0 >= HARVEST_MIN_BID):
            h = hv_state.get(key)
            if h is None and len(hv_state) < HARVEST_MAX_OPEN:
                no_p = 1.0 - bid0
                hv_state[key] = {"pos": HARVEST_SIZE, "cash": -no_p * HARVEST_SIZE,
                                 "entry": no_p, "n_fills": 1, "ts": now.isoformat()}
                snap["harvest"].append({"t": now.isoformat(), "bucket": b["label"],
                                        "action": "BUY_NO", "p": no_p, "sz": HARVEST_SIZE})
        elif key in hv_state:
            h = hv_state[key]
            if ask0 <= HARVEST_EXIT_ASK:
                exit_p = 1.0 - ask0
                h["cash"] += exit_p * h["pos"]
                snap["harvest"].append({"t": now.isoformat(), "bucket": b["label"],
                                        "action": "SELL_NO", "p": exit_p, "sz": h["pos"]})
                hv_state.pop(key)

    st["quotes"] = quotes
    path = os.path.join(LONDON, "field_maker_%s.jsonl" % now.strftime("%Y%m"))
    with open(path, "a") as f:
        f.write(json.dumps(snap) + "\n")
    save_state(st)
    nq = len(snap["quotes"])
    nfl = len(snap["fills"])
    nhv = len(snap["harvest"])
    mtm = sum(r["mtm"] for r in snap["quotes"])
    hcash = sum(h.get("cash", 0.0) for h in hv_state.values())
    log("poll run_max=%s call=%s | %d quoted %d fills %d hv-act | mm-mtm %.2f hv-open %.2f"
        % (run_max, call, nq, nfl, nhv, mtm, hcash))


# --------------------------------------------------------------- status

def status():
    d = datetime.now(timezone.utc).date()
    path = os.path.join(LONDON, "field_maker_%s.jsonl" % d.strftime("%Y%m"))
    if not os.path.exists(path):
        print("no field_maker log today")
        return
    recs = [json.loads(l) for l in open(path)]
    today = [r for r in recs if r["ts"][:10] == d.isoformat()]
    fills = [f for r in today for f in r.get("fills", [])]
    hv = [h for r in today for h in r.get("harvest", [])]
    buys = sum(1 for f in fills if f["side"] == "BUY")
    print("polls today: %d   fills: %d (BUY %d / SELL %d)   harvest acts: %d"
          % (len(today), len(fills), buys, len(fills) - buys, len(hv)))
    days_path = os.path.join(LONDON, "field_maker_days.csv")
    if os.path.exists(days_path):
        print("\nsettled days:")
        print(open(days_path).read())


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    if "--status" in args:
        status()
        return
    if "--once" in args:
        poll(load_state())
        return
    if "--ensure" in args:
        now = datetime.now(timezone.utc)
        hour = now.hour + now.minute / 60.0
        if not (WIN_START - 0.2 <= hour < WIN_END):
            print("outside window (%.1fZ) — not starting" % hour)
            return
        if os.path.exists(PIDFILE):
            try:
                pid = int(open(PIDFILE).read().strip())
                os.kill(pid, 0)
                print("field-maker already running (pid %d)" % pid)
                return
            except (ValueError, ProcessLookupError, OSError):
                pass
        logfile = open(os.path.join(LONDON, "field_maker.log"), "ab")
        devnull = open(os.devnull, "wb")
        subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                         stdin=devnull, stdout=logfile, stderr=logfile,
                         start_new_session=True, close_fds=True)
        print("field-maker started (detached)")
        return
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log("field-maker up; window %02dZ-%02dZ poll %ds | half=%.2f size=%.0f"
        % (WIN_START, WIN_END, POLL_SEC, QUOTE_HALF, SIZE))
    st = load_state()
    try:
        while True:
            now = datetime.now(timezone.utc)
            hour = now.hour + now.minute / 60.0
            if hour >= WIN_END:
                log("window closed — exiting")
                break
            if hour >= WIN_START:
                try:
                    poll(st)
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
