# tests/test_fan_forward_db.py
"""
The forward record against a disposable database (forecaster/fan_forward.py, chunk 8) - the
failure paths a live record meets: a run that restarts and catches up the marks of the last
half hour; an origin bar not stored yet (stale - said once) that arrives later (issued, with
its receipt time); bars rewritten by the collector keeping the time they first arrived; a
forecast made after its targets (expired, never on time); scoring run twice (no duplicate
scores); and an origin bar revised after issuance (the revision measured).
"""

import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import psycopg
import pytest

from contracts import fan as F
from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from forecaster import fan_features as ff
from forecaster import fan_forward as fwd
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster import fan_panel as fp
from forecaster.fan_benchmark import slot_instant, slot_of_time
from tests.test_fan import market

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
NQ, VXN = 811, 812


def _save(conn, days, cid, upto=None, bump=None):
    """``days`` stored as contract ``cid`` (a day cut after slot ``upto`` when given; ``bump``: {slot: factor} on the
    last day's closes)."""
    import pandas as pd
    from database.queries import save_bars_by_day
    from features.session_windows import enrich_candle_timezones
    for k, d in enumerate(days):
        closes = d.closes.copy()
        if bump and k == len(days) - 1:
            for s, f in bump.items():
                closes[s] *= f
        last = d.end if (upto is None or k < len(days) - 1) else upto + 1
        idx = [slot_instant(d.session_date, s) for s in range(last) if np.isfinite(closes[s])]
        c = closes[:last][np.isfinite(closes[:last])]
        o = np.concatenate([[c[0]], c[:-1]])
        df = pd.DataFrame({"timestamp_utc": pd.DatetimeIndex(idx).strftime("%Y-%m-%d %H:%M:%S"), "open": o,
                           "high": np.maximum(o, c) + 0.01, "low": np.minimum(o, c) - 0.01, "close": c,
                           "volume": 10, "contract_id": cid})
        df = enrich_candle_timezones(df)
        save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                                   "low", "close", "volume"]].to_dict("records"))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """45 sessions of a synthetic NQ and VXN in the database, a registered experiment, a frozen linear model fitted on
    the first 30 sessions, and the forward record's rules. The last session is stored to 10:05 ET only."""
    from database import journal_store as store
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    reset_database(DSN)
    conn = get_db_connection(DSN)
    nq = market(45, seed=31, release_every=4)
    vxn = [replace(d, closes=d.closes / 1000.0, releases=()) for d in market(45, seed=32)]
    upsert_contract(conn, NQ, "NQ", "20261218", "CME", local_symbol="NQZ6")
    upsert_contract(conn, VXN, "VXN", None, "CBOE", local_symbol="VXN")
    last = nq[-1]
    cut = slot_of_time(datetime(2000, 1, 1, 10, 5).time())
    _save(conn, nq[:-1], NQ)
    _save(conn, [last], NQ, upto=cut)
    _save(conn, vxn, VXN)
    dates = [d.session_date.isoformat() for d in nq]
    set_active_contracts(conn, "NQ", {d: NQ for d in dates}, "test")
    set_active_contracts(conn, "VXN", {d: VXN for d in dates}, "test")
    store.register_version(conn, F.fan_record())
    store.register_version(conn, F.fan_v2_record())
    manifest = {"name": "fan_fwd_test", "targets": {"primary": "NQ", "secondary": []},
                "horizons": {"primary": {"minutes": 15}},
                "gate": {"pass": "p", "inconclusive": "i", "fail": "f"},
                "instruments": {"availability": {"NQ": {"first_complete": dates[0], "status": "included"},
                                                 "VXN": {"first_complete": dates[0], "status": "included"}}},
                "split": {"development": {"sessions": dates[:30]}}}
    store.register_version(conn, defs._record("fan_fwd_test", "fan_experiment", manifest))
    experiment = store.get_version(conn, "fan_fwd_test")
    frames = fh.baseline_frames(nq[:-1], nq[0].session_date, nq[-2].session_date)
    table = ff.build(fp.load_panel(conn, dates[:-1], ["NQ", "VXN"]), "NQ", {"NQ": dates[0], "VXN": dates[0]})
    names = fm.FEATURE_SETS["own_ivx_linear"]("NQ")
    cand = fm.LinearScale(table, names, "lin_pois_ivx")
    cand.fit(fh.Rows.concat([fh.frame_rows(frames[d], every=5) for d in dates[:30] if d in frames]))
    definition = fm.frozen_definition("lin_pois_ivx", cand, experiment, {"version": "fan_rw_v2", "definition_hash":
                                      F.fan_v2_record()["definition_hash"]}, dates[:30],
                                      {"feature_format": ff.FEATURE_FORMAT, "frame_format": fh.FRAME_FORMAT})
    store.register_version(conn, defs._record("fan_fwd_test_model", "fan_model", definition))
    model = store.get_version(conn, "fan_fwd_test_model")
    fwd.define(conn, model)
    yield {"conn": conn, "nq": nq, "vxn": vxn, "last": last, "experiment": experiment, "model": model,
           "cache": str(tmp_path_factory.mktemp("cache"))}
    conn.close()


