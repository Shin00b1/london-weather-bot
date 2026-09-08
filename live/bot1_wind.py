#!/usr/bin/env python3
"""Bot 1 — EGLC wind archive (the missing frontal marker).

Fetches EGLC METAR wind (sustained speed `sknt` in knots, direction `drct`
in degrees) from Iowa Mesonet and merges idempotently into
london/metar_eglc_wind.csv (keyed by station+valid). Wind is the one
variable the low-band bot has never archived, and it is the ex-ante physical
signature of the post-dawn cold front that is the bot's ONLY losing mode.

Usage:  python3 bot1_wind.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
Defaults: full range 2025-01-22 .. yesterday (London).
"""
import csv
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
ARCHIVE = os.path.join(LONDON, "metar_eglc_wind.csv")

STATION = "EGLC"
FIELDS = ["station", "valid", "sknt", "drct"]


def fetch(d1, d2):
    base = {"station": STATION, "data": "sknt,drct",
            "year1": str(d1.year), "month1": str(d1.month), "day1": str(d1.day),
            "year2": str(d2.year), "month2": str(d2.month), "day2": str(d2.day),
            "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
            "missing": "M", "trace": "T", "direct": "no"}
    pairs = list(base.items()) + [("report_type", "3"), ("report_type", "4")]
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(pairs)
    req = urllib.request.Request(url, headers={"User-Agent": "london-weather-updater/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode()


def parse(text):
    rows = []
    for line in text.strip().split("\n")[1:]:
        p = line.split(",")
        if len(p) < 4:
            continue
        station, valid, sknt, drct = p[0], p[1], p[2], p[3]
        if sknt in ("M", "T", "") and drct in ("M", "T", ""):
            continue
        try:
            datetime.strptime(valid, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        rows.append({"station": station, "valid": valid,
                     "sknt": "" if sknt in ("M", "T", "") else sknt,
                     "drct": "" if drct in ("M", "T", "") else drct})
    return rows


def load_existing():
    out = {}
    if os.path.exists(ARCHIVE):
        with open(ARCHIVE, newline="") as f:
            for row in csv.DictReader(f):
                out[(row["station"], row["valid"])] = row
    return out


def main():
    args = sys.argv[1:]
    def argval(flag, default):
        return args[args.index(flag) + 1] if flag in args else default

    today = date.today()
    d_from = date.fromisoformat(argval("--from", "2025-01-22"))
    d_to = date.fromisoformat(argval("--to", (today - timedelta(days=1)).isoformat()))
    if d_to > today - timedelta(days=1):
        d_to = today - timedelta(days=1)

    existing = load_existing()
    try:
        txt = fetch(d_from, d_to)
        got = parse(txt)
        for row in got:
            existing[(row["station"], row["valid"])] = row
        print(f"fetched {d_from} .. {d_to}: {len(got)} rows")
    except Exception as e:
        print(f"WARN {d_from}..{d_to}: {e!r}")

    with open(ARCHIVE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for key in sorted(existing):
            w.writerow([existing[key][h] for h in FIELDS])
    print(f"OK archive rows={len(existing)}")


def refresh(days=4, quiet=False):
    """Append the last `days` days of wind to the archive (idempotent merge).

    Called nightly from update_weather_sheet.py so the wind archive keeps
    growing forward alongside the METAR temperature archive. Fetches a small
    trailing window (no truncation risk) and merges by station+valid key.
    """
    today = date.today()
    d_from = today - timedelta(days=days)
    d_to = today - timedelta(days=1)
    existing = load_existing()
    txt = fetch(d_from, d_to)
    got = parse(txt)
    for row in got:
        existing[(row["station"], row["valid"])] = row
    with open(ARCHIVE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for key in sorted(existing):
            w.writerow([existing[key][h] for h in FIELDS])
    if not quiet:
        print(f"wind refresh ok: +{len(got)} rows, archive={len(existing)}")


if __name__ == "__main__":
    main()
