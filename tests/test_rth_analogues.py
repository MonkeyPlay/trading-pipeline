# tests/test_rth_analogues.py
"""
The RTH analogue matcher (contracts/nq_rth.py, matching/rth.py), its sets in the
journal (migration 0024, database/journal_store.py, forecaster/rth_analogues.py),
the CLI and the Auto step.

The pure tests need nothing; the rest need a disposable PostgreSQL + TimescaleDB
database whose name contains "test", which they RESET (TEST_DATABASE_URL, see
tests/test_dashboard_data.py).
"""

import asyncio
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction

import psycopg
import pytest

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth
from features import calendar as cal
from matching import rth as mr

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)
UTC = timezone.utc
MIN = timedelta(minutes=1)
DAY = "2026-06-12"


def _ny(day: str, hh: int, mm: int, ss: int = 0) -> datetime:
    return cal.ny_instant(date.fromisoformat(day), time(hh, mm, ss))


# --------------------------------------------------------------------------
# Pure: synthetic openings
# --------------------------------------------------------------------------

def _bars(open_at: datetime, closes, start=20000.0, volume=100.0):
    """1-minute bars from ``open_at`` whose closes are ``start`` + the given moves; highs/lows one point outside."""
    out, prev = [], start
    for i, c in enumerate(closes):
        c = start + c
        out.append((open_at + i * MIN, prev, max(prev, c) + 1, min(prev, c) - 1, c, volume))
        prev = c
    return out


def _opening(day: str, closes, atr=400.0, extra=None, context=True) -> mr.Opening:
    open_at = cal.session(day).rth_open_at
    bars = _bars(open_at, list(closes) + list(extra or []))
    ctx = mr.Context(f"snap-{day}", atr, prev_rth_close=19950.0, on_high=20100.0, on_low=19900.0,
                     overnight_pv=20000.0 * 1000, overnight_volume=1000.0) if context else None
    window, _ = mr.completed_window(bars, open_at)
    return mr.Opening(day, "NQ", 1, open_at, ctx, tuple(window), volume_baseline={n: 100.0 * n for n in
                                                                                   range(1, 61)})


def _line(slope, n=61):
    return [slope * (i + 1) for i in range(n)]


def test_completed_window_confirms_a_bar_by_a_later_one_and_says_why_it_stops():
    """A bar is complete once any later bar of the session is stored. Awaiting confirmation (the newest bar may
    still be forming), not stored yet, and a confirmed gap inside the observed session are kept apart."""
    open_at = cal.session(DAY).rth_open_at
    bars = _bars(open_at, _line(1, 10))                       # 09:30 .. 09:39 stored
    window, stop = mr.completed_window(bars, open_at)
    assert len(window) == 9 and window[-1][0] == _ny(DAY, 9, 38)
    assert stop == {"state": "awaiting_confirmation", "minute": _ny(DAY, 9, 39)}   # may still be forming
    holed = bars[:5] + bars[6:]                               # 09:35 missing, later bars stored
    window, stop = mr.completed_window(holed, open_at)
    assert len(window) == 5 and window[-1][0] == _ny(DAY, 9, 34)                 # 09:34 confirmed by 09:36
    assert stop == {"state": "gap", "minute": _ny(DAY, 9, 35)}                     # a confirmed hole, not filled
    assert mr.completed_window([], open_at)[1] == {"state": "not_stored", "minute": open_at}
    # the last candle of the hour, 10:29: confirmed by the 10:30 bar ...
    hour = _bars(open_at, _line(1, 61))                                          # 09:30 .. 10:30
    window, stop = mr.completed_window(hour, open_at)
    assert len(window) == 60 and window[-1][0] == _ny(DAY, 10, 29) and stop["state"] == "complete"
    # ... or, with the 10:30 bar missing, by any later one (here 10:44, beyond the bars loaded)
    window, stop = mr.completed_window(hour[:60], open_at, newest_start=_ny(DAY, 10, 44))
    assert len(window) == 60 and stop["state"] == "complete"
    # without one, the 10:29 bar awaits confirmation: 59 minutes
    window, stop = mr.completed_window(hour[:60], open_at)
    assert len(window) == 59 and stop == {"state": "awaiting_confirmation", "minute": _ny(DAY, 10, 29)}
    assert mr.completed_window(_bars(open_at, _line(1, 90)), open_at)[0][-1][0] == _ny(DAY, 10, 29)   # 60 at most
    assert mr.completed_window(_bars(open_at, [0]), open_at) == ([], {"state": "awaiting_confirmation",
                                                                     "minute": open_at})


