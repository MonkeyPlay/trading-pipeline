# NQ direction forecaster (scikit-learn)

The pre-open forecast of NQ's 15-minute direction from NQ's own data and a small group of related
markets. It replaced the LLM forecasting system on 2026-10-09.

**Where it stands.** On development data no ML configuration beats the historical frequencies
(arm A), and adding other instruments did not establish an improvement. **B (the analogues)
stays the forecast in force because it is the existing baseline**, not because these results show
it better than A: B − A is +0.017 [−0.013, +0.047], nominally worse. A remains the benchmark
that any promotion must beat. The three ML forecasts are issued beside B as **experimental**.
The forward evaluation `p1_ml_forward_v1` on fresh sessions decides whether any is promoted.

Contract: [contracts/nq_ml.py](../contracts/nq_ml.py). Code: [forecaster/ml_features.py](../forecaster/ml_features.py),
[ml_model.py](../forecaster/ml_model.py), [ml_train.py](../forecaster/ml_train.py),
[ml_eval.py](../forecaster/ml_eval.py), [ml_service.py](../forecaster/ml_service.py),
[delivery.py](../forecaster/delivery.py), [forecast_summary.py](../forecaster/forecast_summary.py).
Reports: [instrument inventory](reports/instrument_inventory.md),
[development comparison](reports/ml_development.md).

## The LLM system is gone

- **Code:** the Claude clients, prompts, runners, approvals and pilot are deleted, along with
  the `annotate-llm`, `llm-forecast` and `d-pilot` commands, `live --with-d`, and the
  dashboard's LLM control, approval dialog and second runner. Arms C and D are gone from the
  forecast contract, along with the LLM-only experiment designs. `anthropic` is removed from the
  requirements and is uninstalled at deployment. Nothing in the code can build a Claude client.
- **Data:** migration [0029](../database/migrations/0029_remove_llm.sql) deletes, in dependency
  order and with no cascade:
  - 13 arm C/D runs with 70 predictions and 13 evidence rows;
  - 6 analogue sets (4 members) built on 5 Claude annotations;
  - 14 inference requests (requests, responses and usage) and 6 annotation attempts;
  - 17 LLM definitions.

  It first checks that no kept record refers to any of them, and stops otherwise. Then it
  drops the LLM-only tables and columns and the `judgement` and `llm` vocabularies. Shared
  records - snapshots, bars and receipts, the rule-based annotations and sets, the A/B runs,
  outcomes, the A/B experiment, review sets, the fan and the RTH evaluations - are untouched.
  It was dry-run on production in a rolled-back transaction, and
  [tests/test_migration_0029.py](../tests/test_migration_0029.py) compares every kept table
  byte for byte.
- **Kept on purpose:** the user's P1/P2 specifications and the guideline's Appendix B in
  `prompts/source/`. They define the targets and fields the deterministic system uses. The
  registered rule-based protocol `nq_structure_rules_v4` still carries its historical
  `replaced_by` sentence, because a registered definition cannot change under its name.

## Instruments

Measured, not assumed: [reports/instrument_inventory.md](reports/instrument_inventory.md)
(`nq_journal.py instrument-inventory`). Twelve instruments are stored, all as 1-minute TRADES bars.

| | Role | Freshness limit | If missing |
|---|---|---|---|
| **NQ** | the target | 5 min | no forecast (the snapshot already waited for its cutoff bar) |
| **ES** | the closest related market; NQ's beta and residual move against it | 5 min | required: the multi-instrument model abstains |
| **RTY** | small caps: breadth and divergence | 5 min | optional: imputed, plus a missing indicator (history from 2025-09-18) |
| **VIX** | implied volatility; closed 09:15–09:30, so its 09:14 value is used | 20 min | optional |
| **10Y** | rates (Micro 10-Year Yield futures; changes in yield units) | 30 min | optional |
| **DX** | the dollar (index futures, a proxy) | 30 min | optional |

**Excluded, with reasons:**
- QQQ, SPY and IWM are the cash ETFs of NQ's, ES's and RTY's own indices.
- SMH has thin premarket prints and a 20-minute feed; it's a candidate for a later set.
- TNX only starts at 08:20 ET.
- VXN has no pre-open values.
- CL, GC, the cash DXY and the 2-year yield are not collected.

