"""Full two-era backtest with the honest D-1 ECMWF forecast archive.

Strategy (deck-equivalent): buy the single bucket containing the model
forecast (round to nearest integer, clamp to edge buckets), filter to buckets
priced 0.10-0.50 at open, hold to resolution. PnL/$1 = (1-p)/p if won else -1.

Compares:
  - archive (look-ahead contaminated fcst_model_c) — for reference
  - genuine D-1 ECMWF (rebuilt) — the honest case
  - market favorite at open — baseline

Covers the full dataset (2025 F-era + 2026 C-era).
"""

import csv
import json
import os
from datetime import datetime, date

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.path.join(BASE, "london", "london_temperature_unified.xlsx")
D1_CSV = os.path.join(BASE, "london", "forecast_archive_d1.csv")


def load_markets():
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb["Markets"]
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        ed = r[col["event_date"]]
        if isinstance(ed, datetime):
            ed = ed.date()
        rows.append({
            "event_date": ed,
            "direction": r[col["direction"]],
            "bucket_low": r[col["bucket_low_c"]],
            "bucket_high": r[col["bucket_high_c"]],
            "bucket_label": r[col["bucket_label"]],
            "open_prob": r[col["open_prob"]],
            "won": r[col["won"]],
            "unit": r[col["unit"]],
        })
    return rows


def load_archive_fc():
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb["Events"]
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    out = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        ed = r[col["event_date"]]
        if isinstance(ed, datetime):
            ed = ed.date()
        fc = r[col["fcst_model_c"]]
        direction = r[col["direction"]]
        if fc is not None:
            out[(ed, direction)] = float(fc)
    return out


def load_d1_fc():
    out = {}
    for row in csv.DictReader(open(D1_CSV)):
        ts = row["date"]
        if not row["d1_ecmwf_max"] or not row["d1_ecmwf_min"]:
            continue
        ed = date.fromisoformat(ts)
        out[(ed, "highest")] = float(row["d1_ecmwf_max"])
        out[(ed, "lowest")] = float(row["d1_ecmwf_min"])
    return out


def forecast_to_bucket(fc, direction, unit, ms):
    # Celsius markets: forecast is already °C. Fahrenheit markets (2025): convert.
    c = fc
    r = round(c)
    for m in ms:
        lo, hi = m["bucket_low"], m["bucket_high"]
        lab = m["bucket_label"] or ""
        if direction == "highest":
            if "below" in lab and r <= hi:
                return m
            if "higher" in lab and r >= lo:
                return m
            if lo == r == hi:
                return m
        else:
            if "below" in lab and r <= hi:
                return m
            if "higher" in lab and r >= lo:
                return m
            if lo == r == hi:
                return m
    for m in ms:
        if m["bucket_low"] == r and m["bucket_high"] == r:
            return m
    return None


def run(forecasts):
    markets = load_markets()
    by_event = {}
    for m in markets:
        by_event.setdefault((m["event_date"], m["direction"]), []).append(m)

    trades = []
    for (ed, direction), fc in forecasts.items():
        ms = by_event.get((ed, direction), [])
        if not ms or fc is None:
            continue
        unit = ms[0]["unit"] if ms else None
        bucket = forecast_to_bucket(fc, direction, unit, ms)
        if bucket is None:
            continue
        p = bucket["open_prob"]
        if p is None or p < 0.10 or p > 0.50:
            continue
        pnl = (1 - p) / p if bucket["won"] else -1.0
        trades.append((ed, direction, p, bucket["won"], pnl))

    n = len(trades)
    if n == 0:
        return {"n": 0, "trades": []}
    wins = sum(1 for t in trades if t[3])
    return {
        "n": n,
        "win_rate": wins / n,
        "pnl_per_dollar": sum(t[4] for t in trades) / n,
        "total_pnl": sum(t[4] for t in trades),
        "trades": trades,
    }