def _et(d, hh, mm, ss=0):
    return cal.NY_TZ.localize(datetime(d.year, d.month, d.day, hh, mm, ss)).astimezone(timezone.utc)


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_a_restarted_run_catches_up_says_stale_once_and_issues_when_the_bar_arrives(world):
    from database.connection import get_db_connection
    w, d = world, world["last"].session_date
    issue = lambda conn, now: fwd.issue(conn, w["model"], w["experiment"], now, "test", w["cache"])
    first = issue(w["conn"], _et(d, 10, 16, 30))                         # pending: 10:00 and 10:15
    assert [(r["mark"].at, r["status"]) for r in first] == [(_et(d, 10, 0), "issued"), (_et(d, 10, 15), "stale")]
    restarted = get_db_connection(DSN)                                    # a new process: nothing held in memory
    again = issue(restarted, _et(d, 10, 18, 0))
    assert [r["status"] for r in again] == ["issued before", "stale (again)"]   # stale is said once per mark
    assert _count(restarted, "SELECT count(*) FROM journal.fan_forward_runs WHERE status = 'stale'") == 1
    before = datetime.now(timezone.utc)
    _save(restarted, [w["last"]], NQ, upto=slot_of_time(datetime(2000, 1, 1, 10, 20).time()))   # the bar arrives
    late = issue(restarted, _et(d, 10, 27, 0))
    assert [r["status"] for r in late] == ["issued before", "issued"]
    rec = late[1]["issue"]
    receipt = datetime.fromisoformat(rec["inputs"]["instruments"]["NQ"]["first_stored_at"])
    assert receipt >= before                                              # its receipt time is when it arrived
    assert all("target_at" in f for f in rec["forecast"].values())
    av = rec["inputs"]["availability"]
    assert set(av) == {"NQ", "VXN"} and av["NQ"]["bars"] > 1380 and av["NQ"]["unknown_times"] == 0
    assert datetime.fromisoformat(av["NQ"]["latest_version_stored_at"]) >= before and av["NQ"]["values_md5"]
    assert rec["inputs"]["instruments"]["NQ"]["version_stored_at"] is not None
    first_bar = restarted.execute("SELECT to_char(first_stored_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') "
                                  "FROM bars WHERE contract_id = %s AND timestamp_utc = %s;",
                                  (NQ, slot_instant(d, slot_of_time(datetime(2000, 1, 1, 9, 0).time())))).fetchone()[0]
    _save(restarted, [w["last"]], NQ, upto=slot_of_time(datetime(2000, 1, 1, 10, 25).time()))   # the day rewritten
    assert restarted.execute("SELECT to_char(first_stored_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') FROM bars "
                             "WHERE contract_id = %s AND timestamp_utc = %s;",
                             (NQ, slot_instant(d, slot_of_time(datetime(2000, 1, 1, 9, 0).time())))).fetchone()[0] \
        == first_bar                                                     # a held bar keeps its first arrival
    assert _count(restarted, "SELECT count(*) FROM journal.fan_forward_issues") == 2
    assert _count(restarted, "SELECT count(*) FROM journal.fan_forward_issues WHERE record IS NOT NULL") == 2
    restarted.close()


