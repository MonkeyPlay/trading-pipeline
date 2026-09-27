# tests/test_labels_v2.py
"""nq_labels_v5_candidate rules (nq_schema_v2 sections 10-11) on hand-built minute paths."""

import numpy as np
import pandas as pd
import pytest

from features import calendar as cal
from forecaster import labels_v2 as lv

S = cal.session("2026-06-10")
A, O = 10.0, 100.0


def _bars(s, closes, opens=None, highs=None, lows=None):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[O], closes[:-1]]) if opens is None else np.asarray(opens, dtype=float)
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, dtype=float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, dtype=float)
    idx = pd.date_range(s.rth_open_at, periods=len(closes), freq="1min")
    return pd.DataFrame({"bar_start_at": idx, "open": opens, "high": highs, "low": lows, "close": closes})


def _full(path15, rest=None, s=S):
    """390 RTH bars: the given first 15 closes, then ``rest`` (default: flat at the 15th close)."""
    n = int((s.scheduled_close_at - s.rth_open_at) / pd.Timedelta(minutes=1))
    tail = np.full(n - len(path15), path15[-1]) if rest is None else np.asarray(rest, dtype=float)
    return np.concatenate([path15, tail])


def _labels(df, ONH=110.0, ONL=90.0, s=S):
    res = lv.compute_metrics(df, s, A, ONH, ONL)
    return {t: (o["label"], o["status"]) for t, o in lv.compute_labels(res["metrics"], res["status"], s).items()}, res


def test_drive_up_opening():
    labels, res = _labels(_bars(S, _full(O + 0.25 * np.arange(1, 16))))   # +3.75 points = 0.375 A, efficient
    assert labels["opening_type_15m"] == ("drive_up", "valid")
    assert labels["direction_15m"] == ("up", "valid")
    assert labels["first_move_5m"] == ("up_first", "valid")   # B = max(1, 0.5) = 1 point, touched at minute 3
    assert res["metrics"]["first_up_touch_minute"] == 3 and res["metrics"]["first_down_touch_minute"] is None
    assert res["metrics"]["return_15m_atr"] == pytest.approx(0.375)


def test_constant_session_is_range_and_neither():
    labels, res = _labels(_bars(S, _full(np.full(15, O))))
    assert res["metrics"]["rth_close_location"] is None           # H = L: undefined ...
    assert labels["session_type_rth"] == ("range", "valid")      # ... yet a constant session is range
    assert labels["opening_type_15m"] == ("range", "valid")
    assert labels["first_move_5m"] == ("neither", "valid")
    assert labels["direction_15m"] == ("flat", "valid") and labels["direction_rth"] == ("flat", "valid")


def test_direction_band_equality_is_flat():
    closes = _full(np.full(15, O + 1.0))                          # exactly +0.10 A at 09:44
    labels, _ = _labels(_bars(S, closes))
    assert labels["direction_15m"] == ("flat", "valid")


@pytest.mark.parametrize("tie_open, expected", [
    (O, (None, "ambiguous_intrabar")),       # open between the barriers: order unknown
    (O + 1.5, ("up_first", "valid")),        # open at/above the upper barrier
    (O - 1.5, ("down_first", "valid")),
])
def test_first_move_same_minute(tie_open, expected):
    closes = _full(np.full(15, O))
    opens = np.full(len(closes), O)
    highs, lows = np.full(len(closes), O), np.full(len(closes), O)
    opens[2], highs[2], lows[2] = tie_open, O + 2, O - 2        # both barriers first touched in minute 2
    labels, res = _labels(_bars(S, closes, opens, highs, lows))
    assert labels["first_move_5m"] == expected
    assert res["metrics"]["first_up_touch_minute"] == res["metrics"]["first_down_touch_minute"] == 2


def test_sweep_low_rebound_and_unknown_on_low():
    # Minute 1 breaks the ON low (99.5) by 0.5 >= 0.02 A, closes back above it; 15m return +0.15 A.
    closes = np.concatenate([[99.8, 99.8], np.linspace(100.0, 101.5, 13)])
    lows = np.minimum(np.concatenate([[O], closes[:-1]]), closes)
    lows[1] = 99.0
    df = _bars(S, _full(closes), lows=np.concatenate([lows, np.full(390 - 15, 101.5)]))
    labels, res = _labels(df, ONL=99.5)
    assert res["metrics"]["on_low_breach_close_reclaim_15m"] is True
    assert labels["opening_type_15m"] == ("sweep_low_rebound", "valid")
    # Without a frozen ON low the sweep rule is unknown, and it could decide the label.
    labels, _ = _labels(df, ONL=None)
    assert labels["opening_type_15m"] == (None, "invalid_reference")


def test_unknown_lower_priority_rule_does_not_block():
    # Downward drive: the (unknown) ON-low sweep rule cannot fire because r > 0.10 is false.
    closes = _full(O - 0.25 * np.arange(1, 16))
    labels, _ = _labels(_bars(S, closes), ONL=None)
    assert labels["opening_type_15m"] == ("drive_down", "valid")


