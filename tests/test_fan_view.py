# tests/test_fan_view.py
"""
The fan as the Session Explorer draws it (dashboard/components/fan.py): which session
has one, the columns per candle at a timeframe, the two fades, v2 drawn as issued (no
display adjustment), the learned fan's brackets - recorded or computed, and what the
caption says of each, in playback too - v2's accuracy measured walk-forward with the
shape each session was issued with, the blank candles that extend the time axis - and
auto mode's schedule (dashboard/jobs.py).
"""

from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pytest

from contracts import fan as F
from dashboard.components import fan
from features import calendar as cal
from forecaster import fan_v2
from tests.test_fan import market

UTC = timezone.utc


def _ny(d: date, hh: int, mm: int = 0) -> datetime:
    return cal.ny_instant(d, time(hh, mm))


def test_the_current_session_runs_from_its_globex_open_to_the_day_end():
    tue = date(2026, 10, 6)
    assert fan.current_session(_ny(tue, 4, 17)) == "2026-10-06"
    assert fan.current_session(_ny(tue, 16, 59)) == "2026-10-06"
    assert fan.current_session(_ny(tue, 17, 30)) is None                     # the daily halt
    assert fan.current_session(_ny(tue, 18, 1)) == "2026-10-07"              # Wednesday's overnight
    assert fan.current_session(_ny(date(2026, 10, 10), 12)) is None          # Saturday
    assert fan.current_session(_ny(date(2026, 10, 11), 18, 30)) == "2026-10-12"   # Sunday evening opens Monday


@pytest.fixture(scope="module")
def fitted():
    days = market(45, seed=3, release_every=4)
    target = days[-1]
    shape = fan_v2.shape_from([fan_v2.errors(fan_v2.fit(d, days[:i]), d) for i, d in enumerate(days[:-1]) if i >= 10])
    ctx = fan.FanContext("NQ", target.session_date, fan_v2.fit(target, days[:-1]),
                         {"sessions": 10, "last": "x", "horizons": [
                             {"horizon": 1, "skill": 0.08, "cover50": 0.55, "cover90": 0.91, "z_rms": 1.0},
                             {"horizon": 60, "skill": 0.04, "cover50": 0.52, "cover90": 0.85, "z_rms": 1.1},
                             {"horizon": 240, "skill": -0.01, "cover50": 0.50, "cover90": 0.95, "z_rms": 0.9}]},
                         shape=shape)
    return ctx, target


def test_confidence_is_skill_relative_to_one_minute(fitted):
    ctx, _ = fitted
    assert ctx.confidence.tolist() == [1.0, 0.5, 0.0]                       # no skill at 240 min: nothing left
    m = np.array([1, 8, 60, 500])
    assert ctx.at(m, ctx.confidence, 0)[0] == 1.0 and ctx.at(m, ctx.confidence, 0)[-1] == 0.0   # held beyond
    assert 0.5 < ctx.at(m, ctx.confidence, 0)[1] < 1.0                       # log-minute interpolation
    unmeasured = fan.FanContext("NQ", date(2026, 1, 2))
    assert unmeasured.at(m, unmeasured.confidence, 0.0).tolist() == [0.0] * 4


def test_columns_follow_the_candles_after_the_origins(fitted):
    ctx, day = fitted
    origin = 600 + 7                                                          # the 04:07 bar
    one = fan.fan_payload(ctx, day, origin, "1m")
    assert [c["minutes"] for c in one["columns"][:3]] == [1, 2, 3] and len(one["columns"]) == fan.FAN_CANDLES
    five = fan.fan_payload(ctx, day, origin, "5m")
    # the origin's candle (04:05) is the last drawn; the first column is 04:10's, its close 04:14 - 7 min ahead
    assert five["columns"][0]["minutes"] == 7 and five["origin"]["time"] == int(fan.chart_times(
        day.session_date, [605])[0])
    assert five["columns"][1]["time"] - five["columns"][0]["time"] == 300
    late = fan.fan_payload(ctx, day, day.end - 3, "1m")                       # the day's end bounds the fan
    assert [c["minutes"] for c in late["columns"]] == [1, 2]
    assert fan.fan_payload(ctx, day, day.end - 1, "1m") is None


def test_a_column_is_the_distribution_of_its_close_with_both_fades(fitted):
    ctx, day = fitted
    p = fan.fan_payload(ctx, day, 950, "1m")
    price = float(day.last_price[950])
    assert p["origin"]["price"] == price and p["z"][p["median"]] == 0
    for c in p["columns"]:
        assert c["q"] == sorted(c["q"]) and c["q"][p["median"]] == round(price, 2)   # zero drift
        assert fan.CONFIDENCE_FLOOR <= c["conf"] <= 1
    width = [c["q"][-1] - c["q"][0] for c in p["columns"]]
    assert width[59] > width[4] > width[0]                                    # it widens ahead
    assert p["columns"][0]["conf"] == 1.0 and p["columns"][-1]["conf"] < 1.0   # and fades


