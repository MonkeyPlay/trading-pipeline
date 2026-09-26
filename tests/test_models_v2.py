# tests/test_models_v2.py
"""
The scikit-learn model and the climatology baseline without a database: the
store is replaced by in-memory training rows, so what the models are allowed to
see, what they learn and what they return can be checked exactly.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from features import calendar as cal
from features import catalogue as catv2
from forecaster import labels_v2, models_v2

DAY = date(2026, 6, 10)
VOCAB = labels_v2.vocabulary()


def _features(rng, gap=None):
    f = {n: None for n in catv2.FEATURE_NAMES}
    for n in models_v2.SKLEARN_FEATURES["numeric"]:
        f[n] = float(rng.normal())
    f["daily_atr_fraction"] = 0.012
    f["vix_level"] = 18.0 + rng.normal()
    f["gap_signed_atr"] = float(rng.normal()) if gap is None else gap
    f["weekday"], f["is_early_close"], f["monthly_opex_week"] = "wed", False, bool(rng.random() < 0.25)
    f["roll_transition"], f["days_to_nq_expiry"] = False, 40
    f["minutes_to_high_event"] = None      # no event calendar: always null
    return f


def _label(target, gap, rng):
    """direction_15m follows the gap (plus noise); every other target is noise."""
    labels = VOCAB[target]
    if target == "direction_15m":
        z = gap + rng.normal(0, 0.5)
        return "up" if z > 0.4 else "down" if z < -0.4 else "flat"
    return labels[rng.integers(0, len(labels) - 1)]   # the last class never occurs


@pytest.fixture
def history():
    rng = np.random.default_rng(3)
    rows = {t: [] for t in VOCAB}
    d = DAY - timedelta(days=400)
    for _ in range(260):
        d += timedelta(days=1)
        feats = _features(rng)
        for t in VOCAB:
            rows[t].append({"session_date": d, "snapshot_id": str(d), "outcome_revision": 1,
                            "features": feats, "actual_label": _label(t, feats["gap_signed_atr"], rng)})
    return rows


@pytest.fixture
def store(monkeypatch, history):
    calls = []

    def training_outcomes(conn, label_version, target_id, feature_version, before_session, available_by,
                          computed_by=None, limit=250, symbol="NQ"):
        calls.append({"target": target_id, "before": before_session, "available_by": available_by,
                      "computed_by": computed_by})
        rows = [r for r in history[target_id] if str(r["session_date"]) < str(before_session)]
        return list(reversed(rows))[:limit]   # newest first, like the store

    monkeypatch.setattr(models_v2.store, "training_outcomes", training_outcomes)
    monkeypatch.setattr(models_v2.store, "get_label_vocabulary", lambda conn, lv: VOCAB)
    return calls


def _snapshot(features, day=DAY, status=None, mode="historical_reconstruction"):
    s = cal.session(day)
    fs = {n: ("valid" if features.get(n) is not None else "missing") for n in catv2.FEATURE_NAMES}
    fs.update(status or {})
    return {"snapshot_id": "s1", "session_date": day, "cutoff_at": s.cutoff_at, "data_mode": mode,
            "feature_status": fs, "features": features}


@pytest.fixture(scope="module")
def fitted_cache():
    return {}


def _fit(store, fitted_cache, day=DAY):
    key = str(day)
    if key not in fitted_cache:
        fitted_cache[key] = models_v2.fit(None, models_v2.SKLEARN, day, cal.session(day).cutoff_at)
    return fitted_cache[key]


def test_training_is_point_in_time(store):
    s = cal.session(DAY)
    fitted = models_v2.fit(None, models_v2.CLIMATOLOGY, DAY, s.cutoff_at)
    assert {c["before"] for c in store} == {str(DAY)}
    assert {c["available_by"] for c in store} == {s.cutoff_at}
    assert all(t["last_session"] < str(DAY) for t in fitted["targets"].values())


def test_learns_a_real_signal_and_keeps_the_prior_for_noise(store, fitted_cache):
    fitted = _fit(store, fitted_cache)
    d15 = fitted["targets"]["direction_15m"]
    assert d15["status"] == "ok" and d15["selected"] != "prior"
    assert d15["cv"][d15["selected"]]["log_loss"] < d15["cv"]["prior"]["log_loss"] - 0.1
    assert d15["calibration_status"] == "validated_raw" and d15["cv_folds"] >= 3
    # Pure-noise targets: no feature model beats the class prior out of sample, so the prior is kept.
    for t in ("first_move_5m", "opening_type_15m", "direction_rth", "session_type_rth"):
        assert fitted["targets"][t]["selected"] == "prior"

    rng = np.random.default_rng(11)
    up = models_v2.predict(None, _snapshot(_features(rng, gap=2.5)), fitted=fitted)[1]
    down = models_v2.predict(None, _snapshot(_features(rng, gap=-2.5)), fitted=fitted)[1]
    p_up = {p["target_id"]: p for p in up}["direction_15m"]
    p_down = {p["target_id"]: p for p in down}["direction_15m"]
    assert p_up["predicted_label"] == "up" and p_down["predicted_label"] == "down"
    assert p_up["probabilities"]["up"] > 0.6 and p_down["probabilities"]["down"] > 0.6


def test_distributions_are_complete_and_argmax(store, fitted_cache):
    rng = np.random.default_rng(5)
    run, preds = models_v2.predict(None, _snapshot(_features(rng)), fitted=_fit(store, fitted_cache))
    assert [p["target_id"] for p in preds] == list(VOCAB)
    for p in preds:
        labels = VOCAB[p["target_id"]]
        assert list(p["probabilities"]) == labels
        assert sum(p["probabilities"].values()) == pytest.approx(1.0, abs=1e-9)
        assert all(0 < v < 1 for v in p["probabilities"].values())   # the never-seen class stays possible
        assert p["prediction_status"] == "issued" and p["decision_reason"] == "none"
        best = max(labels, key=lambda lab: (p["probabilities"][lab], -labels.index(lab)))
        assert p["predicted_label"] == best
    assert run["calibration"]["training"]["direction_15m"]["n"] == 260
    assert run["model_version"] == "nq_sklearn_v1" and run["calibration_version"] is None


def test_invalid_required_input_is_unavailable(store, fitted_cache):
    rng = np.random.default_rng(5)
    f = _features(rng)
    f["gap_signed_atr"] = None
    run, preds = models_v2.predict(None, _snapshot(f, status={"gap_signed_atr": "missing"}),
                                   fitted=_fit(store, fitted_cache))
    assert run["input_quality_status"] == "invalid"
    assert {(p["prediction_status"], p["decision_reason"]) for p in preds} == {("unavailable", "data_quality")}
    assert all(p.get("probabilities") is None for p in preds)


def test_optional_missing_input_is_imputed(store, fitted_cache):
    rng = np.random.default_rng(5)
    f = _features(rng)
    f["vix_level"] = f["rvol_60m_30d"] = f["weekday"] = None
    run, preds = models_v2.predict(None, _snapshot(f), fitted=_fit(store, fitted_cache))
    assert run["input_quality_status"] == "partial"
    assert {p["prediction_status"] for p in preds} == {"issued"}


def test_out_of_distribution_abstains_with_probabilities(store, fitted_cache):
    rng = np.random.default_rng(5)
    f = _features(rng)
    for n in ("return_15m_atr", "range_60m_atr", "vix_change_points"):
        f[n] = 50.0
    run, preds = models_v2.predict(None, _snapshot(f), fitted=_fit(store, fitted_cache))
    assert {(p["prediction_status"], p["decision_reason"]) for p in preds} == {("abstained", "out_of_distribution")}
    assert all(p["probabilities"] is not None and "predicted_label" not in p for p in preds)
    assert set(run["calibration"]["out_of_distribution_features"]["direction_15m"]) == {
        "return_15m_atr", "range_60m_atr", "vix_change_points"}


def test_early_close_abstains_full_rth_targets(store):
    early = next(s for s in cal.sessions_between("2026-01-01", "2026-12-31") if s.is_early_close)
    fitted = models_v2.fit(None, models_v2.CLIMATOLOGY, early.session_date, early.cutoff_at)
    rng = np.random.default_rng(5)
    _, preds = models_v2.predict(None, _snapshot(_features(rng), day=early.session_date),
                                 model=models_v2.CLIMATOLOGY, fitted=fitted)
    by = {p["target_id"]: (p["prediction_status"], p["decision_reason"]) for p in preds}
    assert by["direction_rth"] == by["session_type_rth"] == ("abstained", "shortened_session")
    assert by["direction_15m"] == ("issued", "none")


def test_too_little_history_is_unavailable(store):
    early_day = DAY - timedelta(days=380)   # ~20 earlier sessions in the fake history
    fitted = models_v2.fit(None, models_v2.SKLEARN, early_day, cal.session(DAY).cutoff_at - timedelta(days=380))
    rng = np.random.default_rng(5)
    snap = _snapshot(_features(rng), day=early_day)
    snap["cutoff_at"] = cal.session(DAY).cutoff_at - timedelta(days=380)
    _, preds = models_v2.predict(None, snap, fitted=fitted)
    assert {(p["prediction_status"], p["decision_reason"]) for p in preds} == {("unavailable", "uncertainty")}


def test_a_model_fitted_later_cannot_forecast_earlier(store, fitted_cache):
    rng = np.random.default_rng(5)
    earlier = _snapshot(_features(rng), day=DAY - timedelta(days=7))
    with pytest.raises(ValueError, match="not knowable"):
        models_v2.predict(None, earlier, fitted=_fit(store, fitted_cache))


def test_artifact_round_trip(store, fitted_cache, tmp_path):
    fitted = _fit(store, fitted_cache)
    path = str(tmp_path / "m.joblib")
    models_v2.save_artifact(fitted, path)
    loaded = models_v2.load_artifact(path)
    rng = np.random.default_rng(5)
    snap = _snapshot(_features(rng))
    a = models_v2.predict(None, snap, fitted=fitted)[1]
    b = models_v2.predict(None, snap, fitted=loaded)[1]
    assert [p["probabilities"] for p in a] == [p["probabilities"] for p in b]
    assert (tmp_path / "m.json").exists()


def test_registry_records_are_hashable_and_distinct():
    recs = [models_v2.registry_record(m) for m in models_v2.MODELS.values()]
    assert len({r["definition_hash"] for r in recs}) == len(recs)
    assert all("kind" not in r for r in recs)
    assert models_v2.DEFAULT_MODEL == "nq_sklearn_v1"