def test_two_sided_opening_can_finish_up():
    closes = np.concatenate([[98.0, 97.5], np.linspace(99, 103, 13)])   # d = 0.25 A, u = 0.3 A, r = +0.3 A
    labels, _ = _labels(_bars(S, _full(closes)))
    assert labels["opening_type_15m"] == ("two_sided", "valid")
    assert labels["direction_15m"] == ("up", "valid")


def test_reversal_session():
    first_hour = O + np.linspace(0.1, 3.0, 60)                     # +0.30 A by 10:29
    rest = np.linspace(103.0, 96.0, 330)                           # closes -0.40 A
    labels, res = _labels(_bars(S, np.concatenate([first_hour, rest])))
    assert res["metrics"]["first_hour_return_atr"] == pytest.approx(0.30)
    assert labels["session_type_rth"] == ("reversal", "valid")
    assert labels["direction_rth"] == ("down", "valid")


def test_bull_trend_session():
    labels, res = _labels(_bars(S, O + np.linspace(0.05, 8.0, 390)))   # +0.8 A, closes at the high
    assert labels["session_type_rth"] == ("bull_trend", "valid")
    assert res["metrics"]["efficiency_rth_5m"] == pytest.approx(1.0)


def test_missing_bar_invalidates_only_its_windows():
    df = _bars(S, _full(O + 0.25 * np.arange(1, 16)))
    df = df[df["bar_start_at"] != S.rth_open_at + pd.Timedelta(minutes=10)]
    labels, _ = _labels(df)
    assert labels["first_move_5m"] == ("up_first", "valid")        # [09:30, 09:35) is complete
    assert labels["opening_type_15m"] == (None, "missing_bars")
    assert labels["direction_15m"] == (None, "missing_bars")
    assert labels["direction_rth"] == (None, "missing_bars")


def test_invalid_atr_is_invalid_reference():
    res = lv.compute_metrics(_bars(S, _full(np.full(15, O))), S, None, 110.0, 90.0)
    labels = lv.compute_labels(res["metrics"], res["status"], S)
    assert {o["status"] for o in labels.values()} == {"invalid_reference"}


def test_early_close_rth_targets_are_shortened():
    early = next(s for s in cal.sessions_between("2026-01-01", "2026-12-31") if s.is_early_close)
    closes = _full(O + 0.25 * np.arange(1, 16), s=early)
    res = lv.compute_metrics(_bars(early, closes), early, A, 110.0, 90.0)
    labels = lv.compute_labels(res["metrics"], res["status"], early)
    assert labels["direction_rth"]["status"] == labels["session_type_rth"]["status"] == "shortened_session"
    assert labels["opening_type_15m"]["label"] == "drive_up"       # opening targets stay eligible
    assert labels["direction_rth"]["window_end_at"] == cal.ny_instant(early.session_date, lv.RTH_END)


def test_windows_and_vocabulary():
    ref = {"range_15m_atr": 0.3, "range_rth_atr": 0.8, "range_1h_atr": 0.4}
    labels = lv.compute_labels(*_labels(_bars(S, _full(np.full(15, O))))[1].values(), S, reference=ref)
    assert labels["first_move_5m"]["window_end_at"] == S.rth_open_at + pd.Timedelta(minutes=5)
    assert labels["opening_type_15m"]["window_end_at"] == S.rth_open_at + pd.Timedelta(minutes=15)
    for t, o in labels.items():
        assert o["window_start_at"] == S.rth_open_at
        assert o["label"] in lv.TARGETS[t]["labels"]
    rec = lv.label_registry_record()
    assert [t["target_id"] for t in rec["targets"]] == ["first_move_5m", "opening_type_15m", "direction_15m",
                                                         "direction_rth", "session_type_rth",
                                                         "range_15m_regime", "range_rth_regime",
                                                         "direction_1h", "range_1h_regime", "first_break_1h"]


def test_range_reference_needs_the_full_window():
    hist = {"range_15m_atr": [float(i) for i in range(45)], "range_rth_atr": [1.0] * 39}
    ref = lv.range_reference(hist)
    assert ref["range_15m_atr"] == pytest.approx(19.5)          # the 40 newest: 0 .. 39
    assert ref["range_rth_atr"] is None                          # 39 earlier values are not enough
    assert lv.range_reference(None) == {"range_15m_atr": None, "range_rth_atr": None, "range_1h_atr": None}


def test_range_regime_labels():
    # 15 minutes rising 0.1 point a minute: range 1.5 points = 0.15 A; the rest of the day flat.
    df = _bars(S, _full(O + 0.1 * np.arange(1, 16)))
    res = lv.compute_metrics(df, S, A, 110.0, 90.0)
    assert res["metrics"]["range_15m_atr"] == pytest.approx(0.15)

    def lab(ref):
        return {t: (o["label"], o["status"]) for t, o in
                lv.compute_labels(res["metrics"], res["status"], S, reference=ref).items()}

    assert lab({"range_15m_atr": 0.10, "range_rth_atr": 0.10})["range_15m_regime"] == ("wide", "valid")
    assert lab({"range_15m_atr": 0.15, "range_rth_atr": 0.10})["range_15m_regime"] == ("narrow", "valid")  # equal
    assert lab({"range_15m_atr": 0.30, "range_rth_atr": 0.10})["range_rth_regime"] == ("wide", "valid")
    none = lab(None)
    assert none["range_15m_regime"] == (None, "insufficient_history")
    assert none["range_rth_regime"] == (None, "insufficient_history")


