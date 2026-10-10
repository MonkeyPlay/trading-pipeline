# tests/test_rth_session.py
"""
The full-session RTH matcher nq_match_rth_v3 (contracts/nq_rth.py) and its evaluation
rth_session_v1 (contracts/rth_session.py), beside the first-hour v2 records, which must
not change: windows to the RTH close from the calendar, the confirming bar after the
close, gaps, the windows an issue does not store, timing, the cache, playback, the
evaluation's windows that never cross the close.

The pure tests need nothing; the rest need a disposable PostgreSQL + TimescaleDB
database whose name contains "test", which they RESET (TEST_DATABASE_URL).
"""

import os
from datetime import date, timedelta, timezone

import psycopg
import pytest

from contracts import nq_rth as rth
from contracts import rth_session as rs
from features import calendar as cal
from matching import rth as mr
from tests.test_rth_analogues import _bars, _ny, _store

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)
UTC = timezone.utc
MIN = timedelta(minutes=1)
DAY = "2026-06-12"


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def test_the_session_length_comes_from_the_calendar():
    """390 minutes, 210 on an early close; the RTH close (16:00 ET), never the 17:00 futures halt; daylight saving
    moves the UTC instants, not the length; a holiday has no session."""
    assert rth.session_minutes(cal.session("2026-10-09")) == 390
    assert rth.session_minutes(cal.session("2025-11-28")) == 210 == rth.session_minutes(cal.session("2025-12-24"))
    assert not cal.session("2025-12-25").is_open
    for day, hour in (("2026-03-06", 21), ("2026-03-09", 20), ("2026-10-30", 20), ("2026-11-02", 21)):
        s = cal.session(day)                                  # the weeks around the US switches
        assert rth.session_minutes(s) == 390 and s.scheduled_close_at.hour == hour
        assert f"{s.scheduled_close_at.astimezone(cal.NY_TZ):%H:%M}" == "16:00"


def test_the_last_rth_bar_is_confirmed_by_a_later_bar_of_the_trading_day():
    """The 15:59 bar completes the session only once a later bar of the day is stored - the futures trade on after
    the cash close; without one the window stays at 389 minutes, awaiting confirmation; a missing midday minute is a
    gap, nothing filled in."""
    open_at = cal.session(DAY).rth_open_at
    day = _bars(open_at, [0.25 * i for i in range(391)])           # 09:30 .. 16:00 (the 16:00 bar is after the close)
    window, stop = mr.completed_window(day, open_at, limit=390)
    assert len(window) == 390 and stop == {"state": "complete", "minute": None}
    window, stop = mr.completed_window(day[:390], open_at, limit=390)   # nothing after 15:59 stored yet
    assert len(window) == 389 and stop == {"state": "awaiting_confirmation", "minute": open_at + 389 * MIN}
    holed = [b for b in day if b[0] != open_at + 210 * MIN]        # 13:00 missing, later bars stored
    window, stop = mr.completed_window(holed, open_at, limit=390)
    assert len(window) == 210 and stop == {"state": "gap", "minute": open_at + 210 * MIN}
    early = cal.session("2025-11-28")
    short = _bars(early.rth_open_at, [0.25 * i for i in range(230)])
    window, stop = mr.completed_window(short, early.rth_open_at, limit=rth.session_minutes(early))
    assert len(window) == 210 and stop["state"] == "complete"          # an early close ends the window at 13:00


def test_v3_is_a_new_version_and_v2_is_unchanged():
    assert rth.matcher_record()["definition_hash"].startswith("834cfe20bf1a")     # as registered on 2026-10-09
    v3 = rth.session_record()
    assert v3["version"] == "nq_match_rth_v3" != rth.RTH_MATCHER_VERSION and v3["kind"] == "rth_matcher"
    d = v3["definition"]
    assert d["weights"] == rth.DEFINITION["weights"] and d["tolerances"] == rth.DEFINITION["tolerances"]
    assert "descriptive extension, not validated" in d["tolerances_scope"] and "1..L" in d["window"]
    assert "timely" in d["timing"] and "late" in d["timing"] and "reconstruction" in d["timing"]
    assert rs.record()["definition"]["fed_by"].startswith("sets of nq_match_rth_v3 only")
    sql = open(os.path.join(os.path.dirname(__file__), "..", "database", "migrations", "0031_rth_session.sql")).read()
    assert f"interval '{int(rth.TIMELY_WITHIN.total_seconds() // 60)} minutes'" in sql   # the database agrees


