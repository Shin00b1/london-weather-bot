"""Maker vs taker backtest of the London dawn-lock (lowest) edge.

Answers: if instead of CROSSING the ask (taker) to buy the locked winner, we
POSTED a resting bid (maker), what would the fill rate, fill price, and P&L be?

Reference taker entry  = ask-side VWAP of taker-buys-Yes in [05:30,06:30) UTC.
Maker entry            = resting bid at limit L; filled iff a taker SELLS Yes
                         at price <= L anytime 06Z -> resolution. Fill = L.

Three sub-strategies scored:
  TAKER   : buy winner at ask-side VWAP [05:30,06:30)
  MAKER   : resting bid L on winner (several L values)
  SHORT   : sell ALL non-winner buckets at their ask (short the field), collect
            premium; lose (1 - sell_price) on the bucket that actually wins.

Read-only. Uses parquet (Apr15-Jul18) + tape (Jul19-Aug27) trades.
"""
import csv
import json
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

LON = ZoneInfo("Europe/London")
METAR = "london/metar_eglc.csv"
FCST = "london/forecast_archive_d1.csv"
ROSTER = "london/lowest_slugs_apr_jul.json"
DB = "/tmp/tape_snap.duckdb"
PARQUET = "london/london_temperature_unified_trades.parquet"

D0 = date(2026, 4, 15)
D1 = date(2026, 8, 27)
ENTRY_WIN = (5.5, 6.5)      # [05:30, 06:30) UTC
EDGE_SKIP_C = 0.2
REGIME_MIN_RANGE = 8.0
MIN_OBS = 16
RAIN_TOKENS = ("RA", "DZ", "TS", "SH", "FZRA", "GR", "UP")


def code_is_rain(code):
    if not code or code == "M":
        return False
    return any(t.lstrip("+-").startswith(RAIN_TOKENS) for t in code.split())


def load_metar():
    obs = defaultdict(list)
    rain = defaultdict(list)
    for row in csv.DictReader(open(METAR)):
        try:
            t = float(row["tmpc"])
            dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        d = dt.astimezone(LON).date()
        obs[d].append((dt, t))
        rain[d].append((dt, code_is_rain(row.get("wxcodes"))))
    return obs, rain


def load_fcst():
    out = {}
    for row in csv.DictReader(open(FCST)):
        try:
            out[datetime.strptime(row["date"], "%Y-%m-%d").date()] = \
                float(row["d1_ecmwf_max"]) - float(row["d1_ecmwf_min"])
        except (ValueError, KeyError):
            continue
    return out


def slug_to_parts(slug):
    m = re.search(r"-(\d+)c(orbelow|orhigher)?$", slug)
    if not m:
        return None
    n = int(m.group(1))
    suf = m.group(2)
    label = ("<=%d" % n) if suf == "orbelow" else (">=%d" % n) if suf == "orhigher" else ("%dc" % n)
    m2 = re.search(r"-on-([a-z]+)-(\d+)-2026", slug)
    if not m2:
        return None
    mon = datetime.strptime(m2.group(1), "%B").month
    return date(2026, mon, int(m2.group(2))), n, label


