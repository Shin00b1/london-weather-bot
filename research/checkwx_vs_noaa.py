#!/usr/bin/env python3
"""Benchmark: does CheckWX serve a new EGLC METAR before NOAA aviationweather.gov?

Polls both sources on a fixed interval. When the raw report string changes on a
source, records the first wall-clock time that source returned the new report.
Once both sources have served the same report, computes the edge:
    edge_seconds = NOAA_first_seen - CheckWX_first_seen
    (positive => CheckWX was faster by that many seconds)
"""
import urllib.request
import json
import time
import datetime
import os

NOAA_URL = "https://aviationweather.gov/api/data/metar?ids=EGLC&format=json&taf=true"
CWX_URL = "https://api.checkwx.com/v2/metar/EGLC"
INTERVAL = 10  # seconds between polls

def load_api_key():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if line.startswith("CHECKWX_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("CHECKWX_API_KEY not found in .env")

API_KEY = load_api_key()

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def http_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def get_noaa():
    d = http_json(NOAA_URL, {"User-Agent": "benchmark/1.0"})
    return d[0]["rawOb"], d[0]["receiptTime"]

def get_cwx():
    d = http_json(CWX_URL, {"X-API-Key": API_KEY, "User-Agent": "benchmark/1.0"})
    return d["data"][0], ""

def norm(s):
    return " ".join(s.split())

# first_seen[raw][source] = iso timestamp
first_seen = {}
last = {"noaa": None, "cwx": None}

def log(msg):
    print(f"{now_iso()}  {msg}", flush=True)

log("START interval=%ds" % INTERVAL)

while True:
    for name, fn in (("noaa", get_noaa), ("cwx", get_cwx)):
        try:
            raw, extra = fn()
            raw = norm(raw)
            if raw != last[name]:
                last[name] = raw
                first_seen.setdefault(raw, {})[name] = now_iso()
                seen_by = first_seen[raw]
                log(f"CHANGE {name:5s} -> {raw}")
                if name == "noaa":
                    log(f"           noaa receiptTime={extra}")
                if "noaa" in seen_by and "cwx" in seen_by:
                    t_noaa = datetime.datetime.fromisoformat(seen_by["noaa"])
                    t_cwx = datetime.datetime.fromisoformat(seen_by["cwx"])
                    edge = (t_noaa - t_cwx).total_seconds()
                    log(f"EDGE for {raw}: CheckWX{' first by %.1fs' % edge if edge > 0 else (' lagged by %.1fs' % -edge if edge < 0 else ' tie')}")
        except Exception as e:
            log(f"ERR  {name:5s} {e}")
    time.sleep(INTERVAL)
