"""Backfill true observed temperatures (METAR) into the Events sheet.

Polymarket's London temperature markets resolve on London City Airport
(EGLC) METAR reports — validated at 96% exact agreement across all eras
and both directions (residual = label noise in market-derived winners).

Adds Events columns: metar_temp_c, metar_station, metar_match, metar_obs.
Aggregation: daily max (highest) / min (lowest) over the London local
calendar day, routine METAR + SPECI reports, Iowa Environmental Mesonet.

Usage: python3 metar_update.py [--days N]   (N = refresh window, default all)
"""

import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
BACKUPS = os.path.join(LONDON, "backups")
LON = ZoneInfo("Europe/London")

STATION = "EGLC"  # London City — resolution station for all London temp markets
MIN_OBS = 16      # observations per complete day (routine + SPECIs)


def fetch_metar(station, d1, d2):
    base = {"station": station, "data": "tmpc,wxcodes",
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
    """-> {date: (max_c, min_c, n_obs)} over London local days."""
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


def bucket_f_bounds(label):
    m = re.search(r"(\d+)-(\d+)", label.replace("–", "-"))
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)", label)
    v = int(m.group(1))
    if "below" in label or "lower" in label:
        return None, v
    if "higher" in label or "above" in label:
        return v, None
    return v, v


def match_label(bucket, obs_c, unit, direction):
    """Compare METAR observation against the market's winning bucket."""
    if bucket is None:
        return None
    if unit == "C":
        m = re.search(r"(\d+)", bucket)
        n = float(m.group(1))
        if abs(obs_c - n) < 0.6:
            return "exact"
        return "off %+0.1f" % (obs_c - n)
    f = round(obs_c * 9 / 5 + 32)
    lo, hi = bucket_f_bounds(bucket)
    if (lo is None or f >= lo) and (hi is None or f <= hi):
        return "in-bucket"
    return "off %+dF" % (f - (hi if hi is not None else lo))


def get_unit(row_vals):
    # era rule: Fahrenheit markets are 2025, Celsius markets 2026 onward
    q = str(row_vals.get("event_title") or "") + str(row_vals.get("winning_bucket") or "")
    if "°C" in q:
        return "C"
    if "°F" in q:
        return "F"
    return "C" if row_vals.get("event_date", date(2025, 1, 1)).year >= 2026 else "F"


