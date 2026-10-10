# Deployment plan: full-session RTH analogues (migration 0031)

Prepared 2026-10-10 for the deployment over the weekend, before the futures open on Sunday at 18:00 ET.

**What this deploys:**

- `nq_match_rth_v3`: RTH analogues issued every minute of the regular session, to the calendar's close;
- `rth_session_v1`: its prospective evaluation;
- migration 0031, which they need.

The ML forecaster, the first-hour matcher (`nq_match_rth_v2`) and its evaluations stay exactly as registered.

The ML study ([ml_study.md](ml_study.md)) found no edge, so **no ML change is deployed and nothing is promoted.**

## What changes

**Production paths:**

| Area | Files |
|---|---|
| Matcher v3 and its calendar | `contracts/nq_rth.py`, `matching/rth.py` |
| The full-session evaluation | `contracts/rth_session.py` (new), `forecaster/rth_eval.py` |
| Issuing, misses, the cache | `forecaster/rth_analogues.py`, `database/journal_store.py` |
| Schema | `database/migrations/0031_rth_session.sql` (new) |
| Dashboard | `dashboard/views/rth_analogues.py` ("Full session" / "First hour" toggle), `dashboard/views/candles.py`, docstrings in `dashboard/jobs.py` and `dashboard/components/pipeline.py` |
| CLI | `scripts/nq_journal.py`: `rth-issue`, `rth-show --version`, `rth-backfill --version`, `rth-calibrate --minutes`, `rth-eval-status --version rth_session_v1` |
| Operations | `scripts/backup_db.sh` (now falls back to the database container's `pg_dump`), `scripts/migration_dry_run.py` (new), `database/rollback/0031_rth_session_down.sql` (new, emergency only) |

**Not production:**

- `research/` and `scripts/ml_study.py`: the ML study. Nothing in production imports them;
- `.venv-research/` and `data/research/`: git-ignored;
- tests and docs.

**Unchanged:**

- `forecaster/ml_*.py`, `contracts/nq_ml.py`, the model artifacts, the feature version;
- the fan;
- the first-hour definitions. Their registered hashes equal the code's:
  - `nq_match_rth_v2` `834cfe20bf1a…`
  - `rth_continuation_v2` `a1d37c5deff8…`
  - `rth_operational_v1` `e968e56fd7ac…`

## Compatibility

**Schema v31 is required.** Every process (dashboard, collector, CLI) checks the schema on start and refuses a mismatch. Until 0031 is applied, the new code refuses to start; once it is applied, 900f3cb refuses to start. Deploy the code and the migration together (`scripts/deploy.sh` does both). Never restart the dashboard on the new code without the migration.

**0031 is additive** (rehearsed on production 2026-10-10, rolled back):

- five nullable columns on `journal.rth_analogue_sets`;
- the new append-only table `journal.rth_issue_misses`;
- `elapsed_minutes` limits by version: 60 for v1 and v2 and the earlier evaluations, 390 for the new ones;
- the evaluation forecasts' unique key gains `horizon_minutes`.

The rehearsal: 834 RTH sets, every table's rows unchanged, about 40 s.

**p1_ml_forward_v2 is not affected.** Its runs are pinned by artifact sha256 and feature version, not by code revision (`forecaster/experiments._pinned`), and neither changes.

**New definitions register themselves** on the first RTH issue (Monday 09:31 ET), before their first forecast:

- `nq_match_rth_v3` (hash `e6ed5e07686f…`);
- `rth_session_v1` (hash `37b63c01d5a4…`).

## Steps

Run everything in `~/trading_pipeline`, with the dashboard stopped and outside a session.

1. **Review and commit** the working tree from the IDE: every modified file and the new ones in `git status`, including `research/`, `contracts/rth_session.py`, `database/migrations/0031_rth_session.sql` and `database/rollback/`. Note the commit as `REV`.
2. **Run the tests:**
   ```
   TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_test \
       .venv/bin/python -m pytest -q tests/
   ```
   Expect all to pass. `test_schema_deploy` deploys the last *commit*, so it passes only once 0031 is committed.
3. **Back up:**
   ```
   scripts/backup_db.sh
   ```
   This writes `data/backups/trading_pipeline_backup_<timestamp>.dump`, about 100 MB in about 30 s, using the container's `pg_dump`. TimescaleDB's circular-foreign-key warnings are expected. Keep the file name.
4. **Rehearse the migration** in a transaction that is rolled back:
   ```
   .venv/bin/python scripts/migration_dry_run.py
   ```
   Expect:
   - `rehearsing 0031_rth_session.sql`;
   - `journal.rth_analogue_sets: … 0 removed or changed, 0 added; new columns …`;
   - `journal.rth_issue_misses: new table`;
   - `0 existing table(s) with removed, changed or dropped rows or columns`;
   - `after the rollback: schema v30, tables as before`.
5. **Deploy:**
   ```
   scripts/deploy.sh REV
   ```
   It fast-forwards `main` (a no-op once the commit is on `main`), applies 0031 and checks the schema. Expect `deployed REV … schema v31` in `logs/deployments.log`.
6. **Start.** Run `.venv/bin/python -m dashboard.app`, open the dashboard and switch **Auto** on. Keep IB Gateway logged in. Do this before Sunday 18:00 ET. Auto is in-process state, so it has to be switched on again after every restart.
7. **Before Monday:**
   - `.venv/bin/python -m database.migrations` shows v31;
   - Sunday evening's Auto cycles run the collector only (no RTH step before the 09:30 open);
   - `scripts/nq_journal.py rth-eval-status --version rth_session_v1` says it is not registered yet.

## Monday 2026-10-12: the first live session (times ET)

Nothing below is verified until it is seen on Monday. A replay or a test is not a live check.

| When | Check | Command | Pass |
|---|---|---|---|
| 09:29–10:04 | ML runs N, M, P for 2026-10-12 issued by the replay deadline, and one delivery recorded | `scripts/nq_journal.py summary --date 2026-10-12` | three ML runs, not marked reconstruction; delivery B |
| 09:31 on | First-hour sets every minute to 10:30 | `scripts/nq_journal.py rth-show --date 2026-10-12 --version nq_match_rth_v2 --view issued` | one set per window; most `timely` (the new `issue_class`) |
| 09:31 on | Full-session sets every minute to the close, then "RTH closed" | `scripts/nq_journal.py rth-show --date 2026-10-12 --version nq_match_rth_v3 --view issued` | windows advance with the feed (about 11 minutes behind); misses listed with reasons |
| 09:31 | v3 and rth_session_v1 registered before their first forecast | `rth-eval-status --version rth_session_v1` | registered, hash `37b63c01d5a4…` |
| 09:45 / 10:00 / 10:15 | First-hour evaluation forecasts, as on 2026-10-09 | `rth-eval-status --version rth_continuation_v2` and `--version rth_operational_v1` | three cases each, stored by their limit |
| 10:00–15:30 | rth_session_v1 forecasts at each half-hour cutoff, each horizon that fits | `rth-eval-status --version rth_session_v1` | 4 horizons at most cutoffs; 15:00 and 15:30 lose the horizons that would cross the close |
| all session | Auto keeps its one-minute cadence | the dashboard's job panel or `logs/` | no overlapping runs; the RTH step about 1 s |
| all session | The RTH analogues view | dashboard | "Full session" toggle; "data through hh:mm ET"; feed age; timely/late tags |

## Rollback

**Prefer a forward fix.** If something misbehaves during a session, switch Auto off (first-hour and full-session issuing both stop) and fix forward with a new commit, deployed the same way.

**Before Monday 09:31 ET** (0031 applied, nothing written that uses it):

1. Stop the dashboard.
2. Run:
   ```
   docker exec -i trading_pipeline-timescaledb-1 psql "postgresql://trading:trading@localhost:5432/trading_pipeline" \
       -v ON_ERROR_STOP=1 < database/rollback/0031_rth_session_down.sql
   git revert --no-edit REV          # a new commit that restores 900f3cb's files; nothing is rewritten
   ```
3. Restart the dashboard on the reverted code.

The script restores schema v30 exactly. It was tested on the test database: the schema dump is identical to a fresh v30, and the script refuses, changing nothing, once any full-session set, miss or rth_session_v1 forecast exists.

**After Monday's first issue:** the journal is append-only, so the way back is the backup from step 3. Stop everything, restore the backup into an empty database (the commands are in the header of `scripts/backup_db.sh`, run through `docker exec -i`), and revert the commit. Every row written after the backup is lost: Monday's sets, forecasts and ML runs. That is why a forward fix is preferred.

## Experiment definitions

### rth_session_v1: deployed with this change

Defined in [contracts/rth_session.py](../../contracts/rth_session.py) and documented in [docs/rth_analogues.md](../rth_analogues.md#the-full-session-evaluation-rth_session_v1). It is fed only by `nq_match_rth_v3`.

- **Cutoffs:** every 30 minutes from 10:00 to 15:30 ET.
- **Horizons:** 15 minutes (primary), 5, 30 and 60. A horizon that would cross the close is never forecast.
- **Window:** it starts at the second full minute after the forecast is built. A forecast is eligible only when the database stamps it by that start.
- **Comparison:** RTH-20 against the frozen pre-open analogues and the same-clock history, for size (CRPS) and direction (Brier of the up-share), at 98.75 %.
- **Endpoint:** scored once, at 60 counted sessions or on 2027-06-30 (at least 30). Before that, only `rth-eval-status`.

The ML study's development estimate (section 4) is that RTH-20's member frequencies score *worse* than the same-clock history on direction at every horizon. Expect no directional improvement. The prospective result is what counts, and size is scored separately.

The study also found that a same-clock baseline's up-share carries a little estimation noise. With no view on direction, the same frequencies scored better. So if RTH-20 ever came out ahead of CLOCK on direction, check it against p(up) = 0.5 before reading it as skill. The definition is left as it is.

### p1_ml_forward_v3: a proposal, not registered

p1_ml_forward_v2 stays registered and runs from Monday, unchanged. This is what a stricter successor would be, if you want one. It must be registered before its first session and would supersede v2 only by your decision.

| | p1_ml_forward_v2 (registered) | p1_ml_forward_v3 (proposal) |
|---|---|---|
| Candidates | N, P, M | N, P, M (same artifacts and pins) |
| Claim | point ≤ −0.01 and the 98.33 % upper bound < 0, against both A and B | **material edge**: the upper bound < −0.01 against both A and B (intersection-union) |
| Multiplicity | Bonferroni over three; the switch between qualifying candidates (M vs N, M vs P) has no family-wise cover | **fixed sequence**: P, then N, then M, each at the full 95 %. Testing stops at the first that fails; the first that passes is promoted, with no switching. P also needs the material rule against N. One family-wise guarantee covers the whole procedure |
| Endpoint | 60 sessions, or 2027-06-30 | sized by the power analysis (ml_study.md section 5). At a true 0.03 improvement the material rule needs several hundred sessions, more than a year. Availability is checked at 60 sessions, never a score |
| Availability | ≥ 90 % on time | the same |
| Timing label | cutoff-origin research: the 09:29 forecast lands after the 09:30 open on this feed | the same, named as such in the manifest |

**Recommendation:** keep v2 as it is. Sixty sessions cannot confirm a realistic improvement (ml_study.md section 5). The development data give no candidate worth a longer test, so a v3 would spend more than a year of sessions on a model the study does not support. Register v3 only if you want a definitive answer on N, M and P regardless.
