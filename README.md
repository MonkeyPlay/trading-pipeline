# Trading Pipeline

A local research pipeline for the **CME equity-index futures**: it collects 1-minute bars
from Interactive Brokers into a day-partitioned TimescaleDB store and serves the sessions
in a NiceGUI dashboard drawn with TradingView's Lightweight Charts. Besides Interactive
Brokers, two outside services are used, both by the NQ pre-open journal
([docs/nq_prompt_v2.md](docs/nq_prompt_v2.md)): SEC EDGAR, for the earnings filings behind
Event Risk, and optionally the Claude API, for the structure annotation and forecast arms C
and D, which run only when started by hand - in a terminal after typing "send", or in the
dashboard after its confirmation; nothing else can start them - with `ANTHROPIC_API_KEY` set.
Everything else is computed locally.

Twelve instruments are collected out of the box:

| Symbol | Instrument | IB type | Role |
|---|---|---|---|
| `ES` | S&P 500 E-mini | FUT, rolls quarterly | target future |
| `NQ` | Nasdaq-100 E-mini | FUT, rolls quarterly | target future |
| `RTY` | Russell 2000 E-mini | FUT, rolls quarterly | target future |
| `VIX` | Cboe Volatility Index (spot) | IND | intermarket context |
| `VXN` | Cboe Nasdaq-100 Volatility Index (spot) | IND | intermarket context |
| `TNX` | Cboe 10-Year Treasury Yield Index (yield × 10) | IND | intermarket context |
| `DX` | US Dollar Index future (ICE) — DXY proxy | FUT, rolls quarterly | intermarket context |
| `SMH` | VanEck Semiconductor ETF | STK | intermarket context |
| `10Y` | Micro 10-Year Yield future (quoted in yield) | FUT, rolls monthly | intermarket context |
| `QQQ` | Invesco QQQ Trust (Nasdaq-100 ETF) | STK | research dataset |
| `SPY` | SPDR S&P 500 ETF Trust | STK | research dataset |
| `IWM` | iShares Russell 2000 ETF | STK | research dataset |

