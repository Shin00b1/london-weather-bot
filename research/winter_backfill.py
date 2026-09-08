"""Winter 2025-26 trade backfill for London temperature markets.

Downloads the autumn/winter 2025-26 HIGHEST-temperature bucket markets (the
season missing from the local tape) from Polymarket's OWN data-api into
london/tape.duckdb, one market at a time, checkpointing after every market so
an interrupted run resumes instead of re-downloading.

Why data-api and not Falcon: Falcon's Trades agent (556) is incomplete for
these older markets (returned 2 rows for a $257k-volume market), while the
data-api /trades endpoint serves FULL history for free with no tier limit and
returns rows already in the tape's camelCase schema.

Market roster:   london/winter_markets_2025_26.json  (from london_temp_markets.json)
Checkpoint:      london/winter_backfill_state.json   {condition_id: {...}}
Raw audit log:   london/runs/winter_trades.jsonl     (one data-api row per line)
Tape target:     london/tape.duckdb  (src="data-api-winter")

Usage:
  python3 winter_backfill.py                # 50 highest-volume unfinished markets
  python3 winter_backfill.py --max-markets 200
  python3 winter_backfill.py --dry-run      # show plan only, no network
  python3 winter_backfill.py --list-state   # summarize checkpoint
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import tape

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
ROSTER = os.path.join(LONDON, "winter_markets_2025_26.json")
STATE = os.path.join(LONDON, "winter_backfill_state.json")
RAWLOG = os.path.join(LONDON, "runs", "winter_trades.jsonl")

DATA_API = "https://data-api.polymarket.com"
UA = {"User-Agent": "london-weather-updater/1.0"}

PAGE_SIZE = 500
SLEEP_PAGE = 0.25      # between pages of the same market
SLEEP_MARKET = 0.5     # between markets
DEFAULT_BATCH = 50
FETCHED_AT = datetime.now(timezone.utc).replace(microsecond=0)


def http_json(url, retries=3):
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


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, STATE)


def load_roster():
    return json.load(open(ROSTER))


def append_raw(rows):
    os.makedirs(os.path.dirname(RAWLOG), exist_ok=True)
    with open(RAWLOG, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def fetch_market_full(cond, sleep_page):
    """Page data-api /trades to full history, newest-first. Returns list."""
    rows, offset = [], 0
    while True:
        page = http_json(DATA_API + "/trades?market=%s&limit=%d&offset=%d"
                         % (cond.lower(), PAGE_SIZE, offset))
        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        if sleep_page:
            time.sleep(sleep_page)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--sleep-page", type=float, default=SLEEP_PAGE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-state", action="store_true")
    args = ap.parse_args()

    roster = load_roster()
    state = load_state()

    if args.list_state:
        done = sum(1 for v in state.values() if v.get("done"))
        tot = sum(v.get("trades", 0) for v in state.values())
        print("state: %d markets recorded, %d done, %d trades logged"
              % (len(state), done, tot))
        return

    todo = [m for m in roster if not state.get(m["condition_id"], {}).get("done")]
    todo.sort(key=lambda m: m.get("volume_total") or 0, reverse=True)

    if args.dry_run:
        print("dry-run: %d of %d markets unfinished; would process top %d"
              % (len(todo), len(roster), min(args.max_markets, len(todo))))
        for m in todo[:10]:
            print("  %-8s vol=%9.0f  %s" % (m["end_date"][:10],
                                            m.get("volume_total") or 0, m["slug"]))
        return

    batch = todo[:args.max_markets]
    print("processing %d markets (data-api, sleep %.2fs/page)"
          % (len(batch), args.sleep_page), flush=True)

    con = tape.connect_retry()
    n_new = 0
    for i, m in enumerate(batch, 1):
        cond = m["condition_id"]
        try:
            rows = fetch_market_full(cond, args.sleep_page)
            if rows:
                tape.append_trades(con, rows, FETCHED_AT, src="data-api-winter")
                append_raw(rows)
            state[cond] = {"done": True, "trades": len(rows),
                           "ts": datetime.now().isoformat()}
            n_new += len(rows)
            print("  [%3d/%d] %s vol=%8.0f -> %d trades"
                  % (i, len(batch), m["end_date"][:10],
                     m.get("volume_total") or 0, len(rows)), flush=True)
        except Exception as e:
            state[cond] = {"done": False, "error": repr(e),
                           "ts": datetime.now().isoformat()}
            print("  [%3d/%d] %s ERROR %r" % (i, len(batch), m["end_date"][:10], e),
                  flush=True)
        save_state(state)
        if args.sleep_page:
            time.sleep(SLEEP_MARKET)
    con.close()
    done = sum(1 for v in state.values() if v.get("done"))
    print("batch done: +%d trades | total done %d/%d"
          % (n_new, done, len(roster)))


if __name__ == "__main__":
    main()