def test_first_bar_checkpoints_and_daylight_saving():
    """The open is the calendar's 09:30 ET instant - 13:30 UTC in summer, 14:30 UTC in winter, the Monday after the
    March switch included - and the checkpoints end at 09:45, 10:00 and 10:30 ET."""
    for day, utc_hour in (("2026-06-12", 13), ("2026-01-15", 14), ("2026-03-09", 13), ("2026-03-06", 14),
                          ("2026-11-02", 14), ("2026-10-30", 13)):
        s = cal.session(day)
        assert s.rth_open_at == datetime.fromisoformat(f"{day}T{utc_hour}:30:00+00:00")
        ends = [f"{(s.rth_open_at + m * MIN).astimezone(cal.NY_TZ):%H:%M}" for m in rth.CHECKPOINTS]
        assert ends == ["09:45", "10:00", "10:30"]
    from forecaster.rth_analogues import due_window
    assert due_window(_ny(DAY, 9, 30, 59)) is None                       # the 09:30 bar is not complete yet
    assert due_window(_ny(DAY, 9, 31)) == DAY
    assert due_window(_ny(DAY, 11, 0)) == DAY                            # 10:30 + 30 min for a delayed feed
    assert due_window(_ny(DAY, 11, 0, 1)) is None                        # automatic matching stops
    assert due_window(_ny("2026-06-13", 10, 0)) is None                  # a Saturday


def test_the_definition_adds_up_and_the_database_agrees_on_live():
    assert sum(rth.WEIGHTS.values()) == 100
    assert set(rth.WEIGHTS) == set(rth.TOLERANCES) == set(rth.LABELS) == {f for g in rth.GROUPS.values() for f in g}
    assert sum(rth.WEIGHTS[f] for f in rth.GROUPS["Opening path"]) == 65      # the developing path leads
    sql = open(os.path.join(os.path.dirname(__file__), "..", "database", "migrations",
                            "0025_rth_issue_provenance.sql")).read()
    minutes = int(re.search(r"cutoff_at \+ interval '(\d+) minutes'", sql).group(1))
    assert timedelta(minutes=minutes) == rth.LIVE_MAX_LAG
    assert "IN ('auto', 'manual')" in sql and set(rth.LIVE_ISSUERS) == {"auto", "manual"}
    assert rth.matcher_record()["definition"]["calibration"]["sessions"] == 258
    assert set(rth.CALIBRATION["medians"]) == set(rth.WEIGHTS)
    for f, med in rth.CALIBRATION["medians"].items():            # the tolerances are what the calibration says
        assert rth.TOLERANCES[f] == Fraction(f"{2 * float(med):.2g}")
    assert rth.matcher_record()["kind"] == "rth_matcher" and rth.RTH_MATCHER_VERSION != pre.MATCHER_VERSION
    assert mr.tolerance("path", 30) == float(rth.TOLERANCES["path"])
    assert mr.tolerance("path", 120) == pytest.approx(2 * float(rth.TOLERANCES["path"]))
    assert mr.tolerance("gap", 5) == float(rth.TOLERANCES["gap"])           # not scaled


def test_the_evaluation_is_fixed_before_any_result():
    """The predefined usefulness evaluation: its docs copy is the code's definition, hash included - any change is
    a new version, visible in review."""
    import json
    from contracts import rth_eval
    doc = json.load(open(os.path.join(os.path.dirname(__file__), "..", "docs", "rth_continuation_v1_definition.json")))
    assert doc == json.loads(json.dumps(rth_eval.document()))
    assert doc["definition_hash"] == "977d4c124a257b14"
    assert doc["cutoffs"] == {"09:45": 15, "10:00": 30, "10:15": 45}
    assert set(doc["cutoffs"].values()) <= set(rth.ALWAYS_ISSUED)          # every cutoff is always issued


