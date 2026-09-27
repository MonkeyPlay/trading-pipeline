# tests/test_label_study.py
"""The label-threshold study: parameter overrides, one snapshot per session, relabelling."""

import numpy as np
import pytest

from forecaster import label_study as ls
from forecaster import labels_v2 as lv
from tests.test_labels_v2 import O, S, _bars, _full


def test_overrides_copy_and_validate():
    p = ls.with_overrides({"opening_type_15m.drive.e_min": 0.3, "first_move_atr_fraction": 0.2})
    assert p["opening_type_15m"]["drive"]["e_min"] == 0.3 and p["first_move_atr_fraction"] == 0.2
    assert lv.PARAMETERS["opening_type_15m"]["drive"]["e_min"] == 0.40   # the registered rules are untouched
    for bad in ("opening_type_15m.drive.nope", "nope", "first_move_atr_fraction.x"):
        with pytest.raises(KeyError):
            ls.with_overrides({bad: 1.0})
    for preset in ls.PRESETS.values():
        ls.with_overrides(preset)


def test_one_snapshot_per_session_prefers_live_then_newest():
    snaps = [{"session_date": "2026-06-10", "data_mode": "live_capture", "created_at": "1", "id": "live"},
             {"session_date": "2026-06-10", "data_mode": "historical_reconstruction", "created_at": "2", "id": "h"},
             {"session_date": "2026-06-09", "data_mode": "historical_reconstruction", "created_at": "1", "id": "a"},
             {"session_date": "2026-06-09", "data_mode": "historical_reconstruction", "created_at": "3", "id": "b"}]
    assert [s["id"] for s in ls.one_per_session(snaps)] == ["b", "live"]


class _Bars:
    def __init__(self, df):
        self.df = df

    def bars(self, contract_id, start, end):
        return self.df, "digest", "key"


def test_study_relabels_under_each_variant():
    # A steady +0.15 A in 15 minutes: a drive now (r >= 0.15), 'mixed' under v2's r >= 0.20.
    df = _bars(S, _full(O + 0.1 * np.arange(1, 16)))
    snap = {"session_date": S.session_date, "instrument_id": 1, "data_mode": "historical_reconstruction",
            "created_at": "1", "reference_values": {"A": 10.0, "ONH": 110.0, "ONL": 90.0}}
    variants = dict(ls.PRESETS)
    res = ls.study(_Bars(df), [snap], variants)
    assert res["sessions"] == 1
    assert res["labels"]["current"]["opening_type_15m"] == {"drive_up": 1}
    assert res["labels"]["nq_labels_v2_candidate"]["opening_type_15m"] == {"mixed": 1}
    # the current variant agrees with the stored rules
    m = lv.compute_metrics(df, S, 10.0, 110.0, 90.0)
    stored = lv.compute_labels(m["metrics"], m["status"], S)
    assert all(res["labels"]["current"][t] == {o["label"]: 1} for t, o in stored.items())
    n, qs = res["quantiles"]["return_15m_atr"]
    assert n == 1 and qs[2] == pytest.approx(0.15)
    assert "nq_labels_v2_candidate" in ls.format_report(res, variants)
