"""Daily Polymarket London weather updater (public APIs, no key required).

Fetches the latest London temperature events from Polymarket's public APIs:
  - Gamma API   : event + market metadata  (gamma-api.polymarket.com/events?slug=)
  - CLOB API    : price history + live books (clob.polymarket.com)
  - Data API    : per-trade records          (data-api.polymarket.com/trades)

and upserts them into london/london_temperature_unified.xlsx:
  - Markets: upsert by condition_id (full refresh of owned fields)
  - Events : upsert by event_slug (delta-merged trade counts)
  - Daily  : upsert by trade_date (delta-merged, never double-counts)
README gets one Source-3 row; Reconciliation/FreeLunch/FavoriteAnalysis untouched.

Raw per-run trades and book snapshots land in london/runs/<date>.jsonl.
A backup of the workbook is written to london/backups/ before every save.

Usage:
    python3 update_weather_sheet.py [--events N]   # limit event count (testing)
"""

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
XLSX = os.path.join(LONDON, "london_temperature_unified.xlsx")
STATE_PATH = os.path.join(LONDON, "updater_state.json")
REPORT_PATH = os.path.join(LONDON, "last_run_report.json")
LOG_PATH = os.path.join(LONDON, "update_weather.log")
RUNS_DIR = os.path.join(LONDON, "runs")
BACKUPS_DIR = os.path.join(LONDON, "backups")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def http_json(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "london-weather-updater/1.0"})
    for attempt in range(retries):
        try:
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


# ---------------------------------------------------------------- discovery

def candidate_slugs(last_date, today):
    """Event slugs to probe: 3 days of refresh back, 2 days forward, both directions."""
    out = []
    d = last_date - timedelta(days=3)
    while d <= today + timedelta(days=2):
        for direction in ("highest", "lowest"):
            out.append((d, direction,
                        "%s-temperature-in-london-on-%s-%d-%d" %
                        (direction, MONTHS[d.month - 1], d.day, d.year)))
        d += timedelta(days=1)
    return out


def fetch_event(slug):
    evs = http_json(GAMMA + "/events?slug=" + slug)
    return evs[0] if evs else None


# ---------------------------------------------------------------- parsing

def parse_bucket(question):
    """'Will the highest temperature in London be 24°C on August 22?' ->
    (label, low_c, high_c, unit). Fahrenheit is converted to Celsius."""
    m = re.search(r"be (\d+)°([CF]) or (?:below|lower)", question)
    if m:
        v, u = int(m.group(1)), m.group(2)
        c = round((v - 32) * 5 / 9, 1) if u == "F" else float(v)
        return "%d°%s or below" % (v, u), c, c, u
    m = re.search(r"be (\d+)°([CF]) or (?:higher|above)", question)
    if m:
        v, u = int(m.group(1)), m.group(2)
        c = round((v - 32) * 5 / 9, 1) if u == "F" else float(v)
        return "%d°%s or higher" % (v, u), c, c, u
    m = re.search(r"be between (\d+)-(\d+)°([CF])", question)
    if m:
        a, b, u = int(m.group(1)), int(m.group(2)), m.group(3)
        if u == "F":
            return "%d–%d°%s" % (a, b, u), round((a - 32) * 5 / 9, 1), round((b - 32) * 5 / 9, 1), u
        return "%d–%d°%s" % (a, b, u), float(a), float(b), u
    m = re.search(r"be (\d+)°([CF])", question)
    if m:
        v, u = int(m.group(1)), m.group(2)
        c = round((v - 32) * 5 / 9, 1) if u == "F" else float(v)
        return "%d°%s" % (v, u), c, c, u
    return question, None, None, "?"


def temp_of_bucket(label, low_c, high_c):
    if low_c is None:
        return None
    if low_c == high_c:
        return low_c
    return round((low_c + high_c) / 2, 1)


# ---------------------------------------------------------------- data fetch

