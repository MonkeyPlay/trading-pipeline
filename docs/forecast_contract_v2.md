# v2 forecast records & feature contract (`nq_features_v2`)

This is the NQ pre-open contract: what a snapshot contains, when its inputs were
knowable, how a forecast and its outcome are recorded, and how corrections are
versioned instead of overwritten. The market-data store (`contracts`,
`session_days`, `bars`, `active_contracts`, `asset_sources`) is unchanged; the v1
analogue pipeline and its tables keep working beside it.

It implements the *NASDAQ-100 prediction and classification schema* (`nq_schema_v2`):
the predictor consumes typed feature values only - no chart images, no prompts, no
language model - and a separate deterministic service labels completed outcomes.

| Piece | Where |
|---|---|
| Record schema, triggers, views | [database/migrations/0004_forecast_records.sql](../database/migrations/0004_forecast_records.sql) |
| Persistence | [database/forecast_store.py](../database/forecast_store.py) |
| Session calendar (versioned) | [features/calendar.py](../features/calendar.py) |
| Indicator primitives | [features/indicators.py](../features/indicators.py) |
| Feature catalogue + version parameters | [features/catalogue.py](../features/catalogue.py) |
| Snapshot builder | [features/nq_v2.py](../features/nq_v2.py), data access in [features/market_data.py](../features/market_data.py) |
| Labels + outcome metrics | [forecaster/labels_v2.py](../forecaster/labels_v2.py) |
| Model registry: scikit-learn model + climatology baseline | [forecaster/models_v2.py](../forecaster/models_v2.py) |
| Prediction status / outcome window columns | [database/migrations/0006_prediction_status.sql](../database/migrations/0006_prediction_status.sql) |
| CLI | [scripts/nq_forecast_v2.py](../scripts/nq_forecast_v2.py) |

## 1. Records

All v2 records live in the PostgreSQL schema `forecast`. A separate schema lets them
use the contract's names (`feature_snapshots`, `predictions`) without colliding with
the v1 tables of the same name in `public`, which the dashboard still reads.

| Record | Key | Notes |
|---|---|---|
| `forecast.feature_snapshots` | `snapshot_id` UUID; unique on (instrument_id, session_date, cutoff_at, feature_version, source_revision_id) | metadata columns + `features` JSONB + `feature_status` + `source_status` + `reference_values` |
| `forecast.forecast_runs` | `forecast_run_id` UUID | snapshot, model_version, label_version, generated_at, `calibration` (provenance: training window, class counts, cross-validation scores, selected estimator), `calibration_version`, `input_quality_status`, git `code_revision` |
| `forecast.predictions` | (forecast_run_id, target_id) | predicted_label, full `probabilities` object, `prediction_status` (issued / abstained / unavailable), `decision_reason`, `calibration_status`, `abstained` + detail |
| `forecast.realised_outcomes` | (snapshot_id, label_version, target_id, outcome_revision) | actual_label, eligible + label status (`ineligibility_reason`), outcome window, `available_at`, `computed_at`, link to the metrics revision it came from |
| `forecast.outcome_metrics` | (snapshot_id, metric_version, outcome_revision) | continuous metrics JSONB, per-metric status, `available_at` |