def test_features_read_only_the_window_and_the_frozen_context():
    """Bars after the cutoff - the session's eventual range, high, low and close - change no feature."""
    calm = _opening("2026-06-10", _line(0.5), extra=[0] * 5)
    wild = _opening("2026-06-10", _line(0.5)[:20] + [5000, -5000] * 25, extra=[0] * 5)
    assert mr.features(calm, 20) == mr.features(wild, 20)
    f = mr.features(calm, 20)
    assert f["net_move"] == pytest.approx(10 / 400) and f["gap"] == pytest.approx((20000 - 19950) / 400)
    assert f["in_overnight_range"] == pytest.approx((20010 - 19900) / 200)
    assert f["relative_volume"] == pytest.approx(0.0)                      # 100 a minute against 100 a minute
    assert f["overnight_range"] == pytest.approx(200 / 400)
    no_ctx = _opening("2026-06-10", _line(0.5), context=False)
    assert all(v is None for v in mr.features(no_ctx, 20).values())
    # in its own ATR: the same path in points is a smaller move for a session of a larger ATR
    assert mr.features(_opening("2026-06-10", _line(0.5), atr=800.0), 20)["net_move"] == pytest.approx(f["net_move"] / 2)


def test_rank_scores_the_whole_earlier_pool_afresh_at_each_cutoff():
    """Later bars of the target or of a candidate cannot change an earlier ranking, and a session unlike the
    target early on can enter the top five later."""
    target = _opening(DAY, _line(1.0)[:10] + [10 - i for i in range(1, 52)])        # up 10 minutes, then down
    pool = [_opening(d, _line(1.0)[:10] + [10 + 0.2 * i for i in range(1, 52)])    # up, then on up
            for d in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05")]
    late = _opening("2026-06-08", _line(-0.3)[:10] + [-3 - i for i in range(1, 52)])  # down early, down later
    later = _opening("2026-06-15", _line(1.0))                                      # after the target: never
    nocontext = _opening("2026-06-09", _line(1.0), context=False)
    short = _opening("2026-06-10", _line(1.0)[:12])
    candidates = pool + [late, later, nocontext, short]
    at10 = mr.rank(target, candidates, 10)
    assert at10["excluded"] == {"not_earlier": 1, "no_preopen_context": 1}
    assert at10["pool_size"] == 7
    assert "2026-06-08" not in [m["opening"].session_date for m in at10["selected"]]
    at60 = mr.rank(target, candidates, 60)
    assert at60["excluded"] == {"incomplete_window": 1, "no_preopen_context": 1, "not_earlier": 1}
    assert at60["selected"][0]["opening"].session_date == "2026-06-08"            # entered the top five
    for m in at60["selected"]:
        assert 0 <= m["similarity"] <= 100
    # changing what happened after the 10th minute - the target's and a candidate's - leaves minute 10 as it was
    target2 = _opening(DAY, _line(1.0)[:10] + [999] * 51)
    pool2 = [_opening(o.session_date, _line(1.0)[:10] + [-999] * 51) for o in pool]
    again = mr.rank(target2, pool2 + [late, later, nocontext, short], 10)
    key = lambda r: [(m["opening"].session_date, round(m["similarity"], 9)) for m in r["selected"]]
    assert key(again) == key(at10)
    assert mr.input_digest(target2, 10, again) == mr.input_digest(target, 10, at10)
    assert mr.input_digest(target, 11, mr.rank(target, candidates, 11)) != mr.input_digest(target, 10, at10)


def test_windows_over_mark_observed_targets():
    from forecaster.rth_analogues import windows_over
    assert windows_over(DAY, _ny(DAY, 9, 34), pre.OUTCOME_TARGETS) == {}
    assert windows_over(DAY, _ny(DAY, 9, 45), pre.OUTCOME_TARGETS) == {
        "first_move_5m": "09:35", "opening_type_15m": "09:45", "direction_15m": "09:45", "first_level_tested": "09:45"}
    assert "opening_bias_30m" in windows_over(DAY, _ny(DAY, 10, 0), pre.OUTCOME_TARGETS)
    assert "close_direction_rth" not in windows_over(DAY, _ny(DAY, 15, 59), pre.OUTCOME_TARGETS)
    assert windows_over("2026-11-27", _ny("2026-11-27", 13, 0), ["close_direction_rth"]) == {
        "close_direction_rth": "13:00"}                                           # an early close
    assert windows_over(DAY, None, pre.OUTCOME_TARGETS) == {}


def test_describe_says_how_far_behind_a_live_set_was_issued():
    from forecaster.rth_analogues import describe
    cutoff = _ny(DAY, 9, 53)
    live = {"elapsed_minutes": 23, "cutoff_at": cutoff, "created_at": cutoff + timedelta(minutes=11, seconds=5),
            "checkpoint": None, "quality": {"provisional": False}, "data_mode": "live", "issued_by": "auto",
            "inputs_received_at": cutoff + timedelta(minutes=10, seconds=40), "pit_status": "verified"}
    text = describe(live)
    assert text.startswith("RTH analogues — first 23 minutes — data through 09:53 ET")
    assert "issued live by Auto at 10:04 ET" in text and "window's bars in the store 11 min after its cutoff" in text
    assert "inputs verified as of the cutoff" in text
    early = {**live, "elapsed_minutes": 4, "cutoff_at": _ny(DAY, 9, 34), "quality": {"provisional": True}}
    assert "provisional" in describe(early)
    rebuilt = {**live, "elapsed_minutes": 15, "checkpoint": 15, "cutoff_at": _ny(DAY, 9, 45), "issued_by": "backfill",
               "data_mode": "historical_reconstruction", "pit_status": "unverified",
               "quality": {"provisional": False, "in_calibration_sample": True}}
    text = describe(rebuilt)
    assert "15-minute checkpoint" in text and "historical reconstruction by a backfill" in text
    assert "inputs not verifiable as of the cutoff" in text and "not a forward test" in text


# --------------------------------------------------------------------------
# The Auto step
# --------------------------------------------------------------------------

def test_auto_issues_rth_sets_in_the_first_hour_only():
    from dashboard.jobs import auto_steps
    tue = "2026-10-06"
    assert [s for s, _ in auto_steps(_ny(tue, 9, 15))][:2] == ["collector", "preview"]
    assert [s for s, _ in auto_steps(_ny(tue, 10, 3))] == ["collector", "rth", "forward"]
    assert dict(auto_steps(_ny(tue, 10, 3)))["rth"][-3:] == ["rth-issue", "--by", "auto"]
    assert [s for s, _ in auto_steps(_ny(tue, 11, 3))] == ["collector", "forward"]


def test_the_rth_step_is_skipped_after_a_failed_collection(tmp_path):
    from dashboard.jobs import JobRunner

    async def run(code):
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("auto", "Auto", [("collector", [sys.executable, "-c", code]),
                                      ("rth", [sys.executable, "-c", "print('issued')"]),
                                      ("forward", [sys.executable, "-c", "print('forward')"])])
        while runner.busy:
            await asyncio.sleep(0.05)
        return runner.job

    failed = asyncio.run(run("import sys; sys.exit(2)"))
    assert failed.steps[1].skipped and failed.steps[1].returncode is None
    assert "issued" not in failed.lines and "forward" in failed.lines            # the steps after still run
    assert any("rth skipped: the collector failed" in line for line in failed.lines)
    assert failed.outcome == "collector failed (exit 2)"
    ok = asyncio.run(run("pass"))
    assert not ok.steps[1].skipped and "issued" in ok.lines and ok.outcome == "finished"


# --------------------------------------------------------------------------
# The journal (database)
# --------------------------------------------------------------------------

def _store(conn, df, cid):
    from database.queries import save_bars_by_day
    from features.session_windows import enrich_candle_timezones
    df = df.assign(contract_id=cid)
    df = enrich_candle_timezones(df.assign(timestamp_utc=df["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
    save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                               "low", "close", "volume"]].to_dict("records"))


@pytest.fixture(scope="module")
def journal():
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from forecaster.journal import register, take_snapshot
    from tests.synthetic import ES_CID, NQ_CID, make_market
    reset_database(DSN)
    conn = get_db_connection(DSN)
    bars, sessions = make_market(last_day=DAY, n_sessions=100, seed=11)
    upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME", local_symbol="NQU6")
    upsert_contract(conn, ES_CID, "ES", "20260918", "CME", local_symbol="ESU6")
    _store(conn, bars[NQ_CID], NQ_CID)
    _store(conn, bars[ES_CID], ES_CID)
    days = [s.session_date.isoformat() for s in sessions]
    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    set_active_contracts(conn, "ES", {d: ES_CID for d in days}, "test")
    register(conn)
    for d in days[-24:]:                                   # the last 24 have a valid daily ATR (70 true ranges)
        take_snapshot(conn, d, defs.DEFAULT_PROFILE)
    yield conn, days
    conn.close()


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM journal.{table};").fetchone()[0]


@needs_db
def test_reconstructions_are_idempotent_and_read_by_cutoff(journal):
    from database import journal_store as store
    from forecaster import rth_analogues as ra
    conn, days = journal
    ra.register(conn)
    first = ra.reconstruct(conn, [DAY])
    assert first["new"] == 3 and first["skipped"] == {}
    aset = store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 60)
    assert aset["elapsed_minutes"] == 60 and aset["checkpoint"] == 60
    assert aset["data_mode"] == "historical_reconstruction"
    assert aset["pool_size"] == 23 and aset["excluded"] == {"no_preopen_context": 76}   # no snapshot: no context
    assert 1 <= len(aset["members"]) <= 5 and aset["members"][0]["rank"] == 1
    assert ra.utc(aset["cutoff_at"]) == _ny(DAY, 10, 30)
    assert all(m["session_date"] < DAY for m in aset["members"])
    # repeated on unchanged inputs: nothing new
    again = ra.reconstruct(conn, [DAY])
    assert again == {"new": 0, "already": 3, "skipped": {}}
    # a review at 09:45 (or 09:59) never sees the 10:00 or 10:30 sets
    assert store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 15)["elapsed_minutes"] == 15
    assert store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 29)["elapsed_minutes"] == 15
    assert store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 14) is None
    windows = store.rth_windows(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION)
    assert [(w["elapsed_minutes"], w["checkpoint"], w["sets"]) for w in windows] == [(15, 15, 1), (30, 30, 1),
                                                                                       (60, 60, 1)]
    with pytest.raises(Exception, match="append-only"):
        with conn:
            conn.execute("UPDATE journal.rth_analogue_sets SET pool_size = 0;")


