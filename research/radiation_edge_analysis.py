"""Radiation + morning-temperature edge analysis for the London MAX market.

Tests the user's hypothesis: on a clear (high-radiation) day, a hot morning
locks in a high daily maximum — "too hot to cool down fast after ~1pm BST" —
so the afternoon max is bounded below by the morning reading, and the market
(which prices off the D-1 forecast) lags this same-day observation.

All inputs are knowable by late morning of the target day:
  morning_temp   EGLC METAR temp over the 08:00-11:00 UTC window (observed)
  rad_d1         D-1 shortwave_radiation_sum forecast (Single Runs, ecmwf_ifs)
  max_d1         D-1 ECMWF max forecast (forecast_archive_d1.csv)
Settlement is the EGLC daily max over the London local day.

Sections:
  1. Physics  - does rad_d1 add info about max beyond max_d1?
  2. Floor    - on clear days, is max >= morning_temp (no post-morning cool)?
  3. Accuracy - MAE: max_d1 vs morning_temp vs morning_temp+rad_d1
  4. Market   - pre-noon winning-bucket price vs signal (trade tape, recent)
"""

import csv
import json
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
LON = ZoneInfo("Europe/London")

MORNING_START_UTC = 8   # 08:00 UTC
MORNING_END_UTC = 11    # 11:00 UTC (exclusive)


def load_metar():
    """-> {date: {'max':.., 'min':.., 'morning':.., 'n':..}} over London days."""
    out = defaultdict(lambda: {"vals": [], "morning": []})
    with open(os.path.join(LONDON, "metar_eglc.csv")) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                v = float(row["tmpc"])
            except (ValueError, KeyError):
                continue
            d = dt.astimezone(LON).date()
            out[d]["vals"].append(v)
            if MORNING_START_UTC <= dt.hour < MORNING_END_UTC:
                out[d]["morning"].append(v)
    res = {}
    for d, e in out.items():
        if len(e["vals"]) < 16:
            continue
        res[d] = {
            "max": max(e["vals"]),
            "min": min(e["vals"]),
            "morning": statistics.mean(e["morning"]) if e["morning"] else None,
            "n": len(e["vals"]),
        }
    return res


def load_csv_map(path, key, val, cast=float):
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                if row.get(val) in (None, "", "None"):
                    continue
                out[row[key]] = cast(row[val])
            except (ValueError, KeyError):
                continue
    return out


