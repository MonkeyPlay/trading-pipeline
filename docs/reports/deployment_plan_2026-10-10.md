# Deployment plan: full-session RTH analogues (migration 0031)

Prepared 2026-10-10 for a deployment over the weekend, before the futures open on Sunday at 18:00 ET. Revised the same day after the review of 2026-10-10.

**What this deploys:**

- `nq_match_rth_v3`: RTH analogues every minute of the regular session, to the calendar's close;
- `rth_session_v1`: their prospective evaluation;
- migration 0031, which they need;
- the tools that make the deployment safe: `scripts/release_check.py`, `scripts/verify_backup.py`, `scripts/migration_dry_run.py`, and the emergency reverse script for 0031.

The ML forecaster, the first-hour matcher (`nq_match_rth_v2`) and its evaluations stay exactly as registered. The ML study ([ml_study.md](ml_study.md)) found no directional edge, so **no ML change is deployed and nothing is promoted.**

**Status:**

- **Executed:** 0031 was deployed with `f00f41e` on 2026-10-10 at 07:01:55 UTC; see "What happened" below.
- **Every later revision** is recorded in `logs/deployments.log`.

## What changes

| Area | Files |
|---|---|
| Matcher v3 and its calendar | `contracts/nq_rth.py`, `matching/rth.py` |
| The full-session evaluation | `contracts/rth_session.py`, `forecaster/rth_eval.py` |
| Issuing, misses, the cache | `forecaster/rth_analogues.py`, `database/journal_store.py` |
| Schema | `database/migrations/0031_rth_session.sql` |
| Dashboard | `dashboard/views/rth_analogues.py` ("Full session" / "First hour"), `dashboard/views/candles.py`; docstrings in `dashboard/jobs.py` and `dashboard/components/pipeline.py` |
| CLI | `scripts/nq_journal.py`: `rth-issue`, `rth-show --version`, `rth-backfill --version`, `rth-calibrate --minutes`, `rth-eval-status --version rth_session_v1` |
| Deployment and recovery | `scripts/deploy.sh`, `scripts/release_check.py`, `scripts/migration_dry_run.py`, `scripts/backup_db.sh`, `scripts/verify_backup.py`, `database/rollback/0031_rth_session_down.sql` |

**Not production:**

- `research/` and `scripts/ml_study.py`: nothing in production imports them.
- Tests and docs.
- Git-ignored, never committed: `.venv-research/`, `data/research/` (study working files and TabPFN weights), `data/backups/`, `.env`, `logs/`.

## Compatibility

**Schema v31 is required.** Every process (dashboard, collector, CLI) checks the schema on start and refuses a mismatch:

- the v31 code refuses v30;
- `900f3cb` refuses v31.

`scripts/deploy.sh` moves the code and the schema together.

**0031 is additive.** Production was rehearsed in a rolled-back transaction on 2026-10-10:

- five nullable columns on `journal.rth_analogue_sets`;
- the append-only table `journal.rth_issue_misses`;
- `elapsed_minutes` limits by version: 60 for v1, v2 and the earlier evaluations, 390 for the new ones;
- the evaluation forecasts' unique key gains `horizon_minutes`.

Every existing row was unchanged.

**Frozen experiments.** These were checked against production on 2026-10-10 and are re-checked by `scripts/release_check.py artifacts` on every deployment.

| Definition | Registered | Runtime now |
|---|---|---|
| `nq_match_rth_v2` | hash `834cfe20bf1a…` | the code's record has the same hash |
| `rth_continuation_v2`, `rth_operational_v1` | `a1d37c5deff8…`, `e968e56fd7ac…` | the same |
| `p1_ml_forward_v2` | hash `c3cfaed26589…`, from `900f3cb` | pins N `b9786d182a62`, M `2a7ae831620d`, P `d33f8d8d3fda` and `nq_ml_features_v1` = the installed artifacts and the code's feature version. The promotion rule (−0.01, 98.33 %, 90 % availability, order N, P, M), the 60-session endpoint and the 35-minute replay deadline equal the code's. No ML runtime file has changed since `900f3cb` (`git diff 900f3cb HEAD` on `contracts/nq_ml.py`, `forecaster/ml_*.py`, `forecaster/experiments.py`, `forecaster/delivery.py`, `features/`: empty) |
| `nq_match_rth_v3`, `rth_session_v1` | not yet: they register at their first issue | hashes `e6ed5e07686f…`, `37b63c01d5a4…` |

The forward evaluation pins artifacts and the feature version, not a code revision (`forecaster/experiments._pinned`). A deployment that leaves the ML path unchanged keeps its cases valid.

## The full session: what is covered, and by what

