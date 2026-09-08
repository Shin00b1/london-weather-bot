"""Rate-limited, resumable Falcon trade backfill for London temperature markets.

Downloads the autumn/winter 2025-26 HIGHEST-temperature bucket markets (the
season missing from the local tape) into london/tape.duckdb, one market at a
time, checkpointing after every market so an interrupted run resumes instead
of re-downloading. Free-tier friendly: sleeps between every API call and
processes a bounded number of markets per invocation.

Market roster:   london/winter_markets_2025_26.json  (from london_temp_markets.json)
Checkpoint:      london/falcon_backfill_state.json   {condition_id: {...}}
Raw audit log:   london/runs/falcon_trades.jsonl     (one Falcon row per line)
Tape target:     london/tape.duckdb  (src="falcon")

Usage:
  python3 falcon_backfill.py                 # 25 highest-volume unfinished markets
  python3 falcon_backfill.py --max-markets 100
  python3 falcon_backfill.py --sleep 3.0
  python3 falcon_backfill.py --dry-run       # show plan only, no network
  python3 falcon_backfill.py --list-state    # summarize checkpoint
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import falcon_client as fc
import tape

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
ROSTER = os.path.join(LONDON, "winter_markets_2025_26.json")
STATE = os.path.join(LONDON, "falcon_backfill_state.json")
RAWLOG = os.path.join(LONDON, "runs", "falcon_trades.jsonl")

SLEEP_BETWEEN_CALLS = 2.0   # seconds; conservative for the free tier
DEFAULT_BATCH = 25
FETCHED_AT = datetime.now(timezone.utc).replace(microsecond=0)


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


def iso_to_unix(s):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def falcon_to_tape(row, event_slug):
    """Map a Falcon Trades row (snake_case) to the tape's data-api camelCase."""
    return {
        "timestamp": iso_to_unix(row.get("timestamp")),
        "conditionId": row.get("condition_id"),
        "asset": row.get("token_id"),
        "side": row.get("side"),
        "price": row.get("price"),
        "size": row.get("size"),
        "slug": row.get("slug"),
        "eventSlug": event_slug,
        "outcome": row.get("outcome"),
        "transactionHash": row.get("transaction_hash"),
        "proxyWallet": row.get("proxy_wallet"),
    }


def append_raw(rows):
    os.makedirs(os.path.dirname(RAWLOG), exist_ok=True)
    with open(RAWLOG, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def market_window(m):
    """Full-lifetime unix window for one market, with a day of margin."""
    sd = m.get("start_date") or m.get("end_date")
    ed = m.get("end_date")
    try:
        sdt = datetime.fromisoformat(sd.replace("Z", "+00:00"))
    except Exception:
        sdt = datetime(2025, 9, 1, tzinfo=timezone.utc)
    try:
        edt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
    except Exception:
        edt = datetime(2026, 3, 1, tzinfo=timezone.utc)
    return int((sdt - timedelta(days=1)).timestamp()), int((edt + timedelta(days=2)).timestamp())


def fetch_one_market(m, sleep):
    """Download all trades for one condition_id. Returns (tape_rows, n_pages)."""
    cond = m["condition_id"]
    st, en = market_window(m)
    rows = []
    offset = 0
    pages = 0
    event_slug = m.get("event_slug")
    while True:
        data = fc._post(556, {"condition_id": cond,
                              "start_time": str(st), "end_time": str(en)},
                        offset=offset)
        results = data.get("data", {}).get("results", []) or []
        pm = data.get("pagination", {}) or {}
        has_more = bool(pm.get("has_more", False))
        rows.extend(falcon_to_tape(r, event_slug) for r in results)
        append_raw(results)
        pages += 1
        if not has_more or len(results) < 200:
            break
        offset += len(results)
        if sleep:
            time.sleep(sleep)
    return rows, pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN_CALLS)
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

    # order by volume desc; skip already-done
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
    print("processing %d markets (sleep %.1fs/call, total=%d)"
          % (len(batch), args.sleep, len(roster)), flush=True)

    con = tape.connect_retry()
    n_new = 0
    for i, m in enumerate(batch, 1):
        cond = m["condition_id"]
        try:
            rows, pages = fetch_one_market(m, args.sleep)
            if rows:
                tape.append_trades(con, rows, FETCHED_AT, src="falcon")
            state[cond] = {"done": True, "trades": len(rows),
                           "pages": pages, "ts": datetime.now().isoformat()}
            n_new += len(rows)
            print("  [%3d/%d] %s vol=%8.0f -> %d trades (%d pages)"
                  % (i, len(batch), m["end_date"][:10],
                     m.get("volume_total") or 0, len(rows), pages), flush=True)
        except Exception as e:
            state[cond] = {"done": False, "error": repr(e),
                           "ts": datetime.now().isoformat()}
            print("  [%3d/%d] %s ERROR %r" % (i, len(batch), m["end_date"][:10], e),
                  flush=True)
        save_state(state)
        if args.sleep:
            time.sleep(args.sleep)
    con.close()
    done = sum(1 for v in state.values() if v.get("done"))
    print("batch done: +%d trades | total done %d/%d"
          % (n_new, done, len(roster)))


if __name__ == "__main__":
    main()
