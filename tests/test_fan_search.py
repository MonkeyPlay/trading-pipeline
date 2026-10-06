# tests/test_fan_search.py
"""
The multiple-comparison review (forecaster/fan_search.py): Hansen's SPA and Romano and
Wolf's StepM on synthetic loss differences - no candidate better, one clearly better among
many noisy ones, the lower / consistent / upper ordering, and the alignment of stored runs.
"""

import numpy as np
import pytest

from forecaster import fan_search as fs


def _noise(k=20, n=90, seed=0, ar=0.5, means=0.0):
    """k candidates' session differences, correlated across candidates and over sessions, each row's sample mean set
    exactly to ``means`` (so a test does not depend on a lucky draw)."""
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 1, n)
    e = rng.normal(0, 1, (k, n)) + common
    for t in range(1, n):
        e[:, t] += ar * e[:, t - 1]
    e /= e.std(axis=1, keepdims=True)
    return e - e.mean(axis=1, keepdims=True) + np.broadcast_to(np.asarray(means, dtype=float), (k,))[:, None]


def test_the_blocks_are_consecutive_sessions():
    idx = fs.block_indices(90, 5, 50, 7)
    assert idx.shape == (50, 90) and idx.min() >= 0 and idx.max() <= 89
    assert np.all(np.diff(idx[:, :5], axis=1) == 1)                 # the first block runs on


def test_with_no_better_candidate_neither_test_finds_one():
    d = _noise(means=np.linspace(-0.3, 0.02, 20))                    # the best only 0.02 sd above zero
    r = fs.spa(d)
    assert r["p"]["lower"] <= r["p"]["consistent"] <= r["p"]["upper"]
    assert r["p"]["consistent"] > 0.5 and fs.stepm(d)["better"] == []


def test_a_clearly_better_candidate_is_found_and_named():
    means = np.zeros(20)
    means[4] = 1.0                                                   # one candidate a full sd better per session
    d = _noise(seed=3, means=means)
    r = fs.spa(d)
    assert r["p"]["consistent"] < 0.01 and r["p"]["upper"] < 0.01
    assert fs.stepm(d)["better"] == [4]


def test_bad_candidates_matter_less_to_the_consistent_p_than_to_the_upper():
    means = np.r_[0.2, np.full(30, -0.6)]                            # one marginal, thirty clearly worse
    d = _noise(k=31, seed=8, ar=0.0, means=means)
    r = fs.spa(d)
    assert r["p"]["consistent"] < r["p"]["upper"]


def test_many_useless_candidates_raise_the_bar_for_a_marginal_one():
    alone = _noise(k=1, seed=5, ar=0.0, means=0.25)                   # t about 2.4 on its own
    crowd = np.vstack([alone, _noise(k=40, seed=6, ar=0.0, means=0.0)])
    assert fs.spa(alone)["p"]["consistent"] < 0.05 < fs.spa(crowd)["p"]["upper"]


def test_runs_align_on_shared_sessions_and_refuse_another_baseline():
    runs = {"a": {"per_session": {"d1": {"h15": [2.0, 1.5, 9]}, "d2": {"h15": [1.0, 1.2, 9]}}},
            "b": {"per_session": {"d1": {"h15": [2.0, 1.9, 9]}, "d2": {"h15": [1.0, 0.8, 9]},
                                  "d3": {"h15": [3.0, 3.0, 9]}}}}
    names, d, days = fs.aligned(runs, "h15")
    assert names == ["a", "b"] and days == ["d1", "d2"]
    assert np.allclose(d, [[0.5, -0.2], [0.1, 0.2]])                 # baseline minus each
    names, d, _ = fs.aligned(runs, "h15", benchmark="a")
    assert names == ["b"] and np.allclose(d, [[1.5 - 1.9, 1.2 - 0.8]])
    runs["b"]["per_session"]["d1"]["h15"][0] = 2.5
    with pytest.raises(ValueError):
        fs.aligned(runs, "h15")


def test_studentising_and_recentring_are_choices_that_can_change_the_named_runs():
    means = np.r_[0.35, 0.30, np.full(18, -0.8)]
    d = _noise(k=20, seed=11, ar=0.0, means=means)
    d[1] *= 3                                       # the second: mean 0.9, three times noisier - the same t as 0.3 / 1
    raw = fs.stepm(d, studentize=False, recentre="consistent")
    stud = fs.stepm(d, studentize=True, recentre="own")
    assert 1 in raw["better"]                                        # a large raw mean passes unstudentised ...
    assert raw["steps"][0]["critical"] > 0 and stud["steps"][0]["critical"] > 0
    own, cons = fs.stepm(d, recentre="own"), fs.stepm(d, recentre="consistent")
    assert cons["steps"][0]["critical"] <= own["steps"][0]["critical"]          # poor runs raise only the 'own' bar
    with pytest.raises(ValueError):
        fs.stepm(d, recentre="both")