| Requirement | Evidence |
|---|---|
| Session length from the calendar; early closes; updates after 10:30 through the close | `contracts/nq_rth.session_minutes`; `tests/test_rth_session.py` (390, 210 on 2025-11-28, holidays, DST; issues at 10:45, 12:27, 389 and 390 minutes), `test_rth_analogues` (due window) |
| Full-prefix loading, matching limits, volume baselines, store constraints | `forecaster/rth_analogues.load_session_openings`, `matching/rth.volume_baselines(max_minutes=390)`; 0031's limits by version (`tests/test_migration_0031.py`, `test_rth_session`: v2 refused past 60, v3 past 390) |
| CLI and dashboard selectors | `scripts/nq_journal.py --version` (CLI assertions in `test_rth_analogues`); the dashboard's version choice and toggle are exercised by hand, with no UI test (stated, not claimed) |
| Completed-bar confirmation, gaps, delayed receipts, real issue times | `test_rth_session`: last-bar confirmation, a gap at 13:00, the database's `issue_class` (timely / late / reconstruction) from `inputs_received_at` and its own clock |
| Separate versions; first-hour experiments preserved | v3 and `rth_session_v1` are new definitions; the v2 hash is pinned by a test; production's registered hashes are unchanged |
| No target at or after the close; no shortened horizon | `test_rth_session` (360 minutes: only 5 and 15; nothing past 390), `tests/test_ml_study.py` (windows crossing the close) |
| Idempotent issuing, a cross-process lock, no fabricated live records in catch-up | `test_rth_session` (lock busy; expired windows recorded as misses, never stored later under a made-up time), `test_rth_analogues` (idempotent identity) |
| As issued against reconstructed | `test_rth_session` (playback shows nothing stored later), `test_rth_analogues` |
| Full-day cost | Measured on production data (docs/rth_analogues.md): a v3 issue takes 0.74 s at the median, a ranking at most 71 ms, inside Auto's one-minute cycle. Not re-run for this revision: its matcher and issuing code are unchanged |

## Steps

Run everything in `~/trading_pipeline`, outside a session.

1. **Commit and test.** Commit the release, note it as `REV`, and run on a clean checkout of `REV`:
   ```
   TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_test \
       .venv/bin/python -m pytest -q tests/
   ```
   All must pass. The database and deployment tests run only with that variable set.
2. **Stop every writer.** Switch Auto off and stop the dashboard. No collector, journal, fan or study process may run from the checkout. Check with:
   ```
   .venv/bin/python scripts/release_check.py writers
   ```
   It must say "writers: none running". `deploy.sh` refuses otherwise.
3. **Back up and validate.**
   ```
   BACKUP_RESTORE_CHECK=1 scripts/backup_db.sh
   ```
   It writes `data/backups/trading_pipeline_backup_<local date_time>.dump` and a record (`.dump.json`) holding:
   - the sha256;
   - the archive header;
   - the source database (no credentials), its schema and every table's row count, read right after the dump;
   - a restore into the disposable `trading_pipeline_restore_check` database (never production), with its counts compared, then dropped.

   It must print `VALID`. Move a backup worth keeping into `data/backups/keep/`; the 30-day cleanup never touches that folder.
4. **Rehearse.**
   ```
   .venv/bin/python scripts/migration_dry_run.py
   ```
   Pending migrations are applied in one transaction, compared row by row, and rolled back. Then the schema version, the tables, every column, constraint, index, function, trigger and row are checked to be as before.
   - A migration that fails is reported (exit 2) and rolled back the same way.
   - "nothing pending" is the expected output once 0031 is in.
5. **Deploy.**
   ```
   scripts/deploy.sh REV
   ```
   In order, it:
   - refuses dirty or non-main checkouts, non-fast-forwards and running writers;
   - checks the schema and the ML artifacts before anything changes;
   - fast-forwards `main` (no reset, no links, runtime files untouched);
   - migrates and checks the schema and artifacts again;
   - logs `deployed REV … database localhost:5432/trading_pipeline at schema vN, artifacts verified - the dashboard has not been restarted`.

   A failure after the fast-forward is logged as `FAILED at '<step>'`, with where `main` and the schema were left.
6. **Verify:**
   ```
   .venv/bin/python scripts/release_check.py schema
   .venv/bin/python scripts/release_check.py artifacts
   ```
7. **Start the dashboard and switch Auto on.** Run `.venv/bin/python -m dashboard.app` in `~/trading_pipeline` and switch Auto on in its page. Auto is the dashboard's in-process state, so it must be switched on again after every restart.
8. **Check Auto.** Check the dashboard's pid, its cwd (`/proc/<pid>/cwd` = `~/trading_pipeline`) and its start time (after the deployment). Then check:
   - `data/auto_mode.lock` names that pid and is held: only one Auto;
   - `logs/pipeline_run.log` says "Auto mode switched on (pid …)";
   - the Auto button's tooltip reads "On - waiting: no session in progress" until Sunday 18:00 ET (22:00 UTC, 23:00 UK).

