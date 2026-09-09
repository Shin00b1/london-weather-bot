# Chasing a weather edge: one summer, one real edge, and a lot of dead ends

> A blog post that also happens to be this repo's README. The whole record (code,
> data, backtests, the mistakes) is committed here. I hope it saves someone else
> a few months.

## TL;DR

- **One real edge survived the whole summer:** the London lowest-temperature **dawn-lock**. The day's minimum is decided by ~06:00 UTC, while the market keeps mispricing it for hours. You're buying an already-decided outcome, not predicting weather.
- **Backtest: 18 wins / 18 trades, +$218** at $100/trade (Apr 15 - Sep 8). Out-of-sample: **82% win rate, +$103**. Trust the 82%, not the 100%.
- **The max side (day's highest) is dead.** The high forms while the market is awake, so there's no window where you know the answer and the market doesn't.
- **Market making loses at my size.** The profitable players are better-capitalized makers, not forecasters. At $50-100 I'm a fast *taker*.
- **The edge is real but decaying:** 7 trades in April, 1 in August, 1 in September. Growth is execution speed + more cities, not a better signal.
- **Ceiling:** ~$600-1,300/month London, ~$2,000-4,000/month across ~3 cities.

## Where it landed (the recap)

I spent most of the summer trying to beat Polymarket's daily London
temperature markets. Here is the honest bottom line:

| | Result |
|---|---|
| The one real edge | The **dawn-lock**: the day's minimum is effectively decided by 06:00 UTC, while the market keeps mispricing it for hours. You are not predicting weather; you are buying an outcome that has already happened. |
| Backtest (radiation days) | **18 wins / 18 trades, +$218** at $100/trade, Apr 15 - Sep 8 |
| Out-of-sample walk-forward | **9 wins / 11 trades (82%), +$103** on $50/trade. This is the number to trust, because it was computed on days the backtest never saw |
| The max side (day's highest) | **No edge. Closed.** The maximum forms while the market is awake, so there is no window where you know the answer and it doesn't. |
| Market making | **Loses at my size.** Spread capture is real but belongs to better-capitalized players. |
| The ceiling | London alone: roughly **$600-1,300/month**. The same playbook on ~3 cities: **$2,000-4,000/month**. |

The single most important sentence in this whole post: **the edge is real, but
it is shrinking.** My first profitable backtest had 7 trades in April; the same
bot found 1 in August and 1 in September. The win rate never changed. The market
just got faster. The moat is now execution and breadth, not a better signal.

Everything below is the story of how I got to those numbers, including the part
where I almost published a fake one.

---

## How it started: a Reddit post

This summer I came across a post on Reddit about someone making money on
Polymarket's weather markets: daily markets on the highest and lowest
temperature in a given city. The idea was instantly appealing: weather is
public, abundant, and I already had a background in reading data. If there was
any market where a small, fast, systematic player could find an edge, this felt
like it.

So I did what you do: I scraped a year of METAR observations, pulled every
market and trade Polymarket would give me, and started hunting for something
that beat the market. Within a week I had a backtest that looked fantastic. I
was, in my head, already printing money.

That first backtest was garbage. And I almost didn't catch it.

## The mistake that nearly poisoned everything

My first "profitable" strategy leaned on a forecast data source that looked
clean but wasn't. When I tested it properly, I found the problem: the
"historical forecast" archive I was using didn't serve the forecast *as it
looked on the day of the trade*. It served *later, revised runs*, the version
of the forecast that already knew the answer.

That is called look-ahead bias, and it is the classic way a backtest lies to
you. My bot was quietly trading with tomorrow's information and reporting it as
alpha. I caught it, killed the strategy, and deleted every number that had come
out of it. That part hurt more than it should have, because I'd already started
telling myself the story that I'd found it.

Two rules came out of that and never left:

1. **If a data source can be revised, it will be.** Only point-in-time data
   (exactly what you could have known at the moment you decided) is allowed near
   a backtest.
2. **Never quote a number that isn't leak-tested.** There is a script in this
   repo (`lookahead_check.py`) whose only job is to catch this exact failure.

If you take nothing else from this post: your backtest is guilty until proven
innocent.

## The edge that survived

After the rebuild, almost everything I tried failed. What survived was one
asymmetric fact about how temperature works and how these markets are priced.

On a clear night (what meteorologists call a radiation-dominated day), the
temperature bottoms out before dawn, typically by 05:00 or 06:00 UTC. That
means the day's **minimum** is already physically determined by early morning.
But Polymarket's lowest-temperature market keeps quoting the winning bucket
below its true value for *hours* afterward, because nobody is sitting there
updating the book at 5 a.m.

That is the whole edge, and it's why it works on one side and not the other:

- **The minimum locks while the market sleeps.** You can buy an already-decided
  outcome from counterparties who haven't noticed yet.
- **The maximum forms while the market is awake.** The daytime peak happens
  around 12:00-15:00 UTC, when everyone is watching and the book is already
  sharp. There is no equivalent window on the high side.

I spent a long time trying to make the highest-temperature market work the same
way: station leads, solar-radiation floors, forecast deltas, fading near-certain
winners. Every one of them was already priced. The high side is genuinely
negative-sum: $71,000 of fees were paid across 115 days, and the only people
who netted positive were a handful of market makers harvesting the spread, not
forecasting anything. I closed that branch and stopped looking for reasons to
reopen it.

## What the bot actually does

The market is simple: each day Polymarket lists a market for London's highest
temperature and one for its lowest, each split into whole-degree buckets (12C,
13C, 14C, and so on). You buy the bucket you think wins, and it pays out $1 if
it does. Settlement is the daily minimum (or maximum) at London City Airport
(EGLC), rounded to a whole degree, from the public METAR reports.

The bot trades only the lowest side, and only inside a narrow window:

```
poll the EGLC METAR feed every 60 seconds, 03:00 to 07:30 UTC
  1. classify the day from the D-1 forecast diurnal range:
       radiation (range >= 8C)  -> trade the dawn-lock
       advection (range < 8C)   -> stand down (winter) or use the warm-season leg
  2. on a radiation day, the lowest temperature the station printed before
     06:00 UTC is the call. It names the winning LOWEST bucket ~92% of the time.
  3. if that bucket's ask is cheap enough (between 0.63 and 0.97, and at least
     0.2C clear of a bucket edge), buy it. That is the whole trade.
  4. a second, smaller leg at 03:00 UTC buys the bucket one degree below the
     current leader, betting the minimum is still falling (~82% hit rate).
```

The veto stack is what turns a naive bet into the edge: the regime gate
(radiation vs advection), a rain veto (wet dawns break the lock far more often),
an edge skip (no calls within 0.2C of a bucket boundary), a price band (the
tails are already priced correctly), and an independent settlement cross-check
against Heathrow. On most days the bot does nothing. On the days it fires, it is
not predicting weather. It is buying an outcome that has already happened.

## Methodology: how this was actually achieved

The numbers above come out of a specific pipeline, and the pipeline matters more
than any single result.

**Data.** Observations are METAR reports from aviationweather.gov (Iowa
Mesonet archive), one row every ~20 minutes per station. Forecasts come from
Open-Meteo's Single Runs API, which returns the ECMWF run exactly as it was
published on a given day, no later revisions. Market data comes from Polymarket's
public Gamma and CLOB APIs, and my own recorder writes an orderbook snapshot to
a DuckDB tape every 15 minutes so the microstructure studies run on real books,
not reconstructed ones.

**The point-in-time rule.** Every forecast is stored as it looked on decision
day. There is a script (`lookahead_check.py`) whose entire job is to verify a
dataset is leak-free before any number is allowed near a result. The first
strategy failed this test, which is why the repo leads with it.

**Honest backtesting.** Entries are priced at the ask, not the all-trades
average, because a buyer can only cross the ask; naive VWAP overstates the fill.
Costs and capacity are real: the 03Z leg is capped at $50 because the book only
holds ~$42 of depth, and the headline number moved a lot once the spread was
charged honestly.

**The regime classifier.** The one signal that survived is keyed on the D-1
forecast diurnal range (day max minus day min). Above 8C the day is
radiation-dominated and the minimum locks by dawn 92.4% of the time; below it
the day is advection-dominated and the lock rate collapses. This single gate
splits every day into tradeable and not, and it is what keeps the win rate high
while the trade count stays honest.

**Validation chain.** Nothing is quoted as edge until it has passed, in order: a
backtest on point-in-time data, a bid/ask-corrected re-run, an out-of-sample
walk-forward on a window the rules never saw, and finally a live paper bot
logging real fill prices. The 18/18 is the corrected backtest. The 82% is the
out-of-sample number. The paper bot is the thing still running.

## What the honest numbers actually are

This is the part I most want to get right, because it's where people fool
themselves (I did).

The **dawn-lock bot** buys the bucket the pre-dawn METAR already names, on
radiation days only, and backtests at **18 for 18, +$218** on $100/trade from
April through September 8. That number is bid/ask-corrected: I re-priced every
entry at the ask, because a buyer can only cross the ask, and naive all-trades
VWAP systematically overstates what you'd actually pay.

But 18-for-18 is a *filtered* backtest. The honest forward estimate comes from
walking the same rules over a window the model never saw, which scored **9 for
11 (82%), +$103** at $50/trade. That 82% is the number I believe. The 100% is
what happens when the regime filter removes all the hard days before they can
count against you.

A second, earlier leg (buying the bucket one degree *below* the leader at
03:00 UTC, while the min is still falling) wins about 82% at an average price
near 0.53, versus 0.90-plus for the settled dawn-lock. It backtests at +$397 on
$697 staked (57% ROI), but that ROI is inflated by a $50 cap: the 03Z order book
only holds about $42 of resting depth, so it's an opportunistic add-on, not a
real second bet.

And there's a genuinely *new* candidate I'm still testing: the regime gate
currently stands down on every low-diurnal-range day, but warm-season advection
days actually lock ~79% of the time (94% if dry and the pre-dawn min is at or
below the forecast min). Trading them roughly doubles the dawn-lock's P&L in
backtest, **provided the rain veto stays on**, because a wet September 8 was the
first day it broke. That one is promising but not yet proven forward.

## The thing nobody tells you about edges: they decay

The win rate held. The trade count didn't.

In April my dawn-lock found 7 trades. August: 1. September: 1. The market is
learning to reprice the dawn print faster. The buyable price has drifted from
~0.53 mispricings toward 0.90-plus "already repriced" fills. The edge isn't
dying because the physics changed; it's dying because the counterparties got
smarter.

That reframed the whole project. The path to actually making money is no longer
"find a better signal." It's **execution** (react to the METAR print in seconds,
not hours; rest maker bids instead of lifting the ask; size to measured depth)
and **breadth** (the same market exists for New York, Chicago, Miami: same bot,
new station). The signal is just the entry ticket. The speed is the business.

## What I'd tell someone starting out

1. **Assume your first backtest is lying to you.** Look-ahead bias is the most
   common way to fool yourself, and it produces the most beautiful, most
   confident, most wrong numbers. Leak-test everything.
2. **Ask *when* the market can react, not just whether you know something.** My
   edge works because the information arrives while the book is asleep. The
   mirror image fails because the information arrives while it's awake. Timing
   of information, not information itself, is the whole game.
3. **Pay real costs in the backtest.** A buyer crosses the ask. Charge the
   spread honestly and re-check; my headline number moved a lot when I did.
4. **Negative results are assets, not embarrassment.** The max side, the
   station leads, the rain veto, the maker. Each was studied, quantified, and
   closed with receipts. That discipline is what lets the one real number keep
   meaning something.
5. **Know which game you're in.** In a negative-sum book, the winners are makers
   harvesting spread, not forecasters. At a $50-100 bankroll, I'm a taker. The
   edge is selection, not quoting.

---

## Repository map

| Path | Contents |
|---|---|
| [RECAP.md](RECAP.md) | The full research narrative: every strategy tried, the verdict table, the methodology lessons |
| [data/](data/README.md) | Collected data (METAR, point-in-time forecasts, market records, bot logs) with a data dictionary |
| `live/` | The running stack: `dawn_watcher.py` (the edge), regime classifier, paper market makers, tape recorder, station scrapers |
| `research/` | The backtest and study scripts: the evidence behind every claim, including the leak tests |
| `deploy/` | systemd kit for a $12/month droplet (the dawn window doesn't survive laptop sleep) |

Not committed (reproducible, 800+ MB): the orderbook tape (`tape.duckdb`) and
raw run outputs. The recorder scripts that rebuild them are in `live/`.

## Reproducing

```bash
python3 research/lookahead_check.py        # the leak test every dataset must pass
python3 live/dawn_watcher.py               # dawn-window watcher (dry by default)
python3 live/field_maker.py                # paper MM on the HIGHEST book
```

## Honest limits

- The forward paper test is still thin: a handful of dawns, and the last two
  radiation days of the summer were slept through. Winter is genuinely
  different and stands down.
- All figures are my own research on prediction markets. Nothing here is
  investment advice, and the edge it describes is decaying as you read this.

---

*One person, one summer, one real edge, and a discipline of killing bad ideas
that ended up mattering more than any single trade.*
