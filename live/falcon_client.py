"""Minimal, rate-limited, resumable Falcon (Polymarket Analytics) client.

Free-tier friendly: one request at a time, configurable sleep, and every
successful page is checkpointed to disk so an interrupted run resumes
instead of re-downloading.

Endpoints used (all POST https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized):
  agent 556 = Polymarket Trades (filter by condition_id, start/end SECONDS)
  agent 568 = Candlesticks (filter by token_id, start/end SECONDS)
  agent 572 = Orderbook snapshots (filter by token_id, start/end MILLISECONDS!)
NOTE: 572 requires ms timestamps (13-digit); seconds silently return 0 rows.
Its archive begins ~2026-01-27; earlier markets return empty.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".env")

BASE_URL = "https://retriever.falconapi.net/api/v2/semantic/retrieve/parameterized"
UA = {"User-Agent": "london-falcon/1.0", "Content-Type": "application/json"}


def load_token():
    tok = None
    if os.path.exists(ENV):
        for line in open(ENV):
            line = line.strip()
            if line.startswith("FALCON_API_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not tok:
        raise RuntimeError("FALCON_API_TOKEN not found in .env")
    return tok


def _post(agent_id, params, offset=0, limit=200, timeout=90):
    body = {
        "agent_id": agent_id,
        "params": params,
        "pagination": {"limit": limit, "offset": offset},
        "formatter_config": {"format_type": "raw"},
    }
    tok = load_token()
    headers = dict(UA)
    headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(BASE_URL, headers=headers,
                                 data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_trades(condition_id, start_time, end_time, on_page=None, sleep=1.0, max_pages=20):
    """All trades for one market (condition_id) across pagination.

    on_page(results, offset, has_more) is called after each page for
    checkpointing; return True from it to stop early.
    """
    params = {"condition_id": condition_id,
              "start_time": str(start_time), "end_time": str(end_time)}
    all_rows = []
    offset = 0
    for _ in range(max_pages):
        data = _post(556, params, offset=offset)
        results = data.get("data", {}).get("results", []) or []
        pm = data.get("pagination", {}) or {}
        has_more = bool(pm.get("has_more", False))
        all_rows.extend(results)
        if on_page and on_page(results, offset, has_more):
            break
        if not has_more or len(results) < 200:
            break
        offset += len(results)
        if sleep:
            time.sleep(sleep)
    return all_rows


def fetch_candles(token_id, start_time, end_time, interval="1h", sleep=1.0):
    params = {"token_id": token_id, "interval": interval,
              "start_time": str(start_time), "end_time": str(end_time)}
    data = _post(568, params)
    return data.get("data", {}).get("results", []) or []