@needs_db
def test_a_revised_bar_adds_a_set_beside_the_earlier_one(journal):
    """A vendor revision inside a window makes a new set for that window and keeps the old; a revision after a
    window's cutoff changes nothing about it."""
    from database import journal_store as store
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    ra.register(conn)
    ra.reconstruct(conn, [DAY])
    earlier = store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 15)
    candidate = days[-2]
    # a candidate's bar at 09:50: after its 15-minute window, inside its 30- and 60-minute ones
    conn.execute("UPDATE bars SET close = close + 40, high = high + 40 WHERE contract_id = %s AND timestamp_utc = %s;",
                 (NQ_CID, _ny(candidate, 9, 50)))
    result = ra.reconstruct(conn, [DAY])
    assert result["new"] == 2 and result["already"] == 1                       # 30 and 60 new, 15 unchanged
    assert store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 15)["set_id"] == earlier["set_id"]
    # the target's own bar at 09:40: inside every window
    conn.execute("UPDATE bars SET close = close - 25, low = low - 25 WHERE contract_id = %s AND timestamp_utc = %s;",
                 (NQ_CID, _ny(DAY, 9, 40)))
    assert ra.reconstruct(conn, [DAY])["new"] == 3
    newer = store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 15)
    assert newer["set_id"] != earlier["set_id"] and store.get_rth_set(conn, earlier["set_id"]) is not None
    assert newer["input_digest"] != earlier["input_digest"]
    windows = {w["elapsed_minutes"]: w["sets"] for w in store.rth_windows(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION)}
    assert windows[15] == 2 and windows[60] >= 3
    # what had been issued by a given time: the earlier set
    assert store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 15,
                            stored_by=earlier["created_at"])["set_id"] == earlier["set_id"]
    # a bar after 10:30 changes no set
    conn.execute("UPDATE bars SET close = close + 90 WHERE contract_id = %s AND timestamp_utc = %s;",
                 (NQ_CID, _ny(DAY, 11, 15)))
    assert ra.reconstruct(conn, [DAY])["new"] == 0


