# tests/test_experiments.py
"""Stage 4 scoring (forecaster/experiments.py) on hand-built cases - no database. Registration, frozen cases and
stored results are tested in tests/test_nq_journal.py."""

import math
from fractions import Fraction

import pytest

from contracts import nq_forecast as fc
from forecaster import experiments as ex


def dist(**p):
    return {c: Fraction(v) for c, v in p.items()}


def test_log_loss_and_the_unhalved_brier_sum():
    d = dist(bullish="1/2", bearish="1/4", neutral_band="1/4")
    assert ex.log_loss(d, "bullish") == pytest.approx(math.log(2))
    assert ex.brier(d, "bullish") == pytest.approx(0.25 + 0.0625 + 0.0625)        # (1/2)^2 + (1/4)^2 + (1/4)^2
    assert ex.brier(dist(bullish=1, bearish=0, neutral_band=0), "bearish") == pytest.approx(2.0)   # the maximum
    assert math.isinf(ex.log_loss(dist(bullish=1, bearish=0, neutral_band=0), "bearish"))    # never clipped


def prediction(status="predicted", label="bullish", **p):
    return {"status": status, "predicted_label": label if status == "predicted" else None,
            "distribution": {c: v for c, v in p.items()} or None}


def test_a_case_counts_only_with_a_realised_label_and_a_distribution():
    p = prediction(bullish="1/2", bearish="1/4", neutral_band="1/4")
    assert ex.case_score(p, {"label": None, "reason": "ambiguous_intrabar"}) == {"status": "no_label",
                                                                               "reason": "ambiguous_intrabar"}
    assert ex.case_score(prediction("unavailable"), {"label": "bullish"})["status"] == "no_distribution"
    s = ex.case_score(p, {"label": "bearish"})
    assert (s["status"], s["p"], s["classed"], s["correct"]) == ("scored", 0.25, True, False)
    tie = ex.case_score(prediction("ambiguous_prediction", bullish="1/2", bearish="1/2", neutral_band="0"),
                        {"label": "bullish"})
    assert tie["ambiguous"] and not tie["classed"] and not tie["correct"]        # no class: counted apart


def test_the_block_bootstrap_is_deterministic():
    diffs = [0.1, -0.2, 0.05, 0.3, -0.1, 0.0, 0.2, -0.05, 0.15, 0.1]
    a = ex.block_bootstrap(diffs, 3, 500, 7, 0.95)
    assert a == ex.block_bootstrap(diffs, 3, 500, 7, 0.95) and a[0] <= sum(diffs) / len(diffs) <= a[1]
    assert ex.block_bootstrap([0.5] * 8, 3, 200, 1, 0.95) == (0.5, 0.5)
    assert ex.block_bootstrap([0.5] * 5, 3, 200, 1, 0.95) is None              # under two blocks: no interval


def run(issued_at, mode="historical_replay", acked=None, deadline="2026-06-12 13:29:50"):
    return {"run_id": issued_at, "issued_at": issued_at, "mode": mode, "lifecycle_status": "issued",
            "deadline_at": deadline if mode == "live" else None,
            "events": [{"event": "acknowledged", "at": acked}] if acked else []}


def test_the_official_run_rule():
    runs = [run("2026-06-12 10:00:00"), run("2026-06-12 11:00:00")]
    assert ex.official_run(runs, "first")["run_id"] == "2026-06-12 10:00:00"
    assert ex.official_run(runs, "latest")["run_id"] == "2026-06-12 11:00:00"
    assert ex.official_run([], "first") is None
    live = [run("2026-06-12 13:29:40", "live"), run("2026-06-12 13:29:45", "live", acked="2026-06-12 13:29:46")]
    assert ex.official_run(live, "first_timely")["run_id"] == "2026-06-12 13:29:45"   # the first was never acked


def test_paired_differences_on_common_sessions():
    a = {"d1": {"status": "scored", "brier": 0.6, "log_loss": 1.0, "classed": True, "correct": False},
         "d2": {"status": "scored", "brier": 0.4, "log_loss": math.inf, "classed": True, "correct": True},
         "d3": {"status": "no_label", "reason": "uncovered"}}
    b = {"d1": {"status": "scored", "brier": 0.5, "log_loss": 0.8, "classed": True, "correct": True},
         "d2": {"status": "scored", "brier": 0.5, "log_loss": 0.9, "classed": True, "correct": True},
         "d3": {"status": "scored", "brier": 0.1, "log_loss": 0.1, "classed": True, "correct": True}}
    p = ex.paired(a, b, ["d1", "d2", "d3"], ex.UNCERTAINTY)
    assert p["common"] == 2                                              # d3 has no realised label in A's case
    assert p["brier"]["diff"] == pytest.approx(0.0)                      # (-0.1 + 0.1) / 2
    assert p["log_loss"]["both_finite"] == 1 and p["log_loss"]["diff"] == pytest.approx(-0.2)
    assert p["log_loss"]["infinite_base_only"] == 1 and p["accuracy"]["base"] == pytest.approx(0.5)


def test_the_manifest_fixes_every_choice_before_scoring():
    m = ex.experiment_manifest("x", "2025-09-02", "2026-10-02")
    assert m["arms"] == {"A": {"algorithm": fc.PRIOR_VERSION, "question": ex.QUESTIONS["A"]},
                         "B": {"algorithm": fc.BASELINE_VERSION, "question": ex.QUESTIONS["B"]}}
    assert m["primary"]["target"] == "direction_15m" and m["purpose"] == "development"
    assert "development data" in m["purpose_note"] and m["uncertainty"]["seed"] == 20261004
    for bad in (dict(official_run="best"), dict(official_run="first_timely"), dict(purpose="final"),
                dict(arms={"A": "nq_magic_v1"})):
        with pytest.raises(ValueError):
            ex.experiment_manifest("x", "2025-09-02", "2026-10-02", **bad)
