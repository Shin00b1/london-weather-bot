"""Measure look-ahead in the Open-Meteo historical forecast archive.

The sheet's `fcst_model_c` is sourced from historical-forecast-api.open-meteo.com
which, per Open-Meteo's docs, stitches "each run's first few hours" into a
continuous series. For a given target date that means `best_match` is the
forecast from the run initialized closest to that date — effectively a
near-zero-lead nowcast, NOT the forecast a trader had at D-1.

This script reconstructs the *genuine* D-1 forecast from the Single Runs API
(single-runs-api.open-meteo.com, `run=` parameter) and compares three things
against the actual METAR observation:

  1. archive (best_match, near-zero-lead) error  -> what the backtest used
  2. D-1 forecast (run = target-1 00:00 UTC) error -> what was knowable at entry

Single Runs retains runs back to ~2026-04/05. We therefore cover every resolved
event with a METAR observation since 2026-05-01.

Outputs: london/lookahead_results.json (per-day rows) and a summary print.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
CACHE = os.path.join(LONDON, "lookahead_cache.json")
OUT = os.path.join(LONDON, "lookahead_results.json")

LAT, LON = "51.5053", "0.0553"


def _get(url, timeout=90, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "london-lookahead/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            if a == retries - 1:
                raise
            time.sleep(2 * (a + 1))


def single_runs(run, target):
    """Forecast as issued at `run` 00:00 UTC, read off at `target` date."""
    url = ("https://single-runs-api.open-meteo.com/v1/forecast"
           "?latitude=%s&longitude=%s"
           "&daily=temperature_2m_max,temperature_2m_min"
           "&run=%sT00:00&models=ukmo_seamless&timezone=UTC" % (LAT, LON, run))
    d = _get(url)
    if d.get("error"):
        return None
    t = d["daily"]["time"]
    mx = d["daily"]["temperature_2m_max"]
    mn = d["daily"]["temperature_2m_min"]
    if target not in t:
        return None
    i = t.index(target)
    return mx[i], mn[i]


def archive(target):
    """best_match value from the historical-forecast archive (what the sheet stores)."""
    url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
           "?latitude=%s&longitude=%s"
           "&daily=temperature_2m_max,temperature_2m_min"
           "&start_date=%s&end_date=%s&timezone=Europe%%2FLondon" % (LAT, LON, target, target))
    d = _get(url)
    return d["daily"]["temperature_2m_max"][0], d["daily"]["temperature_2m_min"][0]


def load_events():
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb["Events"]
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        ed = r[col["event_date"]]
        metar = r[col["metar_temp_c"]]
        direction = r[col["direction"]]
        if metar is None:
            continue
        if isinstance(ed, datetime):
            ed = ed.date()
        if ed < date(2026, 5, 1):
            continue
        out.append((ed, direction, float(metar)))
    return out


def main():
    events = load_events()
    # one D-1 run fetch per unique target date
    targets = sorted({e[0] for e in events})
    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(open(CACHE))

    rows = []
    for tgt in targets:
        ts = tgt.isoformat()
        run = (tgt - __import__("datetime").timedelta(days=1)).isoformat()
        key = ts
        if key not in cache:
            try:
                sr = single_runs(run, ts)
                ar = archive(ts)
                cache[key] = {"d1": sr, "archive": ar}
                json.dump(cache, open(CACHE, "w"), indent=1)
            except Exception as e:
                print("skip %s: %r" % (ts, e), file=sys.stderr)
                cache[key] = None
                json.dump(cache, open(CACHE, "w"), indent=1)
                continue
            time.sleep(0.4)

    for ed, direction, metar in events:
        ts = ed.isoformat()
        c = cache.get(ts)
        if not c:
            continue
        d1 = c["d1"]
        ar = c["archive"]
        if d1 is None or ar is None:
            continue
        d1_val = d1[0] if direction == "highest" else d1[1]
        ar_val = ar[0] if direction == "highest" else ar[1]
        rows.append({
            "date": ts, "direction": direction, "metar": metar,
            "d1": d1_val, "archive": ar_val,
            "d1_err": round(abs(d1_val - metar), 2),
            "archive_err": round(abs(ar_val - metar), 2),
        })

    json.dump(rows, open(OUT, "w"), indent=1)

    n = len(rows)
    d1_mae = sum(r["d1_err"] for r in rows) / n
    ar_mae = sum(r["archive_err"] for r in rows) / n
    d1_wins = sum(1 for r in rows if r["d1_err"] <= r["archive_err"])
    print("events compared: %d" % n)
    print("D-1 forecast MAE:  %.2f C" % d1_mae)
    print("archive MAE:       %.2f C" % ar_mae)
    print("archive is closer to actual than D-1 in %d/%d events (%.0f%%)"
          % (n - d1_wins, n, 100 * (n - d1_wins) / n))
    print("results -> %s" % OUT)


if __name__ == "__main__":
    main()
