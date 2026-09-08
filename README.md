# london-weather-bot

Systematic trading research on Polymarket's daily London temperature markets —
the full arc from first (wrong) idea to one validated live edge, with every
dataset, backtest, and negative result included.

**The finding in one sentence:** the day's minimum temperature is effectively
determined by 06:00 UTC, but the market keeps mispricing it for hours — so the
profitable trade is not predicting weather, it is buying an *already-decided
outcome* from counterparties who haven't updated yet.

## Results

| | Result | Caveat |
|---|---|---|
| Core backtest (dawn-lock, Apr–Jul 2026) | **+764 dollars** on 50 dollars/day, 72 trades | summer days only |
| Bid/ask-corrected backtest | **+206 dollars** on 17 trades at 100 dollars/trade | 4-cent median spread charged honestly |
| Forward paper test (live bot, since 2026-08-28) | **17/17 winners, +103 dollars** (core bot) | ~2 weeks, paper fills |
| Combined two-bot forward test | **+207 dollars on 384 dollars risked**, max drawdown 61 dollars | both bots express the same edge |
| Regime gate (the underlying structure) | 92.4% lock on radiation days vs 64.5% otherwise | validated on 591 days |
| Max-side (daily highest) mirror strategy | **no edge — closed** after a 115-day sweep | documented, not hidden |
| Market making | adverse selection on the min side; spread capture too small at this bankroll | two paper MMs built to test it live |

## Why this repo might be worth reading

It isn't a victory lap — it's the full experimental record, including the parts
that failed:

1. **The first strategy was killed by its own author.** A backtest that looked
   profitable was traced to look-ahead contamination in a "historical forecast"
   data source. The evidence (`data/forecasts/lookahead_*`) and the rebuild on
   strictly point-in-time data are in the repo. Every number that survived is
   leak-tested.
2. **Negative results are first-class.** The max-side branch, private-station
   leads (a sun-bias artifact masquerading as a 25-minute edge), rain vetoes
   (redundant), and min-side market making (adverse selection) were each
   studied, quantified, and closed with the receipts committed.
3. **Costs are charged honestly.** Backtests pay the real spread; capacity is
   capped where market impact starts; the paper market maker's losing days sit
   in the public data files.
4. **The edge has a mechanism, not just a backtest**: the minimum settles while
   the market sleeps; the maximum forms while it's awake. That asymmetry
   predicts exactly which side works (min) and which can't (max) — and the data
   confirmed the prediction.

## How the live bot works

```
poll EGLC (London City) METAR every 60 s, 03:00–07:30 UTC
  → classify the day: radiation (forecast diurnal range ≥ 8 °C) or advection
  → on radiation days, the station's own pre-06Z minimum determines the
    winning LOWEST bucket with ~92% reliability
  → if that bucket's ask is below fair value minus costs: buy
  → second leg at 03Z: buy one degree below the pre-03Z leader (~80% hit rate)
```

What it deliberately does *not* do: predict tomorrow, trade the max side,
quote against informed flow, or use anything that wasn't public and point-in-time.

## Repository map

| Path | Contents |
|---|---|
| [RECAP.md](RECAP.md) | **The full research narrative**: every strategy tried, the verdict table, the money summary, and the methodology lessons |
| [data/](data/README.md) | The collected data — 20.5 months of METAR, point-in-time ECMWF forecasts, 4,700+ market records, bot decision logs — with a complete data dictionary |
| `live/` | The running stack: `dawn_watcher.py` (the edge), regime classifier, paper market makers, orderbook tape recorder, station scrapers |
| `research/` | 29 backtest and study scripts — the evidence behind every claim, including the leak tests |
| `deploy/` | systemd deployment kit for a 12 dollars/month droplet (the dawn window doesn't survive laptop sleep) |

Not committed (reproducible, 800+ MB): the 254 MB orderbook tape
(`tape.duckdb`) and raw run outputs. The recorder scripts that rebuild them are
in `live/`.

## Reproducing

```bash
python3 research/lookahead_check.py        # the leak test every dataset must pass
python3 live/dawn_watcher.py               # dawn-window watcher (dry by default)
python3 live/field_maker.py                # paper MM on the HIGHEST book
```

Weather data: NOAA aviationweather.gov (METAR), Open-Meteo (ECMWF runs).
Market data: Polymarket public APIs. Secrets (if any) load from a local,
git-ignored `.env` — the Synoptic station scraper needs no key.

## Honest limits

- The forward test is ~2 weeks of paper fills on a summer edge; **winter is the
  open problem** (the regime structure differs; candidate strategies exist, at
  paper stage only).
- The dawn window is decaying — entries have drifted from ~0.53 mispricings
  toward 0.90+ "already repriced" buys. Execution speed and multi-city breadth
  are the growth levers, not new signals.
- All figures are the author's own research on prediction markets. Nothing here
  is investment advice.

---

*Built as a one-person quant research project: data engineering, market
microstructure forensics, backtesting, live automation, and the discipline to
kill bad ideas — documented end to end.*
