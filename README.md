# Opening Forecast Pipeline

A local, end-to-end research pipeline for the **CME equity-index futures opening**:
it collects 1-minute bars from Interactive Brokers, freezes a pre-open feature snapshot
at 09:30 ET, finds volatility-normalized historical analogues, asks an LLM (or a
deterministic baseline) for an opening forecast, scores that forecast against what
actually happened, and serves the whole thing in a NiceGUI dashboard drawn with
TradingView's Lightweight Charts.

Ten instruments are collected out of the box:

| Symbol | Instrument | IB type | Role |
|---|---|---|---|
| `ES` | S&P 500 E-mini | FUT, rolls quarterly | forecast target |
| `NQ` | Nasdaq-100 E-mini | FUT, rolls quarterly | forecast target |
| `RTY` | Russell 2000 E-mini | FUT, rolls quarterly | forecast target |
| `VIX` | Cboe Volatility Index (spot) | IND | intermarket context |
| `VXN` | Cboe Nasdaq-100 Volatility Index (spot) | IND | intermarket context |
| `TNX` | Cboe 10-Year Treasury Yield Index (yield × 10) | IND | intermarket context |
| `DX` | US Dollar Index future (ICE) — DXY proxy | FUT, rolls quarterly | intermarket context |
| `SMH` | VanEck Semiconductor ETF | STK | intermarket context |
| `10Y` | Micro 10-Year Yield future (quoted in yield) | FUT, rolls monthly | intermarket context |
| `2YY` | Micro 2-Year Yield future (quoted in yield) | FUT, rolls monthly | intermarket context |