@needs_db
def test_live_is_how_a_set_was_issued_not_only_its_age(journal):
    """The database marks a set live only when Auto or a person issued it within 30 minutes of its cutoff: a
    backfill is a reconstruction however soon it runs."""
    from database import journal_store as store
    from forecaster import rth_analogues as ra
    conn, days = journal
    ra.register(conn)
    openings, meta = ra.load_openings(conn, DAY)
    pool = [o for d, o in openings.items() if d < DAY]
    now = datetime.now(UTC)
    modes = {}
    for by, ago in (("auto", 12), ("manual", 12), ("backfill", 12), ("auto", 31)):
        rec, members, _ = ra.build_set(openings[DAY], pool, 20, meta, by)
        set_id, _ = store.save_rth_set(conn, {**rec, "cutoff_at": now - timedelta(minutes=ago),
                                              "input_digest": f"{by}-{ago}"}, members)
        aset = store.get_rth_set(conn, set_id)
        modes[(by, ago)] = aset["data_mode"]
        assert aset["issued_by"] == by
    assert modes == {("auto", 12): "live", ("manual", 12): "live", ("backfill", 12): "historical_reconstruction",
                     ("auto", 31): "historical_reconstruction"}
    with pytest.raises(ValueError):
        ra.build_set(openings[DAY], pool, 20, meta, "cron")
    with pytest.raises(ValueError):
        ra.issue(conn, day=DAY, issued_by="backfill")