def test_issues_made_after_their_targets_are_expired_and_scoring_twice_adds_nothing(world):
    w = world
    conn, d = w["conn"], w["last"].session_date
    s = fwd.summary(conn, w["model"])
    (g,) = s["versions"]                                                  # one rule version; no legacy issue
    assert g["version"].endswith("_forward_v2") and g["labels"] == "v2"
    assert {r["class"] for r in g["horizons"]} == {"expired"}             # recorded long after these past marks
    assert g["operations"]["issued"] == 2
    assert fwd.score(conn, w["model"], "test") == {"scored": 0, "waiting": 2}      # the session is not final
    _save(conn, [w["last"]], NQ, bump={slot_of_time(datetime(2000, 1, 1, 10, 14).time()): 1.001})   # final, revised
    first = fwd.score(conn, w["model"], "test")
    n = _count(conn, "SELECT count(*) FROM journal.fan_forward_scores")
    assert first == {"scored": 2, "waiting": 0} and n > 0
    assert fwd.score(conn, w["model"], "test") == {"scored": 0, "waiting": 0}      # twice: nothing new
    assert _count(conn, "SELECT count(*) FROM journal.fan_forward_scores") == n
    rev = conn.execute("SELECT max(s.revision) FROM journal.fan_forward_scores s JOIN journal.fan_forward_issues i "
                       "ON i.issue_id = s.issue_id WHERE i.mark_at = %s;", (_et(d, 10, 15),)).fetchone()[0]
    assert rev == pytest.approx(np.log(1.001), rel=1e-6)                 # the origin bar was revised after issuance
    with pytest.raises(Exception):
        with conn:
            conn.execute("DELETE FROM journal.fan_forward_scores")       # append-only



