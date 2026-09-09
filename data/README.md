# Data Dictionary

Everything in `data/` is **public data collected point-in-time**, weather
observations (NOAA/aviationweather.gov METAR), forecasts (Open-Meteo, ECMWF
runs as published), and market data (Polymarket public CLOB/Gamma APIs, plus
one public wallet's fills). No accounts, no scraping behind logins, no PII, 
the only wallet identifier here is the public address of the market-maker
studied in the microstructure research.

Coverage: **2025-01-22 → 2026-09-04** (~20.5 months) for the daily series;
shorter where noted.

**The golden rule of this dataset:** every forecast row was saved *as published
on that day* (Open-Meteo Single Runs API, `run=` parameter). Nothing was taken
from sources that revise history. The `forecasts/lookahead_*` files document
what happens when you violate this rule, they are kept as the tombstone of
the project's first (invalidated) strategy.

---

## observations/ - what the weather actually did

| File | What it is | Source / collector |
|---|---|---|
| `metar_eglc.csv` | 29,130 half-hourly METAR reports for **London City Airport (EGLC)**, the signal station: `station, valid (UTC), tmpc (°C), wxcodes`. Jan 2025 → Sep 2026. | aviationweather.gov, via `live/metar_update.py` |
| `metar_egll.csv` | Same for **Heathrow (EGLL)**, cross-check station (28,320 rows) | same |
| `metar_egwu.csv` | Same for **Northolt (EGWU)**, second cross-check (6,119 rows) | same |
| `metar_eglc_wind.csv` | EGLC wind speed (`sknt`, knots) and direction (`drct`), input to the frontal-passage veto study | same |
| `AMB5847_air_temp.csv`, `WXM4398_air_temp.csv` | Two private weather stations (Synoptic network; WXM4398 at 51.473 N, 0.013 W), ~5-minute air temp. Rolling 7-day history cap, **why they couldn't be backtested**. Collected Aug 19 → Sep 4, 2026. | Synoptic public viewer token, discovered at runtime by `live/synoptic_fetch.py` (no key) |
| `egll_daily.csv` | Per-day max/min derived from EGLL obs (590 days) | derived |
| `bot1_regime_daily.csv` | **The regime table**, one row per day: forecast diurnal range, actual range, pre-06Z min, day min, and the `lock06` flag. This is the training/validation record for the ≥ 8 °C radiation-day gate that powers every strategy. | derived in `research/bot1_regime.py` |
| `london_weather_polymarket.csv` | Snapshot of live Polymarket London weather listings: question, outcomes, prices, volume, liquidity, CLOB token ids (155 events) | Polymarket public API |

## forecasts/ - what was knowable the day before

| File | What it is |
|---|---|
| `forecast_archive_d1.csv` | **The honest D-1 forecast**: ECMWF day-max and day-min for each date, taken from the forecast run published the day before. 592 days. This is the only forecast source used in any deployed number. |
| `forecast_archive_cache.json` | Raw cached API payloads keyed by date (provenance for the row above). |
| `lookahead_cache.json`, `lookahead_results.json`, `lookahead_verdict.json` | The audit that **killed the original strategy**: same-day comparison of the "historical forecast" archive vs the honest D-1 forecast, per date/direction, with MAE, the fraction of days the archive impossibly "beat" D-1, and a bootstrap CI on the contaminated backtest. Verdict: `lookahead_proven`. Kept as a negative result. |
| `forecast_rebuild_verdict.json` | Documentation of the rebuild: chosen source, coverage, how gaps were filled (only from staler-but-honest runs). |
| `full_backtest_results.json` | Post-rebuild backtest outputs comparing archive-based vs D-1-based strategies. |
| `radiation_forecast_d1.csv`, `radiation_actual.csv` | D-1 forecast vs actual cumulative shortwave radiation, validation of the radiation-vs-advection regime split. |
| `winter_ladder_results.json` | 70 rows of winter ladder-entry tests: signal, gap, ladder size, PnL per 1 dollar, average entry price. |

## markets/ - what was traded, and who won

| File | What it is |
|---|---|
| `event_roster.json` | Every London weather event slug by date: 575 days, Jan 2025 → Aug 2026. |
| `london_temp_markets.json` | 4,712 individual markets: `condition_id`, event slug/question, outcomes, CLOB token ids, volume, resolution. The market universe. |
| `london_events_summary.csv` | Per-day settlement rollup: number of markets, event volume in USD, **winning temperature band**, the ground truth every backtest scores against (576 rows). |
| `london_daily_history.json` | Per-day market history keyed by slug (february-21, march-7, …). |
| `lowest_slugs_apr_jul.json`, `lowest_tokens_roster.json` | The LOWEST-temperature market universe (968 slugs / 399 token mappings) used by the dawn-lock backtest. |
| `condition_yes_token.json` | condition-id → Yes-token id map. (The 77-digit numbers are Polymarket's ERC-1155 token ids, public identifiers, not secrets.) |
| `winter_markets_2025_26.json`, `winter_mkts_parsed.json` | Last winter's 1,085 markets with winning outcomes, the dataset for the winter candidate strategies. |

## live/ - what the bots actually did in forward time

| File | What it is |
|---|---|
| `dawn_watch_202608.jsonl`, `dawn_watch_202609.jsonl` | `dawn_watcher.py`'s decision log, one line per poll cycle: timestamp, source, hour, number of obs, **pre-06Z minimum, the call, latest reading, rising flag**. The forward-test record since 2026-08-28. |
| `dawn_state_*.json` | Per-day watcher state snapshots (early + dawn legs). |
| `lowband_archive.csv` | Dawn-lock calls vs settlement: station mins at 06Z, the call bucket, margin, D-1 forecast min, settled min, hit flags, the row-level source of the +764 dollars backtest and the live tracking. |
| `field_maker_202608.jsonl`, `field_maker_202609.jsonl` | Paper market maker on the HIGHEST book (3,400+ poll cycles): timestamp, running max, call, quotes, fills, harvest. |
| `field_maker_days.csv` | Per-day MM P&L. **Honest note: the first two quoted days are net negative (−96 and −136 dollars on paper)**, the kill rule (decide at 30 quoted days) is live. |
| `field_maker_state.json` | Current MM quotes/buckets. |
| `falcon_books_state.json`, `falcon_backfill_state.json` | Backfill progress for historical orderbook snapshots, keyed by CLOB token id (Falcon API, ~7-month rolling retention, why the own-tape recorder exists). |
| `jolly_skate_activity.json` | 1,955 public fills of the winning market-maker wallet studied in the microstructure research (+15.2k dollars over 6.5 months = spread capture, ~0.29 dollars/day/market). |
| `updater_state.json`, `last_run_report.json` | Workbook updater state and last run summary. |
| `winter_backfill_state.json` | Winter orderbook backfill progress. |

## Regenerating

- METAR archives: `python3 live/metar_update.py`
- Station CSVs: `python3 live/synoptic_fetch.py` (7-day cap, run it daily or lose history)
- D-1 forecast archive: `research/rebuild_forecast_archive.py` → `research/apply_honest_forecast.py`
- Market rosters: `research/backfill_trades.py` / the Gamma API scripts
- Orderbook tape: `live/book_recorder.py` (15-min snapshots → `tape.duckdb`, 254 MB, not in this repo)
- Leak test every new dataset must pass: `research/lookahead_check.py`

The large binary tape (`london/tape.duckdb`), the raw run outputs (`runs/`,
477 MB) and backups are reproducible from the above and are intentionally not
committed.
