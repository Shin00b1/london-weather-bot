"""Dawn-watcher — live paper-trading watcher for the London dawn-lock edge.

Runs through the pre-dawn window (03:00-07:30 UTC), polls the raw EGLC
METAR feed every 60s, applies Bot 1 (dawn-lock) and Bot 2 (03Z leader-1)
rules LIVE, walks the real CLOB ask ladder for paper fills, and logs every
poll to JSONL. This is the forward-validation instrument: it measures the
two things no backtest can — true sub-minute fill prices and the reprice
lag after each METAR print.

Modes:
    python3 dawn_watcher.py              # daemon: sleep till 03Z, poll to 07:30Z, exit
    python3 dawn_watcher.py --once       # single poll now (any time; smoke tests)
    python3 dawn_watcher.py --ensure     # start daemon unless one is already running
    python3 dawn_watcher.py --status     # print today's log + paper fills

Writes (no DuckDB — avoids lock clashes with book_recorder):
    london/dawn_watch_YYYYMM.jsonl   one record per poll
    london/dawn_paper_fills.csv      one row per paper fill
    london/dawn_state_YYYYMMDD.json  per-day fired flags (restart-safe)
    london/.dawn_watcher.pid
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LON = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
FCST_CSV = os.path.join(LONDON, "forecast_archive_d1.csv")
PIDFILE = os.path.join(LONDON, ".dawn_watcher.pid")
UA = {"User-Agent": "london-dawn-watcher/1.0"}

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
AW = "https://aviationweather.gov/api/data/metar?ids=EGLC&format=raw&hours=8"
IOWA = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

# --- strategy constants (must match the backtests exactly) ---
STAKE_DAWN = 100.0          # Bot 1 paper size
STAKE_EARLY = 50.0          # Bot 2 paper size (before liquidity cap)
SLIPPAGE_EARLY = 0.03
BAND_DAWN = (0.63, 0.97)
BAND_EARLY = (0.35, 0.85)   # on effective (ask+3c) price
EDGE_SKIP_C = 0.2
RANGE_THRESH = 8.0
MIN_OBS = 4                 # live day isn't complete; just need a few obs

WIN_START, WIN_END = 3.0, 7.5      # UTC hours
DAWN_WIN = (5.5, 6.5)              # Bot 1 trigger window
EARLY_WIN = (3.0, 4.0)             # Bot 2 trigger window
POLL_SEC = 60

# --- warm-season advection leg (2026-09-04, bot1_advection.py) ---
# On range<8 days the 06Z lock still holds when the sun is already up:
# dry warm-season advection days lock 84-96% (by year), and dry + pre-06Z min
# already <= D-1 fcst min + 1C locks 93.9%/94.1% (2025/2026, n=50) — the
# only conditioner identical across years. May-Sep only: April and October
# carry the break clusters; Nov-Mar is deep winter (54%) = still stand down.
WARM_ADV_MONTHS = (5, 6, 7, 8, 9)
ADV_COND_MARGIN = 1.0

RAIN_TOKENS = ("RA", "DZ", "TS", "SH", "FZRA", "GR", "UP")


def log(msg):
    print("[%s] %s" % (datetime.now(timezone.utc).strftime("%m-%d %H:%M:%SZ"), msg), flush=True)


def http_get(url, timeout=25, retries=1):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2)


def http_json(url, timeout=25):
    return json.loads(http_get(url, timeout))


# ---------------- METAR ----------------
def parse_raw_metar(line):
    """'METAR EGLC 281350Z 24008KT 9999 FEW030 18/12 Q1018' -> (valid_dt, tempc, rain)"""
    line = re.sub(r"^(METAR|SPECI)\s+", "", line.strip())
    m = re.match(r"^(EGLC)\s+(\d{2})(\d{2})(\d{2})Z\s+(.*)$", line)
    if not m:
        return None
    dd, hh, mm = int(m.group(2)), int(m.group(3)), int(m.group(4))
    body = m.group(5).split()
    now = datetime.now(timezone.utc)
    try:
        valid = now.replace(day=dd, hour=hh, minute=mm, second=0, microsecond=0)
        if (valid - now).total_seconds() > 12 * 3600:
            raise ValueError("obs more than 12h ahead -> previous month")
    except ValueError:
        pm = now.replace(day=1) - timedelta(days=1)
        valid = pm.replace(day=dd, hour=hh, minute=mm, second=0, microsecond=0)
    if (now - valid).total_seconds() > 12 * 3600:      # month rollover
        pm = now.replace(day=1) - timedelta(days=1)
        valid = pm.replace(day=dd, hour=hh, minute=mm, second=0, microsecond=0)
    temp = rain = None
    for tok in body:
        if "/" in tok and re.match(r"^(M?\d{1,2})/(M?\d{1,2})$", tok):
            t = tok.split("/")[0]
            temp = -int(t[1:]) if t.startswith("M") else int(t)
        t2 = tok.strip("-+")
        if t2.lstrip("VC").startswith(RAIN_TOKENS):
            rain = True
    if temp is None:
        return None
    return valid, float(temp), bool(rain)


def fetch_metar_aw():
    """Primary: aviationweather raw feed (seconds-fresh). -> list of obs."""
    txt = http_get(AW, timeout=20)
    out = []
    for line in txt.strip().split("\n"):
        if not line.strip():
            continue
        p = parse_raw_metar(line)
        if p:
            out.append(p)
    return out


def fetch_metar_iowa(day):
    """Fallback: Iowa mesonet (minutes-fresh)."""
    base = {"station": "EGLC", "data": "tmpc,wxcodes",
            "year1": str(day.year), "month1": str(day.month), "day1": str(day.day),
            "year2": str(day.year), "month2": str(day.month), "day2": str(day.day),
            "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
            "missing": "M", "trace": "T", "direct": "no"}
    txt = ""
    for rtype in ("3", "4"):
        pairs = list(base.items()) + [("report_type", rtype)]
        qs = "&".join("%s=%s" % kv for kv in pairs)
        try:
            txt += "\n" + http_get(IOWA + "?" + qs, timeout=40)
        except Exception:
            pass
    out = []
    for line in txt.strip().split("\n"):
        parts = line.split(",")
        if len(parts) < 3 or parts[0] != "EGLC" or parts[2] in ("M", "T", ""):
            continue
        try:
            dt = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        wx = parts[3] if len(parts) > 3 else ""
        is_rain = any(t.strip("-+").startswith(RAIN_TOKENS) for t in wx.split()) if wx and wx != "M" else False
        out.append((dt, float(parts[2]), is_rain))
    return out


def merge_obs(seen, obs_list):
    """dedupe by minute; return list of newly-seen obs"""
    new = []
    for dt, t, rain in obs_list:
        key = dt.strftime("%Y%m%d%H%M")
        if key not in seen:
            seen.add(key)
            new.append((dt, t, rain))
    return new


# ---------------- market ----------------
def today_slug(direction="lowest"):
    d = datetime.now(timezone.utc).date()
    return "%s-temperature-in-london-on-%s-%d-%d" % (direction, MONTHS[d.month - 1], d.day, d.year)


def discover_buckets(direction="lowest"):
    """-> {bucket_int: {"label":.., "token":.., "kind":"c"/"below"/"above"}}"""
    try:
        ev = http_json(GAMMA + "/events?slug=" + today_slug(direction))
    except Exception:
        return None
    if not ev:
        return None
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
        mm = re.match(r"^(\d+)c$", label)
        if mm:
            out[int(mm.group(1))] = {"label": label, "token": toks[0], "kind": "c"}
            continue
        mm = re.match(r"^(\d+)corbelow$", label)
        if mm:
            out[int(mm.group(1))] = {"label": label, "token": toks[0], "kind": "below"}
            continue
        mm = re.match(r"^(\d+)corhigher$", label)
        if mm:
            out[int(mm.group(1))] = {"label": label, "token": toks[0], "kind": "above"}
    return out


def fetch_book(token):
    try:
        b = http_json(CLOB + "/book?token_id=" + token, timeout=15)
    except Exception:
        return None
    asks = sorted([{"p": float(x["price"]), "s": float(x["size"])} for x in (b.get("asks") or [])],
                  key=lambda x: x["p"])
    bids = sorted([{"p": float(x["price"]), "s": float(x["size"])} for x in (b.get("bids") or [])],
                  key=lambda x: -x["p"])
    return {"bids": bids, "asks": asks}


def walk_ask(book, dollars):
    """fill `dollars` lifting asks -> (vwap_price, filled_usd) or None"""
    if not book or not book["asks"]:
        return None
    spent = shares = 0.0
    for lvl in book["asks"]:
        take = min(lvl["s"] * lvl["p"], dollars - spent)
        if take <= 0:
            break
        spent += take
        shares += take / lvl["p"]
        if spent >= dollars - 1e-9:
            break
    if shares <= 0:
        return None
    return spent / shares, spent


def book_summary(book):
    if not book:
        return {"best_bid": None, "best_ask": None, "ask_depth_usd": 0.0}
    asks = book["asks"]
    return {"best_bid": book["bids"][0]["p"] if book["bids"] else None,
            "best_ask": asks[0]["p"] if asks else None,
            "ask_depth_usd": round(sum(l["p"] * l["s"] for l in asks), 2)}


# ---------------- signal ----------------
_REGIME_MEMO = {}   # date_iso -> ((rng, fcst_min) or (None, None), epoch_ts)


def load_regime():
    """D-1 ECMWF diurnal range + forecast min for today's London date.

    Returns (rng, fcst_min); rng may be None when unavailable. The csv row
    for "today" only lands with the ~10:30Z morning update — AFTER the dawn
    window closes — so between 00Z and the update we fetch the D-1 00Z
    ecmwf_ifs run live (same source the archive is built from) and memoize
    per day (failures memoized 15 min to be polite to the API).
    """
    d = datetime.now(timezone.utc).astimezone(LON).date()
    diso = d.isoformat()
    now = time.time()
    memo = _REGIME_MEMO.get(diso)
    if memo and (memo[0][0] is not None or now - memo[1] < 900):
        return memo[0]
    rng = None
    fmin = None
    try:
        with open(FCST_CSV) as f:
            for r in csv.DictReader(f):
                if r["date"] == diso:
                    rng = float(r["d1_ecmwf_max"]) - float(r["d1_ecmwf_min"])
                    fmin = float(r["d1_ecmwf_min"])
                    break
    except Exception:
        pass
    if rng is None:
        try:
            import rebuild_forecast_archive as rfa
            mx, mn = rfa.d1_forecast(d)
            rng = float(mx) - float(mn)
            fmin = float(mn)
        except Exception:
            rng = None
            fmin = None
    _REGIME_MEMO[diso] = ((rng, fmin), now)
    return (rng, fmin)


def bucket_of(n, buckets):
    return buckets.get(n)


def record_fill(row):
    path = os.path.join(LONDON, "dawn_paper_fills.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "leg", "utc_time", "bucket", "stake_usd", "vwap_fill",
                        "best_ask", "ask_depth_usd", "pre6_min", "regime_range", "rain", "note"])
        w.writerow(row)


# ---------------- main poll ----------------
def poll(seen, buckets_cache):
    now = datetime.now(timezone.utc)
    today = now.date()
    hour = now.hour + now.minute / 60.0

    # obs
    src = "aw"
    try:
        obs_all = fetch_metar_aw()
    except Exception as e:
        log("aw feed failed: %r" % e)
        try:
            obs_all = fetch_metar_iowa(today)
            src = "iowa"
        except Exception as e2:
            log("iowa fallback failed: %r" % e2)
            return
    new = merge_obs(seen, obs_all)
    latency = None
    if new:
        freshest = max(new, key=lambda o: o[0])
        latency = (now - freshest[0]).total_seconds()

    pre6 = sorted((dt, t, r) for dt, t, r in obs_all
                  if dt.date() == today and dt.hour < 6)
    if not pre6:
        log("no pre-06Z obs yet (n_all=%d)" % len(obs_all))
        return
    pre6_min = min(t for _, t, _ in pre6)
    rain_pre6 = any(r for _, _, r in pre6)
    call = round(pre6_min)
    edge_dist = 0.5 - abs(pre6_min - call)
    rng, fcst_min = load_regime()
    rising = len(pre6) >= 2 and pre6[-1][1] >= pre6_min + 0.5

    # Bot 1 leg selection: radiation days trade as before; on advection days
    # (<8C range) the dawn-lock only fires May-Sep, dry, with the min already
    # at/below the D-1 forecast min + 1C (94% lock both years, n=50).
    if rng is None:
        leg = None
    elif rng >= RANGE_THRESH:
        leg = "dawn"
    elif (today.month in WARM_ADV_MONTHS and not rain_pre6
          and fcst_min is not None and pre6_min <= fcst_min + ADV_COND_MARGIN):
        leg = "dawn-adv"
    else:
        leg = None

    # market books for call-1, call, call+1
    if buckets_cache.get("day") != today:
        buckets_cache["day"] = today
        buckets_cache["map"] = discover_buckets("lowest") or {}
    buckets = buckets_cache["map"]
    snap = {}
    for n in sorted({call - 1, call, call + 1}):
        b = bucket_of(n, buckets)
        if not b:
            continue
        book = fetch_book(b["token"])
        snap[b["label"]] = book_summary(book)
        if n == call:
            snap["_call_book"] = book

    # state flags (restart-safe via file)
    dstr = today.isoformat()
    state_path = os.path.join(LONDON, "dawn_state_%s.json" % dstr)
    state = {"early": False, "dawn": False}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path))
        except Exception:
            pass

    note = []
    # ---- Bot 2: 03Z leader-1 ----
    if EARLY_WIN[0] <= hour < EARLY_WIN[1] and not state.get("early"):
        target = call - 1
        tb = bucket_of(target, buckets)
        if tb:
            tbook = fetch_book(tb["token"])
            if tbook and tbook["asks"]:
                ask = tbook["asks"][0]["p"]
                eff = ask + SLIPPAGE_EARLY
                depth = sum(l["p"] * l["s"] for l in tbook["asks"])
                if rng is not None and rng >= RANGE_THRESH and BAND_EARLY[0] <= eff <= BAND_EARLY[1]:
                    stake = min(STAKE_EARLY, depth)
                    fill = walk_ask(tbook, stake)
                    if fill:
                        record_fill([dstr, "early", now.strftime("%H:%M:%S"), tb["label"],
                                     round(stake, 2), round(fill[0], 4), ask, round(depth, 2),
                                     pre6_min, rng, int(rain_pre6),
                                     "leader-1 band ok"])
                        state["early"] = True
                        log("PAPER FILL early %s @%.3f ($%.0f)" % (tb["label"], fill[0], stake))
                else:
                    note.append("early-skip rng=%s eff=%.2f" % (rng, eff))

    # ---- Bot 1: dawn-lock ----
    if DAWN_WIN[0] <= hour < DAWN_WIN[1] and not state.get("dawn"):
        cb = snap.get("_call_book")
        label = "%dc" % call
        if cb and cb["asks"]:
            fill = walk_ask(cb, STAKE_DAWN)
            if fill:
                price = fill[0]
                if (leg is not None and edge_dist >= EDGE_SKIP_C
                        and BAND_DAWN[0] <= price <= BAND_DAWN[1]):
                    record_fill([dstr, leg, now.strftime("%H:%M:%S"), label,
                                 STAKE_DAWN, round(price, 4), cb["asks"][0]["p"],
                                 round(sum(l["p"] * l["s"] for l in cb["asks"]), 2),
                                 pre6_min, rng, int(rain_pre6),
                                 "all vetoes pass" if leg == "dawn" else
                                 "warm-advection: dry + min<=fcstmin+1C"])
                    state["dawn"] = True
                    log("PAPER FILL %s %s @%.3f ($%.0f)" % (leg, label, price, STAKE_DAWN))
                else:
                    note.append("dawn-skip leg=%s rng=%s rain=%s fmin=%s edge=%.2f p=%.2f" %
                                (leg, rng, rain_pre6, fcst_min, edge_dist, price))
        else:
            note.append("dawn-no-book")

    json.dump(state, open(state_path, "w"))

    rec = {"ts": now.isoformat(), "src": src, "hour": round(hour, 3),
           "n_obs": len(pre6), "pre6_min": pre6_min, "call": call,
           "latest": {"t": pre6[-1][0].isoformat(), "v": pre6[-1][1]} if pre6 else None,
           "rising": rising, "rain_pre6": rain_pre6, "edge_dist": round(edge_dist, 3),
           "regime_range": rng, "new_obs": len(new),
           "new_obs_latency_s": round(latency, 1) if latency is not None else None,
           "fired": state, "books": {k: v for k, v in snap.items() if k != "_call_book"},
           "note": ";".join(note)}
    path = os.path.join(LONDON, "dawn_watch_%s.jsonl" % now.strftime("%Y%m"))
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    log("poll %s call=%d pre6min=%.1f rng=%s ask=%s %s" % (
        src, call, pre6_min, ("%.1f" % rng) if rng is not None else "?",
        snap.get("%dc" % call, {}).get("best_ask"), ("; ".join(note)) if note else ""))


def seconds_until_start(now):
    """seconds until the next 03:00 UTC window start"""
    start = now.replace(hour=int(WIN_START), minute=int((WIN_START % 1) * 60),
                        second=0, microsecond=0)
    if now >= start:
        start += timedelta(days=1)
    return (start - now).total_seconds()


def daemon():
    log("daemon up; next window starts in %.1f h" % (seconds_until_start(datetime.now(timezone.utc)) / 3600))
    while True:
        now = datetime.now(timezone.utc)
        gap = seconds_until_start(now)
        if gap > 0:
            time.sleep(min(gap, 300))   # sleep toward the window in 5-min chunks
            continue
        h = now.hour + now.minute / 60.0
        if h >= WIN_END:
            log("window closed — exiting")
            cleanup_pid()
            return
        seen, bc = set(), {"day": None, "map": {}}
        while True:
            try:
                poll(seen, bc)
            except Exception as e:
                log("poll failed: %r" % e)
            time.sleep(POLL_SEC)
            nh = datetime.now(timezone.utc)
            if nh.hour + nh.minute / 60.0 >= WIN_END:
                log("window closed — exiting")
                cleanup_pid()
                return


def run_today():
    """Launchd-safe single-window runner: run ONLY today's dawn window, then exit.

    Unlike daemon() (which rolls to the *next* window start and would sit all day
    if launched late), this exits immediately when today's window is already past.
    That keeps a launchd + `caffeinate -s` wrapper from pinning the Mac awake for
    18+ hours on a missed start.
    """
    now = datetime.now(timezone.utc)
    h = now.hour + now.minute / 60.0
    if h >= WIN_END:
        log("today's window already closed (%.2f >= %.1f) — nothing to do" % (h, WIN_END))
        return
    if h < WIN_START:
        sleep_s = (WIN_START - h) * 3600
        log("sleeping %.1f min until %04d UTC window start" % (sleep_s / 60, int(WIN_START * 100)))
        time.sleep(sleep_s)
    seen, bc = set(), {"day": None, "map": {}}
    while True:
        try:
            poll(seen, bc)
        except Exception as e:
            log("poll failed: %r" % e)
        time.sleep(POLL_SEC)
        nh = datetime.now(timezone.utc)
        if nh.hour + nh.minute / 60.0 >= WIN_END:
            log("window closed — exiting")
            return


def cleanup_pid():
    try:
        os.remove(PIDFILE)
    except FileNotFoundError:
        pass


def already_running():
    if not os.path.exists(PIDFILE):
        return False
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def status():
    d = datetime.now(timezone.utc).date()
    path = os.path.join(LONDON, "dawn_watch_%s.jsonl" % d.strftime("%Y%m"))
    recs = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["ts"][:10] == d.isoformat():
                    recs.append(r)
    print("today polls logged: %d" % len(recs))
    lats = [r["new_obs_latency_s"] for r in recs
            if r.get("new_obs") and r.get("new_obs_latency_s") is not None]
    if lats:
        lats.sort()
        print("obs-detection latency s: median %.0f / p90 %.0f / max %.0f (n=%d)" %
              (lats[len(lats) // 2], lats[int(len(lats) * 0.9)], lats[-1], len(lats)))
    # ask path of the final call bucket
    calls = [r.get("call") for r in recs if r.get("call") is not None]
    if calls:
        call = calls[-1]
        pts = []
        for r in recs:
            b = r.get("books", {}).get("%dc" % call)
            if b and b.get("best_ask") is not None:
                pts.append((r["ts"][11:16], b["best_ask"]))
        if pts:
            print("call=%dc ask path: %s" % (call,
                  " ".join("%s:%.3f" % p for p in pts[-12:])))
    fills = os.path.join(LONDON, "dawn_paper_fills.csv")
    if os.path.exists(fills):
        with open(fills) as f:
            rows = [r for r in csv.DictReader(f) if r["date"] == d.isoformat()]
        print("today paper fills: %d" % len(rows))
        for r in rows:
            print("  %s %s %s @%s $%s" % (r["utc_time"], r["leg"], r["bucket"], r["vwap_fill"], r["stake_usd"]))
    else:
        print("no paper fills yet")


def main():
    args = sys.argv[1:]
    if "--status" in args:
        status()
        return
    if "--ensure" in args:
        if already_running():
            print("dawn-watcher already running (pid %s)" % open(PIDFILE).read().strip())
            return
        # Relaunch detached so --ensure returns immediately. The old code wrote
        # the pidfile then called daemon() inline, which blocks the caller (and
        # any automation step) forever. The child runs main() with no args, hits
        # the plain-daemon path below, and writes its own pidfile.
        devnull = open(os.devnull, "wb")
        subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                         stdin=devnull, stdout=devnull, stderr=devnull,
                         start_new_session=True, close_fds=True)
        print("dawn-watcher started (detached)")
        return
    if "--once" in args:
        poll(set(), {"day": None, "map": {}})
        return
    if "--run-today" in args:
        run_today()
        return
    # plain daemon
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    daemon()


if __name__ == "__main__":
    main()