def build_trades():
    """-> {day: {bucket_n: {'buy_yes':[(ts,p_yes)], 'sell_yes':[(ts,p_yes)]}}}
    buy_yes = taker ends net-long Yes (crosses ask) -> fills a maker ASK
    sell_yes = taker ends net-short Yes (crosses bid) -> fills a maker BID
    """
    int_map = defaultdict(dict)
    trades = defaultdict(lambda: defaultdict(lambda: {"buy_yes": [], "sell_yes": []}))

    # parquet Apr15..Jul18
    roster = {s["condition_id"]: s["slug"] for s in json.load(open(ROSTER))}
    con = duckdb.connect()
    con.execute("CREATE VIEW p AS SELECT * FROM read_parquet('%s')" % PARQUET)
    cid_list = ",".join("'%s'" % c for c in roster)
    rows = con.execute(
        "SELECT timestamp, condition_id, prob_yes, nonusdc_side, taker_direction "
        "FROM p WHERE condition_id IN (%s)" % cid_list).fetchall()
    con.close()
    for ts, cid, prob, ns, td in rows:
        slug = roster.get(cid)
        parts = slug_to_parts(slug) if slug else None
        if not parts:
            continue
        d, n, label = parts
        if not (D0 <= d <= date(2026, 7, 18)):
            continue
        int_map[d][n] = label
        p = float(prob)
        if ns == "token1" and td == "BUY":      # buys Yes
            trades[d][n]["buy_yes"].append((ts, p))
        elif ns == "token2" and td == "SELL":   # sells No = buys Yes
            trades[d][n]["buy_yes"].append((ts, p))
        elif ns == "token1" and td == "SELL":   # sells Yes -> fills maker bid
            trades[d][n]["sell_yes"].append((ts, p))
        elif ns == "token2" and td == "BUY":    # buys No = sells Yes -> fills maker bid
            trades[d][n]["sell_yes"].append((ts, p))

    # tape Jul19..Aug27
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT timestamp, slug, outcome, side, price FROM trades
        WHERE slug LIKE '%lowest%'
    """).fetchall()
    con.close()
    for ts, slug, outcome, side, price in rows:
        parts = slug_to_parts(slug)
        if not parts:
            continue
        d, n, label = parts
        if not (date(2026, 7, 19) <= d <= D1):
            continue
        int_map[d][n] = label
        p_yes = float(price) if outcome == "Yes" else 1.0 - float(price)
        if (outcome == "Yes" and side == "BUY") or (outcome == "No" and side == "SELL"):
            trades[d][n]["buy_yes"].append((ts, p_yes))
        else:  # (Yes SELL) or (No BUY) -> sells Yes -> fills maker bid
            trades[d][n]["sell_yes"].append((ts, p_yes))

    for d in trades:
        for n in trades[d]:
            trades[d][n]["buy_yes"].sort()
            trades[d][n]["sell_yes"].sort()
    return int_map, trades


def vwap_ask(tlist, day):
    """taker buy of Yes VWAP over [05:30,06:30) UTC."""
    start = datetime(day.year, day.month, day.day, int(ENTRY_WIN[0]),
                     int((ENTRY_WIN[0] % 1) * 60), tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, int(ENTRY_WIN[1]),
                   int((ENTRY_WIN[1] % 1) * 60), tzinfo=timezone.utc)
    w = [t for t in tlist if start.timestamp() <= t[0] < end.timestamp()]
    if not w:
        return None, 0
    num = sum(p for _, p in w)
    return num / len(w), len(w)


def maker_fill(sell_yes, day, L):
    """resting bid L posted at 06Z; filled iff a sell-Yes prints <= L after 06Z."""
    t0 = datetime(day.year, day.month, day.day, 6, tzinfo=timezone.utc).timestamp()
    for ts, p in sell_yes:
        if ts >= t0 and p <= L + 1e-9:
            return True
    return False


def main():
    obs, rain = load_metar()
    fcst = load_fcst()
    int_map, trades = build_trades()

    # radiation dawn-lock days
    days = []
    cur = D0
    while cur <= D1:
        pts = obs.get(cur, [])
        if len(pts) < MIN_OBS:
            cur += timedelta(days=1)
            continue
        pre = [v for dt, v in pts if dt.hour < 6]
        if not pre:
            cur += timedelta(days=1)
            continue
        pre_min = min(pre)
        call = round(pre_min)
        r = fcst.get(cur)
        if r is None or r < REGIME_MIN_RANGE:
            cur += timedelta(days=1)
            continue
        cutoff = datetime(cur.year, cur.month, cur.day, 6, tzinfo=timezone.utc)
        if any(rr for dt, rr in rain.get(cur, []) if dt < cutoff):
            cur += timedelta(days=1)
            continue
        if 0.5 - abs(pre_min - call) < EDGE_SKIP_C:
            cur += timedelta(days=1)
            continue
        days.append(cur)
        cur += timedelta(days=1)

    day_min = {d: min(v for _, v in obs[d]) for d in obs}
    day_max = {d: max(v for _, v in obs[d]) for d in obs}

    # ---- TAKER reference ----
    taker = []  # (entry, win)
    for d in days:
        call = round(min(v for dt, v in obs[d] if dt.hour < 6))
        winner = round(day_min[d])
        label = int_map.get(d, {}).get(call, "%dc" % call)
        n = call
        buy = trades.get(d, {}).get(n, {}).get("buy_yes", [])
        entry, cnt = vwap_ask(buy, d)
        if entry is None:
            continue
        taker.append((entry, call == winner))

    # ---- MAKER (resting bid L on winner) ----
    print("=== MAKER vs TAKER on radiation dawn-lock days (Apr15-Aug27) ===\n")
    print("Reference TAKER: %d trades" % len(taker))
    if taker:
        w = sum(1 for _, hit in taker if hit)
        avg = statistics.mean(e for e, _ in taker)
        print("  win %.0f%%  avg ask-entry %.3f  net on $1/trade %.2f" %
              (100 * w / len(taker), avg, sum((1/e - 1) if hit else -1 for e, hit in taker)))

    print("\n%-8s %6s %6s %6s %7s %8s %8s" % ("bid L", "fills", "win%", "avgP", "EV/$1", "sum/$50", "trades/day"))
    for L in (0.85, 0.88, 0.90, 0.92, 0.94, 0.96):
        res = []
        for d in days:
            call = round(min(v for dt, v in obs[d] if dt.hour < 6))
            winner = round(day_min[d])
            n = call
            sell = trades.get(d, {}).get(n, {}).get("sell_yes", [])
            if not maker_fill(sell, d, L):
                continue
            res.append((L, call == winner))
        if not res:
            print("%-8s %6d %6s %6s %7s %8s" % (L, 0, "-", "-", "-", "-"))
            continue
        w = sum(1 for _, hit in res if hit)
        ev = [(1/L - 1) if hit else -1 for _, hit in res]
        print("%-8s %6d %5.0f%% %6.3f %+7.3f %+8.2f" %
              (L, len(res), 100 * w / len(res), L, statistics.mean(ev), 50 * sum(ev)))

    # ---- SHORT THE FIELD (sell all non-winner buckets at ask) ----
    print("\n=== SHORT THE FIELD (sell all non-winner buckets at ask, 06Z) ===")
    short = []
    for d in days:
        call = round(min(v for dt, v in obs[d] if dt.hour < 6))
        winner = round(day_min[d])
        # sum of asks across non-winner buckets in entry window
        total_ask = 0.0
        sold = 0
        for n, info in trades.get(d, {}).items():
            if n == call:
                continue
            buy = info["buy_yes"]
            entry, _ = vwap_ask(buy, d)  # ask-side price of this bucket
            if entry is None:
                continue
            total_ask += entry
            sold += 1
        if sold == 0:
            continue
        # if our call is right: keep total_ask premium (each loser -> 0)
        # if wrong: lose 1 - (sell price of the actual winner)
        if call == winner:
            pnl = total_ask
        else:
            # we sold the actual winner bucket at its ask price -> lose (1 - ask)
            win_ask = None
            win_buy = trades.get(d, {}).get(winner, {}).get("buy_yes", [])
            win_ask, _ = vwap_ask(win_buy, d)
            if win_ask is None:
                win_ask = 1.0
            pnl = total_ask - (1.0 - win_ask)
        short.append((pnl, call == winner, total_ask))
    if short:
        w = sum(1 for _, hit, _ in short if hit)
        print("days: %d  win %.0f%%  avg premium collected %.3f" %
              (len(short), 100 * w / len(short), statistics.mean(x[2] for x in short)))
        print("net on $1/cross-bucket: %.2f  (sum of all-bucket asks minus winner payout)" % sum(x[0] for x in short))
        print("per day: mean %+.3f  median %+.3f" %
              (statistics.mean(x[0] for x in short), statistics.median(x[0] for x in short)))


if __name__ == "__main__":
    main()