@needs_db
def test_as_issued_never_shows_a_later_correction(journal):
    """A correction stored after a live issue changes the rankings at the same earlier cutoff: the reconstructed
    view shows the correction, the as-issued view what was issued - at any replay time, never the later one."""
    from database import journal_store as store
    from database.queries import get_day_bars, save_trading_day
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    ra.register(conn)
    day = days[-9]
    openings, meta = ra.load_openings(conn, day)
    pool = [o for d, o in openings.items() if d < day]
    rec, members, _ = ra.build_set(openings[day], pool, 15, meta, "auto")
    issued_at = datetime.now(UTC)
    live_id, _ = store.save_rth_set(conn, {**rec, "cutoff_at": issued_at - timedelta(minutes=8)}, members)  # live
    live = store.get_rth_set(conn, live_id)
    assert live["data_mode"] == "live"
    # the vendor corrects the target's opening bars: a much stronger rise from 09:35
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]
    for r in rows:
        t = ra.utc(r["timestamp_utc"])
        if _ny(day, 9, 35) <= t < _ny(day, 9, 45):
            lift = 25.0 * ((t - _ny(day, 9, 34)) / MIN)
            r.update(open=r["open"] + lift, high=r["high"] + lift + 25, low=r["low"] + lift, close=r["close"] + lift)
    save_trading_day(conn, NQ_CID, day, rows)
    assert ra.reconstruct(conn, [day], minutes=[15])["new"] == 1
    corrected = store.rth_set_at(conn, "NQ", day, rth.RTH_MATCHER_VERSION, 15)
    assert corrected["set_id"] != live_id and corrected["issued_by"] == "backfill"
    assert [m["session_date"] for m in corrected["members"]] != [m["session_date"] for m in live["members"]]
    for view in (store.rth_set_issued(conn, "NQ", day, rth.RTH_MATCHER_VERSION),
                 store.rth_set_issued(conn, "NQ", day, rth.RTH_MATCHER_VERSION, minutes=15),
                 store.rth_set_issued(conn, "NQ", day, rth.RTH_MATCHER_VERSION, at=datetime.now(UTC))):
        assert view["set_id"] == live_id                                            # as issued: never the correction
    assert store.rth_set_issued(conn, "NQ", day, rth.RTH_MATCHER_VERSION,
                                at=ra.utc(live["created_at"]) - timedelta(seconds=1)) is None   # not issued yet
    assert store.rth_set_issued(conn, "NQ", day, rth.RTH_MATCHER_VERSION, minutes=30) is None


