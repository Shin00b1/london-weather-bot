#!/usr/bin/env python3
"""Advection-day analysis — can the dawn-lock trade extend to weak-range days?

Motivation (2026-09-04): the regime gate stands down on every D-1 forecast
range < 8C day. That is right for deep winter (Nov-Feb advection lock ~50%)
but lumps in warm-season weak-range days, which behave completely differently
(sun rises before 06Z -> the min is already locked at dawn). This script
measures that split and price-backtests trading the warm-season advection
days with the same METAR dawn-lock signal.

Read-only analysis; prints physics tables + price backtests.
"""
import csv
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot1_backtest_corrected import (
    load_metar, load_fcst, build_price_lookup, vwap_window, code_is_rain,
    D0, D1, STAKE, ENTRY_WIN, BAND, REGIME_MIN_RANGE, MIN_OBS,
)

LON = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
FCST_CSV = os.path.join(LONDON, "forecast_archive_d1.csv")
WIND_CSV = os.path.join(LONDON, "metar_eglc_wind.csv")
REGIME_CSV = os.path.join(LONDON, "bot1_regime_daily.csv")

WARM_MONTHS = (4, 5, 6, 7, 8, 9, 10)   # sunrise before ~06Z in London
RANGE_THRESH = 8.0
DIR_MIN_WIND = 6.0


def load_wind():
    days = defaultdict(list)
    if not os.path.exists(WIND_CSV):
        return days
    with open(WIND_CSV) as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                sk = float(r["sknt"]) if r.get("sknt") else None
                dr = float(r["drct"]) if r.get("drct") else None
                days[dt.astimezone(LON).date()].append((dt, sk, dr))
            except (ValueError, KeyError):
                continue
    for d in days:
        days[d].sort()
    return days


def at(obs, hour):
    tgt = obs[0][0].replace(hour=hour, minute=0, second=0, microsecond=0)
    return min(obs, key=lambda o: abs((o[0] - tgt).total_seconds()))


def angdiff(a, b):
    if a is None or b is None:
        return None
    return (b - a + 180) % 360 - 180


def season_of(d):
    return "warm" if d.month in WARM_MONTHS else "winter"


