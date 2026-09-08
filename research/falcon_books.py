"""Download historical Polymarket order-book snapshots from Falcon (agent 572)
into london/tape.duckdb `books`.

Coverage (verified 2026-08-27): Falcon's book archive begins ~2026-01-27 and
reaches the present, so it holds the FULL order-book history of the lowest-
temperature markets (Apr 15 - Aug 25) plus late-winter highest markets
(Jan 28 - Feb 28). Anything before ~Jan 27 2026 is gone everywhere.

Token rosters:
  lowest-late : Yes tokens of lowest buckets Jul 19 - Aug 25 (from tape trades)
  lowest-early: Yes tokens Apr 15 - Jul 18 (Markets sheet x parquet mapping)
  winter      : Yes tokens of highest buckets Jan 28 - Feb 28 (winter roster)

Each snapshot row from 572 = {asks: "[{size,price},...]", bids: "[...]",
timestamp ISO, token_id}. Flattened into `books` (token_id, book_ts ms,
side, level, price, size, slug, condition_id, fetched_at), INSERT OR IGNORE
so re-runs are idempotent. Checkpointed per token in
london/falcon_books_state.json.

IMPORTANT: agent 572 takes MILLISECOND timestamps; seconds return 0 rows.

Usage:
  python3 falcon_books.py --group lowest-late --max-tokens 10   # probe batch
  python3 falcon_books.py --group all                           # full run
  python3 falcon_books.py --list-state
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import falcon_client as fc
import tape

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
STATE = os.path.join(LONDON, "falcon_books_state.json")

MAPPING = os.path.join(LONDON, "condition_yes_token.json")  # condition_id -> yes token (parquet-derived)
EARLY = os.path.join(LONDON, "lowest_slugs_apr_jul.json")
LATE = os.path.join(LONDON, "lowest_tokens_roster.json")
WINTER = os.path.join(LONDON, "winter_markets_2025_26.json")

SLEEP = 2.0
FETCHED_AT = datetime.now(timezone.utc).replace(microsecond=0)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}


def save_state(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, STATE)


def iso_to_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def roster_early():
    mapping = json.load(open(MAPPING))
    out = []
    for p in json.load(open(EARLY)):
        tok = mapping.get(p["condition_id"])
        if tok:
            out.append({"token_id": tok, "slug": p["slug"],
                        "condition_id": p["condition_id"],
                        "event_date": p["event_date"]})
    return out


def roster_late():
    out = []
    for p in json.load(open(LATE)):
        m = re.search(r"on-([a-z]+)-(\d+)-2026", p["slug"])
        if not m:
            continue
        d = "%d-%02d-%02d" % (2026, datetime.strptime(m.group(1), "%B").month, int(m.group(2)))
        if d > "2026-08-25":   # settled days only; live days covered by book_recorder
            continue
        out.append({"token_id": p["yes_token"], "slug": p["slug"],
                    "condition_id": None, "event_date": d})
    return out


def roster_winter():
    out = []
    for m in json.load(open(WINTER)):
        if m["end_date"][:10] < "2026-01-28":
            continue
        out.append({"token_id": m["side_a_token_id"], "slug": m["slug"],
                    "condition_id": m["condition_id"],
                    "event_date": m["end_date"][:10]})
    return out


def fetch_snapshots(token_id, d_iso, sleep):
    """All 572 snapshots for one token around its event day. -> list of dicts."""
    d = datetime.fromisoformat(d_iso)
    st = str(int((d - timedelta(days=3)).timestamp()) * 1000)
    en = str(int((d + timedelta(days=2)).timestamp()) * 1000)
    snaps = []
    offset = 0
    while True:
        data = fc._post(572, {"token_id": token_id, "start_time": st, "end_time": en},
                        offset=offset)
        results = data.get("data", {}).get("results", []) or []
        has_more = bool((data.get("pagination") or {}).get("has_more", False))
        snaps.extend(results)
        if not has_more or len(results) < 200:
            break
        offset += len(results)
        if sleep:
            time.sleep(sleep)
    return snaps


def flatten(snaps, token_id, slug, condition_id):
    rows = []
    for s in snaps:
        try:
            ts = iso_to_ms(s["timestamp"])
        except Exception:
            continue
        for side_key, side in (("bids", "BID"), ("asks", "ASK")):
            raw = s.get(side_key)
            if not raw:
                continue
            try:
                levels = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            for i, lv in enumerate(levels):
                try:
                    rows.append((token_id, ts, side, i,
                                 float(lv.get("price")), float(lv.get("size")),
                                 slug, condition_id, FETCHED_AT))
                except (TypeError, ValueError):
                    continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["lowest-late", "lowest-early", "winter", "all"],
                    default="all")
    ap.add_argument("--max-tokens", type=int, default=0, help="0 = no limit")
    ap.add_argument("--sleep", type=float, default=SLEEP)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-state", action="store_true")
    args = ap.parse_args()

    state = load_state()
    if args.list_state:
        done = sum(1 for v in state.values() if v.get("done"))
        print("state: %d tokens recorded, %d done, %d snapshots" %
              (len(state), done, sum(v.get("snaps", 0) for v in state.values())))
        return

    groups = {
        "lowest-late": roster_late,      # OOS window — priority
        "lowest-early": roster_early,    # in-sample window
        "winter": roster_winter,         # winter highest Jan28-Feb28
    }
    targets = []
    for g, fn in groups.items():
        if args.group in (g, "all"):
            targets.extend(fn())
    # dedupe by token_id, skip done
    seen = set()
    todo = []
    for t in targets:
        if t["token_id"] in seen:
            continue
        seen.add(t["token_id"])
        if state.get(t["token_id"], {}).get("done"):
            continue
        todo.append(t)
    if args.max_tokens:
        todo = todo[:args.max_tokens]

    print("tokens to fetch: %d (group=%s, sleep %.1fs)" % (len(todo), args.group, args.sleep),
          flush=True)
    if args.dry_run:
        for t in todo[:10]:
            print("  %s  %s" % (t["event_date"], t["slug"][:60]))
        return

    con = tape.connect_retry()
    n_rows = 0
    for i, t in enumerate(todo, 1):
        try:
            snaps = fetch_snapshots(t["token_id"], t["event_date"], args.sleep)
            rows = flatten(snaps, t["token_id"], t["slug"], t["condition_id"])
            if rows:
                con.executemany("INSERT OR IGNORE INTO books VALUES (?,?,?,?,?,?,?,?,?)", rows)
            state[t["token_id"]] = {"done": True, "snaps": len(snaps), "rows": len(rows),
                                    "ts": datetime.now().isoformat()}
            n_rows += len(rows)
            if i % 10 == 0 or i == len(todo):
                print("  [%4d/%4d] %s  %+6d rows (last: %s)"
                      % (i, len(todo), t["event_date"], len(rows), t["slug"][:44]), flush=True)
        except Exception as e:
            state[t["token_id"]] = {"done": False, "error": repr(e),
                                    "ts": datetime.now().isoformat()}
            print("  [%4d/%4d] ERROR %s %r" % (i, len(todo), t["slug"][:40], e), flush=True)
        save_state(state)
        if args.sleep:
            time.sleep(args.sleep)
    con.close()
    done = sum(1 for v in state.values() if v.get("done"))
    print("batch done: +%d book rows | total tokens done %d" % (n_rows, done))


if __name__ == "__main__":
    main()
