# tests/test_fan_checks.py
"""
The intermarket experiment's checks harness (forecaster/fan_harness.py, chunk 3): the
baseline's frames as issued, training rows every five minutes, a candidate trained only
on the sessions before its check, paired comparisons per horizon, phase, release ahead
and the pre-open slice - on known synthetic markets.
"""

from dataclasses import replace

import numpy as np
import pytest

from forecaster import fan_harness as fh
from forecaster import fan_v2 as v2
from forecaster.fan_scoring import PHASES
from tests.test_fan import market

BLOCK = 8


@pytest.fixture(scope="module")
def setup():
    """A 44-session market with a release every fourth session, its v2 frames, and a manifest whose development
    starts at the 13th session and ends with three checks of BLOCK sessions."""
    days = market(44, seed=21, release_every=4)
    dates = [d.session_date.isoformat() for d in days]
    frames = fh.baseline_frames(days, days[0].session_date, days[-1].session_date)
    dev = dates[12:]
    first = len(dev) - 3 * BLOCK
    blocks = [{"check": k + 1, "sessions": {"first": dev[first + k * BLOCK], "last": dev[first + (k + 1) * BLOCK - 1]}}
              for k in range(3)]
    manifest = {"name": "fan_checks_test", "targets": {"primary": "NQ", "secondary": ["ES", "RTY"]},
                "horizons": {"primary": {"minutes": 15}, "secondary": {"minutes": [5, 30, 60]}},
                "data": {"excluded": {"NQ": [], "ES": [dev[0]]}},
                "split": {"development": {"sessions": dev}, "checks": {"blocks": blocks}}}
    return days, frames, manifest


def test_frames_reproduce_the_baseline_as_the_gate_scores_it(setup):
    days, frames, _ = setup
    issued = list(v2.walk_forward(days, days[30].session_date, days[30].session_date))
    d, model, shape = issued[0]
    fr = frames[d.session_date.isoformat()]
    rows = fh.frame_rows(fr)
    mine = fh.score_frame(fr, rows, np.ones(len(rows)))
    gate = fh.compare_session(d, (model, shape.at), (model, shape.at), horizons=fh.REPORT_HORIZONS)
    for k in ("h1", "h15", "h240", "pre_open_16"):
        assert mine[k][0] == pytest.approx(gate[k][0], rel=1e-9) and mine[k][2] == gate[k][2]
        assert mine[k][0] == mine[k][1]                                   # the identity changes nothing


def test_training_rows_sample_every_five_minutes_and_keep_the_pre_open_origin(setup):
    _, frames, _ = setup
    fr = next(iter(frames.values()))
    every, sampled = fh.frame_rows(fr), fh.frame_rows(fr, every=5)
    for h in fh.REPORT_HORIZONS:
        s = sampled.slot[sampled.horizon == h]
        assert len(s) and set(s % 5) == {0}
        assert abs(len(every.slot[every.horizon == h]) / len(s) - 5) < 0.1
    for m in fh.PRE_OPEN_MINUTES:
        assert list(sampled.slot[sampled.horizon == m]) == [fh.PRE_OPEN_ORIGIN]   # the slice: its one origin only
    assert np.all(every.slot + every.horizon < fr.end)
    assert np.allclose(every.y, fr.log_price[every.slot + every.horizon] - fr.log_price[every.slot])
    assert every.ahead.any() == any(fr.ahead[fh.FRAME_HORIZONS.index(h)].any() for h in fh.REPORT_HORIZONS)


class Spy(fh.Identity):
    """Records the sessions it was trained on and asked about."""
    seen = []

    def fit(self, rows):
        self.trained = set(rows.session.astype(str))

    def predict(self, rows):
        Spy.seen.append((self.trained, set(rows.session.astype(str))))
        return super().predict(rows)


def test_a_check_trains_only_on_the_development_sessions_before_it(setup):
    _, frames, manifest = setup
    Spy.seen = []
    res = fh.run_checks(frames, manifest, "NQ", Spy)
    dev = manifest["split"]["development"]["sessions"]
    for c in res["checks"]:
        first = c["sessions"]["first"]
        mine = [(t, s) for t, s in Spy.seen if min(s) >= first and max(s) <= c["sessions"]["last"]]
        assert len(mine) == BLOCK
        trained = mine[0][0]
        assert trained == {d for d in dev if d < first} and max(trained) < first     # all before it, nothing after
        assert c["train"]["sessions"] == len(trained)
    res_es = fh.run_checks(frames, manifest, "ES", Spy)
    assert res_es["checks"][0]["train"]["sessions"] == res["checks"][0]["train"]["sessions"] - 1   # excluded: unused


def test_the_identity_differs_by_exactly_zero_everywhere(setup):
    _, frames, manifest = setup
    res = fh.run_checks(frames, manifest, "NQ", fh.Identity)
    assert res["candidate"] == "identity" and set(res["results"]) == set(fh.check_keys())
    for k, p in res["results"].items():
        if p is not None:
            assert p["diff_bps"] == 0
            assert res["verdicts"][k] == ("inconclusive" if p["sessions"] >= 10 else "no interval")   # two blocks
    assert res["results"]["h15"]["sessions"] == 3 * BLOCK
    assert all(res["results"][f"h15:{name}"] for name, _, _ in PHASES if name != "after_close")
    assert res["results"]["h15:release"]["sessions"] > 0 and res["results"]["pre_open_31"]["origins"] == 3 * BLOCK


