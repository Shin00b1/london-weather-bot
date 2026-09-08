#!/usr/bin/env python3
"""Bot 1 — wind frontal-veto test.

The low-band bot's ONLY losing mode is a post-dawn cold front that drops the
temperature after 06Z and breaks the locked bucket. A cold front has a wind
signature that is knowable AT 06Z: rising speed (tightening pressure gradient
ahead of the front) and a direction shift (backing/veering). This tests
whether 06Z wind flags the days where the dawn-lock breaks, using the same
"lock06" outcome as bot1_regime.py.

Output: printed table of break-vs-lock days across wind features, plus a
sweep of a simple veto (skip if speed >= X and rising >= Y).
"""
import csv
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(HERE, "london")
REGIME = os.path.join(LONDON, "bot1_regime_daily.csv")
WIND = os.path.join(LONDON, "metar_eglc_wind.csv")

LON = ZoneInfo("Europe/London")


def load_wind():
    days = defaultdict(list)
    with open(WIND) as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                sknt = float(row["sknt"]) if row.get("sknt") else None
                drct = float(row["drct"]) if row.get("drct") else None
                days[dt.astimezone(LON).date()].append((dt, sknt, drct))
            except (ValueError, KeyError):
                continue
    for d in days:
        days[d].sort()
    return days


def nearest(obs, hour_utc):
    """Return (sknt, drct) closest to the given UTC hour on that day."""
    target = obs[0][0].replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    best = None
    for dt, sk, dr in obs:
        if best is None or abs((dt - target).total_seconds()) < abs((best[0] - target).total_seconds()):
            best = (dt, sk, dr)
    return best[1], best[2]


def angdiff(a, b):
    """Signed angular difference b-a in [-180,180], +ve = veering."""
    if a is None or b is None:
        return None
    d = (b - a + 180) % 360 - 180
    return d


def main():
    wind = load_wind()
    rows = []
    with open(REGIME) as f:
        for r in csv.DictReader(f):
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            w = wind.get(d)
            if not w:
                continue
            # 06Z snapshot
            s6, dr6 = nearest(w, 6)
            # pre-dawn trend: 03Z -> 06Z
            s3, dr3 = nearest(w, 3)
            # full morning trend: 00Z -> 06Z
            s0, dr0 = nearest(w, 0)
            ds_03 = (s6 - s3) if (s6 is not None and s3 is not None) else None
            ds_00 = (s6 - s0) if (s6 is not None and s0 is not None) else None
            dd_03 = angdiff(dr3, dr6)
            rows.append({
                "date": r["date"],
                "lock06": int(r["lock06"]),
                "break_c": float(r["break_c"]),
                "sknt_06z": s6,
                "ds_03": ds_03,
                "ds_00": ds_00,
                "dd_03": dd_03,
            })

    have_speed = [r for r in rows if r["sknt_06z"] is not None]
    brk = [r for r in have_speed if r["lock06"] == 0]
    lok = [r for r in have_speed if r["lock06"] == 1]

    def avg(xs):
        return sum(xs) / len(xs) if xs else float("nan")

    print("=== 06Z wind features: break days (lock fails) vs lock days ===")
    print(f"{'feature':<18} {'break(n=%d)' % len(brk):>14} {'lock(n=%d)' % len(lok):>12}")
    for label, key in [("sknt_06z (kt)", "sknt_06z"),
                       ("ds 03->06 (kt)", "ds_03"),
                       ("ds 00->06 (kt)", "ds_00"),
                       ("|veering| 03->06", None)]:
        if key == "sknt_06z":
            a = avg([r[key] for r in brk]); b = avg([r[key] for r in lok])
        elif key == "ds_03":
            a = avg([r[key] for r in brk if r[key] is not None]); b = avg([r[key] for r in lok if r[key] is not None])
        elif key == "ds_00":
            a = avg([r[key] for r in brk if r[key] is not None]); b = avg([r[key] for r in lok if r[key] is not None])
        else:
            a = avg([abs(r["dd_03"]) for r in brk if r["dd_03"] is not None])
            b = avg([abs(r["dd_03"]) for r in lok if r["dd_03"] is not None])
        print(f"{label:<18} {a:>14.2f} {b:>12.2f}")

    print()
    print("=== veto sweep: skip if sknt_06z >= X (rising OR not) ===")
    print("  (break% skipped = share of break days caught; lock% skipped = cost)")
    for x in [6, 8, 10, 12, 14, 16]:
        skip_b = sum(1 for r in brk if r["sknt_06z"] >= x)
        skip_l = sum(1 for r in lok if r["sknt_06z"] >= x)
        print(f"  sknt>= {x:>2}: skip {skip_b:>3}/{len(brk):>3} break days ({skip_b/len(brk)*100:4.1f}%)  "
              f"| cost {skip_l:>3}/{len(lok):>3} lock days ({skip_l/len(lok)*100:4.1f}%)")

    print()
    print("=== veto sweep: skip if rising 03->06 (ds_03 >= X) ===")
    for x in [1, 2, 3, 4]:
        bb = [r for r in brk if r["ds_03"] is not None]
        ll = [r for r in lok if r["ds_03"] is not None]
        sb = sum(1 for r in bb if r["ds_03"] >= x)
        sl = sum(1 for r in ll if r["ds_03"] >= x)
        print(f"  ds_03>= {x:>1}: skip {sb:>3}/{len(bb):>3} break days ({sb/len(bb)*100:4.1f}%)  "
              f"| cost {sl:>3}/{len(ll):>3} lock days ({sl/len(ll)*100:4.1f}%)")


if __name__ == "__main__":
    main()
