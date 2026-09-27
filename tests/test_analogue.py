# tests/test_analogue.py
"""The analogue forecast (analogue_baseline_v2): standardised matching without a
direction flag, first-hour outcomes, and a bias that agrees with the probabilities."""

import pytest

from forecaster import analogue as an
from forecaster.client import FLAT_BAND, HORIZON, MODEL_VERSION, ForecastClient, classify_move
from matching.normalizer import rank_analogues, v1_match_vector


def _cand(day, **vector):
    return {"session_date": day, "vector": vector}


def test_rank_standardises_each_input():
    # 'big' varies in the hundreds, 'small' in tenths: unstandardised, 'big' would decide alone.
    cands = [_cand("2026-01-01", big=100.0, small=0.1), _cand("2026-01-02", big=110.0, small=0.9),
             _cand("2026-01-03", big=300.0, small=0.1), _cand("2026-01-04", big=200.0, small=0.5)]
    ranked = rank_analogues({"big": 100.0, "small": 0.1}, cands, ["big", "small"])
    assert ranked[0]["session_date"] == "2026-01-01" and ranked[0]["distance"] == 0.0
    assert ranked[0]["similarity_score"] == 1.0 and ranked[0]["n_dims"] == 2
    # 110/0.9 differs by 0.1 sd in 'big' but 2 sd in 'small'; 300/0.1 by 2.3 sd in 'big' only
    order = [r["session_date"] for r in ranked]
    assert order.index("2026-01-02") > order.index("2026-01-04")


def test_missing_inputs_are_skipped_and_min_dims_enforced():
    cands = [_cand("2026-01-01", a=1.0, b=None, c=3.0), _cand("2026-01-02", a=2.0, b=2.0, c=None),
             _cand("2026-01-03", a=3.0, b=4.0, c=1.0)]
    ranked = rank_analogues({"a": 1.0, "b": 2.0, "c": 3.0}, cands, ["a", "b", "c"], min_dims=2)
    assert {r["session_date"]: r["n_dims"] for r in ranked} == {"2026-01-01": 2, "2026-01-02": 2,
                                                                "2026-01-03": 3}
    assert rank_analogues({"a": 1.0}, cands, ["a", "b", "c"], min_dims=2) == []


def test_v1_vector_has_no_direction_flag():
    vec, scale = v1_match_vector({"previous_rth_close": 20000.0, "historical_volatility": 0.01,
                                  "gap": -50.0, "overnight_range": 100.0, "pre_open_direction": "DOWN"})
    assert scale == pytest.approx(200.0)
    assert vec == {"gap_sigma": pytest.approx(-0.25), "overnight_range_sigma": pytest.approx(0.5)}
    assert v1_match_vector({"gap": 1.0})[1] is None


def _analogues(labels):
    return [{"match_date": f"2026-01-{i + 1:02d}", "similarity_score": 0.5, "ranking": i + 1,
             "outcome": {"label": lab, "move": 1.0, "normalized": 0.2}} for i, lab in enumerate(labels)]


@pytest.mark.parametrize("labels, bias", [
    (["up"] * 6 + ["down"] * 3 + ["flat"], "BULLISH"),
    (["down"] * 5 + ["up"] * 2 + ["flat"] * 3, "BEARISH"),
    (["flat"] * 4 + ["up"] * 3 + ["down"] * 3, "NEUTRAL"),
    (["up"] * 4 + ["down"] * 4 + ["flat"] * 2, "NEUTRAL"),     # a tie at the top is no lean
])
def test_bias_is_the_most_frequent_outcome(labels, bias):
    f = ForecastClient().get_forecast("2026-02-02", {"gap": -23.25}, _analogues(labels), scale=320.0)
    p = f["probabilities"]
    assert f["opening_bias"] == bias
    assert p["bullish_continuation_pct"] + p["mean_reversion_gap_fill_pct"] + p["bearish_rejection_pct"] \
        == pytest.approx(100.0)
    assert p["matches"] == 10 and p["flat_band_points"] == pytest.approx(FLAT_BAND * 320.0)
    assert f["forecast_horizon"] == HORIZON
    # the v1 contradiction - a bearish bias over a mostly bullish distribution - cannot occur
    if bias == "BEARISH":
        assert p["bearish_rejection_pct"] > p["bullish_continuation_pct"]
    if bias == "BULLISH":
        assert p["bullish_continuation_pct"] > p["bearish_rejection_pct"]


