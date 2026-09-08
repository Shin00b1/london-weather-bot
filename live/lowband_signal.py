#!/usr/bin/env python3
"""Low-band 06Z station signal for the London temperature workbook.

Every night, replays the trading rule validated on 2026-08-26:

  The daily minimum settles pre-dawn. Using the two private Synoptic
  stations (AMB5847 + WXM4398, 5-min air temp), the mean of the two
  per-station running minimums at the 06:00 UTC cutoff — each corrected by
  the per-hour station-minus-EGLC delta fitted on PRIOR days only (no
  look-ahead) — is the near-final call for the day's lowest-temperature
  bucket ("min locked by dawn": held 6/6 days in the validation week,
  worst overshoot 0.09 C).

Days are LONDON-LOCAL calendar days (that is how the markets settle;
overnight lows belong to the morning of the same local date).

For every day covered by the station CSVs (<STID>_air_temp.csv):
  call          mean corrected running min at the 06Z cutoff -> call_bucket
                (nearest int) + call_margin_c (distance to the x.5 edge)
  final         end-of-day mean corrected station min (once day completes)
  settle        EGLC METAR daily min over the London local day (>=16 obs)
  fcst_d1_min   honest D-1 ECMWF min (forecast_archive_d1.csv)

Rows live in london/lowband_archive.csv (source of truth, survives the
7-day rolling window of the free Synoptic token) and are mirrored to the
"LowBand" sheet of london/london_temperature_unified.xlsx with a README
source row and a pre-save backup. Idempotent upsert by date; existing
values are never erased.

Usage:  python3 lowband_signal.py
"""

import bisect
import csv
import os
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
UNIFIED = os.path.join(LONDON, "london_temperature_unified.xlsx")
ARCHIVE = os.path.join(LONDON, "lowband_archive.csv")
BACKUPS = os.path.join(LONDON, "backups")

STIDS = ["AMB5847", "WXM4398"]
VAR = "air_temp"
LON = ZoneInfo("Europe/London")

CUTOFF_UTC_HOUR = 6      # 06:00 UTC = 07:00 BST: the "locked by dawn" moment
MIN_SETTLE_OBS = 16      # same completeness bar as metar_update.py

FIELDS = ["date", "amb_min_06z_raw", "wxm_min_06z_raw", "call_min_c", "call_bucket",
          "call_margin_c", "fcst_d1_min", "settle_min_c", "final_corr_min",
          "call_hit", "final_hit", "rain_pre06", "rain_2h", "rain_after06",
          "n_obs_day", "status"]

# METAR present-weather tokens that mean liquid/convective precipitation
RAIN_TOKENS = ("RA", "DZ", "TS", "SH", "FZRA", "GR", "UP")


def code_is_rain(code):
    if not code or code == "M":
        return False
    return any(t.lstrip("+-").startswith(RAIN_TOKENS) for t in code.split())


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_station(stid):
    out = {}
    path = os.path.join(HERE, f"{stid}_{VAR}.csv")
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[parse_iso(row["date_time"])] = float(row[VAR])
                except (KeyError, ValueError):
                    continue
    return out


def load_metar_flat():
    """EGLC observations sorted by time: [(dt_utc, tmpc, rain)] plus per-London-day map."""
    flat = []
    path = os.path.join(LONDON, "metar_eglc.csv")
    if not os.path.exists(path):
        return flat
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                flat.append((dt, float(row["tmpc"]), code_is_rain(row.get("wxcodes"))))
            except (ValueError, KeyError):
                continue
    flat.sort()
    return flat