def favorite_baseline():
    markets = load_markets()
    by_event = {}
    for m in markets:
        by_event.setdefault((m["event_date"], m["direction"]), []).append(m)
    trades = []
    for key, ms in by_event.items():
        best = None
        for m in ms:
            p = m["open_prob"]
            if p is None:
                continue
            if best is None or p > best["open_prob"]:
                best = m
        if best is None or best["open_prob"] < 0.10 or best["open_prob"] > 0.50:
            continue
        p = best["open_prob"]
        pnl = (1 - p) / p if best["won"] else -1.0
        trades.append((key[0], key[1], p, best["won"], pnl))
    n = len(trades)
    return {
        "n": n,
        "win_rate": sum(1 for t in trades if t[3]) / n,
        "pnl_per_dollar": sum(t[4] for t in trades) / n,
        "trades": trades,
    }


def era_stats(result):
    """Split result trades by era."""
    f = [t for t in result["trades"] if t[0].year == 2025]
    c = [t for t in result["trades"] if t[0].year >= 2026]
    def s(ts):
        if not ts:
            return None
        n = len(ts)
        return {"n": n, "win_rate": sum(1 for t in ts if t[3]) / n,
                "pnl_per_dollar": sum(t[4] for t in ts) / n}
    return s(f), s(c)


def main():
    archive = load_archive_fc()
    d1 = load_d1_fc()
    # restrict archive to events that also have D-1 (full overlap for fairness)
    common = set(d1) & set(archive)

    print("event-days with honest D-1 forecast: %d" % len(set(k[0] for k in d1)))
    print("event rows (highest+lowest) with D-1: %d" % len(d1))

    print("\n=== ARCHIVE (look-ahead) — for reference ===")
    ra = run({k: archive[k] for k in common})
    print(ra["n"], "trades, win %.1f%%, pnl $%.2f/$1" % (100 * ra["win_rate"], ra["pnl_per_dollar"]))
    fa, ca = era_stats(ra)
    if fa: print("  2025 era: n=%d win %.1f%% pnl $%.2f/$1" % (fa["n"], 100 * fa["win_rate"], fa["pnl_per_dollar"]))
    if ca: print("  2026 era: n=%d win %.1f%% pnl $%.2f/$1" % (ca["n"], 100 * ca["win_rate"], ca["pnl_per_dollar"]))

    print("\n=== GENUINE D-1 ECMWF (honest) ===")
    rd = run(d1)
    print(rd["n"], "trades, win %.1f%%, pnl $%.2f/$1" % (100 * rd["win_rate"], rd["pnl_per_dollar"]))
    fd, cd = era_stats(rd)
    if fd: print("  2025 era: n=%d win %.1f%% pnl $%.2f/$1" % (fd["n"], 100 * fd["win_rate"], fd["pnl_per_dollar"]))
    if cd: print("  2026 era: n=%d win %.1f%% pnl $%.2f/$1" % (cd["n"], 100 * cd["win_rate"], cd["pnl_per_dollar"]))

    print("\n=== MARKET FAVORITE (baseline) ===")
    rf = favorite_baseline()
    print(rf["n"], "trades, win %.1f%%, pnl $%.2f/$1" % (100 * rf["win_rate"], rf["pnl_per_dollar"]))
    ff, cf = era_stats(rf)
    if ff: print("  2025 era: n=%d win %.1f%% pnl $%.2f/$1" % (ff["n"], 100 * ff["win_rate"], ff["pnl_per_dollar"]))
    if cf: print("  2026 era: n=%d win %.1f%% pnl $%.2f/$1" % (cf["n"], 100 * cf["win_rate"], cf["pnl_per_dollar"]))

    # bootstrap CI on honest D-1
    import random
    random.seed(42)
    pnls = [t[4] for t in rd["trades"]]
    means = []
    for _ in range(5000):
        s = random.choices(pnls, k=len(pnls))
        means.append(sum(s) / len(s))
    means.sort()
    print("\nHonest D-1 bootstrap 95%% CI: [$%.2f, $%.2f]" % (means[125], means[4874]))
    print("P(negative pnl): %.1f%%" % (100 * sum(1 for m in means if m < 0) / len(means)))

    # save trades for later use
    json.dump({"archive": ra, "d1": rd, "favorite": rf},
              open(os.path.join(BASE, "london", "full_backtest_results.json"), "w"),
              indent=1, default=str)


if __name__ == "__main__":
    main()
