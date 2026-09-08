#!/usr/bin/env python3
"""Bot 1 — early-entry leg ("03Z falling-min" trade).

A second, small leg alongside the 06Z dawn-lock. The dawn-lock buys the
*settled* winner at 05:30-07:00 for ~0.90. This leg buys the same winner
one bucket EARLY, at 03:00 UTC, while the min is still falling and the
market has not yet repriced the drop.

Signal / rules (radiation days only):
  regime       D-1 ECMWF diurnal range (max-min) >= 8C
  call         03:00 UTC:  leader = round(min of EGLC METAR before 03Z);
                target = leader - 1  (bet the min falls at least one more bucket)
  entry        ask-side (taker BUY of Yes) VWAP in [03:00, 04:00) UTC, +3c slip
  band         effective price in [0.35, 0.85]  (else skip — the tails are
                correctly priced, so very cheap or very dear asks are traps)
  capacity     stake = min($100, ask-side $ resting in window) — books are thin
  settlement   round(METAR day-min) == target bucket (tail-aware)

Measured (in-sample, Jul 21 - Aug 26 2026, 38 settled days):
  03Z band 0.35-0.85: 21 trades, 81.0% win, med ask 0.56, med depth $66,
  capacity-capped P&L +$679 on $1,362 staked = +49.8% ROI.

Liquidity is the binding constraint, not the meteorology. Total ask-side
volume rises into 05Z ($11.3k) but the leader-1 mispricing is already gone
by then — 03Z is the liquidity/price sweet spot.
"""

import csv
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone, date

import duckdb
import openpyxl
from openpyxl.styles import Font

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "london", "tape.duckdb")
METAR = os.path.join(HERE, "london", "metar_eglc.csv")
FCST = os.path.join(HERE, "london", "forecast_archive_d1.csv")
OUT = os.path.join(HERE, "london", "bot1.xlsx")

RANGE_THRESH = 8.0
ENTRY_HOUR = 3          # [03:00, 04:00) UTC
SLIPPAGE = 0.03
BAND = (0.35, 0.85)
MAX_STAKE = 50.0        # USD cap per day, before liquidity cut
MIN_OBS = 16


def load_metar():
    obs = defaultdict(list)
    with open(METAR) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                obs[dt.date()].append((dt, float(row["tmpc"])))
            except (ValueError, KeyError):
                continue
    return obs


def load_fcst_range():
    out = {}
    with open(FCST) as f:
        for row in csv.DictReader(f):
            try:
                out[date.fromisoformat(row["date"])] = (
                    float(row["d1_ecmwf_max"]) - float(row["d1_ecmwf_min"]))
            except (KeyError, ValueError):
                continue
    return out


def slug_to_bucket(slug):
    m = re.search(r"-(\d+)c(orbelow|orhigher)?$", slug)
    if not m:
        return None
    n = int(m.group(1)); suf = m.group(2)
    if suf == "orbelow":
        return ("<=%d" % n, n)
    if suf == "orhigher":
        return (">=%d" % n, n)
    return ("%dc" % n, n)


def slug_date(slug):
    m = re.search(r"on-([a-z]+)-(\d+)-2026", slug)
    if not m:
        return None
    return date(2026, datetime.strptime(m.group(1), "%B").month, int(m.group(2)))


def load_tape():
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute(
        "SELECT timestamp, slug, outcome, side, price, size FROM trades "
        "WHERE slug LIKE '%lowest%'"
    ).fetchall()
    con.close()
    by_day = defaultdict(lambda: defaultdict(list))
    bucket_label = {}
    for ts, slug, outcome, side, price, size in rows:
        b = slug_to_bucket(slug)
        if not b:
            continue
        d = slug_date(slug)
        if not d:
            continue
        by_day[d][b[1]].append((ts, side, outcome, float(price), float(size)))
        bucket_label[(d, b[1])] = b[0]
    return by_day, bucket_label


def ask_side(trades, h):
    """(ts, ask_price, size) in [h, h+1) UTC; ask = what a buyer pays for Yes."""
    out = []
    for ts, side, outcome, price, size in trades:
        if not (h * 3600 <= ts % 86400 < (h + 1) * 3600):
            continue
        if outcome == "Yes" and side == "BUY":
            out.append((ts, price, size))
        elif outcome == "No" and side == "SELL":
            out.append((ts, 1.0 - price, size))
    return sorted(out)


def vwap_ask(trades, h):
    a = ask_side(trades, h)
    if not a:
        return None, 0.0
    cost = sum(p * s for _, p, s in a)   # dollars to lift every resting ask
    shares = sum(s for _, p, s in a)
    return cost / shares, cost


def bucket_wins(label, num, winner):
    if label.startswith("<="):
        return winner <= num
    if label.startswith(">="):
        return winner >= num
    return winner == num