def test_no_analogues():
    f = ForecastClient().get_forecast("2026-02-02", {}, [], scale=None)
    assert f["opening_bias"] == "UNKNOWN" and f["probabilities"]["matches"] == 0
    assert f["probabilities"]["bullish_continuation_pct"] is None
    assert ForecastClient().model_name == MODEL_VERSION == "analogue_baseline_v2"


def test_classify_move():
    assert classify_move(FLAT_BAND + 0.01) == "up" and classify_move(-FLAT_BAND - 0.01) == "down"
    assert classify_move(FLAT_BAND) == "flat" and classify_move(0.0) == "flat"


def test_nq_matches_on_point_in_time_snapshots(monkeypatch):
    """NQ: the target's stored snapshot vs every earlier one; analogues without a
    measurable first hour are skipped for the next closest."""
    feats = {k: 1.0 for k in an.MATCH_FEATURES}
    target = {"features": feats, "reference_values": {"A": 300.0}}
    pool = []
    for i in range(20):
        f = {k: 1.0 + 0.1 * i for k in an.MATCH_FEATURES}
        pool.append({"session_date": f"2026-01-{i + 1:02d}", "features": f, "reference_values": {"A": 250.0}})
    monkeypatch.setattr(an, "v2_target", lambda conn, day: target)
    monkeypatch.setattr(an.store, "snapshots_before", lambda conn, day, fv, sym: pool)
    missing = {"2026-01-02"}

    def move(conn, symbol, day):
        return None if day in missing else {"move": 50.0, "open": 1.0, "close": 51.0, "contract_id": 1}

    monkeypatch.setattr(an, "first_hour_move", move)
    found = an.find_analogues(None, "NQ", "2026-02-02", {"previous_rth_close": 1.0}, v1_history=lambda: 1 / 0,
                              k=5)
    days = [a["match_date"] for a in found["analogues"]]
    assert days == ["2026-01-01", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"]
    assert [a["ranking"] for a in found["analogues"]] == [1, 2, 3, 4, 5]
    assert found["pool"] == 20 and found["scale"] == 300.0 and found["scale_name"] == "ATR"
    a = found["analogues"][0]
    assert a["outcome"]["normalized"] == pytest.approx(50.0 / 250.0) and a["outcome"]["label"] == "up"


def test_other_instruments_fall_back_to_v1_features(monkeypatch):
    monkeypatch.setattr(an, "first_hour_move", lambda conn, s, d: {"move": -30.0, "open": 1, "close": 1,
                                                                    "contract_id": 1})
    hist = [{"trading_day": f"2026-01-{i + 1:02d}", "previous_rth_close": 5000.0, "historical_volatility": 0.01,
             "gap": float(i), "overnight_range": 20.0 + i} for i in range(6)]
    hist.append({**hist[0], "trading_day": "2026-02-02"})       # the target day itself is never a candidate
    target = {"previous_rth_close": 5000.0, "historical_volatility": 0.01, "gap": 2.0, "overnight_range": 22.0}
    found = an.find_analogues(None, "ES", "2026-02-02", target, v1_history=hist, k=3)
    assert found["analogues"][0]["match_date"] == "2026-01-03" and found["scale_name"] == "daily σ"
    assert "2026-02-02" not in [a["match_date"] for a in found["analogues"]]
    assert found["analogues"][0]["outcome"]["label"] == "down"          # -30 / 50 = -0.6 daily σ


def test_nq_with_too_few_stored_snapshots_falls_back(monkeypatch):
    feats = {k: 1.0 for k in an.MATCH_FEATURES}
    monkeypatch.setattr(an, "v2_target", lambda conn, day: {"features": feats, "reference_values": {"A": 300.0}})
    monkeypatch.setattr(an.store, "snapshots_before", lambda conn, day, fv, sym: [
        {"session_date": "2026-01-01", "features": feats, "reference_values": {"A": 250.0}}])
    monkeypatch.setattr(an, "first_hour_move", lambda conn, s, d: {"move": 5.0, "open": 1, "close": 1,
                                                                    "contract_id": 1})
    hist = [{"trading_day": f"2026-01-{i + 1:02d}", "previous_rth_close": 20000.0, "historical_volatility": 0.01,
             "gap": float(i), "overnight_range": 100.0 + i} for i in range(5)]
    found = an.find_analogues(None, "NQ", "2026-02-02", dict(hist[2], trading_day="2026-02-02"), hist, k=3)
    assert found["scale_name"] == "daily σ" and found["pool"] == 5
    assert found["analogues"][0]["match_date"] == "2026-01-03"