**On this feed:**
- **Receipt times:** live receipts exist on only 3 days (Oct 6, 7 and 9); the rest of the
  history is reconstruction.
- **Feed delay:** NQ about 11 min, ES, RTY and the 10-year about 15, DX, the ETFs and VIX about 20.

## Features (point in time)

`nq_ml_features_v1` defines every feature, as of the research profile's 09:29 cutoff (the one A and B use):

- **NQ's own:** returns since the previous 16:00 close, since 08:00 and over the last 30
  minutes (each divided by its own standard deviation over the previous 60 sessions); overnight
  realised volatility and volume against their recent norms; where the price sits in the
  overnight range; the gap and distances to the previous RTH high and low in units of the mean
  daily RTH range. These are the snapshot's references and ATRs in normalised form.
- **Context:** ES's and RTY's returns; VIX's change and level; the 10-year yield's change; DX's
  return. Each is normalised by its own instrument's earlier sessions; raw prices or points are
  never compared across instruments.
- **Cross-instrument:**
  - NQ's overnight and premarket moves net of beta × ES's, with beta and the residual's
    deviation taken from earlier sessions only;
  - the NQ–ES correlation;
  - agreement and dispersion of NQ, ES and RTY;
  - NQ's premarket return minus RTY's.
- **Calendar:** a high-tier event before the cutoff, or between the cutoff and the close, from
  the snapshot's frozen events.

**Alignment and availability:**
- **Which bar:** each input is the last completed 1-minute bar ending at or before the cutoff,
  on the session's own contract. New contracts carry warm-up bars, so a return never straddles
  a roll.
- **When it was known:** when an issue time is given, a bar counts only if it was received by
  then (`first_stored_at`). A common timestamp proves nothing.
- **Status of each input:**
  - `observed`: received live;
  - `reconstructed`: history without a live receipt;
  - `closed`: market closed at the cutoff, last value within its limit;
  - `delayed`: a fresher bar wasn't received yet, and the one used is within the limit;
  - `stale`: nothing received within the limit, so missing;
  - `missing`: no bar on the instrument's session;
  - `no_session`: the instrument had no session that day.
- **No filling:** nothing is forward-filled past these limits, and a missing return is never a
  zero.
- **Recorded with each run:** every feature's source, market timestamp, status and data age, and
  each instrument's raw moves.

## Models

| Model (arm) | Rows | Features | Estimator (development choice) |
|---|---|---|---|
| `nq_ml_nq_p1_v1` (N) | NQ sessions | NQ's own + calendar (11) | logistic regression, L2 |
| `nq_ml_multi_p1_v1` (M) | NQ sessions | + context, cross-instrument, missing indicators (30) | logistic regression, L2 |
| `nq_ml_pooled_p1_v1` (P) | NQ, ES and RTY sessions, each with its own label | own features + calendar + identity (13) | shallow gradient boosting |

- **Target:** `direction_15m` exactly as labelled: the 09:44 bar's close minus the 09:30 open,
  bullish above T, bearish below −T, else the neutral band. T is the snapshot's frozen value.
  The pooled rows use ES's and RTY's own moves against 0.5 × their own frozen two-minute ATR.
- **Preprocessing:** median imputation, plus scaling for the logistic model, inside the
  pipeline. It is fitted on the training window only and saved with the model.
- **Tuning:** a predefined grid. Logistic regression tries C ∈ {0.01, 0.03, 0.1, 0.3};
  boosting tries learning rate {0.03, 0.1} × {50, 150} iterations. Each point is fitted on the
  first 75 % of the window and scored by unhalved Brier on the last 25 %. Nothing else is
  selected, and there is no calibration step.
- **Artifacts:** trained offline (`nq_journal.py ml-train`) into `data/models/nq_ml/<version>/`
  as `model.joblib` and `manifest.json`. The manifest records:
  - training dates, sessions and rows per instrument, and class counts;
  - the feature list and the instrument manifest;
  - preprocessing, parameters and tuning scores;
  - software versions and the artifact's SHA-256.

  The registered algorithm definition names that hash. Issuing loads the artifact and refuses
  one whose bytes differ from the manifest or the registration; it never retrains. An existing
  version is never overwritten: a new model is a new version.