def fetch_trades(condition_id, max_pages=6):
    """Taker-side trade feed, newest first. Offset is honored; cap pages to
    stay friendly to the API (busiest bands hold ~2-3 pages)."""
    rows, offset, seen = [], 0, set()
    for _ in range(max_pages):
        page = http_json(DATA_API + "/trades?market=%s&limit=500&offset=%d" % (condition_id, offset))
        if not page:
            break
        for t in page:
            key = (t.get("transactionHash"), t.get("proxyWallet"), t.get("timestamp"), t.get("asset"))
            if key not in seen:
                seen.add(key)
                rows.append(t)
        if len(page) < 500:
            break
        offset += 500
        time.sleep(0.3)
    return rows


def fetch_price_edges(token_id, start_ts, end_ts):
    """(open, close) Yes-price from CLOB prices-history over the market's life."""
    if not token_id:
        return None, None
    url = "%s/prices-history?market=%s&startTs=%d&endTs=%d&fidelity=60" % (
        CLOB, token_id, start_ts, end_ts)
    try:
        h = http_json(url) or {}
    except urllib.error.HTTPError:
        return None, None  # some closed tokens reject the query window
    hist = h.get("history") or []
    if not hist:
        return None, None
    return hist[0]["p"], hist[-1]["p"]


def fetch_book(token_id):
    """Live orderbook; only meaningful for open markets (closed ones 400)."""
    if not token_id:
        return None
    try:
        b = http_json(CLOB + "/book?token_id=" + token_id)
    except urllib.error.HTTPError:
        return None
    if not b:
        return None
    bids = b.get("bids") or []
    asks = b.get("asks") or []
    bts = b.get("timestamp")
    if isinstance(bts, (int, float)) or (isinstance(bts, str) and bts.isdigit()):
        secs = int(bts)
        if secs > 10**12:  # CLOB reports milliseconds
            secs //= 1000
        bts = datetime.utcfromtimestamp(secs).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "timestamp": bts,
        "best_bid": max((float(x["price"]) for x in bids), default=None),
        "best_ask": min((float(x["price"]) for x in asks), default=None),
        "bid_depth_usd": round(sum(float(x["price"]) * float(x["size"]) for x in bids), 2),
        "ask_depth_usd": round(sum(float(x["price"]) * float(x["size"]) for x in asks), 2),
        "n_bids": len(bids), "n_asks": len(asks),
    }


