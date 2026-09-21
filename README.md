# NQ Opening Forecast Pipeline

A local, end-to-end research pipeline for the **NQ (Nasdaq-100 E-mini) futures opening**:
it collects 1-minute bars from Interactive Brokers, freezes a pre-open feature snapshot
at 09:30 ET, finds volatility-normalized historical analogues, asks an LLM (or a
deterministic baseline) for an opening forecast, scores that forecast against what
actually happened, and serves the whole thing in a NiceGUI dashboard drawn with
TradingView's Lightweight Charts.

Everything runs on your machine against a single SQLite file. No server, no cloud.

```
IB Gateway/TWS ──▶ collector ──▶ SQLite ──▶ features ──▶ matching ──▶ forecaster ──▶ SQLite
                                    │                                                  │
                                    └──────────────── NiceGUI dashboard ◀─────────────┘
```

## Quick start

```bash
cd /home/monkeyplay/trading_pipeline
source .venv/bin/activate
python -m dashboard.app        # then open http://127.0.0.1:8080
```

That serves the dashboard against whatever is already in `data/trading_pipeline.db`.
It never needs IB to be running — it only reads stored data. `DASHBOARD_PORT` and
`DASHBOARD_HOST` override where it listens.

**No data yet?** Seed a throwaway database with realistic fake sessions:

```bash
python populate_mock_data.py --reset          # writes data/trading_pipeline.dev.db
DB_PATH=data/trading_pipeline.dev.db python -m dashboard.app
```

### First-time setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The database is created and migrated automatically on first use — there is no
separate init step.

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
python -m collector.ib_collector --expiry 202609 --days 30 --plan-only   # what would it fetch?
python -m collector.ib_collector --expiry 202609 --days 30               # fill the gaps
python -m collector.ib_collector --expiry 202609 --start 2026-08-01 --end 2026-08-31
python -m collector.ib_collector --expiry 202609 --days 30 --full        # re-download everything
```

Collection is **incremental and day-partitioned**: the collector reads the `session_days`
ledger first and only opens a socket for days it actually needs. If nothing is missing it
exits without connecting. See [docs/data_store.md](docs/data_store.md) for the full model —
day statuses, the atomic per-day write path, and the trailing-revision window.

### 3. Daily forecast — features, analogues, prediction, scoring

```bash
python scripts/daily_forecast.py --expiry 202609 --lookback-days 40
```

In one pass ([scripts/daily_forecast.py](scripts/daily_forecast.py)) it:

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
15 9 * * 1-5  /home/monkeyplay/trading_pipeline/scripts/run_pipeline.sh
0  2 * * *    /home/monkeyplay/trading_pipeline/scripts/backup_db.sh
```

Those cron times are in the machine's local timezone — 09:15 ET is 13:15 UTC (14:15 UTC
during EST), so adjust if the box is not on New York time.

`NQ_EXPIRY` overrides the contract month it uses (default `202609`).

## Configuration

Settings come from environment variables or a local `.env`, read by
[config.py](config.py). All have defaults; none are required.

| Variable | Default | What it does |
|---|---|---|
| `DB_PATH` | `data/trading_pipeline.db` | Production store — collector and pipeline write here |
| `DEV_DB_PATH` | `data/trading_pipeline.dev.db` | Throwaway store for `populate_mock_data.py` |
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
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
```

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
opening gap, pre-open direction, historical volatility and VWAP.

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

One SQLite file, upgraded in place and never regenerated. `init_database()` applies any
pending migrations on every process start, so simply running the app upgrades it.

Tables: `contracts`, `session_days` (the ledger of which days are held), `bars`,
`collection_runs`, `feature_snapshots`, `predictions`, `analogue_matches`, `outcomes`.

```bash
python -m database.migrations                      # show current + pending versions
python -m database.migrations --db data/x.db       # apply pending migrations
python -m database.migrations --snapshot           # regenerate database/schema.sql
python -m database.backfill --ledger-only          # recompute session_days from bars
./scripts/backup_db.sh                             # timestamped, gzipped backup (30-day retention)
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

- **Not a git repository.** There is no branch or commit state here.
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
