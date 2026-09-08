"""Max-market (highest-temperature) research harness.

Point-in-time dataset + signal evaluation for the London HIGHEST market.
Settlement = round(EGLC METAR daily max).  Data sources:
  - metar_eglc.csv            : hourly EGLC obs (temp) -> daily max/min + hour curves
  - forecast_archive_d1.csv   : honest D-1 ECMWF max/min (point-in-time)
  - tape.duckdb (snapshot)    : highest trades -> bucket ladder + prices by time

Only D-1-and-earlier information is used for each signal so results are
honest point-in-time.  Read-only scoring.
"""
import csv
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb

LON = ZoneInfo("Europe/London")
DB = "/tmp/tape_snap.duckdb"
METAR = "london/metar_eglc.csv"
FCST = "london/forecast_archive_d1.csv"

REGIME_MIN_RANGE = 8.0
MIN_OBS = 16

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"])}


def load_metar():
    """-> {date: {'max':c,'min':c,'obs':[(dt,temp)], 'n':int}}"""
    days = defaultdict(lambda: {"max": -99.0, "min": 99.0, "obs": [], "n": 0})
    for row in csv.DictReader(open(METAR)):
        try:
            t = float(row["tmpc"])
            if t < -50 or t > 60:
                continue
            dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        d = dt.astimezone(LON).date()
        days[d]["max"] = max(days[d]["max"], t)
        days[d]["min"] = min(days[d]["min"], t)
        days[d]["obs"].append((dt, t))
        days[d]["n"] += 1
    return {d: v for d, v in days.items() if v["n"] >= MIN_OBS}


def load_fcst():
    out = {}
    for row in csv.DictReader(open(FCST)):
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
            out[d] = (float(row["d1_ecmwf_max"]), float(row["d1_ecmwf_min"]))
        except (KeyError, ValueError):
            continue
    return out


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
    return (("<=%d" % n, n) if suf == "orbelow" else
            (">=%d" % n, n) if suf == "orhigher" else
            ("%dc" % n, n))


def load_prices():
    """-> {day: {bucket_int: {'label':.., 'kind':.., 'trades':[(ts,p_yes,size)]}}}"""
    con = duckdb.connect(DB, read_only=True)
    rows = con.execute("""
        SELECT timestamp, slug, outcome, price, size FROM trades
        WHERE slug LIKE '%highest%'
    """).fetchall()
    con.close()
    out = defaultdict(dict)
    for ts, slug, outcome, price, size in rows:
        ev = slug.rsplit("-", 1)[0] if False else slug
        # event slug = slug minus trailing bucket
        b = bucket_of(slug)
        if not b:
            continue
        # strip trailing '-NNc...' to get event slug
        ev = re.sub(r"-\d+c(orbelow|orhigher)?$", "", slug)
        d = slug_date(ev)
        if d is None:
            continue
        p_yes = float(price) if outcome == "Yes" else 1.0 - float(price)
        label, n = b
        if n not in out[d]:
            out[d][n] = {"label": label, "kind": label[0], "trades": []}
        out[d][n]["trades"].append((float(ts), p_yes, float(size)))
    for d in out:
        for n in out[d]:
            out[d][n]["trades"].sort()
    return out


def price_at(trades, t0, t1):
    """VWAP of p_yes over [t0,t1) in epoch; None if empty."""
    w = [(ts, p, s) for ts, p, s in trades if t0 <= ts < t1]
    if not w:
        return None, 0
    num = sum(p * s for _, p, s in w)
    den = sum(s for _, p, s in w)
    return num / den, len(w)


def temp_at(obs, hour):
    """last obs with dt.hour <= hour (and same local date). -> c or None"""
    best = None
    for dt, t in obs:
        if dt.hour <= hour:
            best = t
        else:
            break
    return best


def main():
    metar = load_metar()
    fcst = load_fcst()
    prices = load_prices()

    # settle only days with metar + forecast + trades
    days = sorted(set(metar) & set(fcst) & set(prices))
    print("settled highest days with all three sources: %d" % len(days))

    # ---- establish settlement sanity: winner = round(max) ----
    # (bucket label vs round(max) match rate)
    match = tot = 0
    for d in days:
        mx = metar[d]["max"]
        w = round(mx)
        ladder = prices[d]
        # winner = bucket whose label kind+value contains w
        ok = False
        for n, info in ladder.items():
            lbl = info["label"]
            if lbl.startswith("<="):
                ok = ok or (w <= n)
            elif lbl.startswith(">="):
                ok = ok or (w >= n)
            else:
                ok = ok or (w == n)
        if ok:
            match += 1
        tot += 1
    print("settlement (round(max) falls in some bucket) sanity: %d/%d" % (match, tot))

    # ---- forecast error vs regime ----
    print("\n=== forecast-max error by regime (full sample) ===")
    rows = []
    for d in days:
        fmax, fmin = fcst[d]
        mx = metar[d]["max"]
        rng = fmax - fmin
        err = mx - fmax
        rows.append((d, rng, fmax, mx, err, round(mx) - round(fmax)))
    rad = [r for r in rows if r[1] >= REGIME_MIN_RANGE]
    adv = [r for r in rows if r[1] < REGIME_MIN_RANGE]
    for name, grp in (("radiation(>=8)", rad), ("advection(<8)", adv)):
        if not grp:
            print("  %s: n=0" % name)
            continue
        errs = [r[4] for r in grp]
        sh = Counter(r[5] for r in grp)
        print("  %-16s n=%3d  meanErr=%+.2f  medErr=%+.2f  std=%.2f  "
              "bucketShift=%s" % (name, len(grp), statistics.mean(errs),
                                  statistics.median(errs), statistics.pstdev(errs),
                                  dict(sorted(sh.items()))))
    print("  bucket shift total:", Counter(r[5] for r in rows))


if __name__ == "__main__":
    main()