def test_no_forecast_window_crosses_the_close():
    """A horizon that would run past the RTH close is never forecast - and never shortened to fit."""
    assert [h for h in rs.HORIZONS if rs.applicable(360, h, 390)] == [15, 5]
    assert [h for h in rs.HORIZONS if rs.applicable(330, h, 390)] == [15, 5, 30]
    assert all(rs.applicable(n, h, 390) == (n + rs.LEAD_MINUTES + h <= 390) for n in rs.CUTOFFS for h in rs.HORIZONS)
    early = {(n, h) for n in rs.CUTOFFS for h in rs.HORIZONS if rs.applicable(n, h, 210)}
    assert max(n for n, _ in early) == 180 and (210, 5) not in early


def test_the_windows_an_issue_does_not_store_are_recorded_with_why():
    from forecaster import rth_analogues as ra
    t = _ny(DAY, 12, 20)
    # the last issue a minute ago: the windows confirmed since arrived together
    rows = ra._misses({"elapsed_minutes": 160, "created_at": t - MIN}, 167, [167], [], t, DAY, "auto")
    assert [(r["first_minutes"], r["last_minutes"], r["reason"]) for r in rows] == [(161, 166, "coalesced")]
    # nothing issued for twenty minutes: no run while they were current; a cutoff issued now splits the run
    rows = ra._misses({"elapsed_minutes": 140, "created_at": t - 20 * MIN}, 167, [150, 167], [], t, DAY, "auto")
    assert [(r["first_minutes"], r["last_minutes"], r["reason"]) for r in rows] == [(141, 149, "not_running"),
                                                                                    (151, 166, "not_running")]
    # a cutoff too old to issue live is expired, never issued afterwards
    rows = ra._misses(None, 100, [100], [30, 60], t, DAY, "auto")
    assert [(r["first_minutes"], r["reason"]) for r in rows][-2:] == [(30, "expired"), (60, "expired")]
    assert rows[0]["first_minutes"] == 1 and "first issue" in rows[0]["detail"]


def test_later_bars_never_change_an_earlier_window():
    """Changing anything after minute n - the target's or a candidate's bars - changes neither the ranking at n nor
    its digest, at a long window as at a short one."""
    from tests.test_rth_analogues import _opening
    days = [s.session_date.isoformat() for s in cal.sessions_before(date(2026, 6, 13), 6)]

    def opening(day, slope, tail=0.0):
        closes = [slope * (i + 1) for i in range(250)] + [tail * (i + 1) for i in range(140)]
        op = _opening(day, closes[:390], atr=400.0)
        open_at = cal.session(day).rth_open_at
        bars = _bars(open_at, closes + [0.0])
        window, _ = mr.completed_window(bars, open_at, limit=390)
        return mr.Opening(day, "NQ", 1, open_at, op.context, tuple(window),
                          volume_baseline={n: 100.0 * n for n in range(1, 391)})
    pool = [opening(d, 0.3 * (i + 1)) for i, d in enumerate(days[:-1])]
    target = opening(days[-1], 0.9)
    for n in (200, 250):
        before = mr.rank(target, pool, n)
        later = mr.rank(opening(days[-1], 0.9, tail=-5.0), [opening(o.session_date, 0.3 * (i + 1), tail=7.0)
                                                            for i, o in enumerate(pool)], n)
        assert [x["opening"].session_date for x in before["selected"]] == [x["opening"].session_date
                                                                           for x in later["selected"]]
        assert mr.input_digest(target, n, before, rth.SESSION_VERSION) == mr.input_digest(
            opening(days[-1], 0.9, tail=-5.0), n, later, rth.SESSION_VERSION)
        assert mr.input_digest(target, n, before, rth.SESSION_VERSION) != mr.input_digest(target, n, before)


