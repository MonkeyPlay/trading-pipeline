# tests/test_fan_replay.py
"""
The live-style replay (forecaster/fan_replay.py): its predefined sessions come from what
was known before each opened, and its comparisons treat two missing values as equal and a
float32 rounding as equal - nothing more. The replay itself reads the store
(scripts/fan.py replay).
"""

import numpy as np

from forecaster import fan_replay as frp


def test_the_sessions_are_picked_by_rule_without_overlap():
    days = [f"2026-03-{d:02d}" for d in range(2, 30)]
    day_rv = {d: float(i % 7 - 3) for i, d in enumerate(days)}          # 3 marks the most volatile
    day_rv[days[0]] = np.nan                                            # unknown: never volatile or ordinary
    releases = days[1::5]
    picked = frp.pick_sessions(days, day_rv, releases)
    assert len(releases) == 6 and picked["release"] == [releases[0], releases[2], releases[5]]   # evenly spread
    every = [d for v in picked.values() for d in v]
    assert len(every) == len(set(every)) == 3 * frp.PER_KIND
    assert all(day_rv[d] == 3 for d in picked["volatile"])
    assert all(d not in releases and day_rv[d] == 0 for d in picked["ordinary"])
    assert days[0] not in every


def test_the_comparison_tolerance():
    a = np.array([1.0, np.nan, 2.0, 1e6])
    assert frp._same(a, a.copy()).all()
    assert frp._same(a, a * (1 + 5e-6)).all() and not frp._same(a, a * (1 + 5e-4))[[0, 2, 3]].any()
    assert not frp._same(np.array([np.nan]), np.array([1.0]))[0]
