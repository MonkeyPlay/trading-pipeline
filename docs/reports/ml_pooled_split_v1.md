# The pooled tuning split, corrected (ml_pooled_split_v1)

**Status: not yet run on the production database.** This file is a placeholder; `python scripts/ml_pooled_split.py all`
overwrites it with the before/after report ([research/ml_pooled_split.py](../../research/ml_pooled_split.py)). It
was prepared in an environment without the production data, so it states no result.

## The defect (established from the code, not from a rerun)

- `forecaster/ml_train.dataset()` builds the pooled rows instrument first, date second.
- `training_rows()` kept that order and returned arrays without the dates.
- `forecaster/ml_model.tune()` fitted every grid point on the first 75 % of the rows and scored it on the last 25 %.
- `forecaster/ml_eval._fold()` and ml_study_v1's five-session check used that tuner for P; so did the study's controls
  (`research/ml_study_run._frozen_on`).

So P's inner validation rows were the last instrument's (RTY's), on session dates whose NQ and ES rows - later dates
included - were in the inner training. With 100 dates per instrument the validation set is 75 RTY rows, all 75 of
their dates also in training (`tests/test_ml_split.py::test_the_legacy_row_cut_validates_on_rty_only_on_dates_also_in_training`).
That is a synthetic demonstration of the indexing defect, not a rerun on production data.

The outer walk-forward folds always excluded their test dates. This is not demonstrated outer-test leakage, and the
correction is not assumed to improve P. It makes the inner validation inappropriate for chronological NQ model
selection and weakens the attribution of any pooling benefit. N and M (one row per session, in date order) were cut
chronologically, without the session embargo.

## The correction

`forecaster/ml_split.py` (`contracts/nq_ml.SPLIT`): every inner and outer split is made on the sorted unique session
dates, all instruments of a date on one side, the one-session embargo between inner training and validation, and
the pooled candidate is tuned on NQ's validation rows only. Preprocessing is fitted inside each fit's pipeline on its
training rows; uncertainty resamples per-session differences. The row-position tuner stays as
`ml_model.tune_rows_legacy`, used only to reproduce the registered v1 artifacts and ml_study_v1.

## What the run will report

- whether the legacy refits reproduce ml_study_v1's stored N, M and P predictions;
- per fold, the legacy inner validation's instruments and shared dates on the production rows;
- N, M, P (and the other development fits) before and after, against A and B, and P − N, on the common sessions;
- the five-session check (2026-10-05..09) before and after;
- the frozen arms' shuffled-label and planted-signal controls under both splits.

ml_study_v1's files and hashes are recorded before and after the run and must be unchanged.