def test_the_fog_is_v2_as_issued_even_where_its_band_held_less(fitted):
    """No display adjustment: the 60-minute band held 85 % over the measured sessions, and the column is still v2's
    issued quantiles - so the learned fan's brackets compare with it directly."""
    ctx, day = fitted
    p = fan.fan_payload(ctx, day, 950, "1m")
    issued = fan_v2.fan_from(ctx.model, ctx.shape, day.returns, 950, float(day.last_price[950]))
    for c in p["columns"]:
        assert c["q"] == [round(float(x), 2) for x in issued.prices[c["minutes"] - 1]]


def test_releases_ahead_are_marked(fitted):
    ctx, day = fitted
    assert day.releases                                                       # the market's 08:30 release
    p = fan.fan_payload(ctx, day, 860, "5m")                                  # 08:20, the release 10 minutes on
    assert [r["label"] for r in p["releases"]] == ["08:30 Consumer Price Index"]
    assert fan.fan_payload(ctx, day, 900, "5m")["releases"] == []              # after it


def test_attach_extends_the_time_axis_after_the_last_candle_only(fitted):
    ctx, day = fitted
    p = fan.fan_payload(ctx, day, 950, "1m")
    first = p["columns"][0]["time"]
    spec = {"candles": [{"time": first - 60, "open": 1}, {"time": first, "open": 1}]}   # a later candle is shown
    out = fan.attach(spec, p)
    assert out["fan"] is p and [c["time"] for c in out["candles"][2:4]] == [first + 60, first + 120]
    assert all(set(c) == {"time"} for c in out["candles"][2:])
    assert fan.attach({"candles": []}, p)["candles"] == []
    assert fan.attach({"candles": [{"time": 1}]}, None)["fan"] is None


def test_describe_says_where_the_fan_starts_and_how_its_fades_were_measured(fitted):
    ctx, day = fitted
    text = fan.describe(ctx, fan.fan_payload(ctx, day, 950, "1m"))
    assert text.startswith(f"Fan {F.FAN_V2_VERSION} from the ") and "90 %: 15 min" in text
    assert "against a flat random walk" in text
    assert fan.describe(None, None) == "Loading the fan…"
    assert fan.describe(fan.FanContext("NQ", day.session_date, error="no NQ bars"), None) == "No fan: no NQ bars."


def _learned(ctx):
    from dataclasses import replace as dc_replace
    learned = type("Learned", (), {"version": "frozen_v", "definition": {"candidate": "lin_pois_ivx"}})()
    return dc_replace(ctx, learned=learned)


_MARKS = [{"minutes": 5, "multiplier": 1.2, "sigma": 0.001, "q": list(range(13)), "base": list(range(1, 14))},
          {"minutes": 15, "multiplier": 0.9, "sigma": 0.002, "q": list(range(13)), "base": list(range(1, 14))}]


