#!/usr/bin/env python3
"""Bot 1 backtest — CORRECTED entry (ask-side, not bid+ask VWAP).

Fixes the slippage flaw: the original used all-trades VWAP, which averages
taker BUYs (ask) and taker SELLs (bid) and is systematically too cheap. A
buyer can only cross the ask. This version prices entry at what a buyer of
the Yes token actually pays:

  - tape (Jul 19+):   taker BUY of Yes  -> outcome='Yes' AND side='BUY'
  - parquet (Apr-Jul18): taker BUY of token1(Yes) -> nonusdc_side='token1'
                        AND taker_direction='BUY', price=prob_yes

Window: [05:30, 06:30) UTC — the bot's realistic action time once the 06Z
METAR is in. No extra slippage adder (ask-side VWAP already reflects crossing
the spread); size effects reported separately.

Read-only. Prints every trade + money-math.
"""
import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

LON = ZoneInfo("Europe/London")
METAR = "london/metar_eglc.csv"
FCST = "london/forecast_archive_d1.csv"
ROSTER = "london/lowest_slugs_apr_jul.json"
DB = "london/tape.duckdb"
PARQUET = "london/london_temperature_unified_trades.parquet"

STAKE = 100.0
ENTRY_WIN = (5.5, 6.5)          # [05:30, 06:30) UTC
EDGE_SKIP_C = 0.2
BAND = (0.63, 0.97)
REGIME_MIN_RANGE = 8.0
MIN_OBS = 16
RAIN_TOKENS = ("RA", "DZ", "TS", "SH", "FZRA", "GR", "UP")

D0 = date(2026, 4, 15)
D1 = date(2026, 9, 3)


def code_is_rain(code):
    if not code or code == "M":
        return False
    return any(t.lstrip("+-").startswith(RAIN_TOKENS) for t in code.split())


def load_metar():
    obs = defaultdict(list)
    rain = defaultdict(list)
    with open(METAR) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                d = dt.astimezone(LON).date()
                obs[d].append((dt, float(row["tmpc"])))
                rain[d].append((dt, code_is_rain(row.get("wxcodes"))))
            except (ValueError, KeyError):
                continue
    return obs, rain


def load_fcst():
    out = {}
    with open(FCST) as f:
        for row in csv.DictReader(f):
            try:
                out[row["date"]] = float(row["d1_ecmwf_max"]) - float(row["d1_ecmwf_min"])
            except (KeyError, ValueError):
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


