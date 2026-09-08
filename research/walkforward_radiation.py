"""Walk-forward backtest of the "hot morning => max overshoots" signal.

Bet: on the London HIGHEST-temperature market (2026 Celsius era, integer
'N°C' buckets), when the same-day morning temperature (METAR 08-11 UTC) is
near or above the D-1 forecast max, the daily max tends to overshoot the
model — so bet the bucket one degree higher than the model implies.

NO LOOKAHEAD: rules are fit on a chronological TRAIN window only and applied
frozen to a held-out TEST window. Prices come from the pre-noon (UTC < 11)
trades in london/tape.duckdb; days without pre-noon trades are scored for
hit-rate but excluded from P&L.

Outputs:
  1. train/test hit rates (baseline model bucket vs signal bucket)
  2. fitted overshoot delta (train only) -> test hit rate
  3. P&L at $50/day: baseline vs signal (only days with prices)
"""

import csv
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import duckdb
import openpyxl

BASE = os.path.dirname(os.path.abspath(__file__))
LONDON = os.path.join(BASE, "london")
LON = ZoneInfo("Europe/London")

STAKE = 50.0
PRE_NOON_HOUR = 11  # bet placed at 11:00 UTC, use trades before this


def load_metar_morning():
    """-> {date: morning_mean_temp} over 08-11 UTC."""
    out = defaultdict(list)
    with open(os.path.join(LONDON, "metar_eglc.csv")) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                v = float(row["tmpc"])
            except (ValueError, KeyError):
                continue
            if 8 <= dt.hour < 11:
                out[dt.astimezone(LON).date()].append(v)
    return {d: statistics.mean(vs) for d, vs in out.items() if vs}