# --------------------------------------------------------------------------
# The journal (database)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def journal(tmp_path_factory):
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from database import journal_store as store
    from forecaster.journal import annotate, register, take_snapshot
    from forecaster import rth_analogues as ra
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
        annotate(conn, store.get_snapshot(conn, take_snapshot(conn, d, "research_0929")))
    ra.register(conn)
    yield conn, days
    conn.close()


def _keep(conn, day, rows, hh, mm, drop=()):
    """The day's bars as stored up to hh:mm ET (a feed so far), without ``drop``."""
    from database.queries import save_trading_day
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    cut = _ny(day, hh, mm)
    save_trading_day(conn, NQ_CID, day, [r for r in rows if ra.utc(r["timestamp_utc"]) <= cut
                                         and ra.utc(r["timestamp_utc"]) not in drop])


@needs_db
def test_the_session_issue_follows_the_whole_session_to_its_close(journal):
    """Past 10:30, at midday and into the final minutes: the newest window each time, the evaluation's cutoffs not
    stored yet, the windows in between recorded as not stored, never backfilled with a made-up time; the 15:59 bar
    waits for a later bar, then the session closes; a gap stops the window; a repeat stores nothing; a second
    issue at once is refused; the first-hour (v2) sets are not touched."""
    from database import journal_store as store
    from database.queries import get_day_bars
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    day = days[-2]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]
    v2_before = store.rth_windows(conn, "NQ", day, rth.RTH_MATCHER_VERSION)

    _keep(conn, day, rows, 10, 35)                                    # a feed ten minutes behind 10:45
    r = ra.issue_session(conn, now=_ny(day, 10, 45), day=day)
    assert r["status"] == "issued" and r["minutes"] == 65 and not r["closed"]
    assert [m for m, _, _ in r["stored"]] == [60, 65]                 # 30 (10:00 + 30 min) has expired
    assert {(m["first_minutes"], m["reason"]) for m in r["misses"]} >= {(30, "expired"), (1, "not_running")}
    _keep(conn, day, rows, 12, 17)
    r = ra.issue_session(conn, now=_ny(day, 12, 27), day=day)
    assert r["minutes"] == 167 and [m for m, _, _ in r["stored"]] == [150, 167]
    aset = store.get_rth_set(conn, r["stored"][-1][1])
    assert ra.describe(aset).startswith("RTH analogues — first 167 minutes — data through 12:17 ET")
    assert aset["session_minutes"] == 390 and aset["quality"]["tolerance_extension"]
    assert aset["confirmed_by_start"] is not None and ra.utc(aset["confirmed_by_start"]) == _ny(day, 12, 17)
    assert aset["computation_started_at"] is not None and aset["issue_class"] == "reconstruction"   # a past session
    assert ra.issue_session(conn, now=_ny(day, 12, 27, 30), day=day)["stored"] == [(167, r["stored"][-1][1], False)]
    _keep(conn, day, rows, 15, 59, drop={_ny(day, 13, 0)})            # 13:00 missing, later bars stored
    r = ra.issue_session(conn, now=_ny(day, 16, 9), day=day)
    assert r["minutes"] == 210 and r["stop"] == {"state": "gap", "minute": _ny(day, 13, 0)}
    _keep(conn, day, rows, 15, 59)                                    # the gap repaired; nothing after 15:59 yet
    r = ra.issue_session(conn, now=_ny(day, 16, 10), day=day)
    assert r["minutes"] == 389 and r["stop"]["state"] == "awaiting_confirmation" and not r["closed"]
    _keep(conn, day, rows, 16, 59)                                    # the futures trade on: 16:00 confirms 15:59
    r = ra.issue_session(conn, now=_ny(day, 16, 12), day=day, issued_by="auto")
    assert r["closed"] and r["minutes"] == 390 and r["session_minutes"] == 390
    assert "RTH closed" in ra.describe(store.get_rth_set(conn, r["stored"][-1][1]))
    windows = [w["elapsed_minutes"] for w in store.rth_windows(conn, "NQ", day, rth.SESSION_VERSION)]
    assert windows == sorted(windows) and 390 in windows and max(windows) == 390      # nothing past the close
    misses = store.rth_misses(conn, "NQ", day, rth.SESSION_VERSION)
    covered = {n for m in misses for n in range(m["first_minutes"], m["last_minutes"] + 1)}
    assert not covered & set(windows)                                 # a window is either stored or missed
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM journal.rth_issue_misses")
    assert store.rth_windows(conn, "NQ", day, rth.RTH_MATCHER_VERSION) == v2_before
    from database.connection import get_db_connection
    other = get_db_connection(DSN)
    try:
        other.execute("SELECT pg_advisory_lock(%s);", (ra._LOCK_KEY,))
        assert ra.issue_session(conn, now=_ny(day, 16, 13), day=day)["status"] == "busy"
        other.execute("SELECT pg_advisory_unlock(%s);", (ra._LOCK_KEY,))
    finally:
        other.close()


