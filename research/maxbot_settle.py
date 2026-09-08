"""Correct max-market settlement + signal test.

The HIGHEST market ladder has three bucket kinds:
    'Nc'        -> wins iff round(day-max) == N
    '<=N'       -> wins iff round(day-max) <= N  (bottom tail)
    '>=N'       -> wins iff round(day-max) >= N  (top tail)

A predicted integer `call` maps to a real bucket by the same logic as the
ladder. This harness finds the TRUE winner bucket per day and prices entry
ask-side (taker BUY of Yes), so nothing is falsely scored.
"""
import csv
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import duckdb

LON = ZoneInfo("Europe/London")
DB = "/tmp/tape_snap.duckdb"
METAR = "london/metar_eglc.csv"
FCST = "london/forecast_archive_d1.csv"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"])}


def slug_date(ev):
    m = re.search(r"-on-([a-z]+)-(\d+)-2026$", ev)
    if m:
        return date(2026, MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(r"-on-([a-z]+)-(\d+)$", ev)
    if m:
        mon = MONTHS[m.group(1)]
        y = 2026 if mon in (1, 2) else 2025
        return date(y, mon, int(m.group(2)))
    return None


def bucket_of(slug):
    m = re.search(r"-(\d+)c(orbelow|orhigher)?$", slug)
    if not m:
        return None
    n = int(m.group(1))
    suf = m.group(2)
    kind = "<=" if suf == "orbelow" else ">=" if suf == "orhigher" else "="
    return n, kind


def load_ladders():
    """-> {day: {n: {'kind':.., 'buy':[(ts,p_yes,size)], 'all':[(ts,p_yes,size)]}}}"""
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT timestamp, slug, outcome, side, price, size FROM trades
        WHERE slug LIKE '%highest%' AND slug NOT LIKE '%-684'
          AND slug NOT LIKE '%-587' AND slug NOT LIKE '%-457'
    """).fetchall()
    con.close()
    out = defaultdict(dict)
    for ts, slug, outcome, side, price, size in rows:
        b = bucket_of(slug)
        if not b:
            continue
        n, kind = b
        ev = re.sub(r"-\d+c(orbelow|orhigher)?$", "", slug)
        d = slug_date(ev)
        if not d:
            continue
        p = float(price) if outcome == "Yes" else 1.0 - float(price)
        if n not in out[d]:
            out[d][n] = {"kind": kind, "buy": [], "all": []}
        out[d][n]["all"].append((float(ts), p, float(size)))
        # ask-side BUY of Yes: taker buys Yes (outcome=Yes side=BUY) or
        # taker sells No (outcome=No side=SELL) -> nets long Yes at 1-price.
        if (outcome == "Yes" and side == "BUY") or (outcome == "No" and side == "SELL"):
            out[d][n]["buy"].append((float(ts), p, float(size)))
    for d in out:
        for n in out[d]:
            out[d][n]["all"].sort()
            out[d][n]["buy"].sort()
    return out


def load_metar():
    out = defaultdict(list)
    for row in csv.DictReader(open(METAR)):
        try:
            t = float(row["tmpc"])
            dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        out[dt.astimezone(LON).date()].append((dt.timestamp(), t))
    return out


def load_fcst():
    out = {}
    for row in csv.DictReader(open(FCST)):
        try:
            out[datetime.strptime(row["date"], "%Y-%m-%d").date()] = \
                (float(row["d1_ecmwf_max"]), float(row["d1_ecmwf_min"]))
        except (ValueError, KeyError):
            continue
    return out


def winner_bucket(ladder, rmax):
    """Return the winning bucket key n for round(day-max)=rmax, or None."""
    keys = sorted(ladder)
    for n in keys:
        kind = ladder[n]["kind"]
        if kind == "=" and n == rmax:
            return n
    # tails
    bottom = min(keys)
    top = max(keys)
    if rmax <= bottom and ladder[bottom]["kind"] == "<=":
        return bottom
    if rmax >= top and ladder[top]["kind"] == ">=":
        return top
    return None


def bucket_key_for_call(ladder, call):
    """Map predicted integer call to a real bucket key, or None."""
    if call in ladder and ladder[call]["kind"] == "=":
        return call
    keys = sorted(ladder)
    bottom, top = keys[0], keys[-1]
    if call <= bottom and ladder[bottom]["kind"] == "<=":
        return bottom
    if call >= top and ladder[top]["kind"] == ">=":
        return top
    return None


def vwap(tr, t0, t1, ask_side=False):
    src = tr
    w = [(ts, p, s) for ts, p, s in src if t0 <= ts < t1]
    if not w:
        return None
    return sum(p * s for _, p, s in w) / sum(s for _, p, s in w)


def main():
    metar = load_metar()
    fcst = load_fcst()
    ladders = load_ladders()

    # settle only days with all three
    days = sorted(set(metar) & set(fcst) & set(ladders))
    print("settled days:", len(days))

    # --- signal: predicted_max = morning_min + forecast_range ; buy@09Z ---
    print("\n=== morning-anchored max (buy round(pred) @ [09Z,10Z)) ===")
    for pricemode in ("all", "ask"):
        print("\n--- pricing: %s-side VWAP ---" % pricemode)
        print("%-10s %5s %5s %6s %6s %6s %7s" % ("regime", "n", "win%", "meanP", "medP", "EV/$1", "sumEV"))
        for rname, gate in (("radiation", ">=8"), ("advection", "<8"), ("ALL", "all")):
            res = []
            for d in days:
                fmax, fmin = fcst[d]
                rng = fmax - fmin
                if gate == ">=8" and rng < 8:
                    continue
                if gate == "<8" and rng >= 8:
                    continue
                lst = sorted(metar[d])
                t9 = datetime(d.year, d.month, d.day, 9, tzinfo=timezone.utc).timestamp()
                pre9 = [t for ts, t in lst if ts < t9]
                if not pre9:
                    continue
                mmin = min(pre9)
                pmax = mmin + rng
                call = round(pmax)
                key = bucket_key_for_call(ladders[d], call)
                if key is None:
                    continue
                rmax = round(max(t for _, t in lst))
                wkey = winner_bucket(ladders[d], rmax)
                if wkey is None:
                    continue
                t1 = datetime(d.year, d.month, d.day, 10, tzinfo=timezone.utc).timestamp()
                tr = ladders[d][key]["buy"] if pricemode == "ask" else ladders[d][key]["all"]
                p = vwap(tr, t9, t1)
                if p is None:
                    continue
                hit = (key == wkey)
                res.append((hit, p))
            if len(res) < 5:
                print("%-10s %5d  (too few)" % (rname, len(res)))
                continue
            wins = sum(1 for h, _ in res if h)
            n = len(res)
            mp = statistics.mean(p for _, p in res)
            mep = statistics.median(p for _, p in res)
            ev = [(1.0 / p - 1 if h else -1.0) for h, p in res]
            print("%-10s %5d %5.0f%% %6.3f %6.3f %+6.3f %+7.2f" %
                  (rname, n, 100 * wins / n, mp, mep, statistics.mean(ev), sum(ev)))


if __name__ == "__main__":
    main()