def build_price_lookup():
    int_map = defaultdict(dict)
    trades = defaultdict(lambda: defaultdict(list))  # day -> {label: [(ts, p_ask, weight)]}

    # parquet (Apr 15 .. Jul 18): token1 = Yes. A taker ends net-long Yes in
    # two forms, both at price `prob_yes`:
    #   nonusdc_side='token1' AND taker_direction='BUY'  (buys Yes)
    #   nonusdc_side='token2' AND taker_direction='SELL' (sells No)
    roster = {}
    for s in json.load(open(ROSTER)):
        roster[s["condition_id"]] = s["slug"]
    con = duckdb.connect()
    con.execute("CREATE VIEW p AS SELECT * FROM read_parquet('%s')" % PARQUET)
    cid_list = ",".join("'%s'" % c for c in roster)
    rows = con.execute(
        "SELECT timestamp, condition_id, prob_yes, usd_amount, nonusdc_side, taker_direction "
        "FROM p WHERE condition_id IN (%s) AND ("
        "  (nonusdc_side='token1' AND taker_direction='BUY') OR "
        "  (nonusdc_side='token2' AND taker_direction='SELL'))"
        % cid_list).fetchall()
    for ts, cid, prob, usd, ns, td in rows:
        slug = roster.get(cid)
        parts = slug_to_parts(slug) if slug else None
        if not parts:
            continue
        d, n, label = parts
        if not (D0 <= d <= date(2026, 7, 18)):
            continue
        int_map[d][n] = label
        trades[d][label].append((ts, float(prob), float(usd or 0)))
    con.close()

    # tape (Jul 19 .. Aug 27): taker ends net-long Yes in two forms —
    #   (outcome=Yes AND side=BUY)  buys Yes at `price`
    #   (outcome=No AND side=SELL)  sells No at `price` -> buys Yes at 1-price
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT timestamp, slug, outcome, price, size FROM trades
        WHERE slug LIKE '%lowest%'
          AND ((outcome='Yes' AND side='BUY') OR (outcome='No' AND side='SELL'))
    """).fetchall()
    for ts, slug, outcome, price, size in rows:
        parts = slug_to_parts(slug)
        if not parts:
            continue
        d, n, label = parts
        if not (date(2026, 7, 19) <= d <= D1):
            continue
        p_yes = float(price) if outcome == "Yes" else 1.0 - float(price)
        int_map[d][n] = label
        trades[d][label].append((ts, p_yes, float(size)))
    con.close()

    for d in trades:
        for label in trades[d]:
            trades[d][label].sort()
    return int_map, trades


def vwap_window(tlist, day):
    start = datetime(day.year, day.month, day.day, int(ENTRY_WIN[0]),
                     int((ENTRY_WIN[0] % 1) * 60), tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, int(ENTRY_WIN[1]),
                   int((ENTRY_WIN[1] % 1) * 60), tzinfo=timezone.utc)
    w = [t for t in tlist if start.timestamp() <= t[0] < end.timestamp()]
    if w:
        num = sum(p * x for _, p, x in w)
        den = sum(x for _, p, x in w)
        return num / den, len(w)
    return None, 0


def main():
    obs, rain = load_metar()
    fcst = load_fcst()
    int_map, trades = build_price_lookup()

    print("=== Bot 1 backtest — CORRECTED entry (ask-side BUY only) ===")
    print("entry = VWAP of taker-BUY-of-Yes in [05:30,06:30) UTC (no slippage adder)")
    print("-" * 96)
    print("%-10s %5s %5s %5s %-6s %-6s %6s %-4s %8s %8s  %s" % (
        "date", "pre", "day", "call", "winner", "entry", "hit", "pnl", "cum", "", "note"))
    print("-" * 96)

    taken = wins = 0
    skipped = defaultdict(int)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    monthly = defaultdict(float)
    entries = []

    cur = D0
    while cur <= D1:
        pts = obs.get(cur, [])
        if len(pts) < MIN_OBS:
            skipped["no_metar"] += 1
            cur += timedelta(days=1)
            continue
        day_min = min(v for _, v in pts)
        pre = [v for dt, v in pts if dt.hour < 6]
        if not pre:
            skipped["illiquid"] += 1
            cur += timedelta(days=1)
            continue
        pre_min = min(pre)
        call = round(pre_min)
        winner = round(day_min)

        r = fcst.get(cur.isoformat())
        if r is None or r < REGIME_MIN_RANGE:
            skipped["regime"] += 1
            cur += timedelta(days=1)
            continue
        cutoff = datetime(cur.year, cur.month, cur.day, 6, tzinfo=timezone.utc)
        if any(is_rain for dt, is_rain in rain.get(cur, []) if dt < cutoff):
            skipped["rain"] += 1
            cur += timedelta(days=1)
            continue
        dist = 0.5 - abs(pre_min - call)
        if dist < EDGE_SKIP_C:
            skipped["edge"] += 1
            cur += timedelta(days=1)
            continue

        label = int_map.get(cur, {}).get(call)
        if label is None:
            label = "%dc" % call
        tlist = trades.get(cur, {}).get(label, [])
        entry, n = vwap_window(tlist, cur)
        if entry is None:
            skipped["illiquid"] += 1
            cur += timedelta(days=1)
            continue
        if entry < BAND[0] or entry > BAND[1]:
            skipped["band"] += 1
            cur += timedelta(days=1)
            continue

        taken += 1
        hit = (call == winner)
        if hit:
            pnl = STAKE * (1.0 / entry - 1.0)
            wins += 1
            note = "win"
        else:
            pnl = -STAKE
            note = "LOSS"
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        monthly[cur.strftime("%Y-%m")] += pnl
        entries.append(entry)

        print("%-10s %5.1f %5.1f %5d %-6d %-6s %6.3f %-4s %+8.2f %+8.2f  %s" % (
            cur, pre_min, day_min, call, winner, label, entry,
            "Y" if hit else "N", pnl, cum, note))
        cur += timedelta(days=1)

    print("-" * 96)
    print("\n=== CORRECTED MONEY MATH ===")
    wr = wins / taken * 100 if taken else 0
    print("trades taken: %d | wins %d (%.1f%%)" % (taken, wins, wr))
    print("skips: %s" % dict(skipped))
    print("net P&L: %+.2f on $%d/trade" % (cum, STAKE))
    if taken:
        print("avg entry: %.3f" % (sum(entries) / len(entries)))
        print("avg P&L/trade: %+.2f" % (cum / taken))
        print("max drawdown: %.2f" % max_dd)
    print("monthly:", dict(sorted(monthly.items())))


if __name__ == "__main__":
    main()