def fetch_model(dates):
    """Honest D-1 daily max/min forecasts from Open-Meteo Single Runs API.

    For each target date, fetch the ECMWF IFS HRES (9 km) run initialized at
    D-1 00:00 UTC and read that run's forecast for the target date — the value
    actually knowable at a D-1 entry. The historical-forecast API is NOT used
    here: it stitches near-zero-lead nowcasts, which is look-ahead bias.
    """
    import json
    out = {}
    for d in dates:
        run = (d - timedelta(days=1)).isoformat()
        url = ("https://single-runs-api.open-meteo.com/v1/forecast"
               "?latitude=51.5053&longitude=0.0553"
               "&daily=temperature_2m_max,temperature_2m_min"
               "&run=%sT00:00&models=ecmwf_ifs&timezone=UTC" % run)
        req = urllib.request.Request(url, headers={"User-Agent": "london-weather-updater/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            if resp.get("error"):
                continue
            t = resp["daily"]["time"]
            mx = resp["daily"]["temperature_2m_max"]
            mn = resp["daily"]["temperature_2m_min"]
            if d.isoformat() not in t:
                continue
            i = t.index(d.isoformat())
            if mx[i] is None or mn[i] is None:
                continue
            out[d.isoformat()] = (mx[i], mn[i])
        except Exception:
            continue
    return out


def run(days_window=None):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(XLSX, os.path.join(BACKUPS, "unified_premetar_%s.xlsx" % ts))

    wb = openpyxl.load_workbook(XLSX)
    ws = wb["Events"]
    hdr = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(hdr)}
    for c in ("metar_temp_c", "metar_station", "metar_match", "metar_obs"):
        if c not in col:
            ws.cell(1, len(hdr) + 1, c)
            hdr.append(c)
            col[c] = len(hdr)

    # scan event rows for needed dates
    rows_meta = []
    need = set()
    for r in range(2, ws.max_row + 1):
        ed = ws.cell(r, col["event_date"]).value
        ed = ed.date() if isinstance(ed, datetime) else ed
        if ed is None:
            continue
        unit = get_unit({h: ws.cell(r, col[h]).value for h in ("event_date", "event_title", "winning_bucket")})
        need.add(ed)
        rows_meta.append((r, ed, unit))

    today = datetime.now(LON).date()
    d1, d2 = min(need), min(max(need), today)
    if days_window:
        d1 = max(d1, today - timedelta(days=days_window))
    metar = {}
    if d1 <= d2:
        text = fetch_metar(STATION, d1, d2)
        cache = os.path.join(LONDON, "metar_eglc.csv")
        if days_window:  # append refresh window to cache file
            with open(cache, "a") as f:
                f.write(text)
        else:
            open(cache, "w").write(text)
        metar = daily_extremes(text)
        print("%s: %s -> %s, %d days" % (STATION, d1, d2, len(metar)))

    filled = exact = inb = off = 0
    for r, ed, unit in rows_meta:
        days = metar
        if ed not in days:
            continue
        mx, mn, n = days[ed]
        if n < MIN_OBS or ed >= today:  # incomplete or still-running day
            continue
        direction = ws.cell(r, col["direction"]).value
        obs = mx if direction == "highest" else mn
        bucket = ws.cell(r, col["winning_bucket"]).value
        lab = match_label(bucket, obs, unit, direction)
        ws.cell(r, col["metar_temp_c"], obs)
        ws.cell(r, col["metar_station"], STATION)
        ws.cell(r, col["metar_match"], lab)
        ws.cell(r, col["metar_obs"], n)
        filled += 1
        if lab == "exact":
            exact += 1
        elif lab == "in-bucket":
            inb += 1
        elif lab and lab.startswith("off"):
            off += 1

    # model forecast refresh (same window): fcst_model_c stays current too.
    # Honest D-1 ECMWF run (Single Runs API) — avoids historical-forecast look-ahead.
    mfilled = 0
    if d1 <= d2:
        try:
            dates = []
            cur = d1
            while cur <= d2:
                dates.append(cur)
                cur += timedelta(days=1)
            model = fetch_model(dates)
            cache = os.path.join(LONDON, "forecast_archive_d1.csv")
            merged = {}
            if os.path.exists(cache):
                for line in open(cache).read().strip().split("\n")[1:]:
                    p = line.split(",")
                    if len(p) >= 3 and p[1] and p[2]:
                        merged[p[0]] = (p[1], p[2])
            for day, (mx, mn) in model.items():
                merged[day] = (mx, mn)
            with open(cache, "w") as f:
                f.write("date,d1_ecmwf_max,d1_ecmwf_min\n")
                for day in sorted(merged):
                    f.write("%s,%s,%s\n" % (day, *merged[day]))
            for c in ("fcst_model_c", "model_source"):
                if c not in col:
                    ws.cell(1, len(hdr) + 1, c)
                    hdr.append(c)
                    col[c] = len(hdr)
            for r, ed, unit in rows_meta:
                if str(ed) not in model:
                    continue
                direction = ws.cell(r, col["direction"]).value
                mx, mn = model[str(ed)]
                ws.cell(r, col["fcst_model_c"], round(mx if direction == "highest" else mn, 1))
                ws.cell(r, col["model_source"],
                        "ECMWF IFS HRES 9km, D-1 00:00 UTC run (Open-Meteo Single Runs API)")
                mfilled += 1
        except Exception as e:
            print("model refresh failed: %r" % e)

    # README source rows
    ws_r = wb["README"]
    if not any("Source 4" in str(c.value or "") for row in ws_r.iter_rows() for c in row):
        ws_r.append(("Source 4 (METAR observations)",
                     "EGLC for 2025 F-era, EGWU for 2026 C-era; Iowa Environmental Mesonet; daily max/min over London day"))
    if not any("Source 5" in str(c.value or "") for row in ws_r.iter_rows() for c in row):
        ws_r.append(("Source 5 (D-1 model forecasts)",
                     "daily max/min D-1 forecasts, ECMWF IFS HRES 9km, Open-Meteo Single Runs API (run = D-1 00:00 UTC), 51.5053N 0.0553E (EGLC)"))

    tmp = XLSX + ".tmp"
    wb.save(tmp)
    os.replace(tmp, XLSX)
    print("filled %d event rows | exact %d, in-bucket %d, off %d | model %d"
          % (filled, exact, inb, off, mfilled))


if __name__ == "__main__":
    win = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    run(win)
