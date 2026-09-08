"""Daily ECMWF IFS 025 ensemble archive for London temperature markets.

Pulls the full 50-member ensemble of daily min/max temperature for EGLC
(51.5053N 0.0553E) from Open-Meteo's Ensemble API and stores every member
per (fetch_date, target_date) in london/tape.duckdb (table `ensemble`).

Honesty note: this is a NOWCAST archive, not a D-1 forecast. The Ensemble
API does not expose a `run=` parameter, so each fetch reflects the latest
available model run at fetch time. That means:
  - fetched the same morning -> effectively the day's nowcast, NOT a D-1 value
  - the honest D-1 forecast for settlement stays forecast_archive_d1.csv
    ([[openmeteo-honest-forecast-source]] via the Single Runs API).

What the ensemble adds is SPREAD: member distribution -> P(each bucket)
and a "trade only when spread is tight and disagrees with market" filter,
which is valid only if the archive rows are tagged with fetch_date and
consumed at matching lead times (never cross a target_date row with a
fetch_date that is too late for that target).

Usage: python3 ensemble_archive.py   # one fetch, idempotent, forward-only
"""

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

import tape

LAT, LON = 51.5053, 0.0553
ENS = "https://ensemble-api.open-meteo.com/v1/ensemble"
UA = {"User-Agent": "london-weather-updater/1.0"}


def fetch_ensemble(forecast_days=16):
    url = (ENS + "?latitude=%s&longitude=%s"
           "&daily=temperature_2m_min,temperature_2m_max"
           "&models=ecmwf_ifs025&forecast_days=%d&timezone=UTC"
           % (LAT, LON, forecast_days))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    fetched_at = datetime.now(timezone.utc)
    fetch_date = fetched_at.date()

    d = fetch_ensemble()
    if d.get("error"):
        print("ERROR: %s" % d["error"])
        sys.exit(1)

    daily = d["daily"]
    times = daily["time"]
    min_members = sorted(k for k in daily if k.startswith("temperature_2m_min_member"))
    max_members = sorted(k for k in daily if k.startswith("temperature_2m_max_member"))

    con = tape.connect_retry()
    n_rows = 0
    for i, t in enumerate(times):
        target = date.fromisoformat(t)
        members = []
        for k in min_members:
            member = k[len("temperature_2m_min_member"):]  # "01".."50"
            mn = daily[k][i]
            mx = daily["temperature_2m_max_member" + member][i]
            if mn is None and mx is None:
                continue
            members.append(("m" + member.lstrip("0") or "0", mn, mx))
        n_rows += tape.append_ensemble(con, fetch_date, target, members, fetched_at)

    con.close()
    print("OK fetch_date=%s targets=%d members/target=%d rows=%d" %
          (fetch_date, len(times), len(min_members), n_rows))


if __name__ == "__main__":
    main()