- **Deployed artifacts:** trained (`ml-train --until 2026-10-08`) on the 278 labelled sessions
  2025-09-02 to 2026-10-08. Two separate trainings produced byte-identical files.

  | Model | Chosen parameters | Training rows | SHA-256 |
  |---|---|---|---|
  | `nq_ml_nq_p1_v1` | C = 0.01 | NQ 278 | `b9786d182a624b3d…` |
  | `nq_ml_multi_p1_v1` | C = 0.03 | NQ 278 | `2a7ae831620dbc41…` |
  | `nq_ml_pooled_p1_v1` | learning rate 0.03, 50 iterations | NQ 278, ES 278, RTY 266 | `d33f8d8d3fda13f5…` |

## Development comparison

[reports/ml_development.md](reports/ml_development.md) (`nq_journal.py ml-dev-eval`).

- **Design:** chronological walk-forward over the research pool: train on every earlier session,
  skip a one-session embargo, test the next 20. That gives 8 folds, with 158 common test sessions
  (2026-02-24 to 2026-10-09).
- **Arms:** A and B come from their stored runs of the same snapshots.
- **Development data:** every session was inspected before.

| | Brier (unhalved, lower better) | vs A | vs B |
|---|---:|---|---|
| A frequencies | 0.602 | | |
| B analogues | 0.619 | +0.017 [−0.013, +0.047] | |
| NQ-only, logistic | 0.624 | +0.022 [+0.002, +0.044] | +0.005 [−0.032, +0.039] |
| multi-instrument, logistic | 0.613 | +0.011 [−0.007, +0.029] | −0.006 [−0.039, +0.027] |
| pooled NQ+ES+RTY, boosted | 0.600 | −0.002 [−0.023, +0.021] | −0.019 [−0.057, +0.021] |

- **Do the other instruments help?** Multi-instrument minus NQ-only is −0.011 [−0.030, +0.010]
  (logistic) and −0.003 [−0.030, +0.029] (boosted): not established.
- **Pooled training:** better than the NQ-trained model on NQ, −0.020 [−0.035, −0.005]
  (logistic) and −0.037 [−0.065, −0.007] (boosted). It only reaches the frequencies' level,
  though, so it is a separate candidate (P), not promoted.
- **Calibration:** A is well calibrated (mean ECE 0.022) and the pooled models nearly so (0.03);
  B and the NQ-trained models less so (0.06–0.10).
- **Latency:** about 5 s per session for the features, mostly loading about 85 sessions of bars
  for six instruments. The prediction itself takes milliseconds, and one build serves all three
  models.

## Forward evaluation `p1_ml_forward_v1`

[contracts/nq_ml.py](../contracts/nq_ml.py) `forward_manifest` and `promotion_rule`. It is
registered after deployment for the sessions from 2026-10-12. Registering writes one definition
and sends and schedules nothing.

**What it pins.** Registration records:
- the code revision, which must be a clean commit;
- the feature version and the forecast schema;
- each model's artifact SHA-256, training window, parameters and software versions.

It refuses when an artifact is missing or differs from its registered definition.

**What it scores.**
- **Arms:** A, B, N, M and P, plus the delivered forecast (the session's first recorded
  delivery).
- **The official run of each arm:** the first one issued by the replay deadline (cutoff + 35
  minutes, 10:04 ET), under the pinned artifact and feature version. A later run is a
  reconstruction and no case.
- **Primary score:** `direction_15m`, unhalved multiclass Brier, on the common sessions where all
  five arms are scored.
- **Endpoint:** the first 60 scheduled sessions. `experiment-score` refuses to score before the
  60th has its outcome.
- **Availability:** counted over every scheduled session, with an exact interval.

**The promotion rule, applied mechanically at the endpoint:**
1. **Candidates:** N, M and P. Each is compared with B, the existing baseline, and with A, the
   benchmark, and must beat **both**.
2. **Threshold:** the mean difference must be at most −0.01 (absolute, 0–2 scale), with the
   whole interval below zero. That is evidence of some improvement, not that the true gain is at
   least 0.01.
3. **Multiple comparisons:** Bonferroni over the three candidates. Every interval the rule uses is
   a 98.33 % moving-block bootstrap interval. Requiring both baselines is an intersection-union
   test and needs no further adjustment. Every other comparison is reported at 95 % and is
   descriptive only.