`GC` (gold) and `CL` (WTI crude) are defined too but only collected once added to
`CONTEXT_SYMBOLS`. See [Intermarket sources](#intermarket-sources) for how each logical
asset maps onto these.

The three futures share the same RTH window, the same 18:00 ET Globex roll and the same
holiday calendar, so the session and coverage logic is identical for each. Each is
forecast **independently** — analogues for ES are drawn only from past ES sessions, never
across instruments. `SYMBOLS` selects which ones run.

**Context instruments are never forecast.** VIX, for instance, is the cash index rather
than a future: no expiry, no volume, `sec_type = 'IND'`. It is collected like anything
else, but instead of being forecast it contributes the pre-open VIX level and its change from the prior close to
*every* futures snapshot (`vix_pre_open`, `vix_change`), and those reach the model in the
prompt. `CONTEXT_SYMBOLS` controls this set. Because nothing on the Session Explorer page
applies to a context instrument, they are left out of its contract picker.

If VIX has not been collected, the columns stay NULL and the prompt simply omits the line —
a missing feed never blocks a forecast or reports a stale level.

Everything runs on your machine against a local PostgreSQL + TimescaleDB database. No cloud.

```
IB Gateway/TWS ──▶ collector ──▶ TimescaleDB ──▶ features ──▶ matching ──▶ forecaster ──▶ TimescaleDB
                                    │                                                  │
                                    └──────────────── NiceGUI dashboard ◀─────────────┘
```

## Quick start

```bash
cd trading-pipeline
source .venv/bin/activate
python -m dashboard.app        # then open http://127.0.0.1:8080
```

That serves the dashboard against whatever is already in the `trading_pipeline` database
(`DATABASE_URL`).
It never needs IB to be running — it only reads stored data. `DASHBOARD_PORT` and
`DASHBOARD_HOST` override where it listens.

**No data yet?** Seed a throwaway database with realistic fake sessions for all three
instruments (each scaled to its own index level):

```bash
python populate_mock_data.py --reset          # writes the trading_pipeline_dev database
DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_dev python -m dashboard.app
```

### First-time setup

```bash
git clone https://github.com/MonkeyPlay/trading-pipeline.git
cd trading-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d          # local TimescaleDB on 127.0.0.1:5432 (see docker-compose.yml)
```

The compose file creates both the `trading_pipeline` and `trading_pipeline_dev`
databases with the credentials `config.py` defaults to. Any PostgreSQL 14+ server with
the TimescaleDB extension works — point `DATABASE_URL` at it. The schema is created and
migrated automatically on first use — there is no separate init step.

#### Database server settings

`docker-compose.yml` reads these from the environment or `.env`:

| Variable | Default | What it does |
|---|---|---|
| `POSTGRES_PASSWORD` | `trading` | Password for the `trading` role — change it, and match `DATABASE_URL` |
| `POSTGRES_BIND` | `127.0.0.1` | Interface the DB listens on; `0.0.0.0` if the pipeline runs on another machine (firewall it) |
| `TS_TUNE_MEMORY` | `64GB` | RAM `timescaledb-tune` sizes Postgres for |

With 64 GB the tuner sets `shared_buffers=16GB`, `effective_cache_size=48GB`,
`work_mem=128MB`, `maintenance_work_mem=2GB` and SSD-friendly planner costs. Tuning is
written **once, when the data volume is created**; after changing `TS_TUNE_MEMORY` on an
existing volume, re-run it in place and restart:

```bash
docker compose exec timescaledb timescaledb-tune --memory=64GB --yes --quiet \
    --conf-path=/var/lib/postgresql/data/postgresql.conf
docker compose restart timescaledb
```

`TS_TUNE_MEMORY` must not exceed the RAM Docker can actually give the container
(Docker Desktop's VM limit, if you use it) or Postgres will refuse to start.

**Coming from the SQLite version?** Copy the old file in once (read-only on the SQLite
side, refuses a target that already holds data):

```bash
python -m database.migrate_from_sqlite --sqlite data/trading_pipeline.db
```

## The three things you can run

### 1. Dashboard — look at data and forecasts

```bash
python -m dashboard.app
```

Two pages:

- **Session Explorer** (`/`, [dashboard/views/candles.py](dashboard/views/candles.py)) — pick a
  contract and trading day, plot the candles with indicator overlays (Auto Anchored VWAP,
  TEMA & Session Levels), and run a forecast for that session on demand. The indicator
  settings live in the right-hand drawer behind the **Indicators** button.
- **Evaluation** (`/evaluation`, [dashboard/views/evaluation.py](dashboard/views/evaluation.py)) —
  every stored prediction joined against its realized outcome, so you can see whether the
  bias calls were right.

### 2. Collector — pull fresh bars from IB

Requires IB Gateway or TWS running locally (port `4002` = Gateway paper by default).

```bash
python -m collector.ib_collector --days 30 --plan-only            # what would it fetch?
python -m collector.ib_collector --days 30                        # fill the gaps, all symbols
python -m collector.ib_collector --days 30 --symbol NQ            # just one
python -m collector.ib_collector --start 2026-08-01 --end 2026-08-31
python -m collector.ib_collector --days 30 --full                 # re-download everything
```

With no `--symbol` it collects everything in `SYMBOLS` and `CONTEXT_SYMBOLS`.

**Futures follow their front contract.** Each future has a roll rule in `config.py`
(equity indices: the quarterly contract, rolling 8 days before expiry). The collector asks
IB once for the whole contract chain, expired contracts included, and fetches every
trading day from the contract that was front *on that day*. It also stores the
`ROLL_WARMUP_SESSIONS` (7) trading days before each contract becomes active - collected
as they happen, ahead of the roll - so the first day after a roll has a same-contract
previous close and enough same-contract history for intraday indicators (the v2 5-minute
EMA200 needs about four sessions). The choice is recorded per day in `active_contracts`.
`--expiry 202609` (or `NQ_EXPIRY=202609` for one symbol) pins a single contract instead.

**Backfill before relying on standardized features.** The z-scored intermarket features
need the prior 60 same-window returns, i.e. about three months of history for every
context instrument, and the v2 daily ATRs need more (A: 71 RTH sessions, ATR63: 316 -
see [docs/forecast_contract_v2.md](docs/forecast_contract_v2.md)). Once:

```bash
python -m collector.ib_collector --days 100      # ~70 trading days, across the last roll
python -m collector.ib_collector --days 460      # everything nq_features_v2 can use
```

That is roughly 70 requests per instrument; the pacer keeps it under IB's limit (60
requests / 10 min), so ten instruments take a couple of hours. Later runs only fetch the
new and trailing days.

**All symbols go through one IB connection**, which matters: the request pacer that keeps
you under IB's historical-data rate limit lives on that connection. Running one process
per symbol would give each its own pacer, so each would undercount the others' requests
and a wide backfill could trip the limit.

Collection is **incremental and day-partitioned**: the collector reads the `session_days`
ledger first and only opens a socket for days it actually needs. If nothing is missing for
any symbol it exits without connecting at all. See [docs/data_store.md](docs/data_store.md)
for the full model — day statuses, the atomic per-day write path, and the
trailing-revision window.

### 3. Daily forecast — features, analogues, prediction, scoring

```bash
python scripts/daily_forecast.py --lookback-days 40        # every symbol in SYMBOLS
python scripts/daily_forecast.py --symbol RTY              # just one
```

For each symbol in turn ([scripts/daily_forecast.py](scripts/daily_forecast.py)) it:

1. loads recent 1-minute bars,
2. backfills realized `outcomes` for every session that has already closed,
3. computes the target session's pre-open snapshot → `feature_snapshots`,
4. finds the top-5 analogue sessions,
5. asks the forecaster for an opening bias + scenarios → `predictions`,
6. stores which historical days it leaned on → `analogue_matches`.

### Both together

[scripts/run_pipeline.sh](scripts/run_pipeline.sh) runs the collector then the forecast,
logging to `logs/pipeline_run.log`. It is meant for cron, shortly before the 09:30 ET open:

```cron
15 9 * * 1-5  /path/to/trading-pipeline/scripts/run_pipeline.sh
0  2 * * *    /path/to/trading-pipeline/scripts/backup_db.sh
```

Both scripts resolve the project directory from their own location, so they can be
invoked by absolute path from anywhere without a `cd` first.

Those cron times are in the machine's local timezone — 09:15 ET is 13:15 UTC (14:15 UTC
during EST), so adjust if the box is not on New York time.

