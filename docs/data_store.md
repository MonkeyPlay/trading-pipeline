# Data store & incremental collection

The trading day is the unit of storage. Every bar carries the NY session date it
belongs to, and each day is written, replaced, and reasoned about as one whole.

## One growing database, never regenerated

The store is PostgreSQL with the TimescaleDB extension (`docker compose up -d` runs one
locally).

- **Production DB**: `trading_pipeline` (`Config.DATABASE_URL`). The collector writes
  here. It is append-only in spirit — historical
  bars are immutable once settled.
- **Dev DB**: `trading_pipeline_dev` (`Config.DEV_DATABASE_URL`). `populate_mock_data.py`
  writes here. `--reset` refuses to touch any DB that contains non-`MOCK` bars.
- Point any tool at another database with `--db postgresql://...` or the `DATABASE_URL`
  env var.

`bars` is a TimescaleDB **hypertable** chunked every 30 days on `timestamp_utc`. Every
per-day query also bounds `timestamp_utc` to a window around the session
(`queries._day_window`), so TimescaleDB only opens the one or two chunks that can hold
that day; `save_trading_day()` rejects a bar whose timestamp falls outside its day for
the same reason. The rest are ordinary tables. `database/connection.py` returns dates as `'YYYY-MM-DD'` and
timestamps as `'YYYY-MM-DD HH:MM:SS'` (UTC) strings and JSON columns as text, so query
callers see the same values the SQLite store used to hold.

## Day-partitioned schema

```
session_days                          bars
  contract_id  ─┐                       contract_id  ─┐
  interval      │                       interval      │
  price_type    ├── PRIMARY KEY  ◄───┐   price_type    ├── FK, ON DELETE CASCADE
  trading_day  ─┘                   └── trading_day  ─┘
  status        COMPLETE|PARTIAL|EMPTY  timestamp_utc  ─── + PRIMARY KEY
  bar_count, rth_bar_count,             session_scope  RTH|ETH (derived)
  open_bar_count, expected_bar_count    open/high/low/close/volume
  first_bar_utc, last_bar_utc           wap, bar_count
  source, fetched_at                    source, is_completed
```

`session_days` is the **ledger of which days we hold** — one row per contract,
interval, price type and NY trading day. `bars` is its child, so:

- a bar cannot exist outside a registered day (`trading_day` is `NOT NULL` and a
  foreign key), and
- deleting a day removes its bars atomically.

`session_scope` is *not* part of the bars primary key — it is derived from the
timestamp, so one timestamp now maps to exactly one bar.

### Day status

| status     | meaning                                                        | collector |
|------------|----------------------------------------------------------------|-----------|
| `COMPLETE` | at least 90% of expected bars, none still open, and - for a forecast target - its whole regular session | skipped   |
| `PARTIAL`  | fewer bars than expected, a gap in a forecast target's regular session, or bars flagged `is_completed = 0` | re-fetched |
| `EMPTY`    | the source returned nothing for this day — recorded, not guessed | skipped   |

`EMPTY` matters: without it a day the market never traded would be requested on
every single run. Each instrument's expected count is its `expected_bars` in
`config.INSTRUMENTS` (1380 one-minute bars for the 18:00–17:00 ET ES/NQ Globex day, a
lower bound for thinner series), and the 90% threshold is `DAY_COMPLETE_RATIO`.

Instruments flagged `rth_complete` (ES, NQ, RTY: every 09:30–16:00 minute trades) must
also hold their whole regular session: 390 bars, 210 on an early close. An early close (or a closed regular session)
lowers their day count by the regular-session minutes it loses
(`collector.coverage.day_expectation`). The planner re-judges every stored day from its
counts under this rule instead of trusting its stored status, so a day stored as
`COMPLETE` under an older, looser rule but missing regular-session minutes is re-fetched
the next time its window is collected.

### Writing a day

`database/queries.save_trading_day()` is the only write path for market data:

```python
save_trading_day(conn, contract_id=cid, trading_day="2026-09-08", bars=day_bars)
```

In one transaction it upserts the ledger row, deletes that day's existing bars,
inserts the new ones, and refreshes the day's counts and status. Re-running it is
idempotent, and a day is never left half-written. A bar whose `trading_day` does
not match raises rather than being filed under the wrong day; an empty `bars` list
records the day as `EMPTY`. `save_bars_by_day()` groups a mixed batch by day and
writes each one through the same path.

## Schema migrations

