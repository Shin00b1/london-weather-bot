"""Archive daily solar radiation for the London temperature project.

Two files, mirroring the split already used for temperature:
  london/radiation_actual.csv      - OBSERVED daily shortwave radiation sum
                                     (MJ/m^2) from Open-Meteo Archive API
                                     (ERA5 reanalysis). Backfillable full history.
  london/radiation_forecast_d1.csv - HONEST D-1 forecast of the same variable
                                     from the Single Runs API (run = D-1 00:00 UTC,
                                     ecmwf_ifs). Point-in-time, no look-ahead.

shortwave_radiation_sum is the daily TOTAL incoming solar energy at the
surface. It is the clean proxy for "how much the sun actually heated the
ground" — it collapses cloud cover, day length, and solar angle into one
number. Higher = clearer/hotter day. It does NOT directly equal the daily
max temperature (that lags solar noon by ~1-3h via soil thermal inertia),
which is exactly the physics under test in radiation_edge_analysis.py.

Usage:
    python3 radiation_archive.py --actual           # observed (fast, few calls)
    python3 radiation_archive.py --forecast         # D-1 forecast (1 call/date)
    python3 radiation_archive.py                    # both
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
ACTUAL = os.path.join(LONDON, "radiation_actual.csv")
FORECAST = os.path.join(LONDON, "radiation_forecast_d1.csv")

LAT, LON = 51.5053, 0.0553
START = date(2025, 1, 22)  # match EGLC METAR archive start
UA = {"User-Agent": "london-weather-updater/1.0"}


def http_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_actual(d1, d2):
    url = ("https://archive-api.open-meteo.com/v1/archive"
           "?latitude=%s&longitude=%s"
           "&start_date=%s&end_date=%s"
           "&daily=shortwave_radiation_sum&timezone=UTC" % (LAT, LON, d1, d2))
    return http_json(url)


def backfill_actual():
    merged = {}
    if os.path.exists(ACTUAL):
        with open(ACTUAL, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("shortwave_sum") not in (None, "", "None"):
                    merged[row["date"]] = row["shortwave_sum"]

    # chunk by 90 days to stay within archive-api window limits
    cur = START
    today = date.today() - timedelta(days=1)  # complete days only
    while cur <= today:
        d2 = min(cur + timedelta(days=89), today)
        try:
            d = fetch_actual(cur, d2)
        except Exception as e:
            print("  chunk %s..%s failed: %r" % (cur, d2, e))
            cur = d2 + timedelta(days=1)
            continue
        daily = d.get("daily") or {}
        for t, v in zip(daily.get("time", []), daily.get("shortwave_radiation_sum", [])):
            if v is not None:
                merged[t] = round(v, 2)
        print("  actual %s..%s -> %d days" % (cur, d2, len(daily.get("time", []))))
        cur = d2 + timedelta(days=1)
        time.sleep(0.3)

    with open(ACTUAL, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "shortwave_sum"])
        for d in sorted(merged):
            w.writerow([d, merged[d]])
    print("actual: wrote %d days to %s" % (len(merged), ACTUAL))


def fetch_forecast_one(target):
    """Honest D-1 forecast: the D-1 00:00 UTC run's value for `target`."""
    run = (target - timedelta(days=1)).isoformat()
    url = ("https://single-runs-api.open-meteo.com/v1/forecast"
           "?latitude=%s&longitude=%s"
           "&daily=shortwave_radiation_sum"
           "&run=%sT00:00&models=ecmwf_ifs&timezone=UTC" % (LAT, LON, run))
    d = http_json(url)
    daily = d.get("daily") or {}
    times = daily.get("time", [])
    vals = daily.get("shortwave_radiation_sum", [])
    if target.isoformat() not in times:
        return None
    i = times.index(target.isoformat())
    return vals[i]


def backfill_forecast():
    merged = {}
    if os.path.exists(FORECAST):
        with open(FORECAST, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("d1_shortwave_sum") not in (None, "", "None"):
                    merged[row["date"]] = row["d1_shortwave_sum"]

    today = date.today()
    cur = START
    n = 0
    while cur <= today:
        try:
            v = fetch_forecast_one(cur)
        except Exception as e:
            print("  fcst %s failed: %r" % (cur, e))
            cur += timedelta(days=1)
            continue
        if v is not None:
            merged[cur.isoformat()] = round(v, 2)
            n += 1
        cur += timedelta(days=1)
        if n % 100 == 0:
            print("  ...%d forecast dates fetched" % n)
        time.sleep(0.1)

    with open(FORECAST, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "d1_shortwave_sum"])
        for d in sorted(merged):
            w.writerow([d, merged[d]])
    print("forecast: wrote %d days to %s" % (len(merged), FORECAST))


def main():
    do_actual = "--actual" in sys.argv or (not any(a in sys.argv for a in ("--actual", "--forecast")))
    do_forecast = "--forecast" in sys.argv or (not any(a in sys.argv for a in ("--actual", "--forecast")))
    if do_actual:
        backfill_actual()
    if do_forecast:
        backfill_forecast()


if __name__ == "__main__":
    main()
