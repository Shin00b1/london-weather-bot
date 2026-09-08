"""Out-of-sample walk-forward of the London Dawn Bot (lowest-temperature).

Replicates the original Apr 15 - Jul 20 2026 backtest methodology on the
UNSEEN window Jul 21 - Aug 25 2026, using the same rules so the comparison
is apples-to-apples:

  signal      round of the pre-06Z (UTC) EGLC METAR daily-min  -- the value
              knowable at dawn, when the bot would act (05:30-07:00 London)
  entry       $50 stake on the called bucket; price = 2h VWAP of p_yes trades
              in [04:00, 06:00) UTC on the event day (fallback: last print
              <=8h stale), + flat 3c slippage
  skips       (a) reading within ~0.2C of a bucket edge, (b) effective entry
              > 0.90, (c) no valid <=06Z print (illiquid)
  settlement  round(METAR day-min) == winning bucket (validated 99.2% in the
              original work); London-local calendar day, >=16 obs
  pnl         win:  stake*(1/p_eff - 1)   lose:  -stake

Read-only scoring; no live trading. Prints every day so the result is
auditable, then a summary + money-math table.
"""

import csv
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import duckdb

LON = ZoneInfo("Europe/London")
DB = "london/tape.duckdb"
METAR = "london/metar_eglc.csv"

STAKE = 50.0
SLIPPAGE = 0.03
ENTRY_WINDOW = (4, 6)          # [04:00, 06:00) UTC
FALLBACK_STALE_H = 8           # last print within 8h before 06Z
EDGE_SKIP_C = 0.2              # skip if closer than this to a .5 bucket edge
MAX_PRICE = 0.90               # skip if effective entry above this
MIN_OBS = 16                   # METAR completeness bar


def load_metar():
    obs = defaultdict(list)
    with open(METAR) as f:
        for row in csv.DictReader(f):
            try:
                if row.get("tmpc") in ("M", "T", "", None):
                    continue
                dt = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                obs[dt.astimezone(LON).date()].append((dt, float(row["tmpc"])))
            except (ValueError, KeyError):
                continue
    return obs


def bucket_label_for(call_c):
    """Map a rounded temperature to its bucket suffix. We don't know each
    day's open-ended tails from the settlement alone, but the call and winner
    are both interior integers in summer, so '{N}c' is correct; tails are
    detected dynamically from the actual slugs below."""
    return "%dc" % int(call_c)


def slug_to_bucket(slug):
    """'lowest-...-july-21-2026-14c' -> ('14c', 14) ; '...-10corbelow' -> ('<=10', 10)"""
    m = re.search(r"-(\d+)c(orbelow|orhigher)?$", slug)
    if not m:
        return None
    n = int(m.group(1))
    suf = m.group(2)
    if suf == "orbelow":
        return ("<=%d" % n, n)
    if suf == "orhigher":
        return (">=%d" % n, n)
    return ("%dc" % n, n)