def test_phase_scale_narrows_a_baseline_that_is_too_wide(setup):
    _, frames, manifest = setup
    wide = {k: replace(f, var=f.var * 2.25) for k, f in frames.items()}          # every sigma 1.5x too wide
    res = fh.run_checks(wide, manifest, "NQ", fh.PhaseScale)
    for k in ("h5", "h15", "h60"):
        assert res["results"][k]["diff_bps"] < 0 and res["verdicts"][k] == "better"
    cand = fh.PhaseScale()
    cand.fit(fh.Rows.concat([fh.frame_rows(f, every=5) for f in wide.values()]))
    assert cand.by_horizon[15] == pytest.approx(1 / 1.5, rel=0.1)
    worse = fh.run_checks(frames, manifest, "NQ", lambda: _Fixed(1.6))
    assert worse["verdicts"]["h15"] == "worse"


class _Fixed(fh.Identity):
    def __init__(self, m):
        self.m = m

    def predict(self, rows):
        return np.full(len(rows), self.m)


def test_a_candidate_must_give_every_row_a_positive_multiplier(setup):
    _, frames, _ = setup
    fr = next(iter(frames.values()))
    rows = fh.frame_rows(fr)
    for bad in (np.zeros(len(rows)), np.full(len(rows), np.nan), np.ones(len(rows) - 1)):
        with pytest.raises(ValueError):
            fh.score_frame(fr, rows, bad)


def test_cached_frames_read_back_identical(setup, tmp_path):
    _, frames, _ = setup
    path = str(tmp_path / "frames.npz")
    fh._write_frames(path, frames)
    back = fh._read_frames(path)
    assert sorted(back) == sorted(frames)
    for k, f in frames.items():
        b = back[k]
        assert b.end == f.end and np.array_equal(b.ahead, f.ahead)
        for a in ("log_price", "var", "shape"):
            assert np.array_equal(getattr(b, a), getattr(f, a), equal_nan=True)


def test_roles_follow_the_manifest(setup):
    _, _, m = setup
    role = lambda k, t="NQ": fh._role(k, m, t)
    assert (role("h15"), role("h5"), role("h20"), role("pre_open_16")) == ("primary", "secondary", "exploratory",
                                                                          "secondary")
    assert (role("h15", "ES"), role("h5", "RTY"), role("pre_open_16", "ES")) == ("secondary", "exploratory",
                                                                                   "exploratory")
    assert role("h15:midday") == role("h15:release") == "exploratory"


def test_training_rows_can_start_at_another_minute_of_the_five(setup):
    _, frames, _ = setup
    fr = next(iter(frames.values()))
    r = fh.frame_rows(fr, every=5, offset=2)
    assert set(r.slot[r.horizon == 15] % 5) == {2} and list(r.slot[r.horizon == 16]) == [fh.PRE_OPEN_ORIGIN]


def test_calibration_counts_misses_on_each_side_and_widths(setup):
    _, frames, manifest = setup
    res = fh.run_checks(frames, manifest, "NQ", lambda: _Fixed(1.6))
    b, c = res["calibration"]["base|h15|all"], res["calibration"]["cand|h15|all"]
    assert b["n"] == c["n"] == res["results"]["h15"]["origins"]
    for k in ("0.50", "0.90"):
        assert c["coverage"][k] > b["coverage"][k]                                  # wider covers more
        assert c["width_bps"][k] == pytest.approx(1.6 * b["width_bps"][k], rel=1e-9)
        assert b["coverage"][k] == pytest.approx(1 - b["below"][k] - b["above"][k])
    assert abs(b["coverage"]["0.90"] - 0.90) < 0.05 and sum(b["pit"]) == pytest.approx(1.0)
    assert "base|h15|midday" in res["calibration"]


def test_concentration_of_the_gain():
    rows = [{"h15": (1.0, 1.0 + x, 10)} for x in (-0.5, -0.1, -0.1, 0.2, -0.1, -0.1, -0.1)]
    days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-09", "2026-03-10", "2026-03-16", "2026-03-17"]
    c = fh.concentration(rows, days, "h15")
    assert c["sessions"] == 7 and c["improved"] == pytest.approx(6 / 7) and c["weeks"] == 3
    assert c["top5_share"] == pytest.approx(-0.9 / -0.8)                    # five best carry more than the total
    assert c["worst5_bps"] == pytest.approx(-0.2) and c["leave_week_out"]["weeks_flipping_sign"] == 0


def test_two_stored_runs_pair_on_identical_sessions(setup):
    _, frames, manifest = setup
    a = fh.run_checks(frames, manifest, "NQ", fh.Identity)
    b = fh.run_checks(frames, manifest, "NQ", lambda: _Fixed(1.6))
    assert len(a["per_session"]) == 3 * BLOCK and set(a["per_session"]) == set(b["per_session"])
    c = fh.compare_runs(a, b)
    assert c["sessions"] == 3 * BLOCK and c["verdicts"]["h15"] == "worse"
    assert c["results"]["h15"]["diff_bps"] == pytest.approx(b["results"]["h15"]["diff_bps"])   # identity: v2 itself
    other = dict(b, per_session={d: {k: [v[0] * 2, v[1], v[2]] for k, v in r.items()}
                                 for d, r in b["per_session"].items()})
    with pytest.raises(ValueError):
        fh.compare_runs(a, other)                                    # a different baseline is not comparable