Supporting registries: `feature_versions` / `feature_definitions` (the catalogue),
`label_versions` / `label_definitions` (each target's vocabulary), `model_versions`
(targets and required features), and `source_revisions` (content-addressed
manifests). Views: `forecast.feature_matrix_nq_v2` (typed feature columns),
`forecast.prediction_outcomes` (the prediction/outcome join), and
`public.bar_intervals` (bars with explicit `bar_start_at` / `bar_end_at`).

**Nothing is overwritten.** Every `forecast.*` table has triggers rejecting UPDATE,
DELETE and TRUNCATE. Corrections are new rows:

- a snapshot built from revised data has a new `source_revision_id`, so it is a new
  snapshot (optionally `supersedes_snapshot_id` + `correction_reason`);
- a re-run model is a new `forecast_run_id` (optionally `supersedes_run_id`);
- a recomputed outcome that differs from the latest one becomes `outcome_revision + 1`;
  an identical one is not stored again.
- a definition changed under an existing version name is refused
  (`VersionConflict`): versions are registered with a definition hash.

**Joins.** A prediction reaches its outcome through `snapshot_id`, `target_id` and the
run's `label_version`. `forecast_store.get_prediction_outcomes()` requires the outcome
revision to be chosen explicitly: `outcome_revision=N`, or `outcomes_as_of=ts` (per
outcome, the latest revision computed by then).

**Vocabulary.** Predictions and realised labels are both checked by trigger against the
single `label_definitions` row of their (label_version, target_id): the label must be
in the vocabulary, the probability object must have exactly the vocabulary's keys,
each in [0, 1], summing to 1 (±1e-6). An issued label must be the most probable one,
ties going to the earliest label in the vocabulary; an unavailable prediction has no
probabilities, an abstained one may keep them.

## 2. Time, units and calculations

| Rule | Implementation |
|---|---|
| `bar_start_at` = start of `[start, start + 1m)`, UTC | `bars.timestamp_utc` already is the bar start (IB labels by start); documented on the column and exposed as `bar_intervals.bar_start_at`. An end-labelled provider must be shifted at ingestion. |
| T = 09:29:00 ET; only `bar_end_at <= T` | every read ends at T; the latest bar read starts 09:28. The builder is tested to be unchanged by any bar at/after 09:29 (`test_no_lookahead`). |
| live: `features_frozen_at <= generated_at < 09:30` | CHECK on `feature_snapshots` (live frozen before `forecast.rth_open_at(session_date)`), trigger on `forecast_runs` (generated_at ≥ frozen_at always; < 09:30 for live). `build_snapshot` refuses a live capture outside [T, 09:30). |
| historical values are `historical_reconstruction` / `unverified_historical` | the only way to get `live_capture` is `nq_forecast_v2.py live`; `verified` is set only for a live capture where every included source's current-session observation has a real-time receipt (`bar_receipts`) of the exact value used, received by `features_frozen_at`, and every reference value was stored by then. |
| P = close of the bar starting 09:28; missing → null | taken from exactly that minute; no substitution (`test_missing_p_is_null_not_substituted`). |
| Cprev / PDH / PDL from the previous *scheduled* RTH session | `[09:30, scheduled close)` of `calendar.previous_session()`; the 12:59 bar on an early-close day. The open and closing minute must exist and ≥ 90 % of minutes. |
| A = 14-session Wilder ATR through the previous session | TR with the same contract's previous close; seeded with 14 TRs, `(13·ATR + TR)/14`. No current-day RTH bar is read. A ≤ 0 → ATR-scaled features `undefined`. |
| ON = 18:00 ET previous calendar day → T | 929 expected minutes; Monday uses Sunday 18:00. Globex has no scheduled closure in that window, so every missing minute is a feed gap; below 90 % coverage the ON features are `missing`. |
| Last 60m = [08:29, 09:29), return 09:28 vs 08:28 | all 60 bars plus the 08:28 bar are required. |
| 5m / 15m on fixed ET clock boundaries | `indicators.aggregate_clock`; only buckets with every constituent minute feed an EMA. Latest 5m endpoint must be 09:25 and latest 15m 09:15, else `stale`. The 15m slope needs the five consecutive buckets ending 08:15…09:15. |
| EMA: α = 2/(n+1), SMA seed, ≥ 5n warm-up | evaluated over a **fixed trailing window of exactly 5n complete buckets**, seeded with its first n, so the value never depends on how much history was loaded. Same for Wilder ATR (1m and daily). |
| Units | returns are decimal; ATR distances signed; yields in bps via each instrument's `bps_per_unit` (configured once in `config.INSTRUMENTS`); VIX/VXN in index points. |
| RVOL 30 / divergence 60 previous eligible sessions, full baseline, current session excluded | `SnapshotBuilder.baseline()` searches back up to 90 scheduled sessions for that many *valid* values; fewer → `insufficient_history`. Sample SD; SD 0 → null. |
| Null ≠ zero; finite only; path efficiency of a constant path = 0 | every value goes through `catalogue.check_value`; JSON is written with `allow_nan=False`. |
| Compatible contracts, versioned roll policy | `active_contract_same_contract_reference_v1` (below). Raw prices, contract ids and volumes stay in `bars`. |

### Roll policy `active_contract_same_contract_reference_v1`

- Each session uses the contract `active_contracts` assigns it (the collector's roll
  rule); without a row, the configured contract, and the snapshot says so.
- A cross-session reference — Cprev, an intermarket reference close, the previous close
  inside a daily true range — comes from the **same** contract as the session's value.
  The collector stores `ROLL_WARMUP_SESSIONS` (7) days of each contract before it becomes
  active, which is also enough for the single-contract intraday series on a roll day.
- Intraday indicator series (5m/15m EMA, 1m ATR) use only the snapshot contract's bars.
  Nothing is spliced or back-adjusted.

## 3. Snapshot metadata

Stored as columns of `forecast.feature_snapshots`, outside the `features` object:
`snapshot_id`, `session_date`, `instrument_id` (the exact NQ contract), `cutoff_at`,
`features_frozen_at`, `data_mode`, `pit_availability_status`, `feature_version`,
`source_revision_id`, `session_schedule`, `scheduled_close_at`, `source_status`,
`feature_status`, `data_quality_status`, plus `reference_values` (P, Cprev, PDH, PDL, A,
ONH, ONL, EMA200, …) so outcomes are measured from exactly the snapshot's anchors.

- **source_revision_id** is the SHA-256 of a manifest: calendar version, roll policy,
  the asset-source map, the contract chosen per (symbol, day), and a content digest of
  every bar window read. Identical data gives the identical id, so re-running a
  reconstruction returns the stored snapshot instead of duplicating it.
- **source_status** per asset: symbol, contract id, local symbol, the observation and
  reference bar (start, end, age in minutes), when each value became available
  (`available_at`, with `evidence`: `bar_receipt` - the streamer's receive time of that
  exact bar - or the weaker `day_ledger`, the day's last write time), and a validity code (`valid`, `stale`, `missing`,
  `unmapped`, `no_contract`). NQ adds ON coverage and 60m completeness; there are
  entries for the calendar version and the event calendar.
- **feature_status** per feature: `valid`, `missing`, `stale`, `insufficient_history`,
  `undefined`, `not_applicable`. A derived feature inherits the first failing input's
  status, so a null always says why.
- **data_quality_status**: `invalid` if a required feature is not valid, `partial` if
  only non-required ones are not, else `valid`. For the snapshot row the required set is
  the catalogue's `DEFAULT_REQUIRED`; each forecast run recomputes it against its
  model's `required_features` (`forecast_runs.input_quality_status`).
- **Freshness** is per asset (`config.ASSET_SOURCES.max_age_minutes`) and applies to
  both endpoints. A stale observation is null with its real age recorded — never carried
  forward as if fresh.

## 4–6. Features

The catalogue is [features/catalogue.py](../features/catalogue.py); every feature in
the specification is present under its specified name, plus these, which exist because
a proxy must not borrow a real series' name:

| Added | Why |
|---|---|
| `dx_fut_preopen_return` | the recorded dollar series is the ICE DX **future**; cash DXY is not available through IB, so `dxy_preopen_return` stays null |
| `us10y_yield_fut_change_bps`, `us2y_yield_fut_change_bps`, `yield_fut_curve_10y_2y_change_bps` | Micro Treasury yield futures, kept distinct from spot yields; `us2y_change_bps` stays null (no spot 2-year source) |
| `gc_preopen_return`, `cl_preopen_return` | the optional extensions, null until GC/CL are collected |

CVD, aggressor delta, order-book imbalance and historical-constituent breadth are not
implemented (no side/quote/constituent data is recorded).

**Events.** `economic_event_coverage` and `economic_events` (in `public`) hold an
economic calendar when one is loaded. Without a coverage row for the session, the three
event features are null with status `missing` — "no calendar" is never reported as "no
event". A live capture only sees rows recorded before it froze. No loader for a
particular vendor is included.

## Targets, labels and outcome metrics (`nq_labels_v2_candidate`)

[forecaster/labels_v2.py](../forecaster/labels_v2.py) implements sections 8-11 of the
schema. Every value is measured from the snapshot contract's RTH minute bars: O is the
09:30 open, C5 / C15 / C the 09:34 / 09:44 / 15:59 closes, and every normalised value
divides by the snapshot's frozen A. The opening labels use the snapshot's frozen ON
high/low, never post-open bars.

| target_id | vocabulary | window |
|---|---|---|
| `first_move_5m` | up_first, down_first, neither | [09:30, 09:35) |
| `opening_type_15m` | drive_up, drive_down, sweep_low_rebound, sweep_high_reverse, two_sided, range, mixed | [09:30, 09:45) |
| `direction_15m` | up, down, flat | [09:30, 09:45) |
| `direction_rth` | up, down, flat | [09:30, 16:00) |
| `session_type_rth` | bull_trend, bear_trend, reversal, two_sided_volatile, range, mixed | [09:30, 16:00) |

`nq_outcome_metrics_v2` holds the section-10 metrics (`return_5m_atr`, `return_15m_atr`,
`return_rth_atr`, excursions, ranges, `efficiency_15m`, `rth_close_location`,
`first_hour_return_atr`, `efficiency_rth_5m`, first up/down touch minutes, the ON-low
breach-and-reclaim / ON-high breach-and-reject flags) plus the first-move barrier and
the open of a same-minute double touch. The label rules read only those metrics, with the
section-11 starting thresholds; they are not tuned optima, and a change is a new label
version.

- A window needs every one of its minute bars; otherwise the label is ineligible with
  `missing_bars`. A missing A or ON extreme gives `invalid_reference`.
- First move: both barriers first touched in the same minute is decided by that
  minute's open when it lies beyond a barrier, else `ambiguous_intrabar`. The order
  inside a minute is never guessed.
- Rule lists are evaluated in order with three-valued logic: an unknown condition
  that could have decided the label makes it ineligible; one that cannot (because
  another part of the rule is false) does not.
- On an early-close session the full-RTH targets are `shortened_session`; the opening
  targets stay eligible. A missing later bar never invalidates a complete opening label.
- The label status is stored in `realised_outcomes.ineligibility_reason` (NULL = valid)
  and exposed as `label_status` in `forecast.prediction_outcomes`, together with the
  outcome window. Outcomes are computed two hours after the scheduled close (the
  collector's revision window).

## Models

Both models see a snapshot's typed feature payload and nothing else, and are trained
**walk-forward**: to forecast session D they use only earlier sessions whose realised
label was knowable before D's cutoff (`available_at <= cutoff_at`); a live run also only
uses outcome rows that already existed when it trained. One snapshot per session is
used (the live capture if any).

**`nq_sklearn_v1`** (the default) - one scikit-learn pipeline per target:

- *Inputs*: an explicit allowlist of catalogue features (`SKLEARN_FEATURES`), never the
  whole snapshot. Always-null sources (`us2y_change_bps`, the spot 10y-2y curve, cash
  DXY), the optional GC/CL extensions and near-duplicate fields are left out.
- *Preprocessing*, fitted on the training window only: numeric and boolean inputs are
  median-imputed with a missing-indicator column and standard-scaled; categoricals are
  one-hot encoded over the catalogue's allowed values plus `missing`. It is part of the
  pipeline, so training and inference are identical.
- *Model selection*: the class prior, L2 logistic regression (C = 0.05, 0.5) and a
  shallow `HistGradientBoostingClassifier` are scored by `TimeSeriesSplit` (5 folds,
  folds with fewer than 40 training sessions skipped) on log loss; the best is refitted
  on the whole window, ties going to the simpler candidate. A feature model therefore
  replaces the prior only when it predicts better out of sample.
- *Probabilities* are shrunk toward the Laplace-smoothed class frequencies
  (weight k·α / (n + k·α)), so a class unseen in training keeps a small probability,
  then normalised over the full vocabulary. They are raw model probabilities, validated
  out of sample: `calibration_status = validated_raw`, `calibration_version = NULL`.
- *Status*: `unavailable` with `data_quality` when a required input (A, the gap) is not
  valid, or with `uncertainty` below 60 training sessions; `abstained` (probabilities
  kept) with `out_of_distribution` when at least 3 numeric inputs are more than 8
  training standard deviations from the training mean, or with `shortened_session` for
  the full-RTH targets on an early close; otherwise `issued` with the arg-max label.
  `event_policy` and a minimum top probability are available as parameters, off by
  default.

**`nq_climatology_v2`** - Laplace-smoothed label frequencies of the same training
sessions; the baseline the trained model has to beat.

A run's `calibration` records the training window and class counts per target, every
candidate's cross-validated log loss and accuracy, the selected estimator, the
scikit-learn version and the outcome-selection bounds. Model parameters are part of the
registered definition hash, so changing a feature list, a candidate or a threshold
needs a new model version.

## Running it

```bash
python scripts/nq_forecast_v2.py backfill --start 2026-06-01 --end 2026-09-25   # reconstruct + walk-forward backtest
python scripts/nq_forecast_v2.py outcomes --start 2026-06-01 --end 2026-09-25   # (re)score closed sessions
python scripts/nq_forecast_v2.py train                                           # CV report + data/models/ artifact
python scripts/nq_forecast_v2.py evaluate --outcome-revision 1
python scripts/nq_forecast_v2.py live                                            # at 09:29 ET
```

`backfill` runs both models by default and refits them every `--retrain-every` sessions
(5; 1 refits daily). Reusing a fit is still point-in-time, as it has seen strictly
fewer outcomes. `train` prints, per target, the training window, each candidate's
cross-validated log loss and accuracy and the selected one, and saves the fitted
pipelines (`joblib`) with a JSON report. `forecast --artifact <file>` forecasts with such a saved
model instead of training; it is refused for any session before the one it was
trained for, since it may have seen outcomes that were not yet knowable. `evaluate` lists issued / abstained /
unavailable counts, the accuracy of issued labels, and log loss and Brier score of every
probability distribution with an eligible outcome, per model, target and data mode.

**History needed.** With the 5n warm-up rule, A needs 71 prior RTH sessions and
`daily_volatility_ratio` (ATR63) 316 (about 15 months); divergence needs 61, RVOL 31.
Backfill accordingly (`--days 460` for everything).

### Live capture timing

```
09:00      collector.live_stream starts: one keep-up-to-date 1m stream per instrument
09:25      nq_forecast_v2.py live starts (cron), trains the model(s) on the outcomes
           recorded so far, then sleeps until T
09:29:00   the 09:28 minute ends; the stream finalises it (next_bar / timer, +5 s)
09:29:01   confirm_fetch: one short historical request per instrument re-reads 09:28
09:29:0x   nq_forecast_v2.py live sees the confirmed NQ and ES bars, freezes, forecasts
09:29:45   latest freeze (--wait-until); anything at/after 09:30:00 is refused by the DB
```

Every streamed bar is in `bar_receipts` with its `received_at`; a live snapshot cites
that time per source and is `verified` only if every observation has one (see
[data_store.md](data_store.md#real-time-bars-and-bar_receipts)). Without the streamer,
`live` still works from downloaded bars but the capture is `unverified_historical`.

## Recommendations (not implemented here)

1. **Keep bar revisions.** `save_trading_day` replaces a day's bars, so a source
   revision can *detect* that inputs changed but cannot *reproduce* an old snapshot. An
   append-only archive of replaced days (or a `bar_revisions` table keyed by
   `session_days.fetched_at`) would make every snapshot rebuildable.
2. **Extend the calendar yearly.** `features/calendar.py` covers 2024–2027 and raises
   outside it; add each new year (bump `CALENDAR_VERSION`) — or pin a calendar library
   version and record it, if you prefer.
3. **Retire the v1 snapshot for evaluation.** The v1 snapshot is stamped 09:30 and uses
   the 09:30 opening bar's open as its gap, and v1 records are overwritten on re-run.
   Keep it for the dashboard until that is moved to the v2 views.
