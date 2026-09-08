"""Independent settlement cross-check: London Heathrow (EGLL) METAR.

EGLC (London City) is the resolution station for all London temperature
markets. This script archives a SECOND station, EGLL (Heathrow), from the
same Iowa Environmental Mesonet ASOS feed so every daily min/max settlement
can be independently cross-checked — a guard against a single-station
anomaly (sensor issue, local sea-breeze effect at EGLC, etc.).

Mirrors metar_update.py's fetch path exactly (same URL, same parsing), but
writes to its own cache files so the validated EGLC pipeline is untouched:
  london/metar_egll.csv    raw appended METAR (same append-on-refresh model)
  london/egll_daily.csv    daily max/min/n_obs over London local days

Usage: python3 egll_crosscheck.py [--days N]   (N = refresh window, default all)
"""

import csv
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
STATION = "EGLL"
LON = ZoneInfo("Europe/London")

CACHE = os.path.join(LONDON, "metar_egll.csv")
DAILY = os.path.join(LONDON, "egll_daily.csv")


def fetch_metar(station, d1, d2):
    base = {"station": station, "data": "tmpc",
            "year1": str(d1.year), "month1": str(d1.month), "day1": str(d1.day),
            "year2": str(d2.year), "month2": str(d2.month), "day2": str(d2.day),
            "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
            "missing": "M", "trace": "T", "direct": "no"}
    pairs = list(base.items()) + [("report_type", "3"), ("report_type", "4")]
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(pairs)
    req = urllib.request.Request(url, headers={"User-Agent": "london-weather-updater/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read().decode()


def daily_extremes(text):
    obs = {}
    for line in text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 3 or parts[2] in ("M", "T", ""):
            continue
        try:
            dt = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            v = float(parts[2])
        except ValueError:
            continue
        obs.setdefault(dt.astimezone(LON).date(), []).append(v)
    return {d: (max(v), min(v), len(v)) for d, v in obs.items()}


def run(days=None, quiet=False):
    today = datetime.now(LON).date()

    # full history from 2025-01-22 (matches EGLC cache) unless a window is given
    d1 = date(2025, 1, 22)
    if days:
        d1 = today - timedelta(days=days)
    d2 = today - timedelta(days=1)  # settle only complete days

    text = fetch_metar(STATION, d1, d2)
    ext = daily_extremes(text)
    if not quiet:
        print("%s: %s -> %s, %d days" % (STATION, d1, d2, len(ext)))

    # raw cache: append (re-downloading a window is cheap and deduped by the
    # daily summary anyway; keep the same append model as metar_update.py)
    with open(CACHE, "a") as f:
        f.write(text)

    # daily summary CSV: MERGE (windowed refreshes must not erase history)
    existing = {}
    if os.path.exists(DAILY):
        with open(DAILY, newline="") as f:
            for row in csv.DictReader(f):
                existing[row["date"]] = (row["max_c"], row["min_c"], row["n_obs"])
    for d in ext:
        existing[d.isoformat()] = tuple(str(v) for v in ext[d])

    with open(DAILY, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "max_c", "min_c", "n_obs"])
        for d in sorted(existing):
            w.writerow([d] + list(existing[d]))

    if not quiet:
        print("wrote %d rows to %s" % (len(existing), DAILY))
    return len(existing)


def main():
    win = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    run(days=win)


if __name__ == "__main__":
    main()