def ts_unix(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def utc_dt(unix):
    return datetime.utcfromtimestamp(int(unix))


# ---------------------------------------------------------------- workbook io

def load_sheet_map(wb):
    """header-name -> column index, per sheet; plus key -> row index maps."""
    maps = {}
    for name in ("Markets", "Events", "Daily"):
        ws = wb[name]
        hdr = [c.value for c in ws[1]]
        maps[name] = {"ws": ws, "hdr": hdr, "col": {h: i + 1 for i, h in enumerate(hdr)}}
    mk = maps["Markets"]
    mk["by_cond"] = {}
    for r in range(2, mk["ws"].max_row + 1):
        v = mk["ws"].cell(r, mk["col"]["condition_id"]).value
        if v:
            mk["by_cond"][str(v).lower()] = r
    ev = maps["Events"]
    ev["by_slug"] = {}
    for r in range(2, ev["ws"].max_row + 1):
        v = ev["ws"].cell(r, ev["col"]["event_slug"]).value
        if v:
            ev["by_slug"][v] = r
    dl = maps["Daily"]
    dl["by_date"] = {}
    for r in range(2, dl["ws"].max_row + 1):
        v = dl["ws"].cell(r, dl["col"]["trade_date"]).value
        if v:
            dl["by_date"][v.date() if isinstance(v, datetime) else v] = r
    return maps


def set_row(ws, row, colmap, values):
    for k, v in values.items():
        if k in colmap:
            ws.cell(row, colmap[k], v)


# ---------------------------------------------------------------- main

def main():
    import openpyxl

    limit = int(sys.argv[sys.argv.index("--events") + 1]) if "--events" in sys.argv else None

    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {"markets": {}}
    wb = openpyxl.load_workbook(XLSX)
    maps = load_sheet_map(wb)

    # last event date currently in the sheet
    mk = maps["Markets"]
    last_dates = [mk["ws"].cell(r, mk["col"]["event_date"]).value
                  for r in range(2, mk["ws"].max_row + 1)]
    last_dates = [d.date() if isinstance(d, datetime) else d for d in last_dates if d]
    last_date = max(last_dates)
    today = datetime.now(timezone.utc).date()

    slugs = candidate_slugs(last_date, today)
    if limit:
        slugs = slugs[:limit]
    log("run start: sheet last event %s, probing %d candidate events" % (last_date, len(slugs)))

    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trades_out = open(os.path.join(RUNS_DIR, "trades_%s.jsonl" % run_id), "w")
    books_out = open(os.path.join(RUNS_DIR, "books_%s.jsonl" % run_id), "w")
    holders_out = open(os.path.join(RUNS_DIR, "holders_%s.jsonl" % run_id), "w")

    report = {"run_at": datetime.now(timezone.utc).isoformat(), "markets_added": 0,
              "markets_updated": 0, "events_added": 0, "events_updated": 0,
              "daily_updated": 0, "resolved": [], "open_favorites": [],
              "trades_rows": 0, "errors": []}

    # daily deltas accumulate here: date -> [trades, markets(set), usd, buys, wallets(set)]
    daily_delta = {}
    # event deltas: event_slug -> [d_trades, d_usd]; plus computed full rows for new events
    event_data = {}
    # snapshot rows for the CSV export
    csv_rows = []

    def daily_add(ts, usd, side, wallet, market_key):
        d = utc_dt(ts).date()
        e = daily_delta.setdefault(d, [0, set(), 0.0, 0, set()])
        e[0] += 1
        e[1].add(market_key)
        e[2] += usd
        if side == "BUY":
            e[3] += 1
        e[4].add(wallet)

    seen_events = 0
    for event_day, direction, slug in slugs:
        try:
            ev = fetch_event(slug)
            if not ev or not ev.get("markets"):
                continue
            seen_events += 1
            log("event %s: %d markets, closed=%s" % (slug, len(ev["markets"]), ev.get("closed")))

            ev_trades = ev_usd = 0
            ev_first = ev_last = None
            winner = None
            favorite = None
            for m in ev["markets"]:
                q = m.get("question") or ""
                label, low_c, high_c, unit = parse_bucket(q)
                cond = (m.get("conditionId") or "").lower()
                tokens = json.loads(m.get("clobTokenIds") or "[]")
                prices = json.loads(m.get("outcomePrices") or "[]")
                yes_price = float(prices[0]) if prices else None
                closed = bool(m.get("closed"))
                won = closed and yes_price is not None and yes_price > 0.5
                if won:
                    winner = (label, temp_of_bucket(label, low_c, high_c))
                if favorite is None or (yes_price is not None and (favorite[1] or 0) < yes_price):
                    if not closed:
                        favorite = (label, yes_price)

                # baseline for delta counting (avoid double-counting Daily/Events)
                prev = state["markets"].get(cond)
                row_idx = mk["by_cond"].get(cond)
                if prev is None and row_idx:
                    prev_ts = mk["ws"].cell(row_idx, mk["col"]["last_trade"]).value
                    prev = {"last_ts": ts_unix(prev_ts.isoformat() + "Z") - 1
                            if isinstance(prev_ts, datetime) else 0}

                trades = fetch_trades(cond)
                for t in trades:
                    trades_out.write(json.dumps(t) + "\n")
                report["trades_rows"] += len(trades)
                book = None if closed else fetch_book(tokens[0])
                books_out.write(json.dumps({
                    "condition_id": cond, "slug": m.get("slug"), "book": book,
                }) + "\n")
                if not closed:
                    try:
                        hres = http_json(DATA_API + "/holders?market=" + cond) or []
                        holders_out.write(json.dumps({
                            "condition_id": cond, "slug": m.get("slug"),
                            "prices": prices, "holders": hres}) + "\n")
                    except Exception:
                        pass
                if book and (book.get("best_bid") is not None or book.get("best_ask") is not None):
                    bb, ba = book.get("best_bid"), book.get("best_ask")
                    book_cells = {
                        "best_bid": bb, "best_ask": ba,
                        "spread": round(ba - bb, 4) if (bb is not None and ba is not None) else None,
                        "bid_depth_usd": book.get("bid_depth_usd"),
                        "ask_depth_usd": book.get("ask_depth_usd"),
                        "book_ts": book.get("timestamp"),
                    }
                    report["books_live"] = report.get("books_live", 0) + 1
                else:
                    book_cells = {}
                csv_rows.append({
                    "event_id": ev.get("id"), "event_title": ev.get("title"),
                    "market_question": q, "outcomes": m.get("outcomes"),
                    "prices": m.get("outcomePrices"), "volume": m.get("volume"),
                    "liquidity": m.get("liquidity"),
                    "clob_token_ids": m.get("clobTokenIds"),
                    "end_date": m.get("endDate") or ev.get("endDate"),
                    "closed": closed, "won": won,
                })

                if not trades and row_idx:
                    # known market with no new trades: never zero history,
                    # but still refresh book columns if we got a live one
                    if book_cells:
                        set_row(mk["ws"], row_idx, mk["col"], book_cells)
                    continue

                usd = size_sum = 0.0
                n_buy = first_ts = last_ts = 0
                open_p = close_p = None
                if trades:
                    start_s = ts_unix(m.get("startDate") or m.get("createdAt") or ev["endDate"])
                    end_s = max(ts_unix(ev["endDate"]), int(time.time()))
                    open_p, close_p = fetch_price_edges(tokens[0], start_s - 86400, end_s)
                    usd = sum(float(t["price"]) * float(t["size"]) for t in trades)
                    size_sum = sum(float(t["size"]) for t in trades)
                    n_buy = sum(1 for t in trades if t.get("side") == "BUY")
                    first_ts = int(trades[-1]["timestamp"])   # data-api returns newest-first
                    last_ts = int(trades[0]["timestamp"])
                    baseline = (prev or {}).get("last_ts", 0)
                    for t in trades:
                        ts_i = int(t["timestamp"])
                        if ts_i > baseline:
                            daily_add(ts_i, float(t["price"]) * float(t["size"]),
                                      t.get("side"), t.get("proxyWallet"), cond)
                            ed = event_data.setdefault(slug, {"d_trades": 0, "d_usd": 0.0})
                            ed["d_trades"] += 1
                            ed["d_usd"] += float(t["price"]) * float(t["size"])
                    ev_trades += len(trades)
                    ev_usd += usd
                    ev_first = utc_dt(first_ts) if ev_first is None else min(ev_first, utc_dt(first_ts))
                    ev_last = utc_dt(last_ts) if ev_last is None else max(ev_last, utc_dt(last_ts))

                values = {
                    "slug": m.get("slug"), "event_date": datetime(event_day.year, event_day.month, event_day.day),
                    "direction": direction, "bucket_label": label,
                    "bucket_low_c": low_c, "bucket_high_c": high_c, "unit": unit,
                    "n_trades": len(trades), "volume_usdc": round(usd, 2),
                    "vwap_prob": round(usd / size_sum, 4) if size_sum else None,
                    "open_prob": open_p, "close_prob": close_p,
                    "buy_trades": n_buy, "sell_trades": len(trades) - n_buy,
                    "won": won, "official_volume": m.get("volumeNum"),
                    "liquidity": m.get("liquidityNum"),
                    "outcome_prices": m.get("outcomePrices"), "closed": closed,
                    "first_trade": utc_dt(first_ts) if trades else None,
                    "last_trade": utc_dt(last_ts) if trades else None,
                    "event_slug": ev.get("slug"), "event_title": ev.get("title"),
                    "question": q, "market_id": m.get("id"), "condition_id": cond,
                }
                values.update(book_cells)  # orderbook snapshot columns (when live)
                if row_idx:
                    set_row(mk["ws"], row_idx, mk["col"], values)
                    report["markets_updated"] += 1
                else:
                    mk["ws"].append([values.get(h) for h in mk["hdr"]])
                    mk["by_cond"][cond] = mk["ws"].max_row
                    report["markets_added"] += 1
                state["markets"][cond] = {"last_ts": last_ts}
                if winner:
                    for r in range(2, mk["ws"].max_row + 1):
                        if mk["ws"].cell(r, mk["col"]["event_slug"]).value == ev.get("slug"):
                            mk["ws"].cell(r, mk["col"]["resolved_temp_c"], winner[1])
                time.sleep(0.3)

            # Events upsert
            ec = maps["Events"]["col"]
            existing = maps["Events"]["by_slug"].get(ev.get("slug"))
            base = {"event_date": datetime(event_day.year, event_day.month, event_day.day),
                    "direction": direction, "event_title": ev.get("title"),
                    "n_buckets": len(ev["markets"]), "event_slug": ev.get("slug")}
            if existing:
                ws_e = maps["Events"]["ws"]
                base["trades"] = (ws_e.cell(existing, ec["trades"]).value or 0) + \
                    event_data.get(slug, {}).get("d_trades", 0)
                base["volume_usdc"] = round((ws_e.cell(existing, ec["volume_usdc"]).value or 0) +
                                            event_data.get(slug, {}).get("d_usd", 0), 2)
                base["first_trade"] = ws_e.cell(existing, ec["first_trade"]).value or ev_first
                base["last_trade"] = ev_last or ws_e.cell(existing, ec["last_trade"]).value
                set_row(ws_e, existing, ec, base)
                report["events_updated"] += 1
            else:
                base.update({"trades": ev_trades, "volume_usdc": round(ev_usd, 2),
                             "first_trade": ev_first, "last_trade": ev_last})
                maps["Events"]["ws"].append([base.get(h) for h in maps["Events"]["hdr"]])
                maps["Events"]["by_slug"][ev["slug"]] = maps["Events"]["ws"].max_row
                report["events_added"] += 1
            if winner:
                set_row(maps["Events"]["ws"],
                        maps["Events"]["by_slug"][ev.get("slug")], ec,
                        {"winning_bucket": winner[0], "resolved_temp_c": winner[1]})
                report["resolved"].append({"event": slug, "winning_bucket": winner[0],
                                           "temp_c": winner[1]})
            elif favorite and (favorite[1] or 0) > 0.15:
                report["open_favorites"].append({"event": slug, "favorite": favorite[0],
                                                 "prob": favorite[1]})

            # checkpoint: persist sheet + state after every event so an
            # interrupted run resumes instead of restarting from zero
            tmp_xlsx = XLSX + ".tmp"
            wb.save(tmp_xlsx)
            os.replace(tmp_xlsx, XLSX)
            json.dump(state, open(STATE_PATH + ".tmp", "w"))
            os.replace(STATE_PATH + ".tmp", STATE_PATH)
        except Exception as e:
            report["errors"].append({"event": slug, "error": repr(e)[:300]})
            log("ERROR on %s: %r" % (slug, e))

    trades_out.close()
    books_out.close()
    holders_out.close()

    # append this run's trades to the unified tape (deduped, forward-only)
    try:
        import tape as _tape
        run_path = os.path.join(RUNS_DIR, "trades_%s.jsonl" % run_id)
        _batch = []
        with open(run_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _batch.append(json.loads(_line))
                except json.JSONDecodeError:
                    continue
        _con = _tape.connect_retry()
        _n = _tape.append_trades(_con, _batch, datetime.now(timezone.utc), src="daily")
        _con.close()
        report["tape_trades"] = _n
    except Exception as _e:
        report["errors"].append({"event": "tape_append", "error": repr(_e)[:300]})
        log("tape append failed: %r" % _e)

    # current-markets CSV snapshot (latest run window)
    if csv_rows:
        import csv as _csv
        csv_path = os.path.join(LONDON, "london_weather_polymarket.csv")
        with open(csv_path, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)

    # Daily upsert (delta-merged)
    dc = maps["Daily"]["col"]
    ws_d = maps["Daily"]["ws"]
    for d, (n, markets_set, usd, buys, wallets) in sorted(daily_delta.items()):
        if n == 0:
            continue
        row = maps["Daily"]["by_date"].get(d)
        if row:
            t0 = ws_d.cell(row, dc["trades"]).value or 0
            v0 = ws_d.cell(row, dc["volume_usdc"]).value or 0
            b0 = ws_d.cell(row, dc["buy_trades"]).value or 0
            m0 = ws_d.cell(row, dc["markets"]).value or 0
            w0 = ws_d.cell(row, dc["active_traders"]).value or 0
            set_row(ws_d, row, dc, {
                "trades": t0 + n, "markets": m0 + len(markets_set),
                "volume_usdc": round(v0 + usd, 2), "buy_trades": b0 + buys,
                "avg_trade_usdc": round((v0 + usd) / (t0 + n), 2),
                "active_traders": w0 + len(wallets)})
        else:
            ws_d.append([{"trade_date": datetime(d.year, d.month, d.day), "trades": n,
                          "markets": len(markets_set), "volume_usdc": round(usd, 2),
                          "buy_trades": buys, "avg_trade_usdc": round(usd / n, 2),
                          "active_traders": len(wallets)}.get(h) for h in maps["Daily"]["hdr"]])
            maps["Daily"]["by_date"][d] = ws_d.max_row
        report["daily_updated"] += 1

    # README: document this automation once
    ws_r = wb["README"]
    have = any("Source 3" in str(c.value or "") for row in ws_r.iter_rows() for c in row)
    if not have:
        ws_r.append(("Source 3 (Polymarket public API updater)",
                     "2026-07-23 -> ongoing; Gamma+CLOB+data-api; auto-updated daily"))

    # backup, then atomic save
    shutil.copy2(XLSX, os.path.join(BACKUPS_DIR, "unified_%s.xlsx" % run_id))
    json.dump(state, open(STATE_PATH + ".tmp", "w"))
    os.replace(STATE_PATH + ".tmp", STATE_PATH)
    tmp_xlsx = XLSX + ".tmp"
    wb.save(tmp_xlsx)
    os.replace(tmp_xlsx, XLSX)

    # refresh observed temperatures (METAR) for recent days; runs after the
    # main save because it reloads the workbook from disk
    try:
        import metar_update
        metar_update.run(days_window=4)
        report["metar"] = "ok"
    except Exception as e:
        report["errors"].append({"event": "metar_refresh", "error": repr(e)[:300]})
        log("metar refresh failed: %r" % e)

    # refresh EGLC wind (sknt/drct) for the same trailing window — the frontal
    # marker archive for bot 1 (idempotent merge, appended forward)
    try:
        import bot1_wind
        bot1_wind.refresh(days=4, quiet=True)
        report["wind"] = "ok"
    except Exception as e:
        report["errors"].append({"event": "wind_refresh", "error": repr(e)[:300]})
        log("wind refresh failed: %r" % e)

    # low-band 06Z signal + rain flags (reads the station CSVs, METAR archive
    # and D-1 forecasts this run just refreshed; idempotent upsert by date).
    # Today's call only appears once the 00:15 station fetch has stored the
    # pre-dawn points — until then today's row stays 'pending', by design.
    try:
        import lowband_signal
        lowband_signal.main()
        report["lowband"] = "ok"
    except Exception as e:
        report["errors"].append({"event": "lowband_signal", "error": repr(e)[:300]})
        log("lowband signal failed: %r" % e)

    # refresh the independent EGLL (Heathrow) settlement cross-check
    try:
        import egll_crosscheck
        egll_crosscheck.run(days=2, quiet=True)
        report["egll"] = "ok"
    except Exception as e:
        report["errors"].append({"event": "egll_refresh", "error": repr(e)[:300]})
        log("egll refresh failed: %r" % e)

    json.dump(report, open(REPORT_PATH, "w"), indent=2, default=str)
    log("run ok: +%d/_%d markets, +%d/_%d events, %d daily cells, %d trade rows, %d errors" % (
        report["markets_added"], report["markets_updated"], report["events_added"],
        report["events_updated"], report["daily_updated"], report["trades_rows"],
        len(report["errors"])))


if __name__ == "__main__":
    main()