`GC` (gold) and `CL` (WTI crude) are defined too but only collected once added to
`CONTEXT_SYMBOLS`. `2YY` (Micro 2-Year Yield future) was dropped entirely on 2026-10-06 and
its stored data removed (migration 0018; a CSV copy is in `data/backups/`). See [Intermarket sources](#intermarket-sources)
for how each logical asset maps onto these.

**QQQ, SPY and IWM** are the cash-index ETFs of NQ, ES and RTY. They are collected like the
context instruments (extended hours included, `useRTH=0`; a day counts as complete only
with its whole regular session) but have no intermarket source, so no evidence snapshot,
annotation or forecast reads them: they are stored for the research datasets (the
cross-instrument and longer-history studies) only.

The three target futures share the same RTH window, the same 18:00 ET Globex roll and the
same holiday calendar, so the session and coverage logic is identical for each. `SYMBOLS`
selects which ones run.

**Context instruments are only collected.** VIX, for instance, is the cash index rather
than a future: no expiry, no volume, `sec_type = 'IND'`. It is collected like anything
else, and the Session Explorer leaves context instruments out of its instrument picker.
`CONTEXT_SYMBOLS` controls this set.

Everything runs on your machine against a local PostgreSQL + TimescaleDB database. No cloud.

```
IB Gateway/TWS ──▶ collector ──▶ TimescaleDB ──▶ NiceGUI dashboard
```

## Quick start

```bash
source .venv/bin/activate      # from the project directory
python -m dashboard.app        # then open http://127.0.0.1:8080
```

`address already in use` means a dashboard is already running on that port: open it, or
stop it (`pkill -f dashboard.app`) and start again.

That serves the dashboard against whatever is already in the `trading_pipeline` database
(`DATABASE_URL`).
It needs IB only to collect from it: **Update data**, in the header, runs the collector
and the forecaster (see [below](#update-data-from-the-dashboard)). `DASHBOARD_PORT` and
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
| `TS_TUNE_MEMORY` | `32GB` | RAM `timescaledb-tune` sizes Postgres for |

With 32 GB (and 12 CPUs) the tuner sets `shared_buffers=8GB`, `effective_cache_size=24GB`,
`work_mem=21845kB`, `maintenance_work_mem=2047MB` and SSD-friendly planner costs. Tuning is
written **once, when the data volume is created**; after changing `TS_TUNE_MEMORY` on an
existing volume, re-run it in place and restart (`--dry-run` first shows what it changes):

```bash
docker compose exec timescaledb timescaledb-tune --memory=32GB --yes --quiet \
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

## The two things you can run

### 1. Dashboard — look at the stored sessions

```bash
python -m dashboard.app
```

#### Update data from the dashboard

**Update data**, at the right of the header on every page, opens the pipeline's jobs
([dashboard/components/pipeline.py](dashboard/components/pipeline.py),
[dashboard/jobs.py](dashboard/jobs.py)). Each runs as its own process, the same command as on
the command line:

| Button | Runs | What it does |
|---|---|---|
| **Run collector** | `python -m collector.ib_collector --days N` (default 5, as cron) | The missing bars from IB, then the forecaster, as after every full collection |
| **Run forecaster** | `python -m collector.ib_collector --journal-only` | The journal step alone, without IB: event calendar and earnings, then the snapshot, rule-based annotation, analogue set and baseline and prior forecasts (historical replay) of every session past its cutoff - today's too, once its bars were fetched after the 09:29 cutoff - and outcomes once a session is final. No Claude requests |
| **Run LLM forecast** | `python scripts/nq_journal.py llm-forecast --date D ... --arms CD` | Arms C (restricted LLM) and D (synthesis) for the last N sessions up to today (default 1: today) or for **chosen days** (a calendar of the journal's sessions: single days, or ranges with its switch), arm C or D or both. The Session Explorer forecast's **Run C and D for <day>** does the same for the session day. **Batch API** sends them at half price, answers within 24 h (usually far sooner): the job waits up to 30 minutes and the next run collects a batch still processing. Claude requests: a confirmation first shows each session, the requests and a rough cost, and only its **Send** button lets the job send them (a one-time approval, [forecaster/approvals.py](forecaster/approvals.py)). Evidence already answered is never sent again |
| **Forecast now** | the collector, then `python scripts/nq_journal.py preview` | The next session's forecast from the data so far, at any time from its Globex open (18:00 ET the evening before) until its official snapshot is due at 09:31 ET: the latest bars, then the evidence as of now, its rule-based annotation, analogues and both arms - in memory, never stored. Shown on the Session Explorer forecast's **Forecast now** tab, which has the same button. The preview step runs even when collecting failed, from the bars already stored |
| **Live forecast** | `python scripts/nq_journal.py live` | Today's pre-open live capture and its forecasts ([below](#both-together)); offered on a session day before 09:30 ET only - it then waits for the 09:29 cutoff |

One job runs at a time (a job can have steps: Forecast now collects, then previews). It belongs to the dashboard process, not to the browser tab: every
page shows it and its output, the header shows its name and running time, closing the tab
does not stop it (**Stop** interrupts it as Ctrl-C would), and the output is appended to
`logs/pipeline_run.log`. When it ends, the pages that saw it running read the database
again in place: the session bar's calendar and coverage map (showing the newest session, it
moves on to a newer one), then the page - the Session Explorer's session (a session still in
progress grows), analogues, the day's forecast runs and the preview; Evaluation's cases of the
day; the header's bar count.

**Session bar** (top of every page, [dashboard/components/session_bar.py](dashboard/components/session_bar.py)):
three selectors, in order - the **session day** (a calendar, weeks from Monday, on which only
the days with stored bars can be picked; the arrows either side step to the previous and the
next of them), the **instrument** with bars that day (ES, NQ, ...), and the **contract**
holding it (the one the collector made active that day first) - and the coverage map. The day
is the one every page shows. A page opens on NQ's newest session, or on
`?day=YYYY-MM-DD&symbol=NQ&contract=<id>`; the header's navigation carries the selection to
the other page, and the address bar follows it, so a reload keeps it.
**Database coverage by week** is a small map of what is stored: one cell per instrument and
week, green when every scheduled trading day is complete, then light green (≥ 90 %), yellow
(≥ 50 %), orange (> 0 %) and red (nothing), from the collector's day ledger; hover a cell for
its day counts, and the title gives the newest stored day. Right of the weeks, the **last 10
trading days** are one dot each, coloured by the same rule for that day alone; hover a dot for
its status and bar count ([dashboard/components/coverage_map.py](dashboard/components/coverage_map.py)).

Pages:

- **Evaluation** (`/evaluation`, [dashboard/views/evaluation.py](dashboard/views/evaluation.py)) —
  first the **intermarket fan experiment**: the frozen learned fan and where it draws, its
  sealed holdout's one scoring per horizon, and the forward record per rule version (the marks
  expected and what became of them, the issues by class, the latest with their latency).
  Then the registered P1 experiments (guideline stage 4): each manifest, its stored scorings (paired arm
  differences with intervals, every target, where the differences sit) and its frozen cases,
  each linked to its forecast run in the Session Explorer. Under the manifest, the **session
  day** in the experiment: inside its sessions or not, and per arm its frozen case.
- **Session Explorer** (`/`, [dashboard/views/candles.py](dashboard/views/candles.py)) — built
  around the first hour: the session the bar selects, its analogues and its forecast.
  - **Chart:** the day's regular session with **15 minutes either side** is loaded - 09:15 to
    16:15 ET, or to 13:15 on an early close - and a new day opens on **09:15-10:45**; **Fit**
    shows the whole window (on both charts), and a timeframe change keeps the window being
    looked at. The
    extra minutes are drawn grey (candles, volume, background). The previous session's RTH
    close and the overnight high and low are drawn as reference levels, with the session
    VWAP. The **opening range** (the first 15 minutes) is a grey box over its bars, then
    **ORH** / **ORL** lines with the channel between them shaded (from the 1-minute bars at
    any timeframe; no box at 30 minutes).
  - **Moving averages:** the three lines of the TradingView indicator "TEMA & Session
    Levels" at its default inputs, in its colours - **TEMA 14 (SMA 3)** purple, **EMA 100**
    blue, **EMA 14 (SMA 3)** orange (the script titles the last two "EMA 50" and "EMA 9
    Smoothed"). They run on the chart's timeframe over 1000 earlier bars of the same
    contract, so they start the session settled, as on TradingView
    ([features/calculations.py](features/calculations.py)).
  - **The session in progress** (from its 18:00 ET Globex open to 17:00 ET): drawn whole, from
    the Globex open, with the **price fan** `fan_rw_v2` - the intermarket experiment's baseline,
    with its own fat-tailed shape - to the right of its newest candle
    ([dashboard/components/fan.py](dashboard/components/fan.py), model in
    [docs/fan.md](docs/fan.md)); on **NQ** the frozen **learned fan** adds blue brackets (5-95 %
    whisker, 25-75 % box, median) on the candles 5 and 15 minutes ahead - the horizons it passed on
    the sealed holdout; v2 draws every other horizon ([docs/fan_experiment.md](docs/fan_experiment.md)).
    The line above the chart says how old the fan's origin is when the feed is delayed. Per candle
    ahead (up to 120, to the day's end) the
    distribution of its close as v2 issues it (no display adjustment, so the brackets compare
    with it directly), as a neutral fog whose opacity follows the density - the most
    likely price most opaque - and fades with v2's measured skill against a flat random
    walk at that horizon (walk-forward over the last 30 sessions, each drawn with the shape it
    was issued with; relative to its skill one minute ahead, never below a faint floor). The
    median stays at the origin's price: the fan forecasts how far, not which way. Scheduled
    releases ahead are marked where the fan widens; the crosshair over a column reads its
    5 / 50 / 95 % prices (and on the learned fan's two candles its range, v2's at exactly that
    horizon, and whether it was recorded); the line above the charts gives the 90 % ranges 15,
    30 and 60 minutes ahead and how the fades and the band's coverage were measured. The fan
    follows the active contract (another contract shows none). Where the forward record issued
    the learned fan from the origin shown (every 15 minutes and 09:29 ET), its brackets are that
    **recorded forecast**, read from the journal (solid, "rec"); elsewhere they are computed
    from the bars stored now (hollow, dashed).
    **Playback** steps back through the session's candles (the slider, or the arrows a candle at
    a time) with the fan from each - a **recomputed historical preview**, as the line above the
    chart says: from the bars stored now, so not a record of what was shown then (only a
    recorded forecast is). The later candles are hidden, or drawn grey with **Show what
    followed**; the levels and opening range are the ones known by then. **Live** returns to the
    newest candle; **Fan** hides it.
    **Auto** (beside Fit) collects the session in progress from IB and forecasts once a minute -
    the collector for today only with its journal step, then the preview while one is possible
    ([dashboard/jobs.py](dashboard/jobs.py) `AUTO`) - and moves the explorer to the session in
    progress. Each run's end redraws the chart, the fan and the analogue preview in place, the
    view moving on with the newest candle. It belongs to the dashboard process like any job (it
    waits while another runs, and keeps running with the tab closed); switching it off, or
    stopping one of its runs in Update data, ends it. A run takes about a minute, so the chart
    trails the market by one to two minutes.
  - **Analogue beside it:** the charts are split - on the right, one of the selected NQ
    session's structural analogues, the most similar first (pick another in its **Analogue**
    select, or by its date in the comparison below): that session on its own contract and
    prices, drawn the same way - the same window, timeframe and indicators. The two charts are
    linked by time of day: scrolling or zooming either moves the other.
  - **Analogues** (below the charts, [dashboard/views/analogues.py](dashboard/views/analogues.py)):
    the session and its analogues side by side, feature by feature - their realised labels and
    the outcome frequencies hidden until asked for - with P1's 47-field pre-open record; for the
    days the journal holds a snapshot of. A day in progress has no snapshot until its official
    one is taken (from 09:31 ET, once every instrument's bars were fetched after then): until then
    a **preview** stands in - the same matching on the evidence as of the day's last stored NQ
    bar (the whole pre-open once its 09:29 bar is stored), computed in memory, never stored,
    labelled with its as-of minute ([forecaster/preview.py](forecaster/preview.py)
    `preview_session`). See [docs/nq_prompt_v2.md](docs/nq_prompt_v2.md#analogues-2b-2d).
  - **Forecast** (at the bottom, [dashboard/views/forecast.py](dashboard/views/forecast.py)): the
    session day's NQ forecast; `/?run=<id>` opens a run on its day, `/?view=preview` Forecast now
    (the old `/forecast` links redirect there). Two tabs. **Stored runs**: the day's runs, one
    shown, by its id: provenance, the chart drawn from the snapshot's frozen bars, per-target
    distributions with their denominators, P1's 47 fields, and the realised outcome only when
    asked for; see [docs/nq_prompt_v2.md](docs/nq_prompt_v2.md#stage-3-deterministic-forecasts-guideline-revision-2).
    **Arms for the day** (at the top): one tile per arm - A prior (the benchmark), B
    baseline, C restricted LLM, D synthesis - saying whether it ran (issued, failed, no run)
    and what it rests on (analogues, the C pool, D's confidence); the arm shown is highlighted
    and a click shows another. Below the tiles a radar compares the arms with the benchmark
    (arm A dashed, chance dotted) ([forecaster/grading.py](forecaster/grading.py)): before the
    outcome, how sure each arm is of its predicted class per target, with a table of the classes
    marking each that differs from arm A's; with the realised outcome shown and recorded, the
    grading - the probability each arm gave what happened, hits, mean p(realised) and the
    difference to arm A.
    **Forecast now**: the latest preview (its own button, as in Update data) - as of when, how
    complete its pre-open is, what is not known yet, its analogues, both arms and the 47 fields;
    never stored ([forecaster/preview.py](forecaster/preview.py)).

### 2. Collector — pull fresh bars from IB

Requires IB Gateway or TWS running locally (port `4002` = Gateway paper by default).

```bash
python -m collector.ib_collector --days 30 --plan-only            # what would it fetch?
python -m collector.ib_collector --days 30                        # fill the gaps, all symbols
python -m collector.ib_collector --days 30 --symbol NQ            # just one
python -m collector.ib_collector --start 2026-08-01 --end 2026-08-31
python -m collector.ib_collector --days 30 --full                 # re-download everything
```

With no `--symbol` it collects everything in `SYMBOLS` and `CONTEXT_SYMBOLS`, then brings
the NQ prompt-v2 journal up to date: it reloads the economic calendar, fetches the material
Nasdaq-100 earnings releases from SEC EDGAR (set `SEC_USER_AGENT` to "name e-mail"), and
stores a snapshot and pre-open structure annotation for every session it does not hold yet
whose pre-open is over and stored - a session in progress too, once its bars were fetched
after the 09:29 cutoff - its outcome once it is final (two hours after the close), then the
analogue sets and the historical-replay baseline forecasts, each once ([docs/nq_prompt_v2.md](docs/nq_prompt_v2.md#running-it)). `--no-journal` skips that; a
`--symbol` subset or a pinned `--expiry` skips it too. `--journal-only` runs that step alone,
without collecting or connecting to IB (the dashboard's **Run forecaster**).

**Futures follow their front contract.** Each future has a roll rule in `config.py`
(equity indices: the quarterly contract, rolling 8 days before expiry). The collector asks
IB once for the whole contract chain, expired contracts included, and fetches every
trading day from the contract that was front *on that day*. It also stores the
`ROLL_WARMUP_SESSIONS` (7) trading days before each contract becomes active - collected
as they happen, ahead of the roll - so the first day after a roll has a same-contract
previous close and enough same-contract history for intraday indicators. The choice is
recorded per day in `active_contracts`.
`--expiry 202609` (or `NQ_EXPIRY=202609` for one symbol) pins a single contract instead.

**Backfilling history.** Once:

```bash
python -m collector.ib_collector --days 100      # ~70 trading days, across the last roll
python -m collector.ib_collector --days 460      # as far back as IB serves expired contracts
```

That is roughly 70 requests per instrument; the pacer keeps it under IB's limit (60
requests / 10 min), so twelve instruments take a few hours. Later runs only fetch the
new and trailing days.

A contract IB holds no history for answers `HMDS query returned no data` for every day.
After three such days in a row the collector skips the rest of that contract for the run,
so it cannot use up the request budget the other instruments need; the skipped days are
tried again next run. The skip is per contract: the next contract of the same future
still gets its own tries.

**All symbols go through one IB connection**, which matters: the request pacer that keeps
you under IB's historical-data rate limit lives on that connection. Running one process
per symbol would give each its own pacer, so each would undercount the others' requests
and a wide backfill could trip the limit.

Collection is **incremental and day-partitioned**: the collector reads the `session_days`
ledger first and only opens a socket for days it actually needs. If nothing is missing for
any symbol it exits without connecting at all. See [docs/data_store.md](docs/data_store.md)
for the full model — day statuses, the atomic per-day write path, and the
trailing-revision window.

### Both together

[scripts/run_pipeline.sh](scripts/run_pipeline.sh) runs the collector (and with it the journal
step). It logs to `logs/pipeline_run.log`. It is meant for cron:

```cron
15 9 * * 1-5  /path/to/trading-pipeline/scripts/run_pipeline.sh
0  2 * * *    /path/to/trading-pipeline/scripts/backup_db.sh
```

Both scripts resolve the project directory from their own location, so they can be
invoked by absolute path from anywhere without a `cd` first.

Those cron times are in the machine's local timezone — 09:15 ET is 13:15 UTC (14:15 UTC
during EST), so adjust if the box is not on New York time.

**Live capture is a separate, scheduled job.** The old real-time streamer was removed
(commit a428bab, migration 0008). Its replacement, `python scripts/nq_journal.py live`
(guideline stage 3D, [docs/nq_prompt_v2.md](docs/nq_prompt_v2.md#live-capture-3d)), runs
once before the open: right after the 09:29 cutoff it fetches the session's 1m bars from IB
(its own client id, `IB_CLIENT_ID + 1`), records a receipt per bar with the database time,
freezes a live snapshot and issues the forecasts, which the database marks late after
09:29:50 ET. Schedule it in New York time (a systemd timer example is in the docs). It is
tested against a fake IB, not yet on a trading day; until it runs, every journal snapshot
is a historical reconstruction and every forecast a historical replay.

It collects every symbol in `SYMBOLS`, and hands the whole list to the collector in one
process so its rate-limit pacing stays accurate.

### Benchmark price fan

A forecast arm that is not tied to 09:29: from any minute, the distribution of the price at
every later minute of the trading day - the usual intraday volatility pattern, bumped
around scheduled releases and scaled to the current volatility, as a zero-drift random walk
([docs/fan.md](docs/fan.md), version `fan_rw_v1`). It is the bar a learned fan has to beat.

```bash
python scripts/fan.py now --symbol NQ                               # the fan from the latest closed bar
python scripts/fan.py now --symbol NQ --as-of "2026-10-02 10:15"    # ... replayed from a past minute (ET)
python scripts/fan.py score --symbol NQ --start 2025-09-02 --end 2026-07-10   # every origin, walk-forward
```

`score` scores every origin minute against the flat, intraday-pattern and event-bump
references and writes `docs/reports/fan_rw_v1_<symbol>_<start>_<end>.md`. Nothing is stored
but the registered definition. The Session Explorer draws the fan on the session in progress
(below).

### Intermarket fan experiment

Step 3 of the fan - a learned fan that reads every collected instrument, accepted only
where it beats the benchmark, and how much each instrument contributes - is fixed in
advance as a registered manifest: the anytime fan, NQ at 15 minutes primary, the last 60
sessions to 2026-10-05 as a sealed holdout, a window that follows the stored history
([docs/fan_experiment.md](docs/fan_experiment.md),
[forecaster/fan_experiment.py](forecaster/fan_experiment.py)). Until a frozen model opens
the holdout, `score` refuses a range that reaches into it.

```bash
python scripts/fan.py experiment-register --dry-run   # the manifest, resolved from the store
python scripts/fan.py experiment-show                 # what is registered, sealed or open
python scripts/fan.py panel-audit                     # every instrument on the minute grid, audited
python scripts/fan.py baseline-gate                   # fan_rw_v2 against fan_rw_v1 on the checks (decided once)
python scripts/fan.py checks --candidate phase_scale  # a candidate against the baseline on the three checks
python scripts/fan.py features                        # every instrument's features, screened (development)
python scripts/fan.py checks --candidate gbm_own_ivx  # the learned fan on the checks (gbm_own, gbm_own_iv, gbm_all)
python scripts/fan.py compare gbm_own_ivx lin_pois_ivx   # two stored runs paired on identical sessions
python scripts/fan.py replay                          # live-style replay: every input as the store served it
python scripts/fan.py search                          # SPA and StepM over every stored checks run
python scripts/fan.py freeze --candidate lin_pois_ivx --dry-run   # the frozen definition (registers without --dry-run)
python scripts/fan.py holdout                         # the frozen model on the sealed holdout - once
python scripts/fan.py forward report                  # the forward record: live issues, scored once final
```

`fan_rw_v2` - v1 with releases estimated by name, earnings placed at the close and a
fat-tailed shape - passed the gate on 2026-10-06 and is the experiment's baseline
([docs/fan.md](docs/fan.md#version-2-fan_rw_v2)). `checks` trains a candidate on the
development sessions before each check and scores it against the baseline per horizon,
origin phase and the pre-open slice; the baseline's frames are cached in `data/fan_cache/`.
`features` builds what a candidate reads at an origin - per instrument, how much it moves
against its usual, its recent move, its change since the close, volume, age - each tagged
with its instrument and group. `gbm_own` / `gbm_own_iv` / `gbm_all` are the learned fan: v2's
width scaled per origin by gradient boosting on those features (forecaster/fan_model.py);
`gbm_own_iv` adds VXN's implied over NQ's realised variance, the cross-market quantity that
helps, and `gbm_own_ivx` both sides of that comparison beside the ratio. `lin_pois_ivx` was frozen and passed the sealed holdout
on 2026-10-06 - marginally (-0.14 % at 15 minutes); it now draws NQ at 5 and 15 minutes.
Its forward record runs from `scripts/fan_forward.sh` - two cron lines (neither installed by
this work), both collecting NQ and VXN on client id `IB_CLIENT_ID + 2` under one lock:

```
0,15,29,30,45 * * * *  /path/to/trading-pipeline/scripts/fan_forward.sh mark   # the live attempt at each mark (and 09:29 ET)
*/2 * * * *            /path/to/trading-pipeline/scripts/fan_forward.sh        # the catch-up: the delayed-feed evaluation
```

- or the dashboard's Auto mode, and `scripts/run_pipeline.sh` scores it daily. The IB feed
on this account is delayed (no real-time subscription; `scripts/ib_feed_check.py` measures
it): no forecast is live, so the live 5- and 15-minute forecasts cannot be validated on it.
Every report classifies each issue under the rule version it was made under - live, delayed
origin (the separately labelled delayed-feed evaluation), late, expired - counts the marks the
calendar expected, and times each trigger against the 60 s live deadline (the feed's part -
when the origin bar arrived - apart from the pipeline's). Every checks report
includes calibration (band coverage, misses on each side, widths, PIT) and how concentrated
the gain is.

## Configuration

Settings come from environment variables or a local `.env`, read by
[config.py](config.py). All have defaults; none are required.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | `postgresql://trading:trading@localhost:5432/trading_pipeline` | Production store — collector and pipeline write here |
| `DEV_DATABASE_URL` | `postgresql://trading:trading@localhost:5432/trading_pipeline_dev` | Throwaway store for `populate_mock_data.py` |
| `SYMBOLS` | `ES,NQ,RTY` | Target futures, collected and shown in the Session Explorer, in order |
| `CONTEXT_SYMBOLS` | `VIX,VXN,TNX,DX,SMH,10Y,QQQ,SPY,IWM` | Collected as context only, never forecast |
| `EXPIRY` | `202612` | Fallback contract month where no roll assignment applies (mock data); the collector follows each roll rule regardless |
| `<SYMBOL>_EXPIRY` | — | Pins one future everywhere, collector included, e.g. `RTY_EXPIRY=202612` |
| `IB_HOST` | `127.0.0.1` | IB Gateway/TWS host |
| `IB_PORT` | `4002` | `4002` Gateway paper · `4001` Gateway live · `7497` TWS paper · `7496` TWS live |
| `IB_CLIENT_ID` | `1` | IB socket client id |
| `ROLL_WARMUP_SESSIONS` | `7` | Trading days of a future's next contract stored before it becomes front |
| `DASHBOARD_HOST` | `127.0.0.1` | Interface the dashboard binds to |
| `DASHBOARD_PORT` | `8080` | Port the dashboard listens on |

```bash
# .env
SYMBOLS=ES,NQ,RTY
CONTEXT_SYMBOLS=VIX,VXN,TNX,DX,SMH,10Y,QQQ,SPY,IWM
EXPIRY=202612
```

**Adding another instrument** means one entry in `INSTRUMENTS` in [config.py](config.py)
(symbol, display name, exchange, tick size, multiplier, `sec_type` for a non-future, a
`RollRule` for a future, the unit its values come in, and a rough bar count per day),
then adding the symbol to `SYMBOLS` or `CONTEXT_SYMBOLS`. Nothing else is symbol-specific:
the `contracts` table already keys everything by contract, and the collector reads tick
size and multiplier back from IB. A non-CME or non-equity-index future would also need the
session windows and holiday calendar checked, since those assume the 18:00 ET roll and the
CME equity calendar.

**No API keys are needed** for collecting and the dashboard. The journal's earnings fetch
asks for `SEC_USER_AGENT` ("<name> <contact e-mail>", SEC's rule for automated clients);
the optional Claude structure annotation needs `ANTHROPIC_API_KEY`.

Check what config resolves to:

```bash
python config.py
```

## Intermarket sources

The intermarket context is defined over **logical assets** (`nq`, `es`, `vix`, `us10y`,
...). `ASSET_SOURCES` in [config.py](config.py) maps each one to the instrument actually
recorded, with its freshness rule, and the collector registers that map in the
`asset_sources` table on every run (a changed definition becomes a new row, so every
version stays on record). `python config.py` prints it.

| Asset | Recorded as | Unit | Freshness | Notes |
|---|---|---|---|---|
| `nq`, `es`, `rty` | NQ / ES / RTY front future | price | 5 min | |
| `vix` | VIX spot index | index points | 20 min | printed in Cboe's global hours and RTH, paused 09:15–09:30 ET |
| `vxn` | VXN spot index | index points | 20 min | may print in RTH only — then pre-open is null, not carried |
| `us10y` | TNX | percent × 10 (1 unit = 10 bps) | 30 min | |
| `us2y` | — | | | **unmapped**: IB has no spot 2-year yield history, so this stays null |
| `dxy` | — | | | **unmapped**: ICE does not license the cash DXY index to IB |
| `dx_fut` | DX front future | price | 30 min | **proxy** for DXY, under its own asset name |
| `smh` | SMH | price | 30 min | premarket trades included (`useRTH=0`) |
| `us10y_yield_fut` | 10Y front future | percent (1 unit = 100 bps) | 30 min | **proxy**, deliberately separate asset name |
| `gc`, `cl` | GC / CL front future | price | 10 min | optional, not collected by default |

What the store guarantees for these series:

- **Only genuine prints.** Bars are stored exactly as IB sends them; a minute without a
  print is absent, never forward-filled. `bars.timestamp_utc` is the bar's *start* time
  (`bar_start_at`), and a bar is named by its start: "the 09:28 close" is the bar stamped
  09:28, which ends at 09:29. A value's age is the distance from its bar's close to the
  instant it stands for.
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

## Sessions and reference levels

**Reference levels** ([features/calculations.py](features/calculations.py)): the previous
trading day's RTH high, low and close, and the overnight high and low - the day's bars
before 09:30 ET only. The chart draws the previous close and the overnight extremes.

The trading day starts at **18:00 ET the prior evening** (the Globex open), not midnight. The
collector's window ends at the trading day in progress, so from 18:00 ET it collects the next
session's overnight bars too.
[features/session_windows.py](features/session_windows.py) owns that rule and classifies
each bar `RTH` (09:30–16:00 ET) or `ETH`.

**Economic calendar** ([database/events.py](database/events.py)):

```bash
python -m database.events        # load data/economic_calendar.csv (+ ISM by rule); idempotent
```

It holds FOMC decisions and minutes (federalreserve.gov), BLS CPI, Employment Situation, PPI
and JOLTS as released - the October-November 2025 shutdown rescheduling included - and the
ISM PMIs at 10:00 on the first / third exchange session of the month (ISM's own calendar needs
a login), BEA GDP and Personal Income and Outlays (PCE) at the times BEA's release archive
shows them published, and Census advance retail sales from its retail release schedule.
Material Nasdaq-100 earnings come from SEC EDGAR (`python -m database.earnings`).
`data/economic_calendar_coverage.csv` says which days each source covers; within it
a day without a release had none, outside it the calendar is missing - never "no event". The
CSVs are the source of truth: when the agencies publish the next year's schedules (BLS in the
autumn), add the rows, extend the coverage, and load again; a release whose date changed
replaces its row. Nothing reads the tables at the moment.

## Tests

`pytest` (the database tests run when `TEST_DATABASE_URL` names a disposable database whose
name contains `test`; they reset it).

## Layout

| Path | What lives there |
|---|---|
| [collector/](collector/) | IB API client, coverage planner, request pacing, contract rolls |
| [database/](database/) | Connection, queries, migrations, the journal store, backfill/repair tools, economic calendar and earnings loaders |
| [features/](features/) | Trading calendar, session/timezone classification, VWAP, the chart's reference levels, the NQ evidence snapshot |
| [contracts/](contracts/) | Registered definitions: NQ-v2 labels and conventions, the pre-open structure and matcher, the forecast contract, the benchmark fan |
| [forecaster/](forecaster/) | The NQ journal: labels, structure annotation, forecasts, experiments, the live capture; the benchmark fan and its scoring (`fan_*.py`) |
| [matching/](matching/) | The P1 structural analogue matcher |
| [dashboard/](dashboard/) | NiceGUI app: the session bar on every page, Session Explorer (with the analogues and the forecast), Evaluation; the Lightweight Charts component; Update data (the collector, forecaster and live capture as jobs) |
| [scripts/](scripts/) | The journal CLI, the fan CLI, daily runner, DB backup, report generators |
| [tests/](tests/) | Pure and database tests for all of the above |
| [docs/](docs/) | [Data store & incremental collection](docs/data_store.md), [the NQ prompt-v2 journal](docs/nq_prompt_v2.md), [the benchmark fan](docs/fan.md), [the intermarket fan experiment](docs/fan_experiment.md), [reports](docs/reports/) |

## Database

PostgreSQL with TimescaleDB, upgraded in place and never regenerated. `init_database()`
applies any pending migrations on every process start, so simply running the app upgrades
it. `bars` is a TimescaleDB hypertable chunked monthly (30 days) on `timestamp_utc`; everything else
is a plain table.

Tables: `contracts`, `session_days` (the ledger of which days are held), `bars`,
`collection_runs`, `active_contracts` (the contract that stood for each symbol per day),
`asset_sources` (the logical-asset source map, versioned), and `economic_events` /
`economic_event_coverage` (an optional event calendar). Migration 0008 dropped the earlier
forecasting records (the v1 tables, the `forecast` schema) and `bar_receipts`.

The `journal` schema (`0009`-`0014`) holds the NQ prompt-v2 records
([docs/nq_prompt_v2.md](docs/nq_prompt_v2.md)): the definition registry, evidence
snapshots, revisioned outcomes, review sets, structure annotations, analogue sets, the
inference request ledger, the forecast runs with their evidence and predictions, the
registered experiments with their frozen cases and results (`0015`), and the live captures
with their bar receipts (`0016`). All of it is append-only - the database rejects UPDATE,
DELETE and TRUNCATE.

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

## Charting

The dashboard is [NiceGUI](https://nicegui.io) serving TradingView's
[Lightweight Charts](https://tradingview.github.io/lightweight-charts/) v5. The library is
**vendored** at `dashboard/components/lib/lightweight-charts.standalone.js`, so the
dashboard loads no CDN and works with no network access.

Every chart in the app goes through one component, [dashboard/components/lightweight_chart.js](dashboard/components/lightweight_chart.js).

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
survive a redraw. `update()` cannot rewrite a bar before the series' last one, so it is
used only where the replacement data is provably not older.

One thing Lightweight Charts has no native support for is supplied here:

- **Timezones.** The library always renders UTC. Timestamps are therefore converted to New
  York *wall clock* before being handed over (`to_epoch` in
  [dashboard/components/spec.py](dashboard/components/spec.py)), so the axis reads ET with
  DST handled per bar. A session runs 18:00 the prior evening → 17:00.

The component also carries a canvas primitive for filling the channel between two price
series. Nothing draws bands since the indicator overlays were removed, so it currently sits
unused — kept because it is the only way to shade between series in this library.

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