4. **Availability:** on time in at least 90 % of all scheduled sessions (the share you set for a
   forecast arm). Unavailable, failed, missing and reconstructed runs count against it.
5. **Pooled training:** P qualifies only if it also beats N under the same rule.
6. **Selection:** at most one model. If none qualifies, nothing changes. Otherwise take the
   first qualifying candidate in the order N, P, M (fewest live inputs first), and move to a
   later one only if it beats the current choice under the same rule.

The scoring names the model that qualifies. Promoting it means changing `STATUS` in a new
commit, deployed, which puts it first in the delivery order.

## In production

- **Auto:** each journal step issues the ML forecasts of every snapshot after the models'
  training window ([forecaster/ml_service.py](../forecaster/ml_service.py)).
  - **When:** once the context instruments' cutoff bars are in, or 30 minutes after the cutoff.
  - **How often:** once per snapshot, profile, mode and artifact; a repeat returns the stored
    run.
  - **Outcomes:** issued; unavailable with the reason (in-sample, no T, a required instrument
    stale, NQ's features missing); or failed.
  - **Targets:** the other six P1 targets are unavailable in ML runs, and B's forecast stands
    for them, labelled as B's.
- **Live capture:** on the research profile, the ML forecasts are issued live beside A and B
  (`nq_issue_live_v4`).
- **The forecast in force:** the first usable run in the delivery order (promoted ML, then B,
  then A), with the reason any earlier one was passed over. It is recorded once per session in
  `journal.forecast_deliveries` (migration 0030), stamped by the database clock. It is recorded
  only on the session's morning, by the replay deadline, and the first recorded row is the one
  in force: a later row never changes it.
- **Reconstructions:** a replay run issued after its replay deadline is a reconstruction. That
  covers a catch-up after downtime, such as the deployment's first ML runs for 2026-10-09. It is
  stored and shown as one (summary and arm tiles), but it is never in force, never a forward
  case and never timely. Nothing recreates missed live records: the fan's forward marks are
  issued within 30 minutes of the mark or not at all, and live captures happen live or not at
  all. Historical bars can be collected again; they then carry their late receipt times and are
  reconstructed inputs.
- **Presentation:** [forecaster/forecast_summary.py](../forecaster/forecast_summary.py) builds
  the summary from validated numbers only. It replaces the synthesis and contains no generated
  text, causal claim or invented confidence. It shows:
  - the target window and each arm's probabilities;
  - the ML forecasts' differences from A and B in percentage points;
  - B's reference levels, with distances in points and in multiples of T;
  - the instruments used, missing or stale, with their ages, and what each did, in its own
    units;
  - the cutoff, issue times, model versions and hashes, and the source of each target.

  It appears on the Forecast panel (and in `nq_journal.py summary --date D`). The arm tiles
  show A, B, N, M and P, and the radar draws only the arms that forecast every target.

## The feed and timeliness (unchanged findings)

- **Feed delay:** bars reach the store about 11 minutes after their minute ends. The official
  09:29 snapshot, and the A/B/ML forecasts with it, land after the open: a record, not a
  pre-open forecast. Each run shows its issue time against the cutoff and the open.
- **Earlier cutoff:** of the measured mornings, only a 09:15 cutoff beat the open, by about 3.5
  minutes ([reports/preopen_timeliness.md](reports/preopen_timeliness.md)). The `candidate_0915`
  profile and a configurable live wait exist for that, but have no pool yet.
- **Live capture:** waiting 20 s for the cutoff bar, it goes stale on this feed.
- **Choices left to you:** a real-time feed, or an earlier-cutoff profile.

## Commands

```bash
python scripts/nq_journal.py instrument-inventory        # docs/reports/instrument_inventory.md
python scripts/nq_journal.py ml-dev-eval                 # docs/reports/ml_development.md (development data)
python scripts/nq_journal.py ml-train                    # data/models/nq_ml/<version>/ (a new version per model)
python scripts/nq_journal.py summary --date 2026-10-12   # the session's forecast summary
python scripts/nq_journal.py experiment-register --name p1_ml_forward_v1 --design ml-forward --start 2026-10-12 --end 2027-06-30
python scripts/nq_journal.py experiment-score --name p1_ml_forward_v1   # once, at the endpoint
```
