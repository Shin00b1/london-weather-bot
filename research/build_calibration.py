"""Build the Calibration sheet: market forecasts vs METAR outcomes.

Adds to Events:  fcst_temp_open, fcst_temp_close  (probability-weighted
expected temperature over the event's bands, at open and at close).

Creates sheet "Calibration":
  Table 1 — probability calibration: implied prob buckets vs realized win rate
  Table 2 — temperature forecast accuracy by era/direction (vs metar_temp_c)
  Table 3 — per-event detail

Method: band center temp = bucket midpoint (or bound for or-below/higher
tails); probs normalized to sum 1 across the event's bands. Rerun after
any backfill: python3 build_calibration.py
"""

import os
import shutil
from datetime import datetime

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
BACKUPS = os.path.join(LONDON, "backups")

BUCKETS = [0, .05, .15, .25, .35, .45, .55, .65, .75, .85, .95, 1.0001]
STAGES = ("open_prob", "close_prob", "vwap_prob")


def band_center(low, high):
    if low is None or high is None:
        return None
    if low == high:
        return float(low)
    return round((float(low) + float(high)) / 2, 2)


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(XLSX, os.path.join(BACKUPS, "unified_preCalib_%s.xlsx" % ts))
    wb = openpyxl.load_workbook(XLSX)

    mws = wb["Markets"]
    mhdr = [c.value for c in mws[1]]
    mc = {h: i for i, h in enumerate(mhdr)}   # 0-based for list access
    markets = [[c.value for c in row] for row in mws.iter_rows(min_row=2)]

    ews = wb["Events"]
    ehdr = [c.value for c in ews[1]]
    ec = {h: i for i, h in enumerate(ehdr)}   # 0-based for list access
    for c in ("fcst_temp_open", "fcst_temp_close"):
        if c not in ec:
            ews.cell(1, len(ehdr) + 1, c)
            ehdr.append(c)
            ec[c] = len(ehdr)
    events = [[c.value for c in row] for row in ews.iter_rows(min_row=2)]

    # group markets by event
    by_event = {}
    for r, m in enumerate(markets, start=2):
        es = m[mc["event_slug"]]
        if es:
            by_event.setdefault(es, []).append((r, m))

    # --- Table 1 data: (stage, prob, won) ---
    calib = {}  # (stage, bucket_idx) -> [n, sum_prob, wins]
    fcst_events = []  # (event_row_in_sheet, era, direction, date, fcst_open, fcst_close, metar)
    won_of = lambda m: m[mc["won"]]

    for ev_row, ev in enumerate(events, start=2):
        eslug = ev[ec["event_slug"]]
        ms = by_event.get(eslug)
        if not ms:
            continue
        resolved = all(m[mc["closed"]] for _, m in ms)
        # probability calibration pairs
        if resolved:
            for _, m in ms:
                won = m[mc["won"]]
                if won is None:
                    continue
                for stage in STAGES:
                    p = m[mc[stage]]
                    if p is None:
                        continue
                    p = min(max(float(p), 0.0), 1.0)
                    for bi in range(len(BUCKETS) - 1):
                        if BUCKETS[bi] <= p < BUCKETS[bi + 1]:
                            k = (stage, bi)
                            calib.setdefault(k, [0, 0.0, 0])
                            calib[k][0] += 1
                            calib[k][1] += p
                            calib[k][2] += 1 if won else 0
                            break
        # implied temperature (needs probs + centers for most bands)
        temps = {}
        for stage in ("open_prob", "close_prob"):
            num = den = 0.0
            for _, m in ms:
                p, lo, hi = m[mc[stage]], m[mc["bucket_low_c"]], m[mc["bucket_high_c"]]
                c = band_center(lo, hi)
                if p is None or c is None:
                    continue
                p = max(float(p), 0.0)
                num += p * c
                den += p
            temps[stage] = round(num / den, 1) if 0.5 < den < 1.6 and den else None
        era = "F" if str(ev[ec["event_title"]] or "") and (
            "°F" in str(ms[0][1][mc["question"]])) else "C"
        metar = ev[ec["metar_temp_c"]] if "metar_temp_c" in ec else None
        fmod = ev[ec["fcst_model_c"]] if "fcst_model_c" in ec else None
        direction = ev[ec["direction"]]
        date = ev[ec["event_date"]]
        if temps.get("open_prob") is not None or temps.get("close_prob") is not None or fmod is not None:
            fcst_events.append((ev_row, era, direction, date,
                                temps.get("open_prob"), temps.get("close_prob"), metar, eslug, fmod))
            ews.cell(ev_row, ec["fcst_temp_open"] + 1, temps.get("open_prob"))
            ews.cell(ev_row, ec["fcst_temp_close"] + 1, temps.get("close_prob"))

    # --- write the Calibration sheet ---
    if "Calibration" in wb.sheetnames:
        del wb["Calibration"]
    ws = wb.create_sheet("Calibration")

    ws.append(["CALIBRATION — market forecasts vs METAR outcomes (London temperature events)"])
    ws.append(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M UTC")])
    ws.append(["Method", "band center = bucket midpoint (bound for or-below/higher tails); "
                         "probs normalized across the event's bands; win = market's own resolution"])
    ws.append([])

    ws.append(["TABLE 1 — probability calibration (all resolved markets)"])
    ws.append(["stage", "prob bucket", "n markets", "avg implied prob", "realized win rate"])
    for stage in STAGES:
        for bi in range(len(BUCKETS) - 1):
            k = (stage, bi)
            if k not in calib:
                continue
            n, sp, wins = calib[k]
            ws.append([stage, "%.2f–%.2f" % (BUCKETS[bi], min(BUCKETS[bi + 1], 1.0)),
                       n, round(sp / n, 4), round(wins / n, 4)])
    ws.append([])

    ws.append(["TABLE 2 — forecast accuracy vs METAR (°C): market vs weather model"])
    ws.append(["era", "direction", "events", "MAE model", "MAE open", "MAE close",
               "bias model", "bias open", "bias close"])
    groups = {}
    for _, era, direction, _, fo, fc, metar, _, fm in fcst_events:
        if metar is None:
            continue
        groups.setdefault((era, direction), []).append((fo, fc, float(metar), fm))
    for (era, direction), rows in sorted(groups.items()):
        em = [(fm - t) for _, _, t, fm in rows if fm is not None]
        eo = [(fo - t) for fo, _, t, _ in rows if fo is not None]
        ecc = [(fc - t) for _, fc, t, _ in rows if fc is not None]
        mae_o = round(sum(abs(e) for e in eo) / len(eo), 2) if eo else None
        mae_c = round(sum(abs(e) for e in ecc) / len(ecc), 2) if ecc else None
        bias_o = round(sum(eo) / len(eo), 2) if eo else None
        bias_c = round(sum(ecc) / len(ecc), 2) if ecc else None
        mae_m = round(sum(abs(e) for e in em) / len(em), 2) if em else None
        bias_m = round(sum(em) / len(em), 2) if em else None
        ws.append([era, direction, len(rows), mae_m, mae_o, mae_c, bias_m, bias_o, bias_c])
    ws.append([])

    ws.append(["TABLE 3 — per-event detail (market forecast = prob-weighted expected temp)"])
    ws.append(["date", "direction", "era", "fcst model", "fcst open", "fcst close",
               "metar actual", "err model", "err open", "err close", "event_slug"])
    for _, era, direction, date, fo, fc, metar, eslug, fm in sorted(
            fcst_events, key=lambda x: (str(x[3]), x[2])):
        em_ = round(fm - float(metar), 1) if (fm is not None and metar is not None) else None
        eo = round(fo - float(metar), 1) if (fo is not None and metar is not None) else None
        ec_ = round(fc - float(metar), 1) if (fc is not None and metar is not None) else None
        ws.append([date, direction, era, fm, fo, fc, metar, em_, eo, ec_, eslug])

    tmp = XLSX + ".tmp"
    wb.save(tmp)
    os.replace(tmp, XLSX)
    print("calibration built: %d events, %d calib buckets" % (len(fcst_events), len(calib)))


if __name__ == "__main__":
    main()