def main():
    obs, rain = load_metar()
    fcst_pairs = load_fcst_pairs()
    wind = load_wind()
    int_map, trades = build_price_lookup()

    # ---------- physics: advection lock by season ----------
    reg = load_regime_rows()
    adv = [r for r in reg if r["rng"] is not None and r["rng"] < RANGE_THRESH]
    warm = [r for r in adv if season_of(r["d"]) == "warm"]
    cold = [r for r in adv if season_of(r["d"]) == "winter"]
    print("=== PHYSICS: advection (<8C) lock@06Z by season ===")
    print("warm-season (Apr-Oct): n=%d lock=%.1f%%" % (
        len(warm), 100 * sum(r["lock"] for r in warm) / len(warm)))
    print("winter     (Nov-Mar): n=%d lock=%.1f%%" % (
        len(cold), 100 * sum(r["lock"] for r in cold) / len(cold)))
    print("radiation   (>=8C)  : n=%d lock=%.1f%%" % (
        len(reg) - len(adv),
        100 * sum(r["lock"] for r in reg if r["rng"] is not None and r["rng"] >= RANGE_THRESH)
        / max(1, sum(1 for r in reg if r["rng"] is not None and r["rng"] >= RANGE_THRESH))))

    # ---------- conditioners on warm-season advection ----------
    # features knowable at 06Z, per day
    feat = {}
    for r in warm:
        d = r["d"]
        pts = obs.get(d, [])
        if len(pts) < MIN_OBS:
            continue
        pre = [(dt, v) for dt, v in pts if dt.hour < 6]
        if not pre:
            continue
        pre_min = min(v for _, v in pre)
        last_low_ts = max(dt for dt, v in pre if v == pre_min)
        flat_since_03 = last_low_ts.hour < 3
        fmin = fcst_pairs.get(d.isoformat(), (None, None))[1]
        cond_fcst = (fmin is not None and pre_min <= fmin + 1.0)
        wet = any(isr for dt, isr in rain.get(d, [])
                  if dt < datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc))
        w = wind.get(d, [])
        dd03 = ds03 = None
        if w:
            o3, o6 = at(w, 3), at(w, 6)
            s3, dr3, s6, dr6 = o3[1], o3[2], o6[1], o6[2]
            if s6 is not None and s3 is not None:
                ds03 = s6 - s3
            if dr3 is not None and dr6 is not None and s6 is not None and s6 >= DIR_MIN_WIND:
                dd03 = angdiff(dr3, dr6)
        feat[d] = {"lock": r["lock"], "rng": r["rng"], "pre_min": pre_min,
                   "flat03": flat_since_03, "cond_fcst": cond_fcst, "wet": wet,
                   "dd03": dd03, "ds03": ds03, "fmin": fmin}

    def rate(sub):
        if not sub:
            return "n=0"
        return "n=%d lock=%.1f%%" % (len(sub), 100 * sum(f["lock"] for f in sub) / len(sub))

    print("\n=== CONDITIONERS on warm-season advection (physics, all years) ===")
    print("all warm adv          :", rate(list(feat.values())))
    print("flat since 03Z        :", rate([f for f in feat.values() if f["flat03"]]))
    print("still falling 03-06Z  :", rate([f for f in feat.values() if not f["flat03"]]))
    print("pre6min<=fcstmin+1C   :", rate([f for f in feat.values() if f["cond_fcst"]]))
    print("pre6min> fcstmin+1C   :", rate([f for f in feat.values() if not f["cond_fcst"]]))
    print("dry                   :", rate([f for f in feat.values() if not f["wet"]]))
    print("wet                   :", rate([f for f in feat.values() if f["wet"]]))
    print("wind steady (<10deg)  :", rate([f for f in feat.values() if f["dd03"] is not None and abs(f["dd03"]) < 10]))
    print("wind shifting (>=10)  :", rate([f for f in feat.values() if f["dd03"] is not None and abs(f["dd03"]) >= 10]))
    print("range 6-8C            :", rate([f for f in feat.values() if 6 <= f["rng"] < 8]))
    print("range 5-6C            :", rate([f for f in feat.values() if 5 <= f["rng"] < 6]))
    print("range <5C             :", rate([f for f in feat.values() if f["rng"] < 5]))
    print("cond_fcst AND dry     :", rate([f for f in feat.values() if f["cond_fcst"] and not f["wet"]]))
    print("cond_fcst OR flat03   :", rate([f for f in feat.values() if f["cond_fcst"] or f["flat03"]]))

    # ---------- price backtest ----------
    print("\n=== PRICE BACKTEST (entry VWAP ask-side [05:30,06:30) UTC, $%d/trade) ===" % STAKE)

    def run_variant(name, take_day, band=BAND, use_rain=True):
        taken = wins = 0
        cum = 0.0
        peak = dd = 0.0
        monthly = defaultdict(float)
        detail = []
        cur = D0
        while cur <= D1:
            pts = obs.get(cur, [])
            if len(pts) < MIN_OBS:
                cur += timedelta(days=1)
                continue
            pre = [v for dt, v in pts if dt.hour < 6]
            if not pre:
                cur += timedelta(days=1)
                continue
            pre_min = min(pre)
            call = round(pre_min)
            day_min = min(v for _, v in pts)
            winner = round(day_min)
            if not take_day(cur, pre_min):
                cur += timedelta(days=1)
                continue
            if use_rain and any(isr for dt, isr in rain.get(cur, [])
                                if dt < datetime(cur.year, cur.month, cur.day, 6, tzinfo=timezone.utc)):
                cur += timedelta(days=1)
                continue
            label = int_map.get(cur, {}).get(call) or ("%dc" % call)
            entry, n = vwap_window(trades.get(cur, {}).get(label, []), cur)
            if entry is None:
                cur += timedelta(days=1)
                continue
            if entry < band[0] or entry > band[1]:
                cur += timedelta(days=1)
                continue
            taken += 1
            hit = (call == winner)
            pnl = STAKE * (1.0 / entry - 1.0) if hit else -STAKE
            cum += pnl
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
            wins += hit
            monthly[cur.strftime("%Y-%m")] += pnl
            detail.append((cur, pre_min, winner, entry, hit, pnl, cum))
            cur += timedelta(days=1)
        print("\n--- %s ---" % name)
        for row in detail:
            print("%-10s pre=%5.1f winner=%2d entry=%.3f %s pnl=%+8.2f cum=%+8.2f" % (
                row[0], row[1], row[2], row[3], "W" if row[4] else "L", row[5], row[6]))
        if taken:
            print("trades=%d wins=%d (%.1f%%) net=%+.2f avg/trade=%+.2f maxDD=%.2f" % (
                taken, wins, 100 * wins / taken, cum, cum / taken, dd))
            print("monthly: %s" % {k: round(v, 2) for k, v in sorted(monthly.items())})
        else:
            print("no trades")
        return cum, taken

    fcst_rng = {}
    for k, (mx, mn) in fcst_pairs.items():
        fcst_rng[k] = mx - mn

    def is_rad(d):
        r = fcst_rng.get(d.isoformat())
        return r is not None and r >= REGIME_MIN_RANGE

    def is_warm_adv(d):
        r = fcst_rng.get(d.isoformat())
        return r is not None and r < REGIME_MIN_RANGE and season_of(d) == "warm"

    def is_warm_adv_cond(d, pre_min):
        f = feat.get(d)
        return is_warm_adv(d) and f is not None and f["cond_fcst"]

    run_variant("V0 baseline: radiation only (current bot)", lambda d, p: is_rad(d))
    run_variant("V1 RISKY: radiation + ALL warm-season advection", lambda d, p: is_rad(d) or is_warm_adv(d))
    run_variant("V1b RISKY: warm advection only (isolated)", lambda d, p: is_warm_adv(d))
    run_variant("V2: radiation + warm adv w/ fcst-min conditioner",
                lambda d, p: is_rad(d) or is_warm_adv_cond(d, p))
    run_variant("V3 NO-RAIN-VETO: radiation + warm adv, rain allowed",
                lambda d, p: is_rad(d) or is_warm_adv(d), use_rain=False)
    run_variant("V4 WIDE BAND 0.55-0.97: radiation + warm adv",
                lambda d, p: is_rad(d) or is_warm_adv(d), band=(0.55, 0.97))


def load_fcst_pairs():
    out = {}
    with open(FCST_CSV) as f:
        for r in csv.DictReader(f):
            try:
                out[r["date"]] = (float(r["d1_ecmwf_max"]), float(r["d1_ecmwf_min"]))
            except (KeyError, ValueError):
                continue
    return out


def load_regime_rows():
    rows = []
    with open(REGIME_CSV) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"d": datetime.strptime(r["date"], "%Y-%m-%d").date(),
                             "rng": float(r["fcst_range"]) if r["fcst_range"] else None,
                             "lock": int(r["lock06"])})
            except (KeyError, ValueError):
                continue
    return rows


if __name__ == "__main__":
    main()