def main():
    metar = load_metar()
    con = duckdb.connect(DB, read_only=True)

    # all lowest trades, p_yes computed, bucketed by event day
    rows = con.execute("""
        SELECT timestamp, slug, outcome, price, size
        FROM trades
        WHERE slug LIKE '%lowest%'
    """).fetchall()

    # group trades by event_slug date embedded in slug, and by bucket
    by_day = defaultdict(lambda: defaultdict(list))  # day -> bucket_label -> [(ts,p_yes,size)]
    for ts, slug, outcome, price, size in rows:
        b = slug_to_bucket(slug)
        if not b:
            continue
        # event day = the day in the slug, not the trade day
        m = re.search(r"on-([a-z]+)-(\d+)-2026", slug)
        if not m:
            continue
        mon = datetime.strptime(m.group(1), "%B").month
        day = int(m.group(2))
        d = date(2026, mon, day)
        p_yes = float(price) if outcome == "Yes" else 1.0 - float(price)
        by_day[d][b[0]].append((ts, p_yes, float(size)))

    def vwap_2h(trades, day):
        """2h VWAP of p_yes in [04:00,06:00) UTC on `day`; fallback last print <=8h stale."""
        start = datetime(day.year, day.month, day.day, ENTRY_WINDOW[0], tzinfo=timezone.utc)
        end = datetime(day.year, day.month, day.day, ENTRY_WINDOW[1], tzinfo=timezone.utc)
        window = [t for t in trades if start.timestamp() <= t[0] < end.timestamp()]
        if window:
            num = sum(p * s for _, p, s in window)
            den = sum(s for _, p, s in window)
            return num / den, len(window)
        # fallback: most recent print within FALLBACK_STALE_H before 06Z
        cutoff = start.timestamp() - FALLBACK_STALE_H * 3600
        recent = [t for t in trades if cutoff <= t[0] < end.timestamp()]
        if recent:
            last = max(recent, key=lambda t: t[0])
            return last[1], len(recent)
        return None, 0

    # tradable days: Jul 21 .. Aug 25 2026 (Aug 26/27 settlement incomplete)
    d0, d1 = date(2026, 7, 21), date(2026, 8, 25)
    print("=== London Dawn Bot out-of-sample walk-forward: %s -> %s ===\n" % (d0, d1))
    print("rules: call=round(pre-06Z METAR min); entry=$50 @ 2h VWAP[04-06Z]+3c; "
          "skip edge<0.2C / >0.90 / illiquid")
    print("-" * 104)

    hdr = "%-10s %5s %5s %5s  %-6s %-6s %-4s  %6s %6s %s" % (
        "date", "pre", "day", "call", "winner", "entry", "hit", "pnl", "cum", "note")
    print(hdr)
    print("-" * 104)

    trades_taken = 0
    wins = 0
    skipped = {"edge": 0, "price": 0, "illiquid": 0, "no_metar": 0}
    cum = 0.0
    drawdown = 0.0
    peak = 0.0

    cur = d0
    while cur <= d1:
        pts = metar.get(cur, [])
        # settlement completeness
        if len(pts) < MIN_OBS:
            skipped["no_metar"] += 1
            print("%-10s  -- insufficient METAR (%d obs)" % (cur, len(pts)))
            cur += timedelta(days=1)
            continue
        day_min = min(v for _, v in pts)
        pre = [v for t, v in pts if t.hour < ENTRY_WINDOW[1]]
        if not pre:
            skipped["illiquid"] += 1
            print("%-10s  -- no pre-06Z METAR obs" % cur)
            cur += timedelta(days=1)
            continue
        pre_min = min(pre)

        call = round(pre_min)
        winner = round(day_min)
        hit = (call == winner)

        # edge skip (point-in-time: uses pre-06Z reading)
        dist_to_edge = 0.5 - abs(pre_min - call)
        note = ""
        if dist_to_edge < EDGE_SKIP_C:
            skipped["edge"] += 1
            print("%-10s %5.1f %5.1f %5d  %-6d %-6s %-4s  %6s %6s %s" % (
                cur, pre_min, day_min, call, winner, "skip", "",
                "", "%+.0f" % cum, "edge %.2f" % dist_to_edge))
            cur += timedelta(days=1)
            continue

        # entry price on the CALLED bucket
        bucket = bucket_label_for(call)
        day_trades = by_day.get(cur, {})
        bucket_trades = day_trades.get(bucket, [])
        entry, n_print = vwap_2h(bucket_trades, cur)
        if entry is None:
            skipped["illiquid"] += 1
            print("%-10s %5.1f %5.1f %5d  %-6d %-6s %-4s  %6s %6s %s" % (
                cur, pre_min, day_min, call, winner, "skip", "",
                "", "%+.0f" % cum, "no <=06Z print"))
            cur += timedelta(days=1)
            continue
        entry_eff = entry + SLIPPAGE
        if entry_eff > MAX_PRICE:
            skipped["price"] += 1
            print("%-10s %5.1f %5.1f %5d  %-6d %-6s %-4s  %6s %6s %s" % (
                cur, pre_min, day_min, call, winner, "skip", "",
                "", "%+.0f" % cum, "price %.2f>0.90" % entry_eff))
            cur += timedelta(days=1)
            continue

        # trade taken
        trades_taken += 1
        if hit:
            pnl = STAKE * (1.0 / entry_eff - 1.0)
            wins += 1
            note = "win"
        else:
            pnl = -STAKE
            note = "LOSS (min dropped post-dawn)"
        cum += pnl
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
        print("%-10s %5.1f %5.1f %5d  %-6d %-6s %-4s  %6.2f %6.2f  %+6.2f  %s" % (
            cur, pre_min, day_min, call, winner,
            "%s" % bucket, "Y" if hit else "N",
            entry_eff, cum, pnl, note))
        cur += timedelta(days=1)

    print("-" * 104)
    print("\n=== SUMMARY ===")
    print("trades taken: %d | wins %d (%.1f%%)" % (trades_taken, wins, 100 * wins / trades_taken if trades_taken else 0))
    print("skips: %s" % skipped)
    print("net P&L: %+.2f on $50/trade" % cum)
    print("max drawdown: %.0f%%" % (100 * drawdown / 500.0))
    if trades_taken:
        print("avg P&L per trade: %+.2f" % (cum / trades_taken))
        print("avg entry (eff): --")
        # breakeven win rate
        avg_eff = None
    con.close()


if __name__ == "__main__":
    main()
