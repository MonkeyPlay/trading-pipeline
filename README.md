# Opening Forecast Pipeline

A local, end-to-end research pipeline for the **CME equity-index futures opening**:
it collects 1-minute bars from Interactive Brokers, freezes a pre-open feature snapshot
at 09:30 ET, finds volatility-normalized historical analogues, asks an LLM (or a
deterministic baseline) for an opening forecast, scores that forecast against what
actually happened, and serves the whole thing in a NiceGUI dashboard drawn with
TradingView's Lightweight Charts.

Four instruments are tracked out of the box:

| Symbol | Instrument | Tick | Multiplier | Role |
|---|---|---|---|---|
| `ES` | S&P 500 E-mini | 0.25 | 50 | forecast target |
| `NQ` | Nasdaq-100 E-mini | 0.25 | 20 | forecast target |
| `RTY` | Russell 2000 E-mini | 0.10 | 50 | forecast target |
| `VIX` | CBOE Volatility Index | 0.01 | — | pre-open context |

The three futures share the same RTH window, the same 18:00 ET Globex roll and the same
holiday calendar, so the session and coverage logic is identical for each. Each is
forecast **independently** — analogues for ES are drawn only from past ES sessions, never
across instruments. `SYMBOLS` selects which ones run.

**VIX is context, not a target.** It is the cash index rather than a future: no expiry, no
volume, `sec_type = 'IND'`. It is collected like anything else, but instead of being
forecast it contributes the pre-open VIX level and its change from the prior close to
*every* futures snapshot (`vix_pre_open`, `vix_change`), and those reach the model in the
prompt. `CONTEXT_SYMBOLS` controls this set. Because nothing on the Session Explorer page
applies to an index, cash indices are left out of its contract picker.

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

With no `--symbol` it collects everything in `SYMBOLS`, and with no `--expiry` each symbol
resolves its own contract month (see [Configuration](#configuration)).

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
| `CONTEXT_SYMBOLS` | `VIX` | Collected as pre-open context only; never forecast |
| `EXPIRY` | `202612` | Contract month shared by the futures (they use one quarterly cycle) |
| `<SYMBOL>_EXPIRY` | — | Overrides `EXPIRY` for one instrument, e.g. `RTY_EXPIRY=202612` mid-roll |
| `IB_HOST` | `127.0.0.1` | IB Gateway/TWS host |
| `IB_PORT` | `4002` | `4002` Gateway paper · `4001` Gateway live · `7497` TWS paper · `7496` TWS live |
| `IB_CLIENT_ID` | `1` | IB socket client id |
| `OPENAI_API_KEY` | — | Enables OpenAI-backed forecasts |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic-backed forecasts |
| `LLM_MODEL` | `gpt-4o-mini` | Model name; a name containing `claude` selects Anthropic |
| `DASHBOARD_HOST` | `127.0.0.1` | Interface the dashboard binds to |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |

```bash
# .env
SYMBOLS=ES,NQ,RTY
CONTEXT_SYMBOLS=VIX
EXPIRY=202612
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

**Adding another instrument** means one entry in `INSTRUMENTS` in [config.py](config.py)
(symbol, display name, exchange, tick size, multiplier, and `sec_type` for a non-future),
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

## Layout

| Path | What lives there |
|---|---|
| [collector/](collector/) | IB API client, coverage planner, request pacing |
| [database/](database/) | Connection, queries, migrations, backfill/repair tools |
| [features/](features/) | Session/timezone classification, pre-open feature engineering |
| [matching/](matching/) | Volatility-normalized analogue search |
| [forecaster/](forecaster/) | Prompts, LLM client + offline baseline, outcome evaluator |
| [indicator/](indicator/) | Pine v6 indicator ports (VWAP, TEMA & session levels) + chart serialisation |
| [dashboard/](dashboard/) | NiceGUI app, pages, and the Lightweight Charts component |
| [scripts/](scripts/) | Daily runner, forecast entrypoint, DB backup |
| [docs/](docs/) | [Data store & incremental collection](docs/data_store.md) |

## Database

PostgreSQL with TimescaleDB, upgraded in place and never regenerated. `init_database()`
applies any pending migrations on every process start, so simply running the app upgrades
it. `bars` is a TimescaleDB hypertable chunked monthly (30 days) on `timestamp_utc`; everything else
is a plain table.

Tables: `contracts`, `session_days` (the ledger of which days are held), `bars`,
`collection_runs`, `feature_snapshots`, `predictions`, `analogue_matches`, `outcomes`.

Every table is keyed by `contract_id`, so instruments never need separate tables — adding
ES and RTY needed no migration — only the VIX context columns did (`0002`). It does roughly
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