@needs_db
def test_the_evaluation_forecasts_windows_after_the_issue_and_never_past_the_close(journal, monkeypatch):
    """At a cutoff the forecasts' window starts at the second full minute after the build, one per horizon that ends
    by the close; near the close the longer horizons are not forecast at all - not shortened; the cases show the
    universe and why each case is out, and nothing is scored before the endpoint."""
    from database import journal_store as store
    from database.queries import get_day_bars
    from forecaster import rth_analogues as ra
    from forecaster import rth_eval
    from tests.synthetic import NQ_CID
    conn, days = journal
    day = days[-3]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]
    _keep(conn, day, rows, 12, 31)
    r = ra.issue_session(conn, now=_ny(day, 12, 42), day=day)
    made = [(m, h) for m, _, _, _, h in r["forecasts"]]
    assert sorted(made) == [(180, 5), (180, 15), (180, 30), (180, 60)]
    f = {int(x["horizon_minutes"]): x for x in store.rth_eval_forecasts(conn, rs.VERSION) if x["session_date"] == day}
    open_at = cal.session(day).rth_open_at
    assert {h: ra.utc(x["cutoff_at"]) for h, x in f.items()} == {h: open_at + 194 * MIN for h in (5, 15, 30, 60)}
    assert f[15]["sources"]["match_cutoff_minutes"] == 180 and f[15]["sources"]["matcher_version"] == rth.SESSION_VERSION
    assert set(f[15]["forecasts"]) == {"RTH-20", "RTH-5", "PRE-5", "CLOCK"}
    _keep(conn, day, rows, 15, 31)
    r = ra.issue_session(conn, now=_ny(day, 15, 41), day=day)
    assert sorted(h for m, _, _, _, h in r["forecasts"] if m == 360) == [5, 15]      # 30 and 60 would cross the close
    monkeypatch.setattr(rth_eval, "_registered", lambda c, v=None: {"registered_at": ra.utc(f"{day} 00:00:00")})
    cases = rth_eval.session_cases(conn, now=_ny(day, 17, 0))
    mine = [c for c in cases if c["session_date"] == day]
    assert {(c["minutes"], c["horizon"]) for c in mine} == {(n, h) for n in rs.CUTOFFS for h in rs.HORIZONS
                                                           if rs.applicable(n, h, 390)}
    assert all(c["reason"] == "late" for c in mine if c["forecast"] is not None)     # stored long after S (a past day)
    with pytest.raises(rth_eval.NotAtEndpoint):
        rth_eval.session_score(conn, now=_ny(day, 17, 0))


@needs_db
def test_the_cache_returns_what_a_fresh_load_returns_and_follows_revisions(journal):
    from database.queries import get_day_bars, save_trading_day
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    last = days[-1]
    fresh, fm = ra.load_session_openings(conn, last, cache=False)
    cold, _ = ra.load_session_openings(conn, last)
    warm, wm = ra.load_session_openings(conn, last)
    for d in fresh:
        assert (fresh[d].bars, fresh[d].context, fresh[d].volume_baseline) == (warm[d].bars, warm[d].context,
                                                                              warm[d].volume_baseline) == (
            cold[d].bars, cold[d].context, cold[d].volume_baseline)
        assert fm[d]["stop"] == wm[d]["stop"] and fm[d]["inputs_at"] == wm[d]["inputs_at"]
    earlier = days[-10]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, earlier)]
    at = cal.session(earlier).rth_open_at + 100 * MIN
    try:                                                  # a vendor revision of an earlier session's bar
        save_trading_day(conn, NQ_CID, earlier, [dict(r, close=float(r["close"]) + 3.0)
                                                 if ra.utc(r["timestamp_utc"]) == at else r for r in rows])
        revised, _ = ra.load_session_openings(conn, last)
        assert revised[earlier].bars[100][4] == fresh[earlier].bars[100][4] + 3.0
    finally:
        save_trading_day(conn, NQ_CID, earlier, rows)


