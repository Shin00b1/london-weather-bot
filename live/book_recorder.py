"""Continuous Polymarket London-temperature order-book recorder.

Polls the CLOB /book endpoint for every OPEN London temperature market and
appends the full resting bid/ask ladder to london/tape.duckdb (table
`books`) via tape.py. This closes the biggest forward-only data gap: full
depth-ladder history, which Polymarket serves only while a market is live.

Discovery re-runs each pass so newly listed events are picked up without a
restart. Idempotent — INSERT OR IGNORE dedupes on (token_id, book_ts, side,
level), so overlapping passes (daemon + watchdog cron) are safe.

Modes:
    python3 book_recorder.py --once              # one pass, then exit
    python3 book_recorder.py --interval 600      # loop forever every 600s
    python3 book_recorder.py                     # loop forever every 300s
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import tape

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "london-weather-updater/1.0"}

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]


def http_json(url, retries=2):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def candidate_slugs(day):
    out = []
    d = day - timedelta(days=1)
    while d <= day + timedelta(days=2):
        for direction in ("lowest", "highest"):
            out.append((d, direction,
                        "%s-temperature-in-london-on-%s-%d-%d" %
                        (direction, MONTHS[d.month - 1], d.day, d.year)))
        d += timedelta(days=1)
    return out


def discover_open_markets():
    """-> list of dicts {token_id, condition_id, slug, direction, event_date}."""
    today = datetime.now(timezone.utc).date()
    markets = []
    for d, direction, slug in candidate_slugs(today):
        try:
            ev = http_json(GAMMA + "/events?slug=" + slug)
        except Exception:
            continue
        if not ev:
            continue
        ev = ev[0]
        for m in ev.get("markets", []):
            if m.get("closed"):
                continue
            toks = json.loads(m.get("clobTokenIds") or "[]")
            if not toks:
                continue
            markets.append({
                "token_id": toks[0],
                "condition_id": (m.get("conditionId") or "").lower(),
                "slug": m.get("slug"),
                "direction": direction,
                "event_date": d.isoformat(),
            })
        time.sleep(0.2)
    return markets


def fetch_book(token_id):
    try:
        b = http_json(CLOB + "/book?token_id=" + token_id)
    except urllib.error.HTTPError:
        return None
    if not b or b.get("timestamp") in (None, ""):
        return None
    ts = b["timestamp"]
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    return {
        "token_id": token_id,
        "book_ts": int(ts),  # CLOB reports milliseconds
        "bids": b.get("bids") or [],
        "asks": b.get("asks") or [],
    }


def one_pass():
    now = datetime.now(timezone.utc)
    markets = discover_open_markets()
    con = tape.connect_retry()
    n_levels = n_books = 0
    for mk in markets:
        snap = fetch_book(mk["token_id"])
        if not snap:
            continue
        snap.update({"slug": mk["slug"], "condition_id": mk["condition_id"]})
        n_levels += tape.append_books(con, [snap], now)
        n_books += 1
        time.sleep(0.15)
    con.close()
    print("OK markets=%d books=%d levels=%d at %s" %
          (len(markets), n_books, n_levels, now.isoformat()))
    return n_books


def main():
    once = "--once" in sys.argv
    interval = 300
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])

    while True:
        try:
            one_pass()
        except Exception as e:
            print("pass failed: %r" % e)
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