def load_d1_forecast():
    """-> {date: {'max':.., 'min':..}}."""
    out = {}
    with open(os.path.join(LONDON, "forecast_archive_d1.csv"), newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[row["date"]] = {"max": float(row["d1_ecmwf_max"]),
                                    "min": float(row["d1_ecmwf_min"])}
            except (ValueError, KeyError):
                continue
    return out


def main():
    metar = load_metar()
    rad_d1 = load_csv_map(os.path.join(LONDON, "radiation_forecast_d1.csv"),
                          "date", "d1_shortwave_sum")
    rad_act = load_csv_map(os.path.join(LONDON, "radiation_actual.csv"),
                           "date", "shortwave_sum")
    fcst = load_d1_forecast()

    # join
    rows = []
    for d in sorted(metar):
        if d.isoformat() not in fcst:
            continue
        r = {
            "date": d,
            "max": metar[d]["max"],
            "min": metar[d]["min"],
            "morning": metar[d]["morning"],
            "max_d1": fcst[d.isoformat()]["max"],
            "rad_d1": rad_d1.get(d.isoformat()),
            "rad_act": rad_act.get(d.isoformat()),
        }
        rows.append(r)

    with_both_rad = [r for r in rows if r["rad_d1"] is not None and r["rad_act"] is not None]
    with_morning = [r for r in rows if r["morning"] is not None]
    print("days with METAR+forecast: %d" % len(rows))
    print("days with both radiation: %d" % len(with_both_rad))
    print("days with morning temp:   %d" % len(with_morning))

    # ---- Section 1: physics ----
    def corr(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx == 0 or vy == 0:
            return None
        return cov / (vx * vy) ** 0.5

    def report_corr(label, xs, ys):
        c = corr(xs, ys)
        print("  %-38s r=%.3f (n=%d)" % (label, c, len(xs)) if c is not None
              else "  %-38s insufficient" % label)

    print("\n=== 1. PHYSICS (correlations) ===")
    report_corr("max_actual  vs max_d1", [r["max_d1"] for r in rows],
                [r["max"] for r in rows])
    report_corr("max_actual  vs rad_d1", [r["rad_d1"] for r in with_both_rad],
                [r["max"] for r in with_both_rad])
    report_corr("max_actual  vs rad_actual", [r["rad_act"] for r in with_both_rad],
                [r["max"] for r in with_both_rad])
    report_corr("max_d1      vs rad_d1", [r["rad_d1"] for r in with_both_rad],
                [r["max_d1"] for r in with_both_rad])
    report_corr("diurnal(min-max) vs rad_actual", [r["rad_act"] for r in with_both_rad],
                [r["max"] - r["min"] for r in with_both_rad])
    # partial: residual of max on max_d1, vs rad_d1
    # simple linear residual via least squares on the reduced set
    if len(with_both_rad) > 3:
        xs = [r["max_d1"] for r in with_both_rad]
        ys = [r["max"] for r in with_both_rad]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        a = my - b * mx
        resid = [y - (a + b * x) for x, y in zip(xs, ys)]
        report_corr("resid(max|max_d1) vs rad_d1", [r["rad_d1"] for r in with_both_rad], resid)

    # ---- Section 2: floor ----
    print("\n=== 2. FLOOR (clear-day: max vs morning) ===")
    if with_morning:
        clear = sorted(with_morning, key=lambda r: r["rad_d1"] or 0)
        # top tercile of rad_d1 = "clear"
        n = len(clear)
        clear_days = clear[2 * n // 3:]
        cloudy_days = clear[:n // 3]
        for name, grp in [("cloudy (low rad_d1)", cloudy_days),
                          ("clear  (high rad_d1)", clear_days)]:
            valid = [r for r in grp if r["morning"] is not None]
            gaps = [r["max"] - r["morning"] for r in valid]
            n_neg = sum(1 for g in gaps if g < 0)
            print("  %-22s n=%d  max-morning: mean=%.2f median=%.2f "
                  "min=%.2f  P(max<morning)=%d/%d (%.0f%%)"
                  % (name, len(valid), statistics.mean(gaps),
                     statistics.median(gaps), min(gaps), n_neg, len(valid),
                     100 * n_neg / len(valid)))

    # ---- Section 3: accuracy ----
    print("\n=== 3. ACCURACY (MAE predicting daily max) ===")
    def mae(preds, truth):
        d = [abs(p - t) for p, t in zip(preds, truth) if p is not None and t is not None]
        return statistics.mean(d) if d else None

    common = [r for r in with_morning if r["rad_d1"] is not None]
    t = [r["max"] for r in common]
    print("  baseline max_d1:            MAE=%.2f C (n=%d)"
          % (mae([r["max_d1"] for r in common], t), len(common)))
    print("  morning_temp as predictor:  MAE=%.2f C"
          % mae([r["morning"] for r in common], t))
    # morning + rad_d1 linear model (in-sample, indicative only)
    if len(common) > 5:
        # fit max ~ a + b*morning + c*rad_d1
        X1 = [r["morning"] for r in common]
        X2 = [r["rad_d1"] for r in common]
        Y = [r["max"] for r in common]
        # normal equations for 2 predictors
        import itertools
        A = [[sum(x * y for x, y in zip(xi, xj)) for xj in ([1] * len(Y), X1, X2)]
             for xi in ([1] * len(Y), X1, X2)]
        bvec = [sum(xi * y for xi, y in zip(xi, Y)) for xi in ([1] * len(Y), X1, X2)]
        # solve 3x3
        n = 3
        for col in range(n):
            piv = A[col][col]
            for j in range(n):
                A[col][j] /= piv
            bvec[col] /= piv
            for r in range(n):
                if r == col:
                    continue
                f = A[r][col]
                for j in range(n):
                    A[r][j] -= f * A[col][j]
                bvec[r] -= f * bvec[col]
        a, b, c = bvec[0], bvec[1], bvec[2]
        pred = [a + b * m + c * rd for m, rd in zip(X1, X2)]
        print("  morning+rad_d1 (in-sample): MAE=%.2f C | max = %.2f + %.2f*morning + %.3f*rad"
              % (mae(pred, Y), a, b, c))
        # does rad_d1 coefficient have sign/magnitude worth noting?
        print("  -> rad_d1 coefficient c=%.3f C per MJ/m^2 (positive = more sun -> hotter max)" % c)

    # ---- Section 4: market edge (trade tape) ----
    print("\n=== 4. MARKET (pre-noon winning-bucket price vs signal) ===")
    try:
        import duckdb
        import openpyxl
        wb = openpyxl.load_workbook(os.path.join(LONDON, "london_temperature_unified.xlsx"), read_only=True)
        ws = wb["Markets"]
        hdr = [c.value for c in ws[1]]
        col = {h: i for i, h in enumerate(hdr)}
        winners = {}  # event_slug -> (condition_id, resolved_temp_c, bucket_label)
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r[col["direction"]] != "highest":
                continue
            if r[col["won"]]:
                winners[r[col["event_slug"]]] = (r[col["condition_id"]], r[col["resolved_temp_c"]], r[col["bucket_label"]])
        wb.close()

        con = duckdb.connect(os.path.join(LONDON, "tape.duckdb"), read_only=True)
        # pre-noon = trades before 12:00 UTC on the event date
        results = []
        for slug, (cond, win_temp, label) in winners.items():
            if not cond:
                continue
            rows = con.execute(
                "SELECT timestamp, price, size, outcome FROM trades "
                "WHERE condition_id=? ORDER BY timestamp", [str(cond).lower()]).fetchall()
            if not rows:
                continue
            # convert each to p_yes, keep pre-noon (12:00 UTC) only
            pre = []
            for ts, price, size, outcome in rows:
                p_yes = price if outcome == "Yes" else (1 - price if price is not None else None)
                if p_yes is None:
                    continue
                hh = datetime.fromtimestamp(ts, timezone.utc).hour
                if hh < 12:
                    pre.append((p_yes, size or 0))
            if not pre:
                continue
            vwap = sum(p * s for p, s in pre) / sum(s for p, s in pre) if sum(s for p, s in pre) else statistics.mean([p for p, _ in pre])
            results.append((slug[-13:-4], win_temp, vwap, len(pre)))
        con.close()
        if results:
            print("  winning-bucket pre-noon VWAP (recent highest events, tape window):")
            for slug_date, wt, vw, n in sorted(results):
                print("    %s  win=%s  pre-noon_p=%s (n=%d)" % (slug_date, wt, round(vw, 3), n))
            vws = [v for _, _, v, _ in results]
            print("  -> median pre-noon winning prob: %.3f | min %.3f | max %.3f (n=%d)"
                  % (statistics.median(vws), min(vws), max(vws), len(vws)))
        else:
            print("  (no pre-noon winning-bucket trades in tape window)")
    except Exception as e:
        print("  market section skipped: %r" % e)


if __name__ == "__main__":
    main()