Schema is defined by the ordered files in `database/migrations/` (`NNNN_description.sql`)
and tracked in the `schema_migrations` table. Never edit an applied migration or
`schema.sql` (generated) — add a new migration. A migration may also register Python
`PRE_HOOKS` / `POST_HOOKS` in `database/migrations.py` for work SQL cannot do. Every
process migrates on start; an advisory lock keeps two of them from applying the same file.

`0001` is the SQLite store's final (v0003) schema restated for Postgres. An existing
SQLite file is carried over once with `python -m database.migrate_from_sqlite`.

```
python -m database.migrations                     # show versions
python -m database.migrations --db postgresql://...     # apply pending migrations
python -m database.migrations --snapshot                 # regenerate database/schema.sql
python -m database.backfill --db postgresql://...        # recompute session_days from bars
python -m database.migrate_from_sqlite --sqlite data/x.db  # one-time SQLite import
```

`init_database()` runs pending migrations on every process start, so upgrading is
automatic. An existing database is upgraded in place, never dropped.

Run `database.backfill` if `session_days` and `bars` ever drift apart — the collector
trusts the ledger to decide what to download, so it must agree with what is stored.

## Incremental collection

`collector/ib_collector.py` decides what to download **from the database, before
opening a socket**:

1. It looks the contract up locally (a contract month like `202609` also matches
   IB's full expiry `20260918`) and calls `collector/coverage.plan_trading_days()`,
   a single indexed read of `session_days`. Each expected trading day becomes:
   - `fetch` — not in the database at all
   - `refetch` — stored `PARTIAL`; the trading day in progress, whatever it holds (a
     run just after the 18:00 ET open may have stored it `EMPTY` - on a delayed feed
     IB has none of its bars yet); or within the trailing 2 sessions (the vendor still
     revises those) - except with `--no-trailing-refresh`, which runs every minute use
     (the dashboard's Auto mode, the fan's forward record), leaving that to the daily run
   - `ok` — already stored, skip it
2. If nothing needs fetching, the run ends **without connecting to IB**. Only a
   contract that has never been seen before requires a connection to plan.
3. Each remaining day gets one IB request, newest first (a 48h window that fully
   contains the NY session including its prior-evening Globex open, ending 06:00 UTC
   the next day - or now, while that is still to come). Bars outside the day are
   discarded, and the day is stored atomically via `save_trading_day()`. From the
   18:00 ET open until 06:00 UTC the session in progress and the one before it have
   the same window: it is requested once and both days are stored from its answer
   (IB forbids an identical request within 15 s; until 2026-10-06 the second request
   waited 13 s per symbol for that, which pushed the dashboard's Auto runs past their
   time limit right after the open).
   With `--workers N` the symbols are collected in parallel by N threads over the one
   IB connection, each with its own database connection; each contract's days stay in
   order. The shared pacer (`collector/pacing.py`) reserves every request's slot under a
   lock - identical requests 15 s apart, at most 60 in 10 minutes, 2 s between requests
   for the same contract (IB's burst limit is per contract) and 0.25 s between any two -
   so requests for different contracts go out together. A pacing violation pushes the
   next slot back 30 s instead of sleeping in IB's reader thread. The dashboard's Auto
   mode runs 4 workers; a plain run stays serial.
4. Bars within 2h of "now" are stored `is_completed = 0`, which keeps their day
   `PARTIAL` so a later run finalises it.
5. Every attempt — including days IB had no data for — is logged per day in
   `collection_runs` (`trading_day`, `interval`, `bars_written`).
6. When IB answers `HMDS query returned no data` for a day's whole window, the day is
   left missing rather than stored `EMPTY`: IB may simply not hold that contract's
   history (an expired contract of a thin future), and an `EMPTY` day is never asked
   for again. After `NO_DATA_SKIP_AFTER` (3) such days of one contract in a row, its
   remaining days are skipped for the run instead of using up the request budget; the
   next run tries again. Because days go newest first, a contract is only given up on
   when even its most recent days came back empty.

```
python -m collector.ib_collector --days 30 --plan-only                   # report only
python -m collector.ib_collector --days 30                               # fill the gaps
python -m collector.ib_collector --start 2026-08-01 --end 2026-08-31
python -m collector.ib_collector --days 30 --full                        # re-download all
python -m collector.ib_collector --expiry 202609 --days 30 --symbol NQ   # one pinned contract
```

The holiday calendar is hand-maintained and can be wrong, so any day already in the
ledger is also considered — a partial holiday session can still be completed.

Minute-level gap filling is intentionally not done: thin overnight periods have no
trades, so a missing minute is not a reliable signal. Days are.

### Futures rolls and `active_contracts`

A future with a `RollRule` (`config.INSTRUMENTS`) is collected as a chain rather than
one contract. The collector asks IB once for every listed and recently expired contract
(`includeExpired`), stores them all in `contracts` (with `contract_month`, trading and
liquid hours, time zone), and assigns each trading day the nearest eligible contract whose
expiry is more than the rule's `days_before_expiry` away (`collector/rolls.py`). Then:

- every day is fetched from the contract active on it, and
- the `Config.ROLL_WARMUP_SESSIONS` (default 7) trading days **before** each contract
  becomes active (or before the window starts) are fetched from that contract too. The
  last of them gives the first day after a roll a same-contract previous close - a
  return never mixes two expiries - and all of them give indicators computed on the new
  contract alone enough history on the roll day.
- the next contract's warm-up days are fetched as they happen: once a run's window
  reaches the first of them, they are planned for the incoming contract
  (`rolls.upcoming_roll`), so nothing is left to download on the roll morning.

The day → contract map is written to `active_contracts (symbol, trading_day,
contract_id, rule)`; single-contract instruments (indices, stocks) get rows too, so it is
the one place to look up "which contract was NQ on this day". Once the chain is
stored, planning is offline again; the chain is re-discovered only when it no longer
covers the window (a new roll ahead). `--expiry` / `<SYMBOL>_EXPIRY` pin one contract
and leave `active_contracts` untouched. `--plan-only` writes nothing.

Expected bars per day, and therefore `COMPLETE` vs `PARTIAL`, come from each
instrument's `expected_bars` rather than the futures constant: a spot index or an ETF
prints far fewer minutes than an 18:00–17:00 futures session and would otherwise stay
`PARTIAL` and be re-downloaded on every run. `database.backfill` keeps each day's own
expected count.

### Source map, units and bar timestamps

`asset_sources` holds every version of `config.ASSET_SOURCES` — logical asset → recorded
symbol, `what_to_show`, `value_kind`, `value_unit`, `bps_per_unit`, the freshness rule
`max_age_minutes`, proxy flag, and whether the symbol is in the configured collection
set. The collector registers it at the start of every run; an unchanged definition only
refreshes `last_registered_at`, a changed one becomes a new row. `current_asset_sources`
is the latest definition per asset.

`bars.timestamp_utc` is the bar's **open** time (IB's convention): the 1-minute bar
stamped 09:27 ET closes at 09:28 ET. Bars are exactly what IB sent — a minute without a
print is absent, never forward-filled — so the age of any value is recoverable.
`get_last_bar_at_or_before(conn, contract_id, as_of_utc)` returns the last bar that had
*closed* by an instant, with its `close_time_utc`.

Bars are named by their start: "the 09:28 close" is the bar stamped 09:28, ending 09:29.
The `bar_intervals` view exposes `bar_start_at` and `bar_end_at` explicitly.

A day whose median close falls outside the instrument's `plausible_range` is refused
(logged as a `FAILED` collection run), since it almost always means `value_unit` does not
match what the source sends.

### Reading days back

```python
get_day_bars(conn, contract_id, "2026-09-08")            # one session, in time order
get_bars(conn, cid, "1m", start_day="2026-09-01", end_day="2026-09-08")
get_stored_trading_days(conn, cid)                        # {day: ledger row}
list_trading_days(conn, cid, limit=100)                   # days holding bars, newest first
get_session_day(conn, cid, "2026-09-08")                  # one ledger row
get_active_contract(conn, "NQ", "2026-09-08")             # contract that stood for NQ that day
get_last_bar_at_or_before(conn, cid, "2026-09-08 13:28:00")  # last bar closed by 09:28 ET
get_asset_sources(conn)                                   # {asset: current source definition}
```

### Upgrade path

- Holiday calendar: swap the hand-maintained `_CME_HOLIDAYS` in `collector/coverage.py`
  for `pandas_market_calendars` (`CME_Equity`), including half-days — and derive
  per-day expected bar counts from it instead of one constant.
- Continuous contract: `active_contracts` already records the roll per day; a derived
  `bars_continuous` series can be built from it and `bars` (do not hand-edit).
- Other intervals: `session_days` is keyed by `interval`, so 5m/15m series coexist
  with 1m without any schema change.
