# tests/test_fan_first_hit.py
"""First-hit probabilities (forecaster/fan_first_hit.py): the barrier labels from minute bars, and the pipeline on
synthetic sessions - a planted directional relationship recovered, a no-signal control quiet."""

import numpy as np
import pytest

from forecaster import fan_first_hit as fhit
from forecaster import fan_harness as fh
from forecaster import fan_im_direction as im
from forecaster import fan_power as fp
from forecaster.fan_benchmark import DAY_SLOTS


def _flat(n=DAY_SLOTS):
    lp = np.zeros(n)
    return lp, np.full(n, 0.0005), np.full(n, -0.0005)


def _label(hi, lo, lp=None, t=100, b=0.002):
    lp = np.zeros(DAY_SLOTS) if lp is None else lp
    return fhit.labels(lp, hi, lo, np.array([t]), np.array([b]))[0]


def test_first_barrier_decides():
    lp, hi, lo = _flat()
    assert _label(hi, lo) == fhit.NEITHER
    hi2, lo2 = hi.copy(), lo.copy()
    hi2[105] = 0.003
    lo2[108] = -0.003
    assert _label(hi2, lo2) == fhit.UP
    hi2[105] = 0.0
    assert _label(hi2, lo2) == fhit.DOWN


def test_both_in_one_minute_is_ambiguous_and_only_the_window_is_read():
    lp, hi, lo = _flat()
    hi[103], lo[103] = 0.003, -0.003
    assert _label(hi, lo) == fhit.AMBIGUOUS
    lp, hi, lo = _flat()
    hi[100] = 0.01          # the origin's own bar: already in its close, not after it
    lo[116] = -0.01         # minute 16: past the horizon
    hi[85] = 0.01
    assert _label(hi, lo) == fhit.NEITHER
    lo[115] = -0.01         # minute 15: inside
    assert _label(hi, lo) == fhit.DOWN
    missing = np.full(DAY_SLOTS, np.nan)
    assert _label(missing, missing) == fhit.NEITHER


def test_scores_both_ambiguity_assignments():
    y = np.array([fhit.UP, fhit.AMBIGUOUS, fhit.NEITHER])
    assert (fhit.outcome_matrix(y, "up_first")[1] == [0, 0, 1]).all()
    assert (fhit.outcome_matrix(y, "down_first")[1] == [1, 0, 0]).all()
    p = np.tile([0.3, 0.4, 0.3], (3, 1))
    np.testing.assert_allclose(fhit.brier(p, np.eye(3)[[2, 0, 1]]), [0.09 + 0.16 + 0.49, 0.49 + 0.16 + 0.09,
                                                                     0.09 + 0.36 + 0.09])
    q = fhit._sym(np.array([[0.1, 0.3, 0.6]]))
    np.testing.assert_allclose(q, [[0.35, 0.3, 0.35]])


def _synthetic(kappa, seed=0, n_sessions=150):
    frames, table, blocks, sessions, _ = fp.synthetic(kappa, n_sessions, seed, names=im.inputs("intermarket"),
                                                      planted="ES.ret5")
    rng = np.random.default_rng(seed + 99)
    hl = {}
    for d in sessions:
        lp = frames[d].log_price
        prev = np.concatenate([[lp[0]], lp[:-1]])
        wig = np.abs(rng.normal(0, fp.SIGMA * 0.5, (2, DAY_SLOTS)))
        hl[d] = (np.maximum(prev, lp) + wig[0], np.minimum(prev, lp) - wig[1])
    return frames, table, hl, blocks, sessions


def test_pipeline_recovers_a_planted_directional_first_hit():
    frames, table, hl, blocks, sessions = _synthetic(0.08)
    res = fhit.run(frames, table, hl, blocks, sessions)
    assert res["conclusion"]["H2"]["verdict"] == "pass"
    assert res["conclusion"]["H2"]["directional"] is True
    assert res["blocks"][0]["selection"]["intermarket"]["chosen"]["params"] is not None


@pytest.mark.parametrize("seed", [6])
def test_no_signal_control_does_not_pass(seed):
    frames, table, hl, blocks, sessions = _synthetic(0.0, seed)
    res = fhit.run(frames, table, hl, blocks, sessions)
    assert res["conclusion"]["H1"]["verdict"] != "pass" and res["conclusion"]["H2"]["verdict"] != "pass"