def test_the_dashboards_learned_fan_is_the_forward_records_issue(world):
    """The learned fan the Session Explorer draws from an origin (forecaster/fan_live.py) is the forward record's
    issued forecast from that origin: the same frozen definition, the same features read to the origin only."""
    from database import journal_store as store
    from forecaster import fan_data, fan_live, fan_v2
    w = world
    conn, d = w["conn"], w["last"].session_date
    assert fan_live.drawn(conn, "fan_fwd_test")[1] == []                 # frozen, holdout not scored: nothing drawn
    store.save_experiment_result(conn, "fan_fwd_test", {"kind": "holdout", "verdicts": {
        "h5": "better", "h15": "better", "h60": "inconclusive"}}, "test")
    model, horizons, _ = fan_live.drawn(conn, "fan_fwd_test")
    assert model["version"] == "fan_fwd_test_model" and horizons == [5, 15]
    md, note = fan_live.load_model(conn, "NQ", d, name="fan_fwd_test")
    assert note == "" and fan_live.load_model(conn, "VXN", d, name="fan_fwd_test")[0] is None
    days = fan_data.load_days(conn, "NQ", w["nq"][0].session_date, d)
    day = days[-1]
    v2m = fan_v2.fit(day, days[:-1])
    Q = fwd.v2_shape(conn, "NQ", d, w["cache"])
    issue = conn.execute("SELECT origin_slot, forecast FROM journal.fan_forward_issues WHERE mark_at = %s;",
                         (_et(d, 10, 0),)).fetchone()
    forecast = issue[1] if isinstance(issue[1], dict) else __import__("json").loads(issue[1])
    marks = fan_live.model_marks(conn, md, day, v2m, Q, int(issue[0]))
    assert [m["minutes"] for m in marks] == [5, 15]
    for m in marks:
        f = forecast[str(m["minutes"])]
        assert m["multiplier"] == pytest.approx(f["multiplier"], rel=1e-9)
        assert m["sigma"] == pytest.approx(f["sigma"], rel=1e-9)
        assert m["q"] == pytest.approx(f["model"], abs=0.006)              # rounded to the cent
        assert m["base"] == pytest.approx(f["base"], abs=0.006)            # v2 at exactly the horizon
    # Equal on these inputs - but a recomputation cannot undo a revision or know what had arrived: where the record
    # holds an issue from the origin, the explorer draws that issue, read from the journal.
    rec = fan_live.recorded(conn, md, int(issue[0]))
    assert [m["minutes"] for m in rec["marks"]] == [5, 15] and rec["recorded_at"] > rec["mark_at"] == _et(d, 10, 0)
    for m in rec["marks"]:
        f = forecast[str(m["minutes"])]
        assert m["q"] == pytest.approx(f["model"], abs=0.006) and m["base"] == pytest.approx(f["base"], abs=0.006)
        assert m["class"] == "expired"                                    # recorded long after this past mark
    assert fan_live.recorded(conn, md, int(issue[0]) + 1) is None
    from dashboard.components import fan as explorer
    ctx = explorer.FanContext("NQ", d, v2m, shape=fan_v2.Shape(np.array(fh.FRAME_HORIZONS), Q, F.SHAPE_SESSIONS),
                              learned=md)
    assert explorer.load_marks(conn, ctx, day, int(issue[0]))["source"] == "recorded"
    computed = explorer.load_marks(conn, ctx, day, int(issue[0]) + 1)
    assert computed["source"] == "computed" and [m["minutes"] for m in computed["marks"]] == [5, 15]


def test_a_mark_triggered_issue_keeps_its_trigger_and_is_timed_by_it(world):
    """The mark trigger's issue (scripts/fan.py forward mark): only the mark given is tried, its run row keeps the
    trigger - when it started, when the collection ended - and when the computation started, and the summary times
    it under that trigger against the rules' 60 s deadline. origin_stored reads the store in its own transaction."""
    import json
    w = world
    conn, d = w["conn"], w["last"].session_date
    mark = fwd.mark_at(_et(d, 11, 0, 5))
    assert fwd.origin_stored(conn, "NQ", mark)                            # the session is stored whole by now
    assert not fwd.origin_stored(conn, "NQ", replace(mark, session=d + timedelta(days=1)))   # nothing stored then
    trig = {"kind": "mark", "triggered_at": (mark.at + timedelta(seconds=1)).isoformat(),
            "collected_at": (mark.at + timedelta(seconds=9)).isoformat(), "collections": [{"rc": 0, "stored": True}]}
    res = fwd.issue(conn, w["model"], w["experiment"], datetime.now(timezone.utc), "test", w["cache"], trigger=trig,
                    marks=[mark])
    assert [r["status"] for r in res] == ["issued"]                       # the mark given, nothing pending besides
    detail = conn.execute("SELECT detail FROM journal.fan_forward_runs WHERE issue_id = %s;",
                          (res[0]["issue"]["issue_id"],)).fetchone()[0]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["trigger"] == trig and datetime.fromisoformat(detail["started_at"]) <= datetime.now(timezone.utc)
    timing = {t["trigger"]: t for t in fwd.summary(conn, w["model"])["versions"][0]["timing"]}
    assert timing["mark"]["issued"] == 1 and timing["mark"]["within_deadline"] == 0    # a past mark: long after
    assert timing["mark"]["schedule_s"]["median"] == 1 and timing["mark"]["collect_s"]["median"] == 8
    assert timing["mark"]["arrival_s"] is not None                        # the origin bar's receipt time
    assert timing["unrecorded"]["issued"] == 2                            # the first test's, made without a trigger