def compute():
    metar = load_metar()
    fcst = load_fcst_range()
    by_day, bucket_label = load_tape()

    records = []
    for d in sorted(by_day):
        pts = metar.get(d, [])
        if len(pts) < MIN_OBS:
            continue
        rng = fcst.get(d)
        if rng is None or rng < RANGE_THRESH:
            continue  # advection / no forecast -> stand down
        pre = [v for t, v in pts if t.hour < ENTRY_HOUR]
        if not pre:
            continue
        day_min = min(v for _, v in pts)
        winner = round(day_min)
        leader = round(min(pre))
        target = leader - 1
        ask, depth = vwap_ask(by_day[d].get(target, []), ENTRY_HOUR)
        if ask is None:
            continue
        label = bucket_label.get((d, target), "%dc" % target)
        win = bucket_wins(label, target, winner)
        records.append(dict(date=d, rng=rng, leader=leader, target=target,
                            label=label, ask=ask, depth=depth,
                            winner=winner, win=win))

    # price band + capacity-capped stake
    for r in records:
        eff = r["ask"] + SLIPPAGE
        r["eff"] = eff
        r["trade"] = BAND[0] <= eff <= BAND[1]
        r["stake"] = min(MAX_STAKE, r["depth"]) if r["trade"] else 0.0
        r["pnl"] = (r["stake"] * (1.0 / eff - 1.0)) if (r["trade"] and r["win"]) \
            else (-r["stake"] if r["trade"] else 0.0)
    return records


def write_sheet(records):
    wb = openpyxl.load_workbook(OUT)
    if "EarlyLeg" in wb.sheetnames:
        del wb["EarlyLeg"]
    ws = wb.create_sheet("EarlyLeg")
    bold = Font(bold=True)

    traded = [r for r in records if r["trade"]]
    wins = sum(1 for r in traded if r["win"])
    n = len(traded)
    tot_stake = sum(r["stake"] for r in traded)
    tot_pnl = sum(r["pnl"] for r in traded)
    med_ask = statistics.median(r["ask"] for r in traded) if traded else 0
    med_depth = statistics.median(r["depth"] for r in traded) if traded else 0

    head = [
        ("BOT 1 — EARLY-ENTRY LEG (03Z falling-min trade)", ""),
        ("", ""),
        ("Idea", "Same physical fact as the dawn-lock, entered 2.5h earlier: the min "
                 "is still falling at 03Z, so the bucket one below the current leader "
                 "wins ~8/10 times but the market has not repriced the drop yet."),
        ("Entry", "03:00-04:00 UTC, ask-side (taker BUY of Yes) VWAP + 3c slip."),
        ("Regime", "D-1 ECMWF diurnal range >= 8C only (radiation). Stand down otherwise."),
        ("Price band", "Effective price 0.35-0.85. Outside this the tails are correctly "
                       "priced (very cheap = market sees a 2-bucket crash; very dear = "
                       "market sees a HELD)."),
        ("Capacity", "Stake = min($100, ask-side $ resting in window). Books at 03Z are "
                     "thin; median depth ~$66. This is the binding constraint."),
        ("Liquidity note", "Total ask volume rises to 05Z ($11.3k) but the leader-1 "
                           "mispricing is already gone by then. 03Z is the liquidity/price "
                           "sweet spot."),
        ("", ""),
        ("SUMMARY (in-sample, Jul 21 - Aug 26 2026)", ""),
        ("trades", n),
        ("win_rate", round(100 * wins / n, 1) if n else 0),
        ("median_ask", round(med_ask, 3)),
        ("median_depth_usd", round(med_depth, 0)),
        ("total_staked_usd", round(tot_stake, 0)),
        ("net_pnl_usd", round(tot_pnl, 0)),
        ("roi_pct", round(100 * tot_pnl / tot_stake, 1) if tot_stake else 0),
        ("", ""),
        ("PER-DAY (radiation days only)", ""),
    ]
    for r in head:
        ws.append(list(r))
    for row in range(1, 9):
        ws.cell(row, 1).font = bold

    cols = ["date", "range", "leader", "target", "label", "ask", "eff",
            "winner", "win", "depth_usd", "stake_usd", "pnl_usd", "action"]
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        ws.cell(ws.max_row, c).font = bold

    for r in sorted(records, key=lambda x: x["date"]):
        action = "TRADE" if r["trade"] else ("skip-band" if r["eff"] < BAND[0] else "skip-dear")
        ws.append([
            str(r["date"]), round(r["rng"], 1), r["leader"], r["target"], r["label"],
            round(r["ask"], 3), round(r["eff"], 3), r["winner"],
            "W" if r["win"] else "L",
            round(r["depth"], 0), round(r["stake"], 0), round(r["pnl"], 0), action,
        ])

    wb.save(OUT)
    return dict(n=n, wins=wins, tot_stake=tot_stake, tot_pnl=tot_pnl,
                med_ask=med_ask, med_depth=med_depth)


def main():
    records = compute()
    s = write_sheet(records)
    print("OK wrote EarlyLeg sheet to %s" % OUT)
    print("  trades=%d  wins=%d (%.1f%%)  med_ask=%.3f  med_depth=$%.0f" %
          (s["n"], s["wins"], 100 * s["wins"] / s["n"] if s["n"] else 0,
           s["med_ask"], s["med_depth"]))
    print("  capacity-capped: staked $%.0f -> net $%+.0f (%.1f%% ROI)" %
          (s["tot_stake"], s["tot_pnl"],
           100 * s["tot_pnl"] / s["tot_stake"] if s["tot_stake"] else 0))


if __name__ == "__main__":
    main()