## What happened on 2026-10-10

| Step | Evidence |
|---|---|
| Commit | `f37c113`, `f00f41e`; the full suite ran on a clone with an identical tree (`be78857…`); pushed by the user at 07:05:42 UTC |
| Writers stopped | No dashboard process from 06:34 UTC; the next one started at 07:03:07 UTC, after the deployment |
| Backup | `trading_pipeline_backup_20261010_080108.dump`, taken 07:01:08 UTC at v30, sha256 `d7508403158f…`. Restored into a disposable database: v30, 39 tables, 4,724,528 rows, `pg_restore` exit 0. The counts equal production's, apart from 0031's own changes. Kept as `data/backups/keep/v30_before_0031_20261010_080108.dump` |
| Rehearsal | clean: 834 RTH sets unchanged; rolled back to v30 |
| Deploy | `deployed f00f41e… schema v31` at 07:01:55 UTC (`logs/deployments.log`) |
| Dashboard | Started by the user at 07:03:07 UTC (pid 876089, cwd `~/trading_pipeline`). Auto was switched on at 07:03:17 and off again at 07:04:14 |

Also kept, and restore-tested the same way:

- `keep/v30_before_0031_20261010_003512.dump` (23:35:12 UTC on 2026-10-09, sha256 `4d848958eb58…`);
- `trading_pipeline_backup_20261010_073058.dump` (06:30:59 UTC, sha256 `9c09bb84a661…`).

The 2026-10-04 backup predates migrations 0029 and 0030 and is not a rollback point for this release.

## Monday 2026-10-12: the first live session

The first possible issue is at 09:31 ET, which is **14:31 UK time**. On this feed (about 10 minutes behind), the first sets usually land a few minutes after that. Nothing below is verified until it is seen on Monday; a Friday replay or a simulated clock is not a substitute.

The SQL below runs through the database container (the host has no `psql`):
`docker exec -it trading_pipeline-timescaledb-1 psql -U trading -d trading_pipeline -c "<query>"`.

Only health is checked for the sealed experiments (`rth_continuation_v2`, `rth_operational_v1`, `rth_session_v1`, `p1_ml_forward_v2`). Their scores are never opened before their endpoints: `rth-eval-score` and `experiment-score` refuse before then anyway.

| When (ET) | Check | How (read-only) | Pass |
|---|---|---|---|
| 09:29–10:04 | ML runs N, M, P for 2026-10-12 by the replay deadline, and one delivery | `scripts/nq_journal.py summary --date 2026-10-12` | three runs, not reconstructions; delivery B |
| 09:31 on | First-hour sets every minute to 10:30, with the first-hour evaluations still collecting | `rth-show --date 2026-10-12 --version nq_match_rth_v2 --view issued`; `rth-eval-status --version rth_continuation_v2` and `--version rth_operational_v1` | a set per window; three cases each at 09:45, 10:00 and 10:15 |
| 09:31 on | Full-session sets: their actual issue and receipt times | `SELECT elapsed_minutes, created_at, inputs_received_at, confirmed_received_at, issue_class FROM journal.rth_analogue_sets WHERE matcher_version = 'nq_match_rth_v3' AND session_date = '2026-10-12' ORDER BY created_at` | `created_at` minus `inputs_received_at` is usually under 2 minutes (`timely`); the feed's age shows in `inputs_received_at` against the window's end |
| 10:31 on | Windows beyond 60 minutes | the same query | `elapsed_minutes` above 60, growing each minute through midday |
| midday, late session | Updates continue; gaps are recorded, not filled | `rth-show … --version nq_match_rth_v3` (prints the misses); `SELECT * FROM journal.rth_issue_misses WHERE session_date = '2026-10-12'` | windows advance; any miss carries a reason (coalesced, not_running, expired) |
| 09:31 | v3 and `rth_session_v1` registered before their first forecast | `rth-eval-status --version rth_session_v1` | registered, hash `37b63c01d5a4…` |
| 10:00–15:30 | `rth_session_v1` forecasts, health only | `rth-eval-status --version rth_session_v1` | a case per cutoff and fitting horizon. From 15:00, the horizons that would cross 16:00 are absent, not shortened |
| 16:00 on | The close | the dashboard's RTH view; the v3 query | the last window is 390; "RTH closed"; no window after the close |
| all session | Latency, and Auto's cycle | `logs/pipeline_run.log`; the dashboard's job panel | one run a minute, no overlap; the RTH step about 1 s |