def test_the_learned_fan_is_drawn_on_the_candles_of_its_horizons(fitted):
    ctx, day = fitted
    c = _learned(ctx)
    p = fan.fan_payload(c, day, 952, "5m", {"source": "computed", "marks": _MARKS})   # the 03:52 ET bar
    times = [int(fan.chart_times(day.session_date, [s])[0]) for s in ((952 + 5) // 5 * 5, (952 + 15) // 5 * 5)]
    assert [m["time"] for m in p["model"]["marks"]] == times and p["model"]["name"] == "lin_pois_ivx"
    assert p["model"]["source"] == "computed" and p["model"]["marks"][0]["base"] == list(range(1, 14))
    assert p["quartiles"] == [F.QUANTILES.index(0.25), F.QUANTILES.index(0.75)] and p["model_rgb"]
    text = fan.describe(c, p)
    assert ("learned fan (lin_pois_ivx, frozen; the horizons it passed on the holdout; computed now, not recorded)"
            in text and "(x1.20)" in text)
    assert "learned fan: computing" in fan.describe(c, fan.fan_payload(c, day, 952, "5m"))   # brackets on their way
    assert fan.fan_payload(ctx, day, 952, "5m", None)["model"] is None
    failed = fan.fan_payload(c, day, 952, "5m", {"source": "error", "error": "OperationalError: gone", "marks": []})
    assert "learned fan not drawn: OperationalError: gone" in fan.describe(c, failed)
    none_left = fan.fan_payload(c, day, 952, "5m", {"source": "computed", "marks": []})
    assert "no learned fan from here" in fan.describe(c, none_left)


def test_a_recorded_forecast_is_drawn_and_named_as_recorded(fitted):
    """Where the forward record issued the model from the origin shown, the brackets are that issue - and the
    caption says when it was recorded, how long after its mark, each horizon's class and v2's ranges as issued."""
    ctx, day = fitted
    c = _learned(ctx)
    mark = fan.slot_instant(day.session_date, 953)
    rec = {"source": "recorded", "issue_id": "i", "mark_at": mark, "recorded_at": mark + timedelta(minutes=11, seconds=3),
           "origin_price": 100.0, "record": "frozen_v_forward_v2",
           "marks": [{**m, "class": k} for m, k in zip(_MARKS, ("expired", "late"))]}
    p = fan.fan_payload(c, day, 952, "5m", rec)
    assert p["model"]["source"] == "recorded" and p["model"]["after_mark_minutes"] == 11.1
    assert p["model"]["recorded_at_et"] == f"{(mark + timedelta(minutes=11, seconds=3)).astimezone(cal.NY_TZ):%H:%M:%S}"
    text = fan.describe(c, p)
    assert (f"recorded forecast - the forward record's issue from this origin, recorded {p['model']['recorded_at_et']} "
            "ET, 11.1 min after its mark (5 min expired, 15 min late)") in text
    assert "v2 as issued 90 %: 5 min 3.00–11.00" in text and "computed" not in text


def test_playback_is_labelled_a_recomputed_historical_preview(fitted):
    ctx, day = fitted
    c = _learned(ctx)
    p = fan.fan_payload(c, day, 952, "1m", {"source": "computed", "marks": _MARKS})
    text = fan.describe(c, p, playback=True)
    assert text.startswith(f"Recomputed historical preview: fan {F.FAN_V2_VERSION} from the ")
    assert "from the bars stored now - not a record of what was shown then" in text
    assert "the horizons it passed on the holdout; recomputed)" in text and "the feed is delayed" not in text
    assert not fan.describe(c, p).startswith("Recomputed")                     # the newest candle is not playback


def test_v2_accuracy_is_walk_forward_and_scores_the_distribution_drawn(tmp_path):
    """Each measured session is drawn with the shape it was issued with - fan_v2.walk_forward's, from the sessions
    before it only - and scored as drawn: its CRPS is the harness's (the manifest's quantile form) on that shape."""
    from forecaster import fan_harness as fh
    from forecaster import fan_live
    days = market(40, seed=7, release_every=4)
    before = days[-1].session_date
    walked = list(fan_live._walk(days, before, 4, "NQ", str(tmp_path)))
    ref = list(fan_v2.walk_forward(days, walked[0][0].session_date, walked[-1][0].session_date))
    assert [d.session_date for d, _, _ in walked] == [d.session_date for d, _, _ in ref]
    for (_, _, s), (_, _, r) in zip(walked, ref):
        assert s.sessions == r.sessions >= F.SHAPE_MIN_SESSIONS and np.array_equal(s.q, r.q)
    assert [s.q.tolist() for _, _, s in fan_live._walk(days, before, 4, "NQ", str(tmp_path))] == \
        [s.q.tolist() for _, _, s in walked]                                 # from the cached errors: the same
    acc = fan_live.v2_accuracy(days, before, "NQ", 4, str(tmp_path))
    assert acc["sessions"] == 4 and acc["format"] == fan_live.ACCURACY_FORMAT and "walk-forward" in acc["method"]
    by_h = {r["horizon"]: r for r in acc["horizons"]}
    for h in (5, 60):
        rows = [fh.compare_session(d, (m, s.at), (m, s.at), (h,))[f"h{h}"] for d, m, s in walked]
        n = sum(r[2] for r in rows)
        assert by_h[h]["origins"] == n
        assert by_h[h]["crps_bps"] == pytest.approx(sum(r[0] * r[2] for r in rows) / n, rel=1e-9)
        assert 0 < by_h[h]["cover50"] < by_h[h]["cover90"] < 1 and by_h[h]["skill"] is not None


def test_the_caption_says_when_the_fan_starts_in_the_past(fitted):
    ctx, day = fitted
    p = fan.fan_payload(ctx, day, 950, "1m")
    closed = fan.slot_instant(day.session_date, 951)
    assert "the feed is delayed" not in fan.describe(ctx, p, closed + timedelta(seconds=40))
    late = fan.describe(ctx, p, closed + timedelta(minutes=11))
    assert "- 11 min ago: the feed is delayed, so the fan starts in the past" in late


class _Runner:
    busy = False


def test_auto_mode_runs_once_a_minute_while_a_session_is_in_progress():
    from dashboard.jobs import AUTO_SECOND, AutoMode, auto_steps
    auto = AutoMode(_Runner())
    tue = date(2026, 10, 6)
    at = _ny(tue, 10, 3).replace(second=AUTO_SECOND)
    assert auto.due(at) == "2026-10-06"
    assert auto.due(at.replace(second=AUTO_SECOND - 1)) is None              # the minute's bar may not be closed
    auto._last_minute = at.replace(second=0)
    assert auto.due(at + timedelta(seconds=20)) is None                       # this minute had its run
    assert auto.due(at + timedelta(minutes=1)) == "2026-10-06"
    auto.runner.busy = True
    assert auto.due(at + timedelta(minutes=2)) is None                        # another job is running
    auto.runner.busy = False
    assert auto.due(_ny(tue, 17, 30).replace(second=10)) is None              # no session in progress
    steps = dict(auto_steps(_ny(tue, 8, 0)))
    cmd = steps["collector"]
    assert cmd[cmd.index("--days") + 1] == "0" and "preview" in steps           # today only, then the preview
    assert "--no-trailing-refresh" in cmd                                         # no refetch of complete sessions
    assert cmd[cmd.index("--workers") + 1] == "4"                                 # four symbols at a time
    assert list(dict(auto_steps(_ny(tue, 11, 3)))) == ["collector", "rth", "forward"]   # RTH sets all session (v3),
    assert list(dict(auto_steps(_ny(tue, 16, 30)))) == ["collector", "rth", "forward"]  # to 30 min after the close,
    assert list(dict(auto_steps(_ny(tue, 16, 31)))) == ["collector", "forward"]  # then none; nothing to preview
                                                                                  # after 09:31;
    forward = dict(auto_steps(_ny(tue, 11, 0)))["forward"]                       # the pending marks, by Auto mode
    assert forward[-6:-2] == ["forward", "issue", "--trigger", "auto"] and forward[-2] == "--triggered-at"
    assert datetime.fromisoformat(forward[-1]) == _ny(tue, 11, 0)                    # timed from the run's start
    assert list(dict(auto_steps(_ny(tue, 17, 30)))) == ["collector"]                 # no session: nothing pending


class _LogRunner:
    """A runner that never runs anything, with a log file."""

    def __init__(self, log_file):
        self.busy, self.log_file, self.started = False, str(log_file), []

    def start(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("not expected to start here")


def test_only_one_dashboard_process_runs_auto_mode(tmp_path):
    import asyncio
    from dashboard.jobs import AutoMode
    lock = str(tmp_path / "auto.lock")

    async def go():
        first, second = AutoMode(_LogRunner(tmp_path / "a.log"), lock), AutoMode(_LogRunner(tmp_path / "b.log"), lock)
        first.switch(True)
        with pytest.raises(RuntimeError, match="already on in another dashboard"):
            second.switch(True)
        assert not second.on
        first.switch(False)
        second.switch(True)                                  # free once the first is off
        assert second.on
        second.switch(False)

    asyncio.run(go())
    text = (tmp_path / "a.log").read_text()
    assert "Auto mode switched on" in text and "Auto mode switched off" in text


def test_an_error_in_the_auto_loop_is_logged_and_the_loop_goes_on(tmp_path, monkeypatch):
    import asyncio
    from dashboard import jobs
    monkeypatch.setattr(jobs, "AUTO_ERROR_PAUSE_S", 0.05)
    auto = jobs.AutoMode(_LogRunner(tmp_path / "a.log"), str(tmp_path / "auto.lock"))
    calls = []

    def tick(now):
        calls.append(now)
        if len(calls) == 1:
            raise ValueError("calendar not covered")

    auto.tick = tick

    async def go():
        auto.switch(True)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if len(calls) >= 2:
                break
        assert auto.on and len(calls) >= 2                   # still on, and ticking after the error
        assert auto.error == "ValueError: calendar not covered"
        assert "last attempt failed: ValueError" in auto.status(datetime.now(timezone.utc))
        auto.switch(False)

    asyncio.run(go())
    assert "Auto mode error, trying again" in (tmp_path / "a.log").read_text()


def test_a_stopped_auto_run_switches_auto_mode_off_and_frees_the_lock(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from dashboard.jobs import AutoMode
    lock = str(tmp_path / "auto.lock")

    async def go():
        auto = AutoMode(_LogRunner(tmp_path / "a.log"), lock)
        auto.switch(True)
        auto._started = SimpleNamespace(stopped=True)
        auto.tick(datetime.now(timezone.utc))
        assert not auto.on
        other = AutoMode(_LogRunner(tmp_path / "b.log"), lock)
        other.switch(True)                                   # the lock was released
        other.switch(False)

    asyncio.run(go())
    assert "switched off: its run was stopped" in (tmp_path / "a.log").read_text()