def load_fcst():
    """Honest D-1 min forecasts -> {'YYYY-MM-DD': value}."""
    out = {}
    path = os.path.join(LONDON, "forecast_archive_d1.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                try:
                    out[row["date"]] = float(row["d1_ecmwf_min"])
                except (KeyError, ValueError):
                    continue
    return out


def load_archive_rows():
    if not os.path.exists(ARCHIVE):
        return {}
    rows = {}
    with open(ARCHIVE, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows[row["date"]] = row
            except KeyError:
                continue
    return rows


def fnum(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def main():
    stations = {s: load_station(s) for s in STIDS}
    met_flat = load_metar_flat()
    met_times = [t for t, _, _ in met_flat]
    met_days = defaultdict(list)
    rain_days = defaultdict(list)   # London-day -> [(dt_utc, rain_bool)]
    for dt, v, rain in met_flat:
        met_days[dt.astimezone(LON).date()].append(v)
        rain_days[dt.astimezone(LON).date()].append((dt, rain))
    fcsts = load_fcst()
    archive = load_archive_rows()
    now = datetime.now(timezone.utc)

    def met_nearest(dt, tol_sec=1200):
        i = bisect.bisect_left(met_times, dt)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(met_flat):
                t, v, _ = met_flat[j]
                if abs((t - dt).total_seconds()) <= tol_sec and (
                        best is None or abs((t - dt).total_seconds()) < abs((best[0] - dt).total_seconds())):
                    best = (t, v)
        return best[1] if best else None

    # merged station points grouped by London-local date
    pts_by_day = defaultdict(list)
    for stid in STIDS:
        for dt, v in stations[stid].items():
            pts_by_day[dt.astimezone(LON).date()].append((dt, stid, v))
    for d in pts_by_day:
        pts_by_day[d].sort()

    def hour_table(target_day):
        """Per-UTC-hour station-minus-EGLC delta means, fitted ONLY on
        London-days strictly before target_day."""
        acc = defaultdict(list)
        for d in sorted(pts_by_day):
            if d >= target_day:
                break
            for dt, stid, v in pts_by_day[d]:
                m = met_nearest(dt)
                if m is not None:
                    acc[dt.hour].append(v - m)
        return {h: sum(vs) / len(vs) for h, vs in acc.items() if len(vs) >= 6}

    computed = 0
    for d in sorted(pts_by_day):
        pts = pts_by_day[d]
        day_complete = datetime(d.year, d.month, d.day, tzinfo=LON) + timedelta(days=1) <= now
        table = hour_table(d)
        cutoff = datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR, tzinfo=timezone.utc)

        row = {"date": d.isoformat(), "n_obs_day": len(pts)}

        def corrected_mins(subset):
            """-> {stid: (raw_min, corrected_min)} using each point's hour delta."""
            best = {}
            for dt, stid, v in subset:
                if stid not in best or v < best[stid][0]:
                    best[stid] = (v, dt)
            out = {}
            for stid, (v, dt) in best.items():
                c = table.get(dt.hour)
                out[stid] = (v, v - c if c is not None else None)
            return out

        pre = [(dt, stid, v) for dt, stid, v in pts if dt <= cutoff]
        if len(pre) >= 12:
            pre_mins = corrected_mins(pre)
            corr = [c for _, c in pre_mins.values() if c is not None]
            if corr:
                call = sum(corr) / len(corr)
                row["call_min_c"] = round(call, 2)
                b = round(call)
                row["call_bucket"] = b
                row["call_margin_c"] = round(abs((call - int(call)) - 0.5), 2)
            for k, stid in (("amb_min_06z_raw", "AMB5847"), ("wxm_min_06z_raw", "WXM4398")):
                if stid in pre_mins:
                    row[k] = round(pre_mins[stid][0], 2)
        elif d.isoformat() in archive:
            # station CSV rolled past this day: restore any previously stored call
            for k in FIELDS:
                if row.get(k) in ("", None) and archive[d.isoformat()].get(k) not in ("", None):
                    row[k] = archive[d.isoformat()][k]

        if day_complete:
            day_mins = corrected_mins(pts)
            corr = [c for _, c in day_mins.values() if c is not None]
            if corr:
                row["final_corr_min"] = round(sum(corr) / len(corr), 2)

        mvals = met_days.get(d, [])
        if day_complete and len(mvals) >= MIN_SETTLE_OBS:
            row["settle_min_c"] = min(mvals)

        # rain flags (frontal-veto inputs; knowable at entry vs post-hoc)
        rains = rain_days.get(d, [])
        if rains:
            row["rain_pre06"] = int(any(r for dt, r in rains
                                        if dt < datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR,
                                                         tzinfo=timezone.utc)))
            row["rain_2h"] = int(any(r for dt, r in rains
                                     if datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR - 2,
                                                 tzinfo=timezone.utc) <= dt
                                     < datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR,
                                                tzinfo=timezone.utc)))
            if day_complete:
                row["rain_after06"] = int(any(r for dt, r in rains
                                              if dt >= datetime(d.year, d.month, d.day, CUTOFF_UTC_HOUR,
                                                                tzinfo=timezone.utc)))

        ds = d.isoformat()
        if ds in fcsts:
            row["fcst_d1_min"] = fcsts[ds]

        s = fnum(row.get("settle_min_c"))
        cb = fnum(row.get("call_bucket"))
        fb = fnum(row.get("final_corr_min"))
        if s is not None and cb is not None:
            row["call_hit"] = int(round(cb) == round(s))
        if s is not None and fb is not None:
            row["final_hit"] = int(round(fb) == round(s))

        if day_complete and s is not None:
            row["status"] = "final"
        elif day_complete:
            row["status"] = "done_no_settle"
        elif cb is not None:
            row["status"] = "called"
        else:
            row["status"] = "pending"

        prev = archive.get(ds, {})
        for k in FIELDS:
            if row.get(k) in ("", None) and prev.get(k) not in ("", None):
                row[k] = prev[k]  # never erase computed history
        archive[ds] = {k: ("" if row.get(k) in ("", None) else row.get(k)) for k in FIELDS}
        computed += 1

    # persist archive CSV
    os.makedirs(LONDON, exist_ok=True)
    tmp_csv = ARCHIVE + ".tmp"
    with open(tmp_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for ds in sorted(archive):
            w.writerow(archive[ds])
    os.replace(tmp_csv, ARCHIVE)

    # mirror into workbook
    wb_rows = None
    try:
        import openpyxl
        os.makedirs(BACKUPS, exist_ok=True)
        shutil.copy2(UNIFIED, os.path.join(
            BACKUPS, "unified_prelowband_%s.xlsx"
            % datetime.now().strftime("%Y%m%d_%H%M%S")))
        wb = openpyxl.load_workbook(UNIFIED)
        if "LowBand" in wb.sheetnames:
            ws = wb["LowBand"]
            if ws.max_row > 1:
                ws.delete_rows(2, ws.max_row)
            # refresh header in place (never append a second one)
            for i, name in enumerate(FIELDS, start=1):
                ws.cell(1, i, name)
        else:
            ws = wb.create_sheet("LowBand")
            ws.append(FIELDS)
        for ds in sorted(archive):
            ws.append([archive[ds][k] for k in FIELDS])

        r = wb["README"]
        if not any("Source 8" in str(c.value or "") for rr in r.iter_rows() for c in rr):
            r.append(("Source 8 (low-band 06Z station signal)",
                      "Mean corrected 06Z running min of AMB5847+WXM4398 as the early "
                      "lowest-bucket call; hourly delta fit on prior days only "
                      "(no look-ahead); settlement = EGLC daily min, London day"))

        tmp_x = UNIFIED + ".tmp"
        wb.save(tmp_x)
        os.replace(tmp_x, UNIFIED)
        wb_rows = len(archive)
    except Exception as e:
        print(f"workbook sync skipped: {e!r}")

    last = archive[sorted(archive)[-1]] if archive else {}
    hits = sum(int(r["call_hit"]) for r in archive.values()
               if r.get("call_hit") not in ("", None))
    scored = sum(1 for r in archive.values() if r.get("call_hit") not in ("", None))
    print("OK days=%d latest=%s call=%sC->bucket=%s settle=%s fcst=%s status=%s "
          "| archive hits %d/%d | workbook_rows=%s"
          % (computed, last.get("date"), last.get("call_min_c"), last.get("call_bucket"),
             last.get("settle_min_c"), last.get("fcst_d1_min"), last.get("status"),
             hits, scored, wb_rows))


if __name__ == "__main__":
    main()
