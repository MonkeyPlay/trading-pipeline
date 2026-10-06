# tests/test_fan_forward.py
"""
The forward record (forecaster/fan_forward.py, chunk 8): its issue times, the baseline's
shape built session by session exactly as fan_rw_v2's walk-forward builds it, an issue's
multipliers from the frozen definition alone with both fans ordered, and its scoring from
the origin price as read against the session's final prices; the mark trigger (which mark
is due, collecting until the origin bar is stored or the deadline nears) and the timing of
each trigger against the stored rules' deadline.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Timing rules, by the stored version (pure)
# ---------------------------------------------------------------------------

V2 = {"params": {"live_seconds": 60, "remaining_share": 0.75, "grace_minutes": 30, "mark_minutes": 15}}
V1 = {"timeliness": {"on_time": "at least 75 % of the horizon remains at issuance: issued_at <= mark + 0.25 x h"}}


def test_the_boundaries_of_live_delayed_origin_late_and_expired():
    from datetime import timedelta, timezone
    p = fwd.rules_params(V2)
    mark = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
    at = lambda s: mark + timedelta(seconds=s)
    assert fwd.classify(p, mark, at(60), 5) == "live"                     # exactly the live limit
    assert fwd.classify(p, mark, at(60.001), 5) == "delayed_origin"        # just over it: 79.99 % of 5 min left
    assert fwd.classify(p, mark, at(75), 5) == "delayed_origin"            # exactly 75 % left
    assert fwd.classify(p, mark, at(75.001), 5) == "late"                  # just under
    assert fwd.classify(p, mark, at(299.999), 5) == "late"                 # a moment before the target
    assert fwd.classify(p, mark, at(300), 5) == "expired"                  # at the target
    assert fwd.classify(p, mark, at(301), 5) == "expired"
    assert fwd.classify(p, mark, at(59), 1) == "live" and fwd.classify(p, mark, at(60), 1) == "expired"
    assert fwd.classify(p, mark, at(690), 60) == "delayed_origin"          # 11.5 min late: 80.8 % of an hour left
    assert fwd.classify(p, mark, at(690), 15) == "late"                    # ... but 23 % of 15 minutes


def test_an_issue_is_classified_by_its_own_versions_stored_rules(monkeypatch):
    from datetime import timedelta, timezone
    mark = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
    issued = mark + timedelta(seconds=40)
    p1, p2 = fwd.rules_params(V1), fwd.rules_params(V2)
    assert p1 == {"labels": "v1", "live_seconds": None, "remaining_share": 0.75}
    assert fwd.classify(p1, mark, issued, 15) == "on_time" and fwd.classify(p2, mark, issued, 15) == "live"
    monkeypatch.setattr(fwd, "LIVE_SECONDS", 1)                            # today's defaults change ...
    monkeypatch.setattr(fwd, "REMAINING_SHARE", 0.99)
    monkeypatch.setitem(fwd.RULES, "params", {"live_seconds": 1, "remaining_share": 0.99})
    assert fwd.classify(fwd.rules_params(V1), mark, issued, 15) == "on_time"     # ... the stored versions do not
    assert fwd.classify(fwd.rules_params(V2), mark, issued, 15) == "live"
    assert fwd.rules_params(None) is None


def _mark(d, hh, mm):
    return _et(d, hh, mm).astimezone(timezone.utc)


def test_the_denominator_counts_every_expected_mark_from_activation():
    a, b = date(2026, 10, 7), date(2026, 10, 8)                           # two full sessions
    start, now = _mark(a, 10, 0), _mark(b, 17, 30)
    attempts = [(_mark(b, 14, 0), "issued"), (_mark(b, 14, 15), "stale"), (_mark(b, 14, 30), "issued")]
    issues = [{"issue_id": "1", "record": "rules_v2", "mark_at": _mark(b, 14, 0), "issued_at": _mark(b, 14, 0)
               + timedelta(seconds=30), "horizons": ["5", "60"]},
              {"issue_id": "2", "record": "rules_v2", "mark_at": _mark(b, 14, 30), "issued_at": _mark(b, 14, 41),
               "horizons": ["5", "60"]},
              {"issue_id": "3", "record": None, "mark_at": _mark(a, 9, 0), "issued_at": _mark(a, 9, 20),
               "horizons": ["15"]}]
    out = fwd.summarise([("rules_v2", V2, start)], attempts, issues, [], now)
    g, legacy = out
    o = g["operations"]
    marks_a = len([t for t in fwd.session_marks(a) if t >= start])        # 10:00 onwards, no attempt that day
    assert o["expected"] == marks_a + len(fwd.session_marks(b)) and o["sessions_expected"] == 2
    assert o["sessions_attempted"] == 1 and o["attempted"] == 3 and o["issued_expected"] == 2 and o["stale"] == 1
    assert o["missed"] == o["expected"] - 3                               # the silent session and b's morning count
    assert o["since_first_attempt"]["missed"] < o["missed"]               # the old measure would hide them
    classes = {(r["horizon"], r["class"]) for r in g["horizons"]}
    assert classes == {(5, "live"), (60, "live"), (5, "expired"), (60, "delayed_origin")}
    assert legacy["version"] is None and {r["class"] for r in legacy["horizons"]} == {"legacy"}


# ---------------------------------------------------------------------------
# The mark trigger and the timing (pure)
# ---------------------------------------------------------------------------

def test_the_mark_trigger_acts_in_a_marks_first_minute_only():
    d = date(2026, 10, 7)
    m = fwd.mark_due(_et(d, 10, 15, 0))
    assert m.at == _mark(d, 10, 15) and not m.cutoff                      # at the mark itself
    assert fwd.mark_due(_et(d, 10, 16, 0)) == m                           # exactly MARK_WINDOW later
    assert fwd.mark_due(_et(d, 10, 16, 1)) is None                        # past it: the catch-up's
    cutoff = fwd.mark_due(_et(d, 9, 29, 20))
    assert cutoff.cutoff and cutoff.at == _mark(d, 9, 29)                 # 09:29 ET: minute 29 of the cron line
    assert fwd.mark_due(_et(d, 10, 29, 5)) is None                        # any other :29 does nothing
    assert fwd.mark_due(_et(d, 9, 30, 10)).at == _mark(d, 9, 30)
    assert fwd.mark_due(_et(d, 17, 0, 10)) is None and fwd.mark_due(_et(date(2026, 10, 10), 12, 0, 5)) is None


class _Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def test_the_mark_trigger_collects_until_the_bar_is_stored_or_the_deadline_nears():
    t0 = datetime(2026, 10, 7, 14, 15, 1, tzinfo=timezone.utc)
    clock = _Clock(t0)

    def collect():
        clock.t += timedelta(seconds=6)                                   # a collection takes 6 s
        return 0

    pause = lambda: setattr(clock, "t", clock.t + timedelta(seconds=2))
    stored = iter([False, False, True])
    got = fwd.collect_until(collect, lambda: next(stored), t0 + timedelta(seconds=40), clock, pause)
    assert [g["stored"] for g in got] == [False, False, True]             # stops once the origin bar is in
    assert got[-1]["end"] == (t0 + timedelta(seconds=22)).isoformat()
    clock.t = t0
    never = fwd.collect_until(collect, lambda: False, t0 + timedelta(seconds=40), clock, pause)
    assert len(never) == 5 and not never[-1]["stored"]                    # ending 6, 14, 22, 30, 38 s; the pause hits 40
    assert all(datetime.fromisoformat(g["start"]) < t0 + timedelta(seconds=40) for g in never)   # none starts after
    clock.t = t0 + timedelta(seconds=50)
    assert len(fwd.collect_until(collect, lambda: False, t0, clock, pause)) == 1   # past the deadline: once


def test_timing_measures_each_trigger_against_its_rules_deadline():
    d = date(2026, 10, 7)
    m1, m2, m3 = _mark(d, 14, 0), _mark(d, 14, 15), _mark(d, 14, 30)
    s = lambda t, sec: (t + timedelta(seconds=sec)).isoformat()
    mark_trig = lambda t: {"trigger": {"kind": "mark", "triggered_at": s(t, 1), "collected_at": s(t, 9)},
                           "started_at": s(t, 10)}
    runs = [{"mark_at": m1, "status": "issued", "issue_id": "a", "detail": mark_trig(m1)},       # out at +25 s
            {"mark_at": m2, "status": "stale", "issue_id": None, "detail": mark_trig(m2)},       # bar missing at +40 s
            {"mark_at": m2, "status": "issued", "issue_id": "b", "detail": {
                "trigger": {"kind": "catchup", "triggered_at": s(m2, 660), "collected_at": s(m2, 680)},
                "started_at": s(m2, 681)}},                                                     # out at +11.5 min
            {"mark_at": m3, "status": "issued", "issue_id": "c", "detail": {}}]                 # before triggers
    issues = [{"issue_id": "a", "mark_at": m1, "issued_at": m1 + timedelta(seconds=25),
               "arrival": m1 + timedelta(seconds=4)},
              {"issue_id": "b", "mark_at": m2, "issued_at": m2 + timedelta(seconds=690),
               "arrival": m2 + timedelta(seconds=650)},
              {"issue_id": "c", "mark_at": m3, "issued_at": m3 + timedelta(seconds=700), "arrival": None}]
    out = {t["trigger"]: t for t in fwd.timing(fwd.rules_params(V2), runs, issues)}
    assert list(out) == ["mark", "catchup", "unrecorded"]                 # TRIGGERS order
    mk, cu, un = out["mark"], out["catchup"], out["unrecorded"]
    assert (mk["attempted"], mk["issued"], mk["stale"], mk["within_deadline"]) == (2, 1, 1, 1)
    assert mk["latency_s"]["median"] == 25 and mk["schedule_s"]["median"] == 1 and mk["collect_s"]["median"] == 8
    assert mk["compute_s"]["median"] == 15 and mk["arrival_s"]["median"] == 4
    assert (cu["issued"], cu["within_deadline"], cu["latency_s"]["max"], cu["arrival_s"]["median"]) == (1, 0, 690, 650)
    assert un["issued"] == 1 and un["schedule_s"] is None and un["arrival_s"] is None
    v1 = {t["trigger"]: t for t in fwd.timing(fwd.rules_params(V1), runs, issues)}
    assert v1["mark"]["within_deadline"] is None and v1["mark"]["deadline_s"] is None   # v1 had no live deadline
    g = fwd.summarise([("rules_v2", V2, _mark(d, 13, 0))], [(r["mark_at"], r["status"]) for r in runs],
                      [dict(i, record="rules_v2", horizons=["5"]) for i in issues], [], _mark(d, 17, 30), runs)[0]
    assert [t["trigger"] for t in g["timing"]] == ["mark", "catchup", "unrecorded"]
    assert g["timing"][0]["within_deadline"] == 1
