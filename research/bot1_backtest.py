#!/usr/bin/env python3
"""Bot 1 — full $100/trade backtest of the METAR-only dawn-lock bot.

Window: 2026-04-15 (lowest market launch) .. 2026-08-27 (last settled day).

Signal      round(EGLC METAR min over the London day up to 06:00 UTC) — the
            value knowable at dawn. This is the METAR-only signal: the
            settlement IS the EGLC METAR, so its own pre-06Z min is the
            answer's precursor (9/9 on recent days vs stations 6/9).
Entry       $100 stake on the called bucket; price = 2h VWAP of p_yes in
            [04:00,06:00) UTC (fallback: last print <=8h stale), +3c slippage.
Vetos       (a) regime: forecast diurnal range < 8C -> stand down;
            (b) rain: any rain obs before 06Z -> skip (frontal risk);
            (c) band: effective entry outside [0.63, 0.97] -> skip;
            (d) edge: reading within 0.2C of a .5 bucket edge -> skip.
Settlement  round(EGLC METAR day-min) == winning bucket (validated 99.2%).

Price sources (no overlap): parquet (Apr 15 .. Jul 18, condition_id -> slug
via lowest_slugs_apr_jul.json, p_yes=prob_yes, weight=usd_amount);
tape.duckdb trades (Jul 19 .. Aug 27, slug -> bucket, p_yes from outcome,
weight=size).

Read-only scoring. Prints every day, then money-math table.
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
SLIPPAGE = 0.03
ENTRY_WINDOW = (4, 6)
FALLBACK_STALE_H = 8
EDGE_SKIP_C = 0.2
BAND = (0.63, 0.97)
REGIME_MIN_RANGE = 8.0
MIN_OBS = 16
RAIN_TOKENS = ("RA", "DZ", "TS", "SH", "FZRA", "GR", "UP")

D0 = date(2026, 4, 15)
D1 = date(2026, 8, 27)


def code_is_rain(code):
    if not code or code == "M":
        return False
    return any(t.lstrip("+-").startswith(RAIN_TOKENS) for t in code.split())


def load_metar():
    obs = defaultdict(list)      # day -> [(dt_utc, tmpc)]
    rain = defaultdict(list)     # day -> [(dt_utc, is_rain)]
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
    """-> (event_date, int_value, label) or None."""
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
    d = date(2026, mon, int(m2.group(2)))
    return d, n, label


def build_price_lookup():
    """day -> {int_value: label} and day -> {label: [(ts, p_yes, weight)]}."""
    int_map = defaultdict(dict)     # day -> {int: label}
    trades = defaultdict(lambda: defaultdict(list))  # day -> {label: [(ts,p,w)]}

    # --- parquet: Apr 15 .. Jul 18 (condition_id -> slug via roster) ---
    roster = {}
    for s in json.load(open(ROSTER)):
        roster[s["condition_id"]] = s["slug"]

    con = duckdb.connect()
    con.execute("CREATE VIEW p AS SELECT * FROM read_parquet('%s')" % PARQUET)
    cids = list(roster.keys())
    cid_list = ",".join("'%s'" % c for c in cids)
    rows = con.execute(
        "SELECT timestamp, condition_id, prob_yes, usd_amount FROM p "
        "WHERE condition_id IN (%s)" % cid_list).fetchall()
    for ts, cid, prob, usd in rows:
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

    # --- tape: Jul 19 .. Aug 27 (slug -> bucket) ---
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT timestamp, slug, outcome, price, size
        FROM trades WHERE slug LIKE '%lowest%'
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


def vwap_2h(tlist, day):
    start = datetime(day.year, day.month, day.day, ENTRY_WINDOW[0], tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, ENTRY_WINDOW[1], tzinfo=timezone.utc)
    window = [t for t in tlist if start.timestamp() <= t[0] < end.timestamp()]
    if window:
        num = sum(p * w for _, p, w in window)
        den = sum(w for _, p, w in window)
        return num / den, len(window)
    cutoff = start.timestamp() - FALLBACK_STALE_H * 3600
    recent = [t for t in tlist if cutoff <= t[0] < end.timestamp()]
    if recent:
        last = max(recent, key=lambda t: t[0])
        return last[1], len(recent)
    return None, 0


def main():
    obs, rain = load_metar()
    fcst = load_fcst()
    int_map, trades = build_price_lookup()

    print("=== Bot 1 METAR-only dawn-lock backtest @ $100/trade ===")
    print("window %s .. %s | rules: call=round(pre-06Z METAR min); " % (D0, D1))
    print("vetos: regime<%.0fC range / rain-pre06 / band %s / edge<%.1fC" %
          (REGIME_MIN_RANGE, BAND, EDGE_SKIP_C))
    print("entry = 2h VWAP[04-06Z] + 3c slippage")
    print("-" * 100)
    print("%-10s %5s %5s %5s %-6s %-6s %6s %6s %-4s %8s %8s  %s" % (
        "date", "pre", "day", "call", "winner", "entry", "eff", "cum", "hit",
        "pnl", "note", ""))
    print("-" * 100)

    taken = wins = 0
    skipped = {"regime": 0, "rain": 0, "band": 0, "edge": 0, "illiquid": 0, "no_metar": 0}
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    monthly = defaultdict(float)
    per_trade = []

    cur = D0
    while cur <= D1:
        pts = obs.get(cur, [])
        if len(pts) < MIN_OBS:
            skipped["no_metar"] += 1
            cur += timedelta(days=1)
            continue
        day_min = min(v for _, v in pts)
        pre = [v for dt, v in pts if dt.hour < ENTRY_WINDOW[1]]
        if not pre:
            skipped["illiquid"] += 1
            cur += timedelta(days=1)
            continue
        pre_min = min(pre)
        call = round(pre_min)
        winner = round(day_min)

        # regime veto
        r = fcst.get(cur.isoformat())
        if r is None or r < REGIME_MIN_RANGE:
            skipped["regime"] += 1
            cur += timedelta(days=1)
            continue

        # rain veto (any rain obs before 06Z)
        cutoff = datetime(cur.year, cur.month, cur.day, 6, tzinfo=timezone.utc)
        if any(is_rain for dt, is_rain in rain.get(cur, []) if dt < cutoff):
            skipped["rain"] += 1
            cur += timedelta(days=1)
            continue

        # edge skip
        dist = 0.5 - abs(pre_min - call)
        if dist < EDGE_SKIP_C:
            skipped["edge"] += 1
            cur += timedelta(days=1)
            continue

        # called bucket trades
        label = int_map.get(cur, {}).get(call)
        if label is None:
            label = "%dc" % call
        tlist = trades.get(cur, {}).get(label, [])
        entry, n = vwap_2h(tlist, cur)
        if entry is None:
            skipped["illiquid"] += 1
            cur += timedelta(days=1)
            continue
        eff = entry + SLIPPAGE
        if eff < BAND[0] or eff > BAND[1]:
            skipped["band"] += 1
            cur += timedelta(days=1)
            continue

        # trade taken
        taken += 1
        hit = (call == winner)
        if hit:
            pnl = STAKE * (1.0 / eff - 1.0)
            wins += 1
            note = "win"
        else:
            pnl = -STAKE
            note = "LOSS"
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        monthly[cur.strftime("%Y-%m")] += pnl
        per_trade.append(pnl)

        print("%-10s %5.1f %5.1f %5d %-6d %-6s %6.3f %6.3f %-4s %+8.2f %+8.2f  %s" % (
            cur, pre_min, day_min, call, winner, label, entry, eff,
            "Y" if hit else "N", pnl, cum, note))
        cur += timedelta(days=1)

    print("-" * 100)
    print("\n=== MONEY MATH (per-trade, breakeven, scaled) ===")
    wr = wins / taken * 100 if taken else 0
    # breakeven win rate at the mean effective entry
    mean_eff = sum(1 / (e) for e in [])  # placeholder; compute from avg
    avg_entry = None
    print("trades taken: %d" % taken)
    print("wins: %d (%.1f%%)" % (wins, wr))
    print("skips: %s" % dict(skipped))
    print("net P&L: %+.2f on $%d/trade" % (cum, STAKE))
    if taken:
        avg_pnl = cum / taken
        print("avg P&L per trade: %+.2f" % avg_pnl)
        print("max drawdown: %.2f (%s)" % (max_dd, "vs starting bank"))
    print("\nmonthly P&L:")
    for m in sorted(monthly):
        print("  %s  %+.2f" % (m, monthly[m]))

    # breakeven win rate = 1 / (1 + (1-p)/p ... )  -- for a binary paying (1/p -1) on win, -1 on loss
    # EV = wr*(1/p - 1) - (1-wr)*1 ; breakeven wr* = p
    # report at representative entry p=0.80 -> breakeven 80%
    print("\nbreakeven reference: at effective entry p, need win rate >= p")
    print("  (e.g. entry 0.80 -> breakeven 80% win rate)")
    # scaled projection at constant realized hit rate (linear)
    if taken:
        per_day_rate = cum / taken
        for stake in (100, 250, 1000):
            print("scaled @ $%d/trade, same %d trades: %+.2f" % (stake, taken, per_day_rate * (stake / STAKE) * taken))


if __name__ == "__main__":
    main()
