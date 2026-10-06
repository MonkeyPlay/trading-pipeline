# tests/test_fan_forward.py
"""
The forward record (forecaster/fan_forward.py, chunk 8): its issue times, the baseline's
shape built session by session exactly as fan_rw_v2's walk-forward builds it, an issue's
multipliers from the frozen definition alone with both fans ordered, and its scoring from
the origin price as read against the session's final prices.
"""

from dataclasses import replace
from datetime import date, datetime, time

import numpy as np
import pytest

from features import calendar as cal
from forecaster import fan_forward as fwd
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster import fan_v2 as v2
from forecaster.fan_benchmark import Day, slot_instant, slot_of_time
from tests.test_fan_checks import setup  # noqa: F401 - the shared synthetic market and manifest
from tests.test_fan_model import SMALL, mis_sized  # noqa: F401


def _et(d, hh, mm, ss=0):
    return cal.NY_TZ.localize(datetime(d.year, d.month, d.day, hh, mm, ss))


def test_the_issue_times():
    d = date(2026, 10, 7)                                                  # a full Wednesday session
    m = fwd.mark_at(_et(d, 10, 15, 30))
    assert m.session == d and m.origin_slot == slot_of_time(time(10, 14)) and not m.cutoff
    assert m.at == slot_instant(d, slot_of_time(time(10, 15)))             # the origin bar closes at the mark
    c = fwd.mark_at(_et(d, 9, 29, 20))
    assert c.cutoff and c.origin_slot == fh.PRE_OPEN_ORIGIN
    assert fwd.horizons_for(c)[-3:] == list(fh.PRE_OPEN_MINUTES)
    assert not fwd.mark_at(_et(d, 9, 31)).cutoff and fwd.mark_at(_et(d, 9, 31)).origin_slot == slot_of_time(time(9, 29))
    assert fwd.mark_at(_et(d, 16, 59, 59)).origin_slot == slot_of_time(time(16, 44))
    assert fwd.mark_at(_et(d, 17, 5)) is None                              # the halt
    evening = fwd.mark_at(_et(date(2026, 10, 6), 18, 20))                  # 18:20 belongs to the next session
    assert evening.session == date(2026, 10, 7) and evening.origin_slot == 14
    assert fwd.mark_at(_et(date(2026, 10, 6), 18, 10)) is None             # before the first mark
    assert fwd.mark_at(_et(date(2026, 10, 10), 12, 0)) is None             # a Saturday


def test_the_shape_is_the_walk_forwards_session_by_session(setup, tmp_path):
    days, frames, _ = setup
    last = days[-1].session_date
    want = next(iter(v2.walk_forward(days, last, last)))[2].at(np.array(fh.FRAME_HORIZONS))
    got = fwd.shape_from_history(days[:-1], str(tmp_path), "NQ")
    assert np.array_equal(got, want) and np.array_equal(got, frames[last.isoformat()].shape)
    again = fwd.shape_from_history(days[:-1], str(tmp_path), "NQ")         # from the cached errors
    assert np.array_equal(again, want) and any(tmp_path.rglob("*.npz"))


def _issue_inputs(mis_sized, setup, hhmm):
    wrong, manifest, table, _ = mis_sized
    days = {d.session_date.isoformat(): d for d in setup[0]}
    sessions = sorted(wrong)
    cand = fm.LinearScale(table, ["NQ.hint"], "lin_pois_ivx")
    cand.fit(fh.Rows.concat([fh.frame_rows(wrong[d], every=5) for d in sessions[:20]]))
    experiment = {"version": "fan_test", "definition_hash": "abc", "definition": {
        "gate": {"pass": "p", "inconclusive": "i", "fail": "f"}, "targets": {"primary": "NQ"},
        "horizons": {"primary": {"minutes": 15}}}}
    definition = fm.frozen_definition("lin_pois_ivx", cand, experiment, {"version": "fan_rw_v2",
                                      "definition_hash": "h"}, sessions[:20], {"feature_format": 3, "frame_format": 1})
    s = sessions[25]
    d = date.fromisoformat(s)
    mark = fwd.mark_at(_et(d, *hhmm))
    fr = wrong[s]
    price_lp = fr.log_price.copy()
    closes = np.exp(price_lp)
    read = closes.copy()
    read[mark.origin_slot + 1:] = np.nan                                   # the store as of the mark
    day = replace(days[s], closes=read, complete=False)
    return mark, day, fr, table, definition, cand


