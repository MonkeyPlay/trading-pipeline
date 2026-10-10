# Deployment plan: the ML redesign (split correction, seven-target bundles in shadow)

Prepared 2026-10-10 for a later, separately undertaken deployment. **Nothing has been deployed.** It replaces no
part of [deployment_plan_2026-10-10.md](deployment_plan_2026-10-10.md), whose record of the 0031 deployment stands.

**What this deploys** (four reviewable commits on `claude/eager-ritchie-xgxvch`, see the final handoff for IDs):

1. the session-date tuning split (`forecaster/ml_split.py`) and the corrected study `ml_pooled_split_v1`;
2. the seven-target bundles' infrastructure: registry, own-market labels, heads, service, per-arm scheduling;
3. their model experiments and the development evaluation `ml_bundle_eval_v1`;
4. the Forecast page, the summary and the release checks for them; documentation.

**What it does not deploy:** no promotion, no change to the delivery order (B in force, A the benchmark), no new
forward registration, no change to `p1_ml_forward_v2`, its artifacts, runs or deliveries.

## Schema and data

- **No migration.** The schema stays v31. Bundle runs use the existing ledger: `forecast_algorithm` definitions,
  the `model` estimation status (0030), predictions per target, the existing run statuses. Their definitions
  (`nq_ml_features_v2`, `nq_forecast_schema_v4`, `market_labels_v1` of kind `convention`, the three bundle versions)
  are registered when the first bundle artifact exists; every kind already exists.
- `scripts/migration_dry_run.py` must print "nothing pending". If it prints anything, stop: this revision has no
  migration.

## Compatibility with the registered experiments

| Registered | Why it is unchanged | Checked by |
|---|---|---|
| `nq_ml_features_v1`, `nq_forecast_schema_v3`, the label version | their definitions hash exactly as before | `tests/test_ml_split.py::test_the_registered_v1_definitions_are_unchanged`; `release_check.py artifacts` (registered = code hash) |
| v1 artifacts N `b9786d18…`, M `2a7ae831…`, P `d33f8d8d…` | not retrained; issuing loads them as before | `release_check.py artifacts` |
| `p1_ml_forward_v2` pins (artifacts + feature version) | the same artifacts and feature version | `release_check.py artifacts` |
| v1 probabilities | NQ-only features give the same N and P probabilities as the full build | `tests/test_ml_bundle.py::test_n_and_p_never_read_the_context_markets_current_data` |
| v1 training (fresh machine) | still the row-position tuner (`tune_rows_legacy`), byte-for-byte the v1 algorithm | `tests/test_ml_split.py::test_the_legacy_tuner_is_the_v1_algorithm_unchanged` |

**Behaviour that does change, deliberately:**

- **v1 N and P issue earlier.** They no longer wait for the context instruments' cutoff bars (only M does). Their
  official runs are still the first issued by the replay deadline; their probabilities are the same; their run
  evidence lists only NQ under the instruments. An earlier issue can only help their availability.
- **One arm's exception no longer stops the others** in Auto and in live capture; a raised arm is logged and
  counted (`issue_pending` returns `errors`).
- `ml-dev-eval` writes `ml_development_dates.md` (date split); `--legacy-row-split` reproduces `ml_development.md`.

## Steps (the established process)

Run in `~/trading_pipeline`, outside a session. `REV` is the final commit of this branch once merged to `main`.

1. **Commit and test** on a clean checkout of `REV`:
   `TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_test .venv/bin/python -m pytest -q tests/`
   - all must pass;
   - the bundle tests build a synthetic NQ/ES/RTY journal and train three bundles: allow about 10 minutes more than before.
2. **Stop every writer:** switch Auto off, stop the dashboard, then `.venv/bin/python scripts/release_check.py writers`
   must say "writers: none running".
3. **Back up and validate:** `BACKUP_RESTORE_CHECK=1 scripts/backup_db.sh` must print `VALID`; keep the dump under
   `data/backups/keep/`.
4. **Rehearse:** `.venv/bin/python scripts/migration_dry_run.py` must print "nothing pending".
5. **Deploy:** `scripts/deploy.sh REV`.
6. **Verify:** `release_check.py schema` (v31) and `release_check.py artifacts`:
   - the three v1 artifacts = manifest = registered;
   - "no artifact, not registered" for the three bundles;
   - `p1_ml_forward_v2`'s pins match.
7. **Start the dashboard and switch Auto on**, then check Auto as before (pid, cwd, lock, log, tooltip).

## After deployment: the shadow bundles (a separate decision)

Nothing issues bundle runs until artifacts exist. To start shadow runs:

1. `python scripts/nq_journal.py ml-bundle-train --until <the last labelled session>`:
   - reads the journal and every NQ, ES and RTY bar;
   - builds an ES and an RTY snapshot per session in memory;
   - writes only `data/models/nq_ml/nq_ml_bundle_*`;
   - with Auto off, it needs no writer stop; its first issue registers the bundle definitions.
2. `release_check.py artifacts`: each bundle = manifest (registered after its first issue).
3. The next Auto cycle issues the bundles' runs for every session after their training window:
   - those of past sessions are reconstructions (after the replay deadline): shown as such, never cases;
   - from the next morning, the N7 and P7 runs are issued with N and P, and M7 with M.
4. The development evaluation runs only with the user's agreement (it reads, writes only `docs/reports/`):
   `ml-bundle-eval predict`, then `score`.
   Its report is development evidence. A prospective protocol is a separate, reviewed registration
   ([ml_bundle_forward_proposal.md](ml_bundle_forward_proposal.md)).
5. The corrected pooled study: `python scripts/ml_pooled_split.py all` (read-only on the database;
   writes `docs/reports/ml_pooled_split_v1*`).

## Monday checks (health only; no score is opened)

| When (ET) | Check | Pass |
|---|---|---|
| 09:29-09:40 | `summary --date <day>` | the v1 N and P runs soon after the snapshot; M by the replay deadline; delivery B |
| 09:29-10:04 | (if bundles are trained) the same summary | N7 / P7 issued with N / P, M7 with M, all "shadow"; B in force |
| all day | `logs/pipeline_run.log` | no "could not be issued" line; `errors` 0 |

## Rollback

**Prefer a forward fix:** switch Auto off and deploy a corrected revision the same way.

**To stop the bundles only:** move `data/models/nq_ml/nq_ml_bundle_*` out of the models directory.
- Auto issues no bundle run without an artifact.
- Their runs already stored stay in the append-only journal.
- `release_check.py artifacts` then notes "registered but not installed" for a registered bundle without refusing (a
  shadow bundle: no forecast or registered evaluation depends on it); record the removal in `logs/deployments.log`.

**To return to the previous code:** `scripts/deploy.sh <previous REV>`.
- **No schema change:** nothing to reverse.
- **The old code ignores bundle runs:**
  - their algorithm versions map to no arm in its summary or dashboard, and are never in its delivery order;
  - registered bundle definitions are inert rows.
- **The v1 artifacts and `p1_ml_forward_v2` are untouched by either direction.**
- The 0031 reverse script (`database/rollback/0031_rth_session_down.sql`) addresses only migration 0031. It is
  **not** a rollback for this redesign and must not be run for it.
