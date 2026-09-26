# tests/test_scoring_v2.py
"""Group scores and the paired, same-session comparison with the baseline."""

import math

import pytest

from forecaster import scoring_v2 as sc

UNIFORM = {"up": 1 / 3, "down": 1 / 3, "flat": 1 / 3}
SHARP = {"up": 0.6, "down": 0.2, "flat": 0.2}


def _row(model, day, probs, actual, status="issued", mode="historical_reconstruction", generated="1",
         eligible=True, target="direction_15m"):
    return {"model_version": model, "target_id": target, "data_mode": mode, "session_date": day,
            "probabilities": probs, "actual_label": actual if eligible else None, "eligible": eligible,
            "prediction_status": status, "generated_at": generated,
            "predicted_label": max(probs, key=probs.get) if probs and status == "issued" else None}


def test_group_scores():
    rows = [_row("m", "d1", SHARP, "up"), _row("m", "d2", SHARP, "down"),
            _row("m", "d3", None, None, status="unavailable", eligible=False)]
    g = sc.score_groups(rows)[0]
    assert (g["n"], g["issued"], g["unavailable"], g["ineligible"]) == (3, 2, 1, 1)
    assert g["accuracy"] == 0.5
    assert g["log_loss"] == pytest.approx((-math.log(0.6) - math.log(0.2)) / 2)
    assert g["brier"] == pytest.approx(((0.4 ** 2 + 0.04 + 0.04) + (0.36 + 0.64 + 0.04)) / 2)


def test_paired_comparison_uses_common_sessions_only():
    rows = [
        # the baseline also scored d0, which the model never did: excluded
        _row("base", "d0", UNIFORM, "flat"),
        _row("base", "d1", UNIFORM, "up"), _row("base", "d2", UNIFORM, "down"),
        _row("model", "d1", SHARP, "up"), _row("model", "d2", SHARP, "down"),
        # an older run of the model on d1 is superseded by the newer one above
        _row("model", "d1", {"up": 0.1, "down": 0.1, "flat": 0.8}, "up", generated="0"),
        # abstained predictions with probabilities are scored; unavailable ones are not
        _row("model", "d3", None, "up", status="unavailable"), _row("base", "d3", UNIFORM, "up"),
    ]
    (c,) = sc.paired_comparison(rows, "base")
    assert c["n"] == 2
    ll_model = (-math.log(0.6) - math.log(0.2)) / 2
    assert c["log_loss"] == pytest.approx(ll_model)
    assert c["baseline_log_loss"] == pytest.approx(math.log(3))
    assert c["log_loss_skill"] == pytest.approx(1 - ll_model / math.log(3))
    assert c["gain"] == pytest.approx(math.log(3) - ll_model)
    gains = [math.log(3) + math.log(0.6), math.log(3) + math.log(0.2)]
    mean = sum(gains) / 2
    assert c["gain_se"] == pytest.approx(math.sqrt(sum((g - mean) ** 2 for g in gains) / 1 / 2))
    assert c["brier_skill"] == pytest.approx(1 - c["brier"] / c["baseline_brier"])


def test_paired_comparison_keeps_modes_apart():
    rows = [_row("base", "d1", UNIFORM, "up"), _row("model", "d1", SHARP, "up", mode="live_capture")]
    assert sc.paired_comparison(rows, "base") == []
