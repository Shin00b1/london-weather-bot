"""Shared tape storage for the London temperature project.

A single DuckDB file (london/tape.duckdb) holds two forward-only,
deduplicated tables:

  books   - full order-book ladder snapshots polled from Polymarket CLOB
  trades  - taker-side trade records from the Polymarket data-api

Both tables use INSERT OR IGNORE keyed on natural row identities so any
number of recorder passes (daemon + watchdog cron, overlapping daily
fetches) can append safely without double-counting.

DuckDB is used instead of pandas/pyarrow because neither is installed in
this environment; duckdb reads/writes parquet natively if that is ever
needed for exports.
"""

import hashlib
import os
import time

import duckdb

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "london", "tape.duckdb")

BOOKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
    token_id       VARCHAR NOT NULL,
    book_ts        BIGINT  NOT NULL,
    side           VARCHAR NOT NULL,
    level          INTEGER NOT NULL,
    price          DOUBLE,
    size           DOUBLE,
    slug           VARCHAR,
    condition_id   VARCHAR,
    fetched_at     TIMESTAMP,
    PRIMARY KEY (token_id, book_ts, side, level)
)
"""

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    row_id       VARCHAR PRIMARY KEY,
    timestamp    BIGINT,
    condition_id VARCHAR,
    asset        VARCHAR,
    side         VARCHAR,
    price        DOUBLE,
    size         DOUBLE,
    slug         VARCHAR,
    event_slug   VARCHAR,
    outcome      VARCHAR,
    txn_hash     VARCHAR,
    wallet       VARCHAR,
    src          VARCHAR,
    fetched_at   TIMESTAMP
)
"""

ENSEMBLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ensemble(
    fetch_date  DATE NOT NULL,
    target_date DATE NOT NULL,
    member      VARCHAR NOT NULL,
    min_c       DOUBLE,
    max_c       DOUBLE,
    fetched_at  TIMESTAMP,
    PRIMARY KEY (fetch_date, target_date, member)
)
"""


def connect(read_only=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH, read_only=read_only)
    if not read_only:
        con.execute(BOOKS_SCHEMA)
        con.execute(TRADES_SCHEMA)
        con.execute(ENSEMBLE_SCHEMA)
    return con


def connect_retry(read_only=False, attempts=6, wait=5.0):
    """Open the tape with a retry loop so concurrent writers (book recorder
    cron vs nightly pipeline) don't hard-fail on DuckDB's single-writer lock."""
    last = None
    for i in range(attempts):
        try:
            return connect(read_only=read_only)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(wait)
    raise last


def _book_rows(snapshot, fetched_at):
    """Flatten one book snapshot into per-level rows for the books table.

    snapshot: {"token_id", "book_ts"(ms), "slug", "condition_id",
               "bids": [{"price","size"},...], "asks": [...]}
    """
    token = snapshot["token_id"]
    ts = int(snapshot["book_ts"])
    slug = snapshot.get("slug")
    cond = snapshot.get("condition_id")
    out = []
    for side in ("bids", "asks"):
        s = "BID" if side == "bids" else "ASK"
        levels = snapshot.get(side) or []
        for i, lv in enumerate(levels):
            out.append((token, ts, s, i,
                        float(lv["price"]) if lv.get("price") not in (None, "") else None,
                        float(lv["size"]) if lv.get("size") not in (None, "") else None,
                        slug, cond, fetched_at))
    return out


def append_books(con, snapshots, fetched_at):
    rows = []
    for snap in snapshots:
        rows.extend(_book_rows(snap, fetched_at))
    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO books VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _trade_row_id(t):
    raw = "|".join([
        str(t.get("timestamp")), str(t.get("conditionId")), str(t.get("asset")),
        str(t.get("side")), str(t.get("price")), str(t.get("size")),
        str(t.get("proxyWallet")), str(t.get("transactionHash")),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def trade_tuple(t, fetched_at, src="data-api"):
    return (
        _trade_row_id(t),
        t.get("timestamp"),
        t.get("conditionId"),
        t.get("asset"),
        t.get("side"),
        float(t["price"]) if t.get("price") not in (None, "") else None,
        float(t["size"]) if t.get("size") not in (None, "") else None,
        t.get("slug"),
        t.get("eventSlug"),
        t.get("outcome"),
        t.get("transactionHash"),
        t.get("proxyWallet"),
        src,
        fetched_at,
    )


def append_trades(con, trades, fetched_at, src="data-api"):
    rows = [trade_tuple(t, fetched_at, src) for t in trades]
    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def append_ensemble(con, fetch_date, target_date, members, fetched_at):
    """members: list of (member_name, min_c, max_c)."""
    rows = [(fetch_date, target_date, m, mn, mx, fetched_at)
            for m, mn, mx in members]
    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO ensemble VALUES (?,?,?,?,?,?)", rows)
    return len(rows)