def test_an_issue_comes_from_the_frozen_definition_alone(mis_sized, setup):
    mark, day, fr, table, definition, cand = _issue_inputs(mis_sized, setup, (9, 29, 10))
    V = fr.var[:, mark.origin_slot]
    built = fwd.build_issue(mark, day, V, fr.shape, table, definition, [])
    assert built["horizons"] == list(fh.REPORT_HORIZONS) + list(fh.PRE_OPEN_MINUTES)   # the cutoff adds the slice
    rows = fh.frame_rows(fr)
    at = rows.take(rows.slot == mark.origin_slot)
    want = dict(zip(at.horizon.tolist(), cand.predict(at)))
    for h, f in built["forecast"].items():
        assert f["multiplier"] == pytest.approx(want[int(h)], rel=1e-12)
        assert np.all(np.diff(f["base"]) > 0) and np.all(np.diff(f["model"]) > 0)
        assert f["base"][fwd.CHART_LEVELS.index(0.5)] == pytest.approx(built["origin_price"], rel=1e-12)  # no drift
    assert built["origin_price"] == pytest.approx(np.exp(fr.log_price[mark.origin_slot]))
    late = replace(day, closes=np.where(np.arange(len(day.closes)) >= mark.origin_slot, np.nan, day.closes))
    with pytest.raises(ValueError):
        fwd.build_issue(mark, late, V, fr.shape, table, definition, [])        # the origin bar was not read


def test_an_issue_is_scored_from_its_origin_price_as_read(mis_sized, setup):
    mark, day, fr, table, definition, _ = _issue_inputs(mis_sized, setup, (11, 0, 20))
    built = fwd.build_issue(mark, day, fr.var[:, mark.origin_slot], fr.shape, table, definition, [])
    final = replace(day, closes=np.exp(fr.log_price), complete=True)
    shapes = {h: fr.shape[i] for i, h in enumerate(fh.FRAME_HORIZONS)}
    rows = fwd.score_issue(built["forecast"], mark.origin_slot, built["origin_price"], final, shapes)
    assert {r["horizon"] for r in rows} == set(built["horizons"])
    for r in rows:
        f = built["forecast"][str(r["horizon"])]
        y = fr.log_price[mark.origin_slot + r["horizon"]] - fr.log_price[mark.origin_slot]
        assert r["realised"] == pytest.approx(y) and r["revision"] == pytest.approx(0.0, abs=1e-12)
        Q = shapes[r["horizon"]]
        assert r["crps_base_bps"] == pytest.approx(f["sigma"] * fh.crps(np.array([y / f["sigma"]]), Q)[0] * 1e4)
        sm = f["sigma"] * f["multiplier"]
        assert r["crps_model_bps"] == pytest.approx(sm * fh.crps(np.array([y / sm]), Q)[0] * 1e4)
        assert 0 <= r["pit_base"] <= 1 and 0 <= r["pit_model"] <= 1
    revised = replace(final, closes=final.closes * np.where(np.arange(len(final.closes)) == mark.origin_slot, 1.001, 1.0))
    r2 = fwd.score_issue(built["forecast"], mark.origin_slot, built["origin_price"], revised, shapes)
    assert r2[0]["revision"] == pytest.approx(np.log(1.001))               # a revised origin bar is measured


def test_the_pending_marks_are_the_last_half_hours_oldest_first():
    d = date(2026, 10, 7)
    at = lambda m: (m.at.astimezone(cal.NY_TZ).hour, m.at.astimezone(cal.NY_TZ).minute)
    assert [at(m) for m in fwd.pending_marks(_et(d, 10, 47, 30))] == [(10, 30), (10, 45)]
    assert [at(m) for m in fwd.pending_marks(_et(d, 9, 40, 5))] == [(9, 15), (9, 29), (9, 30)]
    assert [m.cutoff for m in fwd.pending_marks(_et(d, 9, 40, 5))] == [False, True, False]
    assert fwd.pending_marks(_et(date(2026, 10, 10), 12, 0)) == []                 # a Saturday: nothing pending
    across = fwd.pending_marks(_et(date(2026, 10, 6), 18, 20))                     # the new session's first mark
    assert [(m.session, at(m)) for m in across] == [(date(2026, 10, 7), (18, 15))]
