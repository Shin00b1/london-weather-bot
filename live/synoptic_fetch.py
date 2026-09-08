#!/usr/bin/env python3
"""Fetch one Synoptic viewer station without the paid API.

This reuses the same public v2 endpoint that the Synoptic map viewer calls
from the browser. To survive the viewer changing its code, every run:
  1. fetches the viewer index page,
  2. finds the current JS bundle filename (the hash changes),
  3. extracts the embedded public token from that bundle,
  4. calls the stations/timeseries endpoint with it.

Usage:
    python3 synoptic_fetch.py [STID]

STID defaults to AMB5847 (existing station). Pass a different station id to
run the same fetch for it, e.g. `python3 synoptic_fetch.py WXM4398`. Each
station gets its own CSV (<STID>_air_temp.csv) and its own sheet in the
unified London workbook, so the two automations never write the same sheet.

Stdlib only (no third-party dependencies).
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VIEWER_ROOT = "https://viewer.synopticdata.com/"
API_BASE = "https://api.synopticdata.com/v2"

DEFAULT_STID = "AMB5847"
VAR = "air_temp"
UNITS = "metric"
OBTIMEZONE = "UTC"
BACKFILL_DAYS = 7      # initial lookback when no local file exists yet
OVERLAP_MINUTES = 15   # overlap on incremental runs so no gaps are missed

# Documentation only (README source row). Coordinates are static per station.
STATION_COORDS = {
    "AMB5847": "51.5206N 0.10549E",
    "WXM4398": "51.47302N 0.01264W",
}

HERE = os.path.dirname(os.path.abspath(__file__))

UA = {"User-Agent": "Mozilla/5.0"}


def csv_path(stid):
    return os.path.join(HERE, f"{stid}_{VAR}.csv")


def http_text(url):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def http_json(url, params=None):
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers=UA)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_token():
    html = http_text(VIEWER_ROOT)
    m = re.search(r'src="(/assets/index-[A-Za-z0-9_-]+\.js)"', html)
    if not m:
        raise RuntimeError("Could not find the JS bundle in the viewer HTML")
    bundle_path = m.group(1)
    js = http_text(VIEWER_ROOT.rstrip("/") + bundle_path)
    t = re.search(r'token\s*:\s*["\']([A-Za-z0-9_-]+)["\']', js)
    if not t:
        raise RuntimeError("Could not find the embedded token in the JS bundle")
    return t.group(1)


def fetch_timeseries(token, start_dt, stid):
    start = start_dt.strftime("%Y%m%d%H%M")
    end = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    data = http_json(f"{API_BASE}/stations/timeseries", {
        "stid": stid, "token": token, "vars": VAR,
        "start": start, "end": end,
        "units": UNITS, "obtimezone": OBTIMEZONE,
    })
    obs = data["STATION"][0]["OBSERVATIONS"]
    dates = obs["date_time"]
    var_key = f"{VAR}_set_1"
    values = obs.get(var_key) or obs.get(VAR)
    return [(dt, float(v)) for dt, v in zip(dates, values) if v is not None]


def load_existing(stid):
    rows = {}
    path = csv_path(stid)
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    rows[row["date_time"]] = float(row[VAR])
                except (KeyError, ValueError):
                    continue
    return rows


def next_source_number(wb):
    """Highest existing 'Source N' number in README + 1."""
    n = 0
    for row in wb["README"].iter_rows():
        for c in row:
            m = re.search(r"Source\s+(\d+)", str(c.value or ""))
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def sync_workbook(rows, stid):
    """Mirror a station's air-temp series into the unified London workbook.

    Writes a sheet named after the station (date_time, air_temp) into
    london/london_temperature_unified.xlsx and documents it in README.
    Best-effort: a workbook problem must never break the CSV fetch, so any
    error is reported and swallowed.
    """
    import shutil
    import openpyxl
    try:
        unified = os.path.join(HERE, "london", "london_temperature_unified.xlsx")
        if not os.path.exists(unified):
            return None
        backups = os.path.join(HERE, "london", "backups")
        os.makedirs(backups, exist_ok=True)
        shutil.copy2(unified, os.path.join(
            backups, "unified_pre%s_%s.xlsx"
            % (stid.lower(), datetime.now().strftime("%Y%m%d_%H%M%S"))))

        wb = openpyxl.load_workbook(unified)
        if stid in wb.sheetnames:
            ws = wb[stid]
            if ws.max_row > 1:
                ws.delete_rows(2, ws.max_row)
            ws.cell(1, 1, "date_time")
            ws.cell(1, 2, VAR)
        else:
            ws = wb.create_sheet(stid)
            ws.append(["date_time", VAR])
        for dt in sorted(rows):
            ws.append([dt, rows[dt]])

        r = wb["README"]
        if not any(stid in str(c.value or "") for row in r.iter_rows() for c in row):
            coords = STATION_COORDS.get(stid, "see viewer URL")
            r.append(("Source %d (%s air temp)" % (next_source_number(wb), stid),
                      "Synoptic viewer public API (station %s, %s); "
                      "5-min air_temp; auto-updated daily" % (stid, coords)))

        tmp = unified + ".tmp"
        wb.save(tmp)
        os.replace(tmp, unified)
        return len(rows)
    except Exception as e:
        print(f"workbook sync skipped: {e!r}")
        return None


def main():
    stid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STID

    existing = load_existing(stid)

    if existing:
        last = max(existing)
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        start_dt = last_dt - timedelta(minutes=OVERLAP_MINUTES)
    else:
        start_dt = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)

    token = discover_token()
    points = fetch_timeseries(token, start_dt, stid)

    before = set(existing)
    for dt, val in points:
        existing[dt] = val
    new_count = sum(1 for dt, _ in points if dt not in before)

    with open(csv_path(stid), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date_time", VAR])
        for dt, val in sorted(existing.items()):
            w.writerow([dt, val])

    wb_rows = sync_workbook(existing, stid)

    latest_dt, latest_val = sorted(existing.items())[-1]
    print(f"OK stid={stid} total={len(existing)} new={new_count} "
          f"latest={latest_dt} {VAR}={latest_val} workbook={wb_rows}")


if __name__ == "__main__":
    sys.exit(main())