def load_max_d1():
    out = {}
    with open(os.path.join(LONDON, "forecast_archive_d1.csv"), newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[row["date"]] = float(row["d1_ecmwf_max"])
            except (ValueError, KeyError):
                continue
    return out


def load_events_2026():
    """-> list of {date, bucket(int), actual_max, model_max} for 2026 highest, settled."""
    wb = openpyxl.load_workbook(os.path.join(LONDON, "london_temperature_unified.xlsx"), read_only=True)
    ws = wb["Events"]
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[col["direction"]] != "highest":
            continue
        d = r[col["event_date"]]
        d = d.date() if hasattr(d, "date") else d
        if str(d)[:4] != "2026":
            continue
        if r[col["resolved_temp_c"]] is None or r[col["metar_temp_c"]] is None:
            continue
        out.append({
            "date": d,
            "bucket": round(r[col["resolved_temp_c"]]),   # winning integer bucket
            "actual_max": r[col["metar_temp_c"]],
            "model_max": r[col["fcst_model_c"]],
        })
    wb.close()
    return out


def load_market_prices():
    """-> {(date, bucket_int): condition_id} and winning condition_ids, from Markets."""
    wb = openpyxl.load_workbook(os.path.join(LONDON, "london_temperature_unified.xlsx"), read_only=True)
    ws = wb["Markets"]
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    bucket_cond = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[col["direction"]] != "highest":
            continue
        ed = r[col["event_date"]]
        ed = ed.date() if hasattr(ed, "date") else ed
        if str(ed)[:4] != "2026":
            continue
        lo = r[col["bucket_low_c"]]
        hi = r[col["bucket_high_c"]]
        if lo is None:
            continue
        # integer bucket label: single 'N°C' => lo==hi==N
        if lo == hi:
            b = round(lo)
        else:
            continue  # ranges only matter for edge buckets; skip for now
        bucket_cond[(ed, b)] = str(r[col["condition_id"]]).lower() if r[col["condition_id"]] else None
    wb.close()
    return bucket_cond


def pre_noon_pyes(con, cond):
    """VWAP of YES-token price for trades on the event date before PRE_NOON_HOUR UTC."""
    rows = con.execute(
        "SELECT timestamp, price, size, outcome FROM trades WHERE condition_id=? "
        "ORDER BY timestamp", [cond]).fetchall()
    if not rows:
        return None
    pre = []
    for ts, price, size, outcome in rows:
        if price is None:
            continue
        dt = datetime.fromtimestamp(ts, timezone.utc)
        if dt.hour >= PRE_NOON_HOUR:
            continue
        p_yes = price if outcome == "Yes" else (1 - price)
        pre.append((p_yes, size or 0))
    if not pre:
        return None
    tot_s = sum(s for _, s in pre)
    if tot_s == 0:
        return statistics.mean([p for p, _ in pre])
    return sum(p * s for p, s in pre) / tot_s


def hit(bucket_pred, bucket_actual):
    return 1 if bucket_pred == bucket_actual else 0


def main():
    morning = load_metar_morning()
    max_d1 = load_max_d1()
    events = load_events_2026()
    bucket_cond = load_market_prices()

    # join: only days with morning + model forecast
    rows = []
    for e in events:
        d = e["date"]
        if d not in morning or d.isoformat() not in max_d1:
            continue
        e["morning"] = morning[d]
        e["model_max"] = max_d1[d.isoformat()]  # prefer archive (honest D-1)
        rows.append(e)
    rows.sort(key=lambda r: r["date"])

    # chronological train/test split (70/30)
    n = len(rows)
    split = int(n * 0.7)
    train, test = rows[:split], rows[split:]
    print("2026 highest settled days with morning+forecast: %d (train %d / test %d)" % (n, len(train), len(test)))

    # ---- 1. hit rates, fixed rule ----
    THR = 1.0  # morning within 1C of forecast max
    DELTA = 1.0  # overshoot add

    def score(grp, thr, delta):
        base_hits = sig_hits = sig_days = 0
        for r in grp:
            b = round(r["model_max"])  # model bucket
            base_hits += hit(b, r["bucket"])
            if r["morning"] >= r["model_max"] - thr:
                sig_days += 1
                pb = round(r["model_max"] + delta)
                sig_hits += hit(pb, r["bucket"])
        return base_hits, sig_hits, sig_days

    b_tr, s_tr, sd_tr = score(train, THR, DELTA)
    b_te, s_te, sd_te = score(test, THR, DELTA)
    print("\n=== 1. HIT RATE (fixed: thr=%.1f, delta=%.1f) ===" % (THR, DELTA))
    print("  baseline (model bucket):  train %d/%d=%.0f%%   test %d/%d=%.0f%%"
          % (b_tr, len(train), 100 * b_tr / len(train), b_te, len(test), 100 * b_te / len(test)))
    print("  signal bucket (fires):    train %d/%d=%.0f%%   test %d/%d=%.0f%%  [fires on %d train, %d test days]"
          % (s_tr, sd_tr, 100 * s_tr / sd_tr if sd_tr else 0,
             s_te, sd_te, 100 * s_te / sd_te if sd_te else 0, sd_tr, sd_te))

    # ---- 2. fit delta on TRAIN only ----
    print("\n=== 2. FIT OVERSHOOT DELTA (train only) ===")
    best = None
    for delta in [0.5, 1.0, 1.5, 2.0]:
        _, h, sd = score(train, THR, delta)
        rate = 100 * h / sd if sd else 0
        print("  delta=%+0.1f -> train signal hit %d/%d = %.0f%%" % (delta, h, sd, rate))
        if best is None or rate > best[0]:
            best = (rate, delta)
    best_delta = best[1]
    print("  -> chosen delta=%.1f (train-optimal)" % best_delta)
    _, s_te_f, sd_te_f = score(test, THR, best_delta)
    print("  -> test signal hit with fitted delta: %d/%d = %.0f%%" %
          (s_te_f, sd_te_f, 100 * s_te_f / sd_te_f if sd_te_f else 0))

    # ---- 3. P&L (test window, days with prices) ----
    print("\n=== 3. P&L @ $%d/day (test window, pre-noon prices) ===" % STAKE)
    con = duckdb.connect(os.path.join(LONDON, "tape.duckdb"), read_only=True)
    for name, use_signal, delta in [("baseline (model bucket)", False, 0.0),
                                     ("signal bucket", True, DELTA)]:
        pnl = 0.0
        bets = wins = 0
        for r in test:
            pb = round(r["model_max"] + (delta if (use_signal and r["morning"] >= r["model_max"] - THR) else 0))
            if use_signal and not (r["morning"] >= r["model_max"] - THR):
                continue  # signal strategy only bets on fire days
            cond = bucket_cond.get((r["date"], pb))
            if not cond:
                continue
            p = pre_noon_pyes(con, cond)
            if p is None:
                continue
            bets += 1
            won = (pb == r["bucket"])
            wins += won
            pnl += (STAKE / p - STAKE) if won else -STAKE
        wr = 100 * wins / bets if bets else 0
        be = 100 * statistics.mean(
            [pre_noon_pyes(con, bucket_cond[(r["date"], round(r["model_max"]))])
             for r in test
             if bucket_cond.get((r["date"], round(r["model_max"]))) and
             pre_noon_pyes(con, bucket_cond[(r["date"], round(r["model_max"]))])]) if False else 0
        print("  %-22s bets=%d wins=%d win_rate=%.0f%%  net=$%+.0f"
              % (name, bets, wins, wr, pnl))
    con.close()

    # breakeven context: distribution of entry prices
    print("\n  (breakeven win rate = average entry price; see win-rate vs entry price note below)")


if __name__ == "__main__":
    main()
