#!/usr/bin/env python3
"""Bot 1 — regime classifier.

Splits London days into two physical regimes using the honest D-1 forecast
diurnal range (d1_ecmwf_max - d1_ecmwf_min), then measures the thing that
matters for the lowest-band trade: does the daily minimum LOCK by 06:00 UTC?

  radiation-dominated regime  -> strong diurnal cycle, big forecast range,
                                 overnight low is radiative and bottoms
                                 pre-dawn, so the min is locked by 06Z.
  advection-dominated regime  -> weak cycle, small range, min is set by the
                                 arriving airmass (often a post-dawn cold
                                 front) and is NOT locked by 06Z.

Outcome ("lock06") is defined exactly as the bot would trade it: the minimum
of all EGLC METAR temps up to 06:00 UTC on the London-local day rounds to the
same whole degree as the final daily minimum.

Outputs:
  london/bot1_regime_daily.csv   per-day classifier rows
  (printed)                      lock-rate table vs forecast-range bins
"""
import csv
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
FCST = os.path.join(LONDON, "forecast_archive_d1.csv")
METAR = os.path.join(LONDON, "metar_eglc.csv")
OUT = os.path.join(LONDON, "bot1_regime_daily.csv")

LON = ZoneInfo("Europe/London")
CUTOFF_UTC_HOUR = 6
MIN_SETTLE_OBS = 16


def load_fcst():
    out = {}
    with open(FCST) as f:
        for row in csv.DictReader(f):
            try:
                out[row["date"]] = (float(row["d1_ecmwf_max"]),
                                    float(row["d1_ecmwf_min"]))
            except (KeyError, ValueError):
                continue
    return out


def load_metar():
    days = defaultdict(list)
    with open(METAR) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                days[dt.astimezone(LON).date()].append((dt, float(row["tmpc"])))
            except (ValueError, KeyError):
                continue
    return days


def main():
    fcst = load_fcst()
    metar = load_metar()

    rows = []
    for d in sorted(metar):
        obs = metar[d]
        if len(obs) < MIN_SETTLE_OBS:
            continue
        cutoff = datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR, tzinfo=timezone.utc)
        pre = [v for dt, v in obs if dt < cutoff]
        if not pre:
            continue
        day_min = min(v for _, v in obs)
        day_max = max(v for _, v in obs)
        pre_min = min(pre)
        locked = int(round(pre_min) == round(day_min))
        f = fcst.get(d.isoformat())
        rows.append({
            "date": d.isoformat(),
            "fcst_range": round(f[0] - f[1], 1) if f else "",
            "actual_range": round(day_max - day_min, 1),
            "pre06_min": round(pre_min, 1),
            "day_min": round(day_min, 1),
            "break_c": round(round(day_min) - round(pre_min), 1),
            "lock06": locked,
        })

    # persist
    fields = ["date", "fcst_range", "actual_range", "pre06_min", "day_min",
              "break_c", "lock06"]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # lock-rate table vs forecast-range bins
    scored = [r for r in rows if r["fcst_range"] != ""]
    bins = [(-99, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9),
            (9, 10), (10, 12), (12, 99)]
    print("=== lock06 rate vs forecast diurnal range (D-1 ECMWF max-min) ===")
    print("range_bin   n   lock%   mean_actual_range   mean_break_c")
    for lo, hi in bins:
        g = [r for r in scored if lo <= r["fcst_range"] < hi]
        if not g:
            continue
        n = len(g)
        lk = sum(r["lock06"] for r in g) / n * 100
        ar = sum(r["actual_range"] for r in g) / n
        bc = sum(r["break_c"] for r in g) / n
        print(f"  {lo:>3}-{hi:<3}  {n:>4}   {lk:5.1f}   {ar:17.1f}   {bc:12.2f}")

    print()
    print("=== clean threshold sweep (lock% above vs below) ===")
    for thr in [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 8.0, 9.0, 10.0]:
        hi = [r for r in scored if r["fcst_range"] >= thr]
        lo = [r for r in scored if r["fcst_range"] < thr]
        if not hi or not lo:
            continue
        print(f"  thr={thr:>4}: radiation(>=) n={len(hi):>3} lock={sum(r['lock06'] for r in hi)/len(hi)*100:5.1f}%  "
              f"| advection(<) n={len(lo):>3} lock={sum(r['lock06'] for r in lo)/len(lo)*100:5.1f}%")

    print()
    total = sum(r["lock06"] for r in scored)
    print(f"overall lock rate: {total/len(scored)*100:.1f}% (n={len(scored)} settled days)")


if __name__ == "__main__":
    main()