@needs_db
def test_sets_record_when_their_inputs_reached_the_store(journal):
    from database import journal_store as store
    from forecaster import rth_analogues as ra
    conn, days = journal
    ra.register(conn)
    ra.reconstruct(conn, [DAY], minutes=[30])
    aset = store.rth_set_at(conn, "NQ", DAY, rth.RTH_MATCHER_VERSION, 30)
    inputs, pool = ra.utc(aset["inputs_received_at"]), ra.utc(aset["pool_received_at"])
    assert inputs is not None and pool is not None and inputs <= ra.utc(aset["created_at"])
    assert aset["pit_status"] == "unverified"             # earlier sessions' bars stored long after this cutoff
    # verified needs the earlier inputs by the cutoff and the target's within 30 minutes of it
    from contracts import nq_rth as rth_defs
    openings, meta = ra.load_openings(conn, DAY)
    target, cutoff = openings[DAY], openings[DAY].rth_open_at + 30 * MIN
    for d in [*[o for o in openings if o < DAY], DAY]:
        meta[d] = {**meta[d], "inputs_at": cutoff - timedelta(days=1)}
    timely = {t: cutoff + timedelta(minutes=9) for t in meta[DAY]["stored"]}
    meta[DAY] = {**meta[DAY], "stored": timely, "overnight_at": cutoff - timedelta(hours=1),
                 "snapshot_built_at": cutoff - timedelta(minutes=40)}
    earlier = [o for o in openings if o < DAY]
    assert ra.provenance(target, 30, meta, earlier)["pit_status"] == "verified"
    revised = {**meta, DAY: {**meta[DAY], "stored": {**timely, target.bars[3][0]: cutoff + timedelta(hours=5)}}}
    assert ra.provenance(target, 30, revised, earlier)["pit_status"] == "unverified"   # a target bar revised later
    late_pool = {**meta, earlier[-1]: {**meta[earlier[-1]], "inputs_at": cutoff + timedelta(minutes=1)}}
    assert ra.provenance(target, 30, late_pool, earlier)["pit_status"] == "unverified"  # an earlier input revised
    assert rth_defs.LIVE_MAX_LAG == timedelta(minutes=30)
    assert aset["quality"]["in_calibration_sample"] is True     # 2026-06-12 lies in the calibration's date range
    receipts = ra._Receipts(conn)
    applied = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 23;").fetchone()[0]
    assert receipts.floor == ra.utc(applied)
    early = receipts.floor - timedelta(days=3)
    assert receipts.bound([early]) == early
    assert receipts.bound([early, None]) == receipts.floor          # no receipt time: stored before 0023 applied
    assert receipts.bound([]) is None


