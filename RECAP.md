# London Weather Markets — Full Research Recap

**Project:** systematic trading of Polymarket daily London temperature markets (daily HIGHEST and LOWEST temperature buckets), starting from a small bankroll (50 to 100 dollars per day per bot).

**Period covered:** 2026-08-26 to 2026-09-08 of live research, with backtests reaching back to April 2026.

**What follows is the complete record of what we tried, what we proved, what we disproved, and what we concluded.** Strategies are labeled: INVALIDATED (methodology error), CLOSED (studied and rejected), LIVE (running with real or paper fills), PAPER (forward-tested only), CANDIDATE (promising, not yet forward-tested).

---

## 1. TL;DR

| Question | Answer |
|---|---|
| Is there a real edge? | Yes — one. The **lowest-temp dawn-lock**: the day's minimum temperature is effectively knowable by 06:00 UTC, while the market keeps mispricing it for hours. |
| How strong? | Backtest: +764 dollars on 50 dollars/day over 72 trades (Apr–Jul 2026). Forward paper test: +207 dollars on 384 dollars risked, 2 bots, win rates 100% and 78%, max drawdown 61 dollars. |
| Does the max side (day's highest) have an edge? | **No.** 115-day point-in-time sweep: every hypothesis was already priced. The max market is negative-sum after fees; profit there is market-making plumbing, not prediction. |
| Does market making work? | Spread capture works for well-capitalized makers (~0.29 dollars/day/market per our best forensic case study). At our scale, maker rewards are too small and directional making loses to adverse selection. |
| Biggest methodological lesson | The first "profitable" strategy was a **look-ahead artifact**. It was discarded and every later number was rebuilt on strictly point-in-time data. |
| Ceiling | London alone: 600 to 1,300 dollars/month. Same playbook on ~3 cities: 2,000 to 4,000 dollars/month. The dawn window is decaying as the market gets sharper. |

---

## 2. How it started: a lesson in humility (INVALIDATED)

The first strategy for London daily temperature markets looked great in backtest — and was garbage.

**2026-08-26: look-ahead bias proven.** The original pipeline used forecast data that was not available at decision time (the "historical forecast" archive serves *revised later runs*, not the forecast as it looked on the day). Every pre-08-26 number was contaminated.

Two permanent fixes came out of this:

1. **Honest forecast source.** All decision-time forecasts come from the Open-Meteo **Single Runs API** (`run=` parameter, `ecmwf_ifs` model), which returns the forecast exactly as published on that run. The historical-forecast API is banned for backtesting.
2. **Point-in-time discipline.** A D-1 forecast archive was rebuilt (`rebuild_forecast_archive.py`, `apply_honest_forecast.py`), and every subsequent backtest loads only what was knowable at decision time (`lookahead_check.py` exists specifically to test for leakage).

**Rule adopted: never deploy (or even quote) a number that is not point-in-time clean.**

---

## 3. The structure of the market: regimes (validated)

Daily London temperature is not one market — it is two regimes:

- **Radiation-dominated days** (clear skies, large diurnal range): the day's trajectory locks in early and is highly predictable from the overnight trend.
- **Advection-dominated days** (clouds/warm front, small diurnal range): temperature is driven by moving air masses, less predictable intraday but with its own tradable structure in summer.

**Regime classifier** (`bot1_regime.py`): keyed on the D-1 forecast diurnal range. Range ≥ 8 °C → the daily minimum locks at 92.4%; range < 8 °C → only 64.5%. This single gate became the core veto of every later strategy (veto stack band 0.63–0.97).

**Rain veto is redundant.** A 585-day study showed rain adds no information once the range gate is applied — rainy days are almost always low-range days. The rain veto was **removed** from the live bots.

**Warm-advection leg:** range < 8 °C days are tradeable May–Sep (94% lock, n = 50) and were wired in as a second leg (`bot1_advection.py`, leg `dawn-adv`). **November–March this leg is dark** (54% lock) — see the winter section.

**LCY microclimate:** a study of London City Airport (EGLC) vs Heathrow-area stations found the microclimate effect is a *bounded static offset* — the dynamics (timing of the min, locking behavior) dominate, so we trade the same structure everywhere and don't need per-station models.

---

## 4. The core edge: the dawn-lock (LIVE)

**The insight.** Polymarket runs daily LOWEST-temperature bucket markets. By ~06:00 UTC on a radiation day, the pre-dawn minimum observed at the airport (METAR) already tells you which bucket will win — but the market is still asleep and mispriced. You are not predicting weather; you are **buying already-determined outcomes from counterparties who haven't updated**.

**Backtest** (`lowband_signal.py` era, Apr–Jul 2026): 06Z dawn-locked entries, 72 trades at 50 dollars/day → **+764 dollars**. Summer-only; winter untested at that point.

**Refinements found live:**

- **Bot 1 signal is METAR-only** (`bot1-metar-only` finding): the call is EGLC's own pre-06Z minimum, 9/9 correct in the live sample — *not* the blend of private stations we originally built.
- **Market reprices fast but late enough**: the winning bucket is buyable at 0.90–0.97 after the METAR prints; median spread 4 cents. Backtest at 100 dollars/trade with bid/ask costs: **+206 dollars over 17 trades**.
- **A second leg at 03Z**: on radiation days, buying one degree below the pre-03Z leader won 80% at an average price of ~0.53 (+53% ROI) — but capacity-capped at ~50 dollars/day. A small second leg, not a new edge.
- **Wind marker** (`bot1_wind.py`): pre-dawn wind shifts / falling speed at EGLC correlate with frontal passage — kept as an *exploratory* veto, not a rule.

**Live result** (`dawn_watcher.py`, live since 2026-08-28, paper fills, 60-second polling in the 03:00–07:30Z window):

| Bot | Trades | Win rate | P&L |
|---|---|---|---|
| Bot 1 (dawn-lock, METAR-only) | 17 | 17/17 | +103 dollars |
| Bot 2 (advection leg + variants) | — | 78% | +104 dollars |
| **Combined** | — | — | **+207 dollars on 384 dollars risked, max DD 61 dollars** |

Only one day did the two bots disagree — they are the same edge expressed twice, which is why scaling means sizing Bot 1 up, not diversifying.

---

## 5. The max side: studied to death, closed (CLOSED)

The symmetric idea — trade the daily HIGHEST market the same way — was killed by evidence, repeatedly:

- **115-day point-in-time sweep** (`maxbot_research.py`): every max-side hypothesis (station leads, radiation floors, forecast deltas) was *already priced* by the time it was observable. The structural reason: **the min locks while the market is asleep (06Z); the max forms while the market is awake (12–15Z)**. Asymmetry kills the mirror strategy.
- **Fading a near-certain max at 0.99 ask is negative EV** — rejected.
- **Station "leads" on the max are sun contamination**: private stations read 2–4 °C hot in daytime (radiative error on the shield), so an apparent lead is a bias, not information. (This same bias is why private stations lost the min-side signal job to METAR.)
- **Who actually profits in the max book**: it is negative-sum (−71,000 dollars of fees paid over 115 days). The profitable wallets (~15 of them, +24,000 dollars net) are makers who are **net short the field** — they earn the spread, they don't predict weather. Money = plumbing, not prediction.
- A 25-minute station-lead idea for early entry was **closed** too: EV-negative until 06Z because of the same sun bias.

**Conclusion: the max branch is closed permanently. Don't rebuild it.** (A 2026-08-31 audit also fixed 3 tape-recording bugs; after the fix the overnight max book prices as FAIR.)

---

## 6. Market making: what we learned (mixed — mostly no)

- **Field spread is ~17 cents** and there is no riskless cross-bucket arb.
- **Making the dawn-lock loses** (`maker_backtest.py`): your bids only fill when the lock *breaks* — textbook adverse selection. Short-the-field making nets ≈ 0.
- **Maker rewards are tiny at our scale**: the best forensic case study (a real winning weather MM wallet, "+Jolly-Skate") made +15,200 dollars over 6.5 months across ~290 markets — that is spread capture averaging **0.29 dollars per market per day**, and the edge *peaked in June and decayed after*. Maker reward pools in these markets are too small to matter at 50–100 dollar scale.
- Two live paper market makers were built to test this in forward time: `field_maker.py` (±0.02 quotes on all HIGHEST buckets, running since 08-31) and `dawn_maker.py` (two-sided reward-quote maker with regime tilt; open risk: the `rewardsDailyRate` units are unverified). Decision rule: decide at 30 quoted days, kill if net negative.

**Conclusion: at our bankroll, we are takers. The edge is selection, not quoting.**

---

## 7. Winter: the open problem (CANDIDATE)

Everything above is a warm-season edge. Winter is genuinely different:

- The warm-advection leg is dark Nov–Mar (54% lock).
- Winter-max dawn-lock: **closed** (same asymmetry as summer max).
- **Winter-MIN conditioner is the main candidate**: 77% historical win rate (n = 48), paper only. 
- A flat-morning variant (n = 22) is paper-only.
- Data risk: the Falcon orderbook archive rolls at ~7 months retention — **October–January winter market depth will be gone** before we can backtest it properly. Recording our own tape through this winter is therefore not optional.

---

## 8. Infrastructure (what had to exist first)

- **The tape** (`tape.py`, `london/tape.duckdb`): books, trades, and forecast ensembles in one DuckDB; `book_recorder.py` snapshots orderbooks every 15 minutes via launchd (both directions, Yes-token only, rolling 15-day retention). This is the project's most valuable *asset* — every microstructure study above runs on it.
- **Falcon API** for historical orderbooks (agent 572 = snapshots; millisecond timestamps required; ~7-month rolling window).
- **Observations**: EGLC/EGLL/EGWU METAR archives (`metar_*.csv`), two private stations via Synoptic (AMB5847 and WXM4398, scraped daily, 7-day history cap) — kept for cross-checking, demoted from signal duty due to sun bias.
- **Forecasts**: Open-Meteo ECMWF point-in-time archive + ensemble spread (`forecast_archive_d1.csv`, `forecast_archive_cache.json`).
- **Deployment kit** (`deploy/`): bootstrap + systemd units for a 2 GB DigitalOcean droplet (12 dollars/month) so the dawn window survives Mac sleep. Live trading would use a fresh key on the server only.

---

## 9. The verdict table

| Strategy / idea | Status | Headline number |
|---|---|---|
| Original temp strategy | INVALIDATED (look-ahead) | numbers withdrawn |
| Regime gate (range ≥ 8 °C) | VALIDATED, live | 92.4% vs 64.5% lock |
| Rain veto | CLOSED (redundant) | explained by range gate |
| Dawn-lock, lowest-temp (Bot 1) | LIVE | +764 backtest; 17/17 forward |
| Advection leg (Bot 2) | LIVE, seasonal | 94% lock May–Sep; dark Nov–Mar |
| 03Z leader−1 entry | LIVE, small | 80% @ ~0.53, capacity-capped |
| Wind frontal veto | EXPLORATORY | not a rule yet |
| Max-side mirror strategy | CLOSED | nothing left to test |
| Max-side fade @ 0.99 | CLOSED | negative EV |
| Private-station leads | CLOSED | sun bias, not signal |
| Min-side market making | CLOSED | adverse selection |
| Field making (max book) | PAPER (decide at 30 days) | ±0.02 quotes; **first days net negative (−96, −136 dollars)** |
| Dawn rewards maker | PAPER | reward units unverified |
| Winter-MIN conditioner | CANDIDATE | 77% (n = 48) |
| Winter flat-morning | PAPER | n = 22 |
| US city expansion | MAPPED, not started | NYC/CHI/MIA/LA/SF dailies |

---

## 10. Conclusions and roadmap

1. **There is exactly one durable edge**, and it is settlement-latency shaped: the market under-reacts to a minimum that has already happened. Every predictive (weather-forecast-alpha) angle we tested was either already priced or a data artifact.
2. **The way to grow is execution + breadth, not new signals**: sub-minute reaction to METAR prints, maker bids at 03Z on both books, then replicate the whole playbook on other cities (the US dailies: NYC, Chicago, Miami, LA, SF — same HIGHEST structure; winter = LOWEST + snowfall markets).
3. **The window is decaying.** Maker edges peaked in June; our own dawn-lock prices have tightened from 0.53-style mispricings toward 0.90+ buys. London alone caps at roughly 600 to 1,300 dollars/month; breadth is where the 2,000 to 4,000 dollars/month case lives.
4. **Survivorship of the research process matters more than any single strategy**: the look-ahead kill, the point-in-time rebuild, and the willingness to close the max branch after 115 days of negative results are the actual alpha-preservation mechanism.

## 11. Hard lessons (methodology)

1. **If the data source can be revised, it will be** — forecast archives, orderbook retention, station history. Rebuild from raw point-in-time sources or lie to yourself.
2. **Free forecasts are honest only via the Single Runs API.** Everything labeled "historical forecast" mixes post-hoc revisions into the past.
3. **Asymmetric information ≠ asymmetric opportunity**: the min side works because the info arrives while the market sleeps. Check *when* the market can react, not just *whether* you know something.
4. **Biases masquerade as leads**: sun-contaminated private stations produced a fake 25-minute edge. A "lead" that only exists in daytime is a thermometer problem, not alpha.
5. **Backtest fills are fiction until bid/ask-corrected** — our +764 became +206 at 100 dollars/trade once the 4-cent spread was charged honestly.
6. **Negative-sum books can still host winners** — but they are makers harvesting spreads, not forecasters. Know which one you are.