def test_range_regime_on_early_close_and_missing_bars():
    early = cal.session("2026-11-27")
    res = lv.compute_metrics(_bars(early, _full(np.full(15, O), s=early)), early, A, 110.0, 90.0)
    out = lv.compute_labels(res["metrics"], res["status"], early,
                            reference={"range_15m_atr": 0.1, "range_rth_atr": 0.5})
    assert out["range_rth_regime"]["status"] == "shortened_session"
    assert out["range_15m_regime"]["label"] == "narrow"            # the opening window is complete
    gap = _bars(S, _full(np.full(15, O))).drop(index=[7])           # 09:37 missing
    res = lv.compute_metrics(gap, S, A, 110.0, 90.0)
    out = lv.compute_labels(res["metrics"], res["status"], S, reference={"range_15m_atr": 0.1, "range_rth_atr": 0.5})
    assert out["range_15m_regime"]["status"] == "missing_bars"


def test_range_regime_digest_carries_the_threshold():
    class _Bars:
        def bars(self, cid, start, end):
            return _bars(S, _full(np.full(15, O))), "d", "k"

    snap = {"session_date": S.session_date, "instrument_id": 1,
            "reference_values": {"A": A, "ONH": 110.0, "ONL": 90.0}}
    hist = {"range_15m_atr": [0.2] * 40, "range_rth_atr": [0.6] * 40}
    a = lv.compute_outcome(_Bars(), snap, hist)
    b = lv.compute_outcome(_Bars(), snap, {**hist, "range_15m_atr": [0.3] * 40})
    assert a["digest"] == b["digest"]                               # the session's own inputs are the same
    assert a["labels"]["range_15m_regime"]["digest"] != b["labels"]["range_15m_regime"]["digest"]
    assert a["labels"]["range_rth_regime"]["digest"] == b["labels"]["range_rth_regime"]["digest"]
    assert "digest" not in a["labels"]["direction_15m"]


def test_first_hour_targets():
    # 15 minutes between 100 and 101, a break above at 09:52, the hour ending at 103 (+0.3 A)
    closes = [100.5] * 22 + list(np.linspace(101.5, 103.0, 38))
    highs = [101.0] * 15 + [100.8] * 7 + [c + 0.1 for c in closes[22:]]
    lows = [100.0] * 15 + [100.2] * 7 + [c - 0.1 for c in closes[22:]]
    opens = [100.5] + closes[:-1]
    rest = np.full(390 - 60, closes[-1])
    df = _bars(S, np.concatenate([closes, rest]), opens=np.concatenate([opens, rest]),
               highs=np.concatenate([highs, rest]), lows=np.concatenate([lows, rest]))
    res = lv.compute_metrics(df, S, A, 110.0, 90.0)
    m = res["metrics"]
    assert m["first_break_side_1h"] == 1.0 and m["first_break_minute_1h"] == 22
    assert m["range_1h_atr"] == pytest.approx((103.1 - 100.0) / A)
    assert m["first_hour_return_atr"] == pytest.approx((103.0 - 100.5) / A)
    out = lv.compute_labels(m, res["status"], S, reference={"range_1h_atr": 0.2})
    assert out["direction_1h"]["label"] == "up" and out["first_break_1h"]["label"] == "above"
    assert out["range_1h_regime"]["label"] == "wide"                 # 0.31 A > 0.2
    assert out["first_break_1h"]["available_at"] == S.rth_open_at + pd.Timedelta(minutes=60)


def test_first_break_none_and_same_minute():
    flat = np.full(390, O)
    base = dict(opens=flat.copy(), highs=flat + 0.5, lows=flat - 0.5)
    res = lv.compute_metrics(_bars(S, flat, **base), S, A, 110.0, 90.0)
    assert res["metrics"]["first_break_side_1h"] == 0.0 and res["metrics"]["first_break_minute_1h"] is None
    assert lv.compute_labels(res["metrics"], res["status"], S)["first_break_1h"]["label"] == "none"
    highs, lows, closes = flat + 0.5, flat - 0.5, flat.copy()
    highs[30], lows[30], closes[30] = O + 2, O - 2, O - 0.2             # both sides at 10:00, closing below the middle
    res = lv.compute_metrics(_bars(S, closes, opens=flat, highs=highs, lows=lows), S, A, 110.0, 90.0)
    assert res["metrics"]["first_break_side_1h"] == -1.0
    gap = _bars(S, flat, **base).drop(index=[40])                       # a missing minute in the hour
    res = lv.compute_metrics(gap, S, A, 110.0, 90.0)
    assert lv.compute_labels(res["metrics"], res["status"], S)["first_break_1h"]["status"] == "missing_bars"
