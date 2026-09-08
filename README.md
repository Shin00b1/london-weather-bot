# london-weather-bot

Systematic trading bot for Polymarket daily London temperature markets (daily
HIGHEST / LOWEST temperature buckets). The full research story, results, and
conclusions live in **[RECAP.md](RECAP.md)** — read that first.

**The one-line version:** the day's minimum temperature is effectively known by
06:00 UTC; the market keeps mispricing it for hours afterward. The bot buys
already-determined outcomes from counterparties who haven't updated. Everything
else in this repo is either infrastructure, or the graveyard of strategies that
didn't survive honest testing.

## Status

- `dawn_watcher.py` — LIVE since 2026-08-28 (paper fills): 17/17 on the core
  dawn-lock signal, combined forward P&L +207 dollars on 384 dollars risked.
- `field_maker.py` — paper market maker on the HIGHEST book (decision at 30
  quoted days).
- Backtest record: +764 dollars on 50 dollars/day over 72 trades (Apr–Jul 2026),
  bid/ask-corrected variant +206 dollars over 17 trades at 100 dollars/trade.

## Repository layout

```
live/       the running stack
  dawn_watcher.py       dawn-window polling bot (60s, 03:00–07:30Z) — the edge
  lowband_signal.py     06Z dawn-lock signal construction
  bot1_regime.py        radiation/advection regime classifier (range ≥ 8 °C gate)
  bot1_wind.py          EGLC wind archival + frontal-passage marker
  dawn_maker.py         paper rewards-quoter on the LOWEST book
  field_maker.py        paper market maker on the HIGHEST book
  book_recorder.py      15-min orderbook snapshots (launchd)
  tape.py               DuckDB tape access (books/trades/ensemble)
  falcon_client.py      historical orderbook API client (env: FALCON_API_TOKEN)
  metar_update.py       METAR archive maintenance
  ensemble_archive.py   forecast ensemble capture
  synoptic_fetch.py     private-station obs (discovers the public token at runtime)
  update_weather_sheet.py  workbook pipeline

research/   backtests, studies, post-mortems (the evidence for RECAP.md)
  lookahead_check.py        leak tests for every dataset
  rebuild_forecast_archive.py / apply_honest_forecast.py   point-in-time rebuild
  full_ecmwf_backtest.py, bot1_backtest.py, bot1_backtest_corrected.py,
  bot1_build.py, bot1_advection.py, bot1_early_leg.py, bot1_windveto.py,
  dawn_bot_oos.py, maker_backtest.py, maxbot_research.py, maxbot_settle.py,
  radiation_archive.py, radiation_edge_analysis.py, walkforward_radiation.py,
  d1_vs_archive_backtest.py, winter_backfill.py, egll_crosscheck.py,
  checkwx_vs_noaa.py, build_calibration.py, clean_sheet.py, ...

deploy/     bootstrap.sh + systemd units for a 2 GB droplet (watcher, field
            maker, book recorder)

data/       the collected data behind every number in RECAP.md
  observations/   METAR archives (EGLC/EGLL/EGWU), private-station CSVs
  forecasts/      point-in-time D-1 ECMWF archive, radiation backfills
  markets/        Polymarket market/token rosters (daily + winter 2025-26)
  live/           bot state and forward-test logs (dawn_watch_*.jsonl,
                  field_maker_*.jsonl, ...)

NOT in the repo (size): london/tape.duckdb (254 MB orderbook tape),
london/runs/ (477 MB), london/backups/ (83 MB). The tape is reproducible from
falcon_backfill.py + book_recorder.py.
```

## The signal, in pseudocode

```
at 03:00–07:30 UTC, poll EGLC METAR every 60 s:
    regime = classify(D-1 forecast diurnal range)      # ≥ 8 °C = radiation day
    if regime != radiation: stand down (or advection leg in May–Sep)
    low = EGLC's own pre-06Z observed minimum           # METAR only, no blends
    if the LOWEST bucket containing low is mispriced:  # ask < fair − costs
        buy it                                         # it has already happened
```

## Running

The code was written for one machine (the author's workspace) and assumes the
original directory layout for data paths. Quickstart equivalents:

```bash
python3 live/dawn_watcher.py            # dawn-window watcher (dry by default)
python3 live/field_maker.py             # paper MM on HIGHEST buckets
python3 research/lookahead_check.py     # the leak test every dataset must pass
```

Secrets are read from a local `.env` (never committed):
`FALCON_API_TOKEN=...` (orderbook API), `CHECKWX_API_KEY=...` (optional
METAR-latency benchmark). The Synoptic scraper needs no key — it discovers the
viewer's public token at runtime.

## Deploy

`deploy/` targets a 2 GB DigitalOcean droplet (~12 USD/month): `bootstrap.sh`
installs dependencies and systemd units/timers for dawn_watcher, field_maker,
and book_recorder. Sleep-proofing is the whole point — the dawn window is
03:00–07:30 UTC and laptops sleep.

## Honest limits

- Forward test is ~2 weeks of paper fills on a summer edge; winter behavior is
  the open problem (see RECAP.md §7).
- The dawn window is decaying: entries have drifted from ~0.53 mispricings
  toward 0.90+ "already repriced" buys.
- Backtests are bid/ask-corrected but capacity is real: ~50 dollars/day before
  market impact starts to bite.

*This repo is research record, not investment advice. Predicting weather markets
is mostly a discipline in not fooling yourself.*
