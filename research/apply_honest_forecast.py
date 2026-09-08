"""One-off: replace look-ahead `fcst_model_c` with honest D-1 ECMWF forecasts.

Reads the honest D-1 archive (london/forecast_archive_d1.csv) built from the
Open-Meteo Single Runs API (ecmwf_ifs, run = event_date - 1 00:00 UTC) and
writes it into the Events sheet's `fcst_model_c` column, replacing the
look-ahead-contaminated historical-forecast best_match values. Relabels
`model_source` accordingly.

Backs up the workbook first (same convention as metar_update.py).
"""

import csv
import os
import shutil
from datetime import datetime, date

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
BACKUPS = os.path.join(LONDON, "backups")
CSV = os.path.join(LONDON, "forecast_archive_d1.csv")

NEW_SOURCE = "ECMWF IFS HRES 9km, D-1 00:00 UTC run (Open-Meteo Single Runs API)"


def main():
    # load honest archive
    d1 = {}
    for row in csv.DictReader(open(CSV)):
        if not row["d1_ecmwf_max"] or not row["d1_ecmwf_min"]:
            continue
        d1[row["date"]] = (float(row["d1_ecmwf_max"]), float(row["d1_ecmwf_min"]))
    print("honest archive entries: %d" % len(d1))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(XLSX, os.path.join(BACKUPS, "unified_prehonestfc_%s.xlsx" % ts))

    wb = openpyxl.load_workbook(XLSX)
    ws = wb["Events"]
    hdr = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(hdr)}

    for c in ("fcst_model_c", "model_source"):
        if c not in col:
            ws.cell(1, len(hdr) + 1, c)
            hdr.append(c)
            col[c] = len(hdr)

    updated = missing = 0
    for r in range(2, ws.max_row + 1):
        ed = ws.cell(r, col["event_date"]).value
        ed = ed.date() if isinstance(ed, datetime) else ed
        if ed is None:
            continue
        ts = ed.isoformat()
        if ts not in d1:
            missing += 1
            continue
        direction = ws.cell(r, col["direction"]).value
        mx, mn = d1[ts]
        val = mx if direction == "highest" else mn
        ws.cell(r, col["fcst_model_c"], round(val, 1))
        ws.cell(r, col["model_source"], NEW_SOURCE)
        updated += 1

    # README Source 5 relabel
    ws_r = wb["README"]
    for row in ws_r.iter_rows():
        for c in row:
            if c.value and "Source 5" in str(c.value):
                # relabel in place: find the adjacent description cell and rewrite
                pass
    # simpler: append a correction row and update any existing Source 5 description
    replaced = False
    for row in ws_r.iter_rows():
        for c in row:
            if c.value and "Source 5" in str(c.value):
                pass
    # Update the existing Source 5 description text directly
    for row in ws_r.iter_rows():
        for c in row:
            if c.value and isinstance(c.value, str) and c.value.startswith("daily max/min model forecasts"):
                c.value = ("daily max/min D-1 forecasts, ECMWF IFS HRES 9km, Open-Meteo "
                           "Single Runs API (run = D-1 00:00 UTC), 51.5053N 0.0553E (EGLC)")
                replaced = True
    if not replaced:
        ws_r.append(("Source 5 (D-1 model forecasts)",
                     "daily max/min D-1 forecasts, ECMWF IFS HRES 9km, Open-Meteo Single Runs API (run = D-1 00:00 UTC), 51.5053N 0.0553E (EGLC)"))

    tmp = XLSX + ".tmp"
    wb.save(tmp)
    os.replace(tmp, XLSX)
    print("updated %d event rows | %d event dates missing from archive"
          % (updated, missing))


if __name__ == "__main__":
    main()