@needs_db
def test_issue_follows_the_session_as_its_bars_arrive(journal):
    """The session in progress: nothing before the first completed bar, then its newest window and the windows it
    passed; a missing minute stops the window, reported as a confirmed gap; a second issue at once is refused."""
    from database import journal_store as store
    from database.queries import get_day_bars, save_trading_day
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    ra.register(conn)
    day = days[-4]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]

    def keep_until(hh, mm, drop=()):
        cut = _ny(day, hh, mm)
        kept = [r for r in rows if ra.utc(r["timestamp_utc"]) <= cut and ra.utc(r["timestamp_utc"]) not in drop]
        save_trading_day(conn, NQ_CID, day, kept)

    assert ra.issue(conn, now=_ny(day, 9, 30, 30), day=day)["status"] == "waiting"      # before the first bar ends
    keep_until(9, 30)                                                                    # 09:30 stored, may be forming
    result = ra.issue(conn, now=_ny(day, 9, 41), day=day)
    assert result["status"] == "waiting" and "first completed RTH bar" in result["reason"]
    keep_until(9, 52)                                                                    # a feed ~10 minutes late
    result = ra.issue(conn, now=_ny(day, 10, 3), day=day)
    assert result["status"] == "issued" and result["minutes"] == 22
    assert result["stop"] == {"state": "awaiting_confirmation", "minute": _ny(day, 9, 52)}
    assert [m for m, _, new in result["stored"]] == [15, 22]
    assert ra.issue(conn, now=_ny(day, 10, 3, 30), day=day)["stored"] == [(22, result["stored"][1][1], False)]
    keep_until(10, 25, drop={_ny(day, 10, 10)})                                          # the 10:10 bar missing
    result = ra.issue(conn, now=_ny(day, 10, 36), day=day)
    assert result["minutes"] == 40 and [m for m, _, _ in result["stored"]] == [30, 40]  # 10:09 confirmed by 10:11
    assert result["stop"] == {"state": "gap", "minute": _ny(day, 10, 10)}
    aset = store.get_rth_set(conn, result["stored"][-1][1])
    assert aset["quality"]["stopped"] == "gap" and aset["quality"]["stopped_at"] == ra.iso(_ny(day, 10, 10))
    assert aset["quality"]["newest_bar_end"] == ra.iso(_ny(day, 10, 26))
    assert aset["issued_by"] == "manual"
    assert [w["elapsed_minutes"] for w in store.rth_windows(conn, "NQ", day, rth.RTH_MATCHER_VERSION)] == [15, 22, 30,
                                                                                                          40]
    # one issue at a time across processes
    from database.connection import get_db_connection
    other = get_db_connection(DSN)
    try:
        other.execute("SELECT pg_advisory_lock(%s);", (ra._LOCK_KEY,))
        assert ra.issue(conn, now=_ny(day, 10, 37), day=day)["status"] == "busy"
        other.execute("SELECT pg_advisory_unlock(%s);", (ra._LOCK_KEY,))
    finally:
        other.close()
    keep_until(16, 59)                                                                   # the whole day back
    result = ra.issue(conn, now=_ny(day, 10, 59), day=day, issued_by="auto")
    assert result["minutes"] == 60 and [m for m, _, _ in result["stored"]] == [45, 60]   # 45 is always issued


@needs_db
def test_pre_open_sets_are_untouched(journal):
    """The RTH sets live in their own tables: the pre-open analogue sets and the snapshots stay as they were."""
    from forecaster import rth_analogues as ra
    from forecaster.journal import match
    conn, days = journal
    match(conn)
    before = (_count(conn, "analogue_sets"), _count(conn, "analogue_members"), _count(conn, "snapshots"))
    ra.register(conn)
    ra.reconstruct(conn, days[-6:], minutes=range(1, 61))
    assert (_count(conn, "analogue_sets"), _count(conn, "analogue_members"), _count(conn, "snapshots")) == before
    assert match(conn) == 0


@needs_db
def test_cli_backfill_show_and_calibrate(journal, capsys):
    from scripts.nq_journal import main
    conn, days = journal
    day = days[-7]                                         # no other test stores its sets
    assert main(["--db", DSN, "rth-backfill", "--date", day]) == 0
    assert main(["--db", DSN, "rth-show", "--date", day, "--minute", "45"]) == 0
    out = capsys.readouterr().out
    assert "3 new set(s)" in out and "Stored windows: 15*, 30*, 60*" in out
    assert "Reconstructed (the newest calculation, live or not):" in out
    assert "RTH analogues — first 30 minutes — data through 10:00 ET" in out
    assert "30-minute checkpoint" in out and "by a backfill" in out and "not a probability" in out
    assert main(["--db", DSN, "rth-show", "--date", day, "--minute", "10"]) == 1         # no window that short
    assert main(["--db", DSN, "rth-show", "--date", day, "--view", "issued"]) == 1       # nothing issued live
    assert "nothing was issued live" in capsys.readouterr().out
    assert main(["--db", DSN, "rth-calibrate", "--end", DAY]) == 0
    out = capsys.readouterr().out
    assert "(registered: 258 2025-09-29..2026-10-07)" in out and "path" in out