**The intermarket features read the bars ending at 09:29 ET** (the v2 cutoff T), which a
09:15 run has not seen yet, and which must be stored before 09:30. Re-downloading days
cannot do that for ten instruments, so a **real-time streamer** keeps one IB
keep-up-to-date 1-minute stream per instrument open and stores each minute the moment it
is final, with the time it was received:

```cron
0  9 * * 1-5  cd /path/to/trading-pipeline && .venv/bin/python -m collector.live_stream >> logs/live_stream.log 2>&1
29 9 * * 1-5  cd /path/to/trading-pipeline && .venv/bin/python scripts/nq_forecast_v2.py live >> logs/pipeline_run.log 2>&1
```

The streamer ([collector/live_stream.py](collector/live_stream.py)) runs until `--until`
(09:31 ET by default), re-reads the 09:28 minute of every instrument right after it ends
(`--confirm-at`) so P is IB's own historical bar, and uses client id `IB_CLIENT_ID + 1`
so the historical collector can run beside it. Each bar goes to `bars` (as not yet
completed, so the regular collector re-downloads the day later) and, append-only, to
`bar_receipts` with its `received_at` - the per-bar point-in-time proof a `verified`
live snapshot cites. `nq_forecast_v2.py live` waits (until 09:29:45) for the confirmed
NQ and ES 09:28 bars, then freezes and forecasts before 09:30.

Collecting after the open adds no look-ahead: features select bars by timestamp
(`get_last_bar_at_or_before`), never by what happens to be stored.

It covers every symbol in `SYMBOLS`, and hands the whole list to each step in one process
so the collector's rate-limit pacing stays accurate and one instrument's missing data does
not suppress the others' forecasts.

## Configuration

Settings come from environment variables or a local `.env`, read by
[config.py](config.py). All have defaults; none are required.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `postgresql://trading:trading@localhost:5432/trading_pipeline` | Production store — collector and pipeline write here |
| `DEV_DATABASE_URL` | `postgresql://trading:trading@localhost:5432/trading_pipeline_dev` | Throwaway store for `populate_mock_data.py` |
| `SYMBOLS` | `ES,NQ,RTY` | Instruments that are forecast, in order |
| `CONTEXT_SYMBOLS` | `VIX,VXN,TNX,DX,SMH,10Y,2YY` | Collected as intermarket context only; never forecast |
| `EXPIRY` | `202612` | Contract month the forecast and dashboard read; the collector follows each roll rule regardless |
| `<SYMBOL>_EXPIRY` | — | Pins one future everywhere, collector included, e.g. `RTY_EXPIRY=202612` |
| `IB_HOST` | `127.0.0.1` | IB Gateway/TWS host |
| `IB_PORT` | `4002` | `4002` Gateway paper · `4001` Gateway live · `7497` TWS paper · `7496` TWS live |
| `IB_CLIENT_ID` | `1` | IB socket client id (the real-time streamer uses this + 1) |
| `ROLL_WARMUP_SESSIONS` | `7` | Trading days of a future's next contract stored before it becomes front |
| `OPENAI_API_KEY` | — | Enables OpenAI-backed forecasts |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic-backed forecasts |
| `LLM_MODEL` | `gpt-4o-mini` | Model name; a name containing `claude` selects Anthropic |
| `DASHBOARD_HOST` | `127.0.0.1` | Interface the dashboard binds to |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |

```bash
# .env
SYMBOLS=ES,NQ,RTY
CONTEXT_SYMBOLS=VIX,VXN,TNX,DX,SMH,10Y,2YY
EXPIRY=202612
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

**Adding another instrument** means one entry in `INSTRUMENTS` in [config.py](config.py)
(symbol, display name, exchange, tick size, multiplier, `sec_type` for a non-future, a
`RollRule` for a future, the unit its values come in, and a rough bar count per day),
then adding the symbol to `SYMBOLS` or `CONTEXT_SYMBOLS`. Nothing else is symbol-specific:
the `contracts` table already keys everything by contract, and the collector reads tick
size and multiplier back from IB. A non-CME or non-equity-index future would also need the
session windows and holiday calendar checked, since those assume the 18:00 ET roll and the
CME equity calendar.

**Without an API key the pipeline still works.** `ForecastClient`
([forecaster/client.py](forecaster/client.py)) falls back to a deterministic,
analogue-driven baseline engine, so you can develop and evaluate offline. The shipped
`.env` is empty, which is exactly this case.

Check what config resolves to:

```bash
python config.py
```

## Intermarket sources

The pre-open intermarket features are defined over **logical assets** (`nq`, `es`, `vix`,
`us10y`, ...). `ASSET_SOURCES` in [config.py](config.py) maps each one to the instrument
actually recorded, with its freshness rule, and the collector registers that map in the
`asset_sources` table on every run (a changed definition becomes a new row, so every
version a feature could have used stays on record). `python config.py` prints it.

| Asset | Recorded as | Unit | Freshness | Notes |
|---|---|---|---|---|
| `nq`, `es`, `rty` | NQ / ES / RTY front future | price | 5 min | |
| `vix` | VIX spot index | index points | 15 min | printed in Cboe's global hours and RTH |
| `vxn` | VXN spot index | index points | 15 min | may print in RTH only — then pre-open is null, not carried |
| `us10y` | TNX | percent × 10 (1 unit = 10 bps) | 30 min | |
| `us2y` | — | | | **unmapped**: IB has no spot 2-year yield history, so this stays null |
| `dxy` | — | | | **unmapped**: ICE does not license the cash DXY index to IB, so `dxy_preopen_return` stays null |
| `dx_fut` | DX front future | price | 30 min | **proxy** for DXY, under its own feature names (`dx_fut_*`) |
| `smh` | SMH | price | 30 min | premarket trades included (`useRTH=0`) |
| `us10y_yield_fut`, `us2y_yield_fut` | 10Y / 2YY front future | percent (1 unit = 100 bps) | 30 min | **proxy**, deliberately separate asset names |
| `gc`, `cl` | GC / CL front future | price | 10 min | optional, not collected by default |

What the store guarantees for these features:

- **Only genuine prints.** Bars are stored exactly as IB sends them; a minute without a
  print is absent, never forward-filled. `bars.timestamp_utc` is the bar's *start* time
  (`bar_start_at`). The v2 contract names a bar by its start: "the 09:28 close" is the bar
  stamped 09:28, which ends at 09:29 = T. A value's age is the
  distance from its bar's close to the instant it stands for.
- **One contract per future per day.** `active_contracts` says which contract stood for a
  symbol on each trading day, and the days before each activation are stored too, so a
  pre-open value and its previous-RTH-close reference always come from the same contract.
- **Units configured once.** Each instrument's `value_unit` (and for yields
  `bps_per_unit`) is recorded with the source. A day whose median value is implausible
  for the configured unit (e.g. TNX arriving as plain percent) is refused, not stored.
- **No implicit substitution.** An asset without a source (`us2y`) has no row to fall back
  on; proxies are flagged `is_proxy` and futures-yield proxies carry their own names.

`database/queries.py` has the read primitives: `get_active_contract(conn, symbol, day)`,
`get_last_bar_at_or_before(conn, contract_id, as_of_utc)` (last *closed* bar, with its
close time) and `get_asset_sources(conn)`.

Breadth (historical-constituent advance/decline) is not collected: it needs historical
index membership and per-constituent data, which nothing here records.

## How a forecast is built

**Features** ([features/calculations.py](features/calculations.py)) are frozen at 09:30 ET
of the target day — no bar at or after the open is used, so the snapshot carries no
look-ahead bias. It captures previous-RTH high/low/close, overnight high/low/range, the
opening gap, pre-open direction, historical volatility and VWAP, plus the cash VIX level
as of the same cutoff and its change from the prior close.

The trading day starts at **18:00 ET the prior evening** (the Globex open), not midnight.
[features/session_windows.py](features/session_windows.py) owns that rule and classifies
each bar `RTH` (09:30–16:00 ET) or `ETH`.

**Matching** ([matching/normalizer.py](matching/normalizer.py)) scales the gap and overnight
range by `previous_close × historical_volatility`, turning them into "sigma of a typical
day". That makes 2023 sessions comparable to 2026 ones at different price levels and
volatility regimes. Nearest neighbours by Euclidean distance on
`[gap, overnight_range, direction]`.

**Forecasting** ([forecaster/prompts.py](forecaster/prompts.py),
[forecaster/client.py](forecaster/client.py)) hands the model the target snapshot plus each
analogue *with its realized outcome*, and requires strict JSON back: `opening_bias`,
`scenarios`, `probabilities`, `forecast_horizon`.

**Evaluation** ([forecaster/evaluator.py](forecaster/evaluator.py)) computes what actually
happened once a session closes — first 15/30 minutes, the 60-minute Initial Balance, and
full RTH high/low/close — anchored to 09:30 ET regardless of which bar arrived first.

## v2: versioned NQ forecast records

A second, stricter pipeline runs beside the one above
([docs/forecast_contract_v2.md](docs/forecast_contract_v2.md)). It freezes an
`nq_features_v2` snapshot at **T = 09:29 ET** from bars ending by T only - NQ
price/volume structure in ATR units, intermarket returns and yield/volatility changes
with per-source freshness, calendar and event context - with a status for every feature
and source, a content-addressed source revision, and a live/historical and
point-in-time flag. Forecast runs, per-target probability distributions (with
abstention), realised labels and continuous outcome metrics are separate, append-only
records; corrections become new versions or revisions, never overwrites.

```bash
python scripts/nq_forecast_v2.py backfill --start 2026-06-01 --end 2026-09-25  # reconstruct + backtest
python scripts/nq_forecast_v2.py evaluate --outcome-revision 1
python scripts/nq_forecast_v2.py live        # 09:29 ET, once the 09:28 bars are stored
```

Tests: `pytest` (the database tests run when `TEST_DATABASE_URL` names a disposable
database whose name contains `test`; they reset it).

## Layout

| Path | What lives there |
|---|---|
| [collector/](collector/) | IB API client, coverage planner, request pacing, real-time bar streamer |
| [database/](database/) | Connection, queries, migrations, backfill/repair tools |
| [features/](features/) | Session/timezone classification, pre-open feature engineering |
| [matching/](matching/) | Volatility-normalized analogue search |
| [forecaster/](forecaster/) | Prompts, LLM client + offline baseline, outcome evaluator |
| [indicator/](indicator/) | Pine v6 indicator ports (VWAP, TEMA & session levels) + chart serialisation |
| [dashboard/](dashboard/) | NiceGUI app, pages, and the Lightweight Charts component |
| [scripts/](scripts/) | Daily runner, v1 and v2 forecast entrypoints, DB backup |
| [tests/](tests/) | Calendar, indicator, v2 snapshot and forecast-record tests |
| [docs/](docs/) | [Data store & incremental collection](docs/data_store.md), [v2 forecast contract](docs/forecast_contract_v2.md) |

## Database

PostgreSQL with TimescaleDB, upgraded in place and never regenerated. `init_database()`
applies any pending migrations on every process start, so simply running the app upgrades
it. `bars` is a TimescaleDB hypertable chunked monthly (30 days) on `timestamp_utc`; everything else
is a plain table.

Tables: `contracts`, `session_days` (the ledger of which days are held), `bars`,
`collection_runs`, `active_contracts` (the contract that stood for each symbol per day),
`asset_sources` (the logical-asset source map, versioned), `bar_receipts` (every
real-time bar as received, with `received_at`; append-only), `economic_events` /
`economic_event_coverage` (an optional event calendar), and the v1 forecast tables
`feature_snapshots`, `predictions`, `analogue_matches`, `outcomes`.

The v2 records (`0004`) live in their own schema, `forecast`: `feature_snapshots`,
`forecast_runs`, `predictions`, `realised_outcomes`, `outcome_metrics`, their version
registries and `source_revisions`. They are append-only - the database rejects UPDATE,
DELETE and TRUNCATE - and are described in
[docs/forecast_contract_v2.md](docs/forecast_contract_v2.md).

Every table is keyed by `contract_id`, so instruments never need separate tables — adding
ES and RTY needed no migration — only the VIX context columns did (`0002`), and the roll
and source bookkeeping for the intermarket context (`0003`). It does roughly
quadruple the stored volume against a single-symbol history: budget about 5 MB per contract
per month of 1-minute bars.

```bash
python -m database.migrations                      # show current + pending versions
python -m database.migrations --db postgresql://...  # apply pending migrations
python -m database.migrations --snapshot           # regenerate database/schema.sql
python -m database.backfill                        # recompute session_days from bars
python -m database.migrate_from_sqlite --sqlite data/x.db  # one-time import of a SQLite store
./scripts/backup_db.sh                             # timestamped pg_dump backup (30-day retention)
```

Never edit an applied migration or the generated `database/schema.sql` — add a new
migration file in `database/migrations/`. Read
[docs/data_store.md](docs/data_store.md) before changing anything about how days are stored.

## Indicators

[indicator/](indicator/) holds faithful Python ports of two Pine v6 TradingView indicators,
with the original `.pine` sources kept alongside for reference.
[indicator/pine.py](indicator/pine.py) reimplements the TradingView built-ins they need
(`ema`, `sma`, session parsing) preserving Pine's warm-up and `na`-propagation semantics
rather than the pandas defaults, so the Python output matches the chart it came from.

Each indicator computes over an OHLC frame and returns an `IndicatorResult`, which
[indicator/lwc.py](indicator/lwc.py) turns into Lightweight Charts series, bands and
levels. Nothing in the indicator package knows about the chart or the web framework.

## Charting

The dashboard is [NiceGUI](https://nicegui.io) serving TradingView's
[Lightweight Charts](https://tradingview.github.io/lightweight-charts/) v5. The library is
**vendored** at `dashboard/components/lib/lightweight-charts.standalone.js`, so the
dashboard loads no CDN and works with no network access.

Every chart in the app — candles and the evaluation trajectory alike — goes through one
component, [dashboard/components/lightweight_chart.js](dashboard/components/lightweight_chart.js).

**The chart is built once and then mutated in place.** Python never issues drawing
commands; it builds a *declarative spec* of what the chart should show
([dashboard/components/spec.py](dashboard/components/spec.py)) and the component
reconciles that against what it has already drawn, picking the cheapest operation:

| what changed | what the component does |
|---|---|
| nothing | no call at all |
| style only (width, colour, dash, visibility) | `series.applyOptions()` |
| bars appended, or the last bar revised | `series.update()` per changed bar |
| values recomputed across the window | `series.setData()` on the **existing** series |

Because no series or chart object is ever recreated, your zoom, scroll and crosshair
survive every settings change. Changing one indicator's length touches exactly one series;
switching timeframes leaves the overlay series in place. `update()` cannot rewrite a bar
before the series' last one, so it is used only where the replacement data is provably not
older — recomputing an indicator falls back to `setData` on the live series.

Indicator-only changes omit the OHLC payload from the message entirely, so tweaking a
setting sends the overlay series and nothing else.

Two things Lightweight Charts has no native support for are supplied here:

- **Band fills.** There is no fill-between-series, so the component carries a small canvas
  series primitive that shades the channel between two price arrays — that is what draws
  the Auto Anchored VWAP bands. It paints beneath the candles and breaks the polygon across
  gaps rather than closing over them.
- **Timezones.** The library always renders UTC. Timestamps are therefore converted to New
  York *wall clock* before being handed over ([indicator/lwc.py](indicator/lwc.py)), so the
  axis reads ET with DST handled per bar. A session runs 18:00 the prior evening → 17:00.

Horizontal levels are sent as their two endpoints rather than one point per bar: the value
is constant, so a dozen levels would otherwise ship tens of thousands of identical numbers
to the browser.

## Notes & gotchas

- The holiday calendar in [collector/coverage.py](collector/coverage.py) is hand-maintained
  and can be wrong; days already in the ledger are reconsidered so a partial holiday
  session can still be completed.
- Minute-level gap filling is deliberately not attempted — thin overnight periods have no
  trades, so a missing minute is not a reliable signal. **Days** are the unit of truth.
- `populate_mock_data.py --reset` refuses to touch any database containing non-`MOCK` bars.
  Pass `--force` only if you genuinely mean to destroy real collected data.
- The dashboard does its pandas work synchronously on the server. That is fine for one
  local user; it is not a multi-user service.
- This is research tooling, not trading infrastructure. Nothing here places orders, and
  nothing here is investment advice.