## Rollback

**Prefer a forward fix.** If something misbehaves, switch Auto off and fix forward with a new revision deployed the same way. Never restore production automatically because a check failed: decide first, knowing what a restore loses.

**The rollback target:** code `900f3cb`, schema v30, the same ML artifacts (`b9786d18…`, `2a7ae831…`, `d33f8d8d…`), and the backups listed above.

**While no row needs v31,** the reverse script applies: `database/rollback/0031_rth_session_down.sql`. Whether it is safe is decided from the data, not the calendar. It refuses, changing nothing, when any of these exist, whoever wrote them (Auto, a manual issue, a backfill, a test):

- a full-session set;
- any set stored since 0031: its trigger stamps every new row, first-hour sets included;
- a recorded miss;
- a forecast or result of a new evaluation;
- an evaluation key the v30 constraint would reject.

It locks every table it reads or alters before checking, so no writer can add a row in between, and it runs as one transaction. It was tested on the test database:

- the schema afterwards is exactly v30;
- every first-hour record and every definition is kept byte for byte;
- it refuses each kind of row above;
- a refusal leaves no partial change;
- a writer waits out the lock.

The commands:

```
.venv/bin/python scripts/release_check.py writers      # stop everything first
docker exec -i trading_pipeline-timescaledb-1 psql "postgresql://trading:trading@localhost:5432/trading_pipeline" \
    -v ON_ERROR_STOP=1 < database/rollback/0031_rth_session_down.sql
git revert --no-edit <the commits since 900f3cb>       # new commits restoring 900f3cb's files; nothing rewritten
scripts/deploy.sh HEAD
```

**Once a row needs v31,** the only way back is a backup. Stop everything, then restore the chosen archive into an empty database. The restore steps are in `scripts/backup_db.sh`'s header, run through `docker exec -i`, and `scripts/verify_backup.py --restore` shows the procedure on a disposable database first.

**A restore loses every write after the backup's snapshot:** sets, forecasts, ML runs, deliveries, collected bars. That holds unless those writes are exported first and reconciled by hand after the restore. Bars can be collected again from IB; journal records cannot be recreated as they were issued.

## Experiment definitions

### rth_session_v1: deployed with this change

Defined in [contracts/rth_session.py](../../contracts/rth_session.py) and documented in [docs/rth_analogues.md](../rth_analogues.md#the-full-session-evaluation-rth_session_v1). It is fed only by `nq_match_rth_v3`.

- **Cutoffs:** every 30 minutes from 10:00 to 15:30 ET.
- **Horizons:** 15 minutes (primary), 5, 30 and 60. A horizon that would cross the close is never forecast.
- **Window:** it starts at the second full minute after the build. A forecast is eligible only when the database stamps it by that start.
- **Comparison:** RTH-20 against the frozen pre-open analogues and the same-clock history, for size (CRPS) and direction (Brier of the up-share), at 98.75 %.
- **Endpoint:** scored once, at 60 counted sessions or on 2027-06-30 (at least 30). Before that, only `rth-eval-status`.

The study's development estimate (section 4) is that RTH-20's member frequencies score worse than the same-clock history on direction at every horizon. The same-clock up-share itself carries some estimation noise, so a direction win over CLOCK alone should be checked against p(up) = 0.5 before it is read as skill. The definition stays as it is.

### p1_ml_forward_v3: a proposal, not registered

`p1_ml_forward_v2` stays registered and runs from Monday, unchanged. This is what a stricter successor would be, if you want one. It must be registered before its first session, and it supersedes v2 only by your decision.

| | p1_ml_forward_v2 (registered) | p1_ml_forward_v3 (proposal) |
|---|---|---|
| Candidates | N, P, M | the same artifacts and pins |
| Claim | point ≤ −0.01 and the 98.33 % upper bound < 0, against both A and B | **material edge**: the upper bound < −0.01 against both A and B (intersection-union) |
| Multiplicity | Bonferroni over three. Switching between qualifying candidates (M against N, M against P) has no family-wise cover | **fixed sequence**: P, then N, then M, each at the full 95 %. Testing stops at the first that fails, and the first that passes is promoted, with no switching. P also needs the material rule against N |
| Endpoint | 60 sessions, or 2027-06-30 | sized by ml_study.md section 5: at a true 0.03 the material rule needs several hundred sessions. Availability is checked at 60, never a score |
| Availability | ≥ 90 % on time | the same |
| Timing label | cutoff-origin research: the 09:29 forecast lands after the 09:30 open on this feed | the same, named so in the manifest |

**Recommendation:** keep v2. Sixty sessions cannot confirm a realistic improvement, and the development data offer no candidate worth a longer test.