@needs_db
def test_as_issued_playback_never_shows_a_later_record(journal):
    from database import journal_store as store
    from database.queries import get_day_bars
    from forecaster import rth_analogues as ra
    from tests.synthetic import NQ_CID
    conn, days = journal
    day = days[-4]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]
    ra.reconstruct_session(conn, [day], [90, 120])               # backfills: never "as issued"
    assert store.rth_set_issued(conn, "NQ", day, rth.SESSION_VERSION) is None
    _keep(conn, day, rows, 11, 10)
    first = ra.issue_session(conn, now=_ny(day, 11, 20), day=day)["stored"][-1][1]
    stamp = store.get_rth_set(conn, first)["created_at"]
    _keep(conn, day, rows, 11, 40)
    later = ra.issue_session(conn, now=_ny(day, 11, 50), day=day)["stored"][-1][1]
    # issued sets are reconstructions here (a past session): as issued shows the live ones only, so none
    assert store.rth_set_issued(conn, "NQ", day, rth.SESSION_VERSION, at=stamp) is None
    seen = store.rth_set_at(conn, "NQ", day, rth.SESSION_VERSION, 110, stored_by=stamp)
    assert seen["elapsed_minutes"] <= 110 and seen["set_id"] != later        # never a window or set stored later
    assert store.get_rth_set(conn, later)["elapsed_minutes"] == 130


@needs_db
def test_the_database_decides_the_issue_class(journal):
    """timely: issued live within two minutes of its last input reaching the store; late: issued live but later;
    reconstruction: a backfill or more than 30 minutes after the cutoff - and a v2 set still cannot pass 60
    minutes, while v3 stops at 390."""
    from database import journal_store as store
    from forecaster.provenance import code_revision
    conn, days = journal
    from forecaster import rth_analogues as ra
    snap = conn.execute("SELECT snapshot_id, contract_id FROM journal.snapshots LIMIT 1;").fetchone()
    now = ra.utc(conn.execute("SELECT clock_timestamp();").fetchone()[0])

    def insert(version, minutes, issued_by, cutoff, received, digest):
        rec = {"symbol": "NQ", "session_date": "2026-01-02", "contract_id": snap[1], "matcher_version": version,
               "context_snapshot_id": str(snap[0]), "elapsed_minutes": minutes, "cutoff_at": cutoff,
               "input_digest": digest, "pool_size": 0, "pool_hash": "x", "excluded": {}, "mean_similarity": None,
               "target_features": {}, "quality": {}, "code_revision": code_revision(), "issued_by": issued_by,
               "inputs_received_at": received, "pool_received_at": None, "pit_status": "unverified"}
        return store.get_rth_set(conn, store.save_rth_set(conn, rec, [])[0])
    assert insert(rth.SESSION_VERSION, 200, "auto", now - 5 * MIN, now - MIN, "a")["issue_class"] == "timely"
    assert insert(rth.SESSION_VERSION, 201, "auto", now - 15 * MIN, now - 10 * MIN, "b")["issue_class"] == "late"
    assert insert(rth.SESSION_VERSION, 202, "backfill", now - 5 * MIN, now - MIN, "c")["issue_class"] == \
        "reconstruction"
    assert insert(rth.SESSION_VERSION, 203, "manual", now - 45 * MIN, now - MIN, "d")["issue_class"] == \
        "reconstruction"
    assert insert(rth.SESSION_VERSION, 390, "auto", now - 5 * MIN, None, "e")["issue_class"] == "late"   # unknown
    with pytest.raises(psycopg.Error, match="elapsed_minutes"):
        insert(rth.SESSION_VERSION, 391, "auto", now, now, "f")
    with pytest.raises(psycopg.Error, match="elapsed_minutes"):
        insert(rth.RTH_MATCHER_VERSION, 61, "auto", now, now, "g")
