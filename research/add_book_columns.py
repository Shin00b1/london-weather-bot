"""Add orderbook columns to the Markets sheet and backfill everything we hold.

New columns (appended at the right edge):
    best_bid, best_ask, spread, bid_depth_usd, ask_depth_usd, book_ts

Backfill sources (newest snapshot wins per market):
    1. Fresh live snapshots of all open events (fetched now via CLOB /book)
    2. london/runs/books_20260823_090322.jsonl  (this morning's 66 live books)
    3. Falcon Aug-20 settlement ladders        (aug20_orderbooks_parsed.json)
    4. Falcon Aug-21 live favorites            (aug21_orderbook.json)

Backs up the workbook, writes atomically.
"""

import json
import os
import re
import shutil
import time
import urllib.request
from datetime import datetime, timezone

import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
BACKUPS = os.path.join(LONDON, "backups")
RUNS = os.path.join(LONDON, "runs")

BOOK_COLS = ["best_bid", "best_ask", "spread", "bid_depth_usd", "ask_depth_usd", "book_ts"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "london-weather-updater/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_book(token_id):
    try:
        b = get("https://clob.polymarket.com/book?token_id=" + token_id)
    except Exception:
        return None
    bids, asks = b.get("bids") or [], b.get("asks") or []
    bb = max((float(x["price"]) for x in bids), default=None)
    ba = min((float(x["price"]) for x in asks), default=None)
    return {
        "best_bid": bb, "best_ask": ba,
        "spread": round(ba - bb, 4) if (bb is not None and ba is not None) else None,
        "bid_depth_usd": round(sum(float(x["price"]) * float(x["size"]) for x in bids), 2),
        "ask_depth_usd": round(sum(float(x["price"]) * float(x["size"]) for x in asks), 2),
        "book_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def normalize(b, ts=None):
    if not b:
        return None
    bb, ba = b.get("best_bid"), b.get("best_ask")
    out = {
        "best_bid": bb, "best_ask": ba,
        "spread": b.get("spread") or (round(ba - bb, 4) if (bb is not None and ba is not None) else None),
        "bid_depth_usd": b.get("bid_depth_usd", b.get("bid_depth")),
        "ask_depth_usd": b.get("ask_depth_usd", b.get("ask_depth")),
        "book_ts": ts or b.get("timestamp") or b.get("book_ts"),
    }
    return out if any(v is not None for v in out.values()) else None


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(XLSX, os.path.join(BACKUPS, "unified_prebooks_%s.xlsx" % ts))
    wb = openpyxl.load_workbook(XLSX)
    ws = wb["Markets"]
    hdr = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(hdr)}
    for c in BOOK_COLS:  # append any missing columns
        if c not in col:
            ws.cell(1, len(hdr) + 1, c)
            hdr.append(c)
            col[c] = len(hdr)

    # slug -> row index
    slug_row = {}
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, col["slug"]).value
        if v:
            slug_row[v] = r

    books = {}  # slug -> normalized book (later sources overwrite earlier)

    # source 2: this morning's run snapshots
    for line in open(os.path.join(RUNS, "books_20260823_090322.jsonl")):
        d = json.loads(line)
        b = normalize(d.get("book"))
        if b:
            books[d["slug"]] = b

    # source 3: Falcon Aug-20 settlement books (match band -> slug via market metadata)
    try:
        m20 = json.load(open(os.path.join(BASE, "aug20_markets.json")))
        p20 = json.load(open(os.path.join(BASE, "aug20_orderbooks_parsed.json")))
        for m in m20:
            band = re.search(r"(\d+)", m["question"]).group(1)
            if band in p20 and p20[band]:
                b = normalize(p20[band])
                if b:
                    b["book_ts"] = b["book_ts"] or "2026-08-20T18:51:38Z (Falcon settlement)"
                    books[m["slug"]] = b
    except FileNotFoundError:
        print("aug20 falcon files missing, skipped")

    # source 4: Falcon Aug-21 live favorites (match by bucket label)
    try:
        a21 = json.load(open(os.path.join(BASE, "aug21_orderbook.json")))
        label_book = {e["band"]: normalize({"best_bid": e.get("best_bid"),
                                            "best_ask": e.get("best_ask"),
                                            "bid_depth_usd": None, "ask_depth_usd": None},
                                           ts="2026-08-20T21:33:00Z (Falcon live)")
                      for e in a21}
        for slug, r in slug_row.items():
            if "august-21-2026" in slug and slug.startswith("highest"):
                lab = ws.cell(r, col["bucket_label"]).value
                if lab in label_book and label_book[lab]:
                    books[slug] = label_book[lab]
    except FileNotFoundError:
        print("aug21 falcon file missing, skipped")

    n_prior = len(books)
    print("matched from stored snapshots:", n_prior)

    # source 1 (highest priority): fresh live books for every open event row
    open_rows = {}
    for slug, r in slug_row.items():
        cd = ws.cell(r, col["closed"]).value
        if cd is False or cd is None and "august-2" in slug:
            open_rows[slug] = r
    fresh = 0
    gamma_cache = {}
    for slug in sorted(open_rows):
        day_slug = "-".join(slug.split("-")[:8])  # event slug from market slug
        if day_slug not in gamma_cache:
            try:
                evs = get("https://gamma-api.polymarket.com/events?slug=" + day_slug)
                gamma_cache[day_slug] = evs[0] if evs else None
            except Exception:
                gamma_cache[day_slug] = None
            time.sleep(0.3)
        ev = gamma_cache[day_slug]
        if not ev:
            continue
        for m in ev.get("markets", []):
            if m.get("slug") != slug or m.get("closed"):
                continue
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if not tokens:
                continue
            b = fetch_book(tokens[0])
            time.sleep(0.25)
            if b and any(v is not None for v in (b["best_bid"], b["best_ask"])):
                books[slug] = b
                fresh += 1

    # write cells
    written = 0
    manual = open(os.path.join(RUNS, "books_manual_%s.jsonl" % ts), "w")
    for slug, b in books.items():
        r = slug_row.get(slug)
        if not r:
            continue
        for c in BOOK_COLS:
            ws.cell(r, col[c], b.get(c))
        written += 1
        manual.write(json.dumps({"slug": slug, "book": b}) + "\n")
    manual.close()

    tmp = XLSX + ".tmp"
    wb.save(tmp)
    os.replace(tmp, XLSX)
    print("fresh live books: %d | total rows updated with books: %d / %d markets"
          % (fresh, written, len(slug_row)))


if __name__ == "__main__":
    main()
