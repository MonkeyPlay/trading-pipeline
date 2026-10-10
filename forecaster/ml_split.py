# forecaster/ml_split.py
"""
Session-date splits for every ML fold, outer and inner (contracts/nq_ml.SPLIT).

Rows carry their session date and their instrument. A split is made on the sorted, unique session
dates - never on row positions - so every instrument's row of one session falls on one side, every
training date precedes the embargo and the validation or test dates, and a row-count cut can never
divide a date. Sorting rows alone would not be enough: a cut by rows can still fall inside a date.

  DateFold                     (train, embargo, test) ISO dates, disjoint and in time order
  outer_folds(dates, ...)      chronological walk-forward: train on every earlier date less the embargo,
                               test the next block (contracts/nq_ml.DEV_EVALUATION's design)
  inner_split(dates, ...)      one validation block: the last share of a training window's dates, the
                               embargo dates before it removed from inner training
  inner_folds(dates, ...)      several chronological validation blocks at the end of a training window,
                               each predicted from the dates before it less the embargo (the bundles'
                               selection and out-of-fold calibration)
  mask(row_dates, dates)       the rows whose session date is in ``dates``
  objective(row_dates, row_instruments, fold, instrument)
                               the validation rows a candidate is scored on: the validation dates' rows
                               of the deployed instrument (NQ) only - the pooled model is trained on every
                               instrument's rows, but deployment predicts NQ
  check(fold)                  raises unless the fold is disjoint and chronological with its embargo
                               strictly between training and the held-out dates

The row-position split this replaces (forecaster/ml_model.tune_rows_legacy) took the first 75 % of the
rows for training and the last 25 % for validation. The pooled rows are built instrument first, date
second, so its validation rows were the last instrument's (RTY's) and shared their session dates with
the training rows (NQ's and ES's of the same days, later days included). It is kept only to reproduce
the registered v1 artifacts and study (docs/reports/ml_pooled_split_v1.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class DateFold:
    train: Tuple[str, ...]
    embargo: Tuple[str, ...]
    test: Tuple[str, ...]

    def describe(self) -> dict:
        return {"train": [self.train[0], self.train[-1], len(self.train)] if self.train else [None, None, 0],
                "embargo": list(self.embargo), "test": [self.test[0], self.test[-1], len(self.test)]
                if self.test else [None, None, 0]}


def unique_dates(row_dates: Iterable[str]) -> List[str]:
    """The sorted unique session dates of rows (ISO strings sort chronologically)."""
    return sorted({str(d) for d in row_dates})


def check(fold: DateFold) -> None:
    """Raises AssertionError unless the fold's three date sets are disjoint, the embargo lies strictly between the
    training and the held-out dates, and every training date precedes every held-out date."""
    tr, em, te = set(fold.train), set(fold.embargo), set(fold.test)
    if tr & te or tr & em or em & te:
        raise AssertionError("a session date is in more than one of training, embargo and held-out")
    if list(fold.train) != sorted(fold.train) or list(fold.test) != sorted(fold.test):
        raise AssertionError("fold dates are not in time order")
    if fold.train and fold.test and max(fold.train) >= min(fold.test):
        raise AssertionError("a training date is not earlier than every held-out date")
    if fold.embargo:
        if fold.train and max(fold.train) >= min(fold.embargo):
            raise AssertionError("a training date is not earlier than the embargo")
        if fold.test and max(fold.embargo) >= min(fold.test):
            raise AssertionError("an embargo date is not earlier than the held-out dates")


def outer_folds(dates: Sequence[str], initial: int, block: int, embargo: int) -> List[DateFold]:
    """Walk-forward over the unique ``dates``: the first test block starts after ``initial`` dates; each fold trains
    on every date before its test block less the ``embargo`` dates immediately before it."""
    ds = unique_dates(dates)
    out, start = [], initial
    while start < len(ds):
        fold = DateFold(tuple(ds[:max(0, start - embargo)]), tuple(ds[max(0, start - embargo):start]),
                        tuple(ds[start:start + block]))
        check(fold)
        out.append(fold)
        start += block
    return out


def inner_split(dates: Sequence[str], validation_share: float, embargo: int) -> DateFold:
    """One chronological validation block inside a training window: the last ``validation_share`` of its unique dates
    (rounded, at least one) validate; the ``embargo`` dates before them are left out of inner training."""
    ds = unique_dates(dates)
    n_val = max(1, int(round(len(ds) * validation_share)))
    start = len(ds) - n_val
    fold = DateFold(tuple(ds[:max(0, start - embargo)]), tuple(ds[max(0, start - embargo):start]), tuple(ds[start:]))
    check(fold)
    if not fold.train:
        raise ValueError(f"{len(ds)} dates leave no inner training dates before a {n_val}-date validation block")
    return fold


def inner_folds(dates: Sequence[str], blocks: int, block: int, embargo: int, min_train: int) -> List[DateFold]:
    """The last ``blocks`` blocks of ``block`` unique dates of a training window, oldest first, each predicted from the
    window's dates before it less ``embargo``; a block whose training would have fewer than ``min_train`` dates is
    skipped."""
    ds = unique_dates(dates)
    out = []
    for b in range(blocks, 0, -1):
        start = len(ds) - b * block
        if start - embargo < min_train:
            continue
        fold = DateFold(tuple(ds[:start - embargo]), tuple(ds[start - embargo:start]), tuple(ds[start:start + block]))
        check(fold)
        out.append(fold)
    return out


def mask(row_dates: Sequence[str], dates: Iterable[str]) -> np.ndarray:
    keep = set(dates)
    return np.array([str(d) in keep for d in row_dates], dtype=bool)


def objective(row_dates: Sequence[str], row_instruments: Optional[Sequence[str]], fold: DateFold,
              instrument: str = "NQ") -> np.ndarray:
    """The held-out rows a candidate is scored on: the fold's test dates, ``instrument``'s rows only (every row when
    no instruments are given - a single-instrument dataset)."""
    m = mask(row_dates, fold.test)
    if row_instruments is not None:
        m &= np.array([str(s) == instrument for s in row_instruments], dtype=bool)
    return m


def legacy_row_split(n_rows: int, validation_share: float) -> int:
    """The row position at which forecaster/ml_model.tune_rows_legacy cut (kept to reproduce the registered v1
    artifacts and study, and to demonstrate its defect)."""
    return int(round(n_rows * (1 - validation_share)))


def legacy_diagnostics(row_dates: Sequence[str], row_instruments: Sequence[str], validation_share: float) -> dict:
    """What the legacy row cut did to rows in the given order: the validation rows' instruments, how many validation
    dates also had training rows, and whether training reached later dates than validation began."""
    cut = legacy_row_split(len(row_dates), validation_share)
    tr_d, va_d = [str(d) for d in row_dates[:cut]], [str(d) for d in row_dates[cut:]]
    va_s = [str(s) for s in row_instruments[cut:]]
    shared = set(tr_d) & set(va_d)
    return {"rows": len(row_dates), "validation_rows": len(va_d),
            "validation_instruments": {s: va_s.count(s) for s in sorted(set(va_s))},
            "validation_dates": len(set(va_d)), "validation_dates_also_in_training": len(shared),
            "latest_training_date": max(tr_d) if tr_d else None,
            "earliest_validation_date": min(va_d) if va_d else None,
            "training_after_validation_start": bool(tr_d and va_d and max(tr_d) >= min(va_d))}
