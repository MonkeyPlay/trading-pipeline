# tests/test_fan_experiment.py
"""
The intermarket fan experiment's manifest (forecaster/fan_experiment.py): the window
resolved from what is stored, instruments deferred until backfilled, the development
checks, the holdout fixed by date whatever is backfilled - and, against a disposable
database, registration and the sealed holdout.
"""

import os
from datetime import date

import psycopg
import pytest

from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from forecaster import fan_experiment as fx

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)
FIRST = {"NQ": "2025-06-18", "ES": "2025-06-18", "RTY": "2025-09-18", "VIX": "2024-07-29", "VXN": "2024-10-21",
         "TNX": "2025-01-31", "10Y": "2025-09-03", "DX": "2025-09-12", "QQQ": "2025-02-12", "SPY": "2025-08-28",
         "SMH": "2025-01-31", "IWM": "2026-02-20"}


def _days(first=None, gaps=None):
    """Every full session from each instrument's first to the holdout's end, less ``gaps``."""
    first = {**FIRST, **(first or {})}
    out = {}
    for sym, start in first.items():
        days = [s.session_date.isoformat() for s in cal.sessions_between(date.fromisoformat(start),
                                                                           date(2026, 10, 5)) if s.is_open]
        out[sym] = [d for d in days if d not in (gaps or {}).get(sym, ())]
    return out


def test_the_window_starts_with_nq_and_reads_every_other_instrument_from_its_own_start():
    m = fx.experiment_manifest("t", _days())
    sp, inst = m["split"], m["instruments"]["availability"]
    assert sp["window"]["start"] == "2025-06-18" and sp["window"]["set_by"] == ["NQ"]
    assert inst["RTY"]["status"] == "included" and inst["RTY"]["missing_before"] == "2025-09-18"
    assert 0.75 <= inst["RTY"]["development_coverage"] < 0.9 and inst["VIX"]["missing_before"] is None
    assert inst["IWM"]["status"] == "deferred" and "backfilled" in inst["IWM"]["deferred_because"]   # from 2026-02
    hold, dev = sp["holdout"], sp["development"]
    assert (hold["first"], hold["last"], hold["count"]) == ("2026-07-13", "2026-10-05", 60)
    assert dev["last"] == "2026-07-10" and dev["count"] >= fx.MIN_DEVELOPMENT_SESSIONS
    full = [s.session_date.isoformat() for s in cal.sessions_between(date(2025, 6, 18), date(2026, 7, 10))
            if s.schedule == "full"]
    assert dev["sessions"] == full[fx.WARM_UP_SESSIONS:]                     # full sessions only, after the warm-up
    assert not set(dev["sessions"]) & set(hold["sessions"])


def test_the_checks_are_the_last_development_blocks_each_trained_on_what_came_before():
    m = fx.experiment_manifest("t", _days())
    dev = m["split"]["development"]["sessions"]
    blocks = m["split"]["checks"]["blocks"]
    assert [b["sessions"]["count"] for b in blocks] == [fx.CHECK_SESSIONS] * fx.CHECKS
    assert blocks[-1]["sessions"]["last"] == dev[-1]
    for a, b in zip(blocks, blocks[1:]):
        assert dev.index(b["sessions"]["first"]) == dev.index(a["sessions"]["last"]) + 1   # consecutive
    for b in blocks:
        assert b["train"]["first"] == dev[0] and dev.index(b["train"]["last"]) + 1 == dev.index(b["sessions"]["first"])
        assert b["train"]["sessions"] == dev.index(b["sessions"]["first"])


def test_a_backfill_never_moves_the_holdout():
    before = fx.experiment_manifest("t", _days())
    backfilled = fx.experiment_manifest("t", _days({"IWM": "2025-08-11"}))           # IWM backfilled
    inst = backfilled["instruments"]["availability"]["IWM"]
    assert inst["status"] == "included" and inst["missing_before"] == "2025-08-11"
    assert backfilled["split"]["holdout"] == before["split"]["holdout"]
    assert backfilled["split"]["development"] == before["split"]["development"]      # NQ sets the window
    longer = fx.experiment_manifest("t", _days({"NQ": "2025-06-02", "ES": "2025-06-02"}))
    assert longer["split"]["window"]["start"] == "2025-06-02"
    assert longer["split"]["holdout"] == before["split"]["holdout"]
    assert longer["split"]["development"]["count"] > before["split"]["development"]["count"]


def test_a_target_is_never_deferred_and_the_holdout_ends_on_a_full_session():
    with pytest.raises(ValueError, match="RTY cover too little of development, and a target cannot be deferred"):
        fx.experiment_manifest("t", _days({"RTY": "2026-03-02"}))
    with pytest.raises(ValueError, match="full session"):
        fx.experiment_manifest("t", _days(), holdout_end="2025-11-28")            # the day after Thanksgiving
    days = _days()
    days["NQ"] = []
    with pytest.raises(ValueError, match="NQ"):
        fx.experiment_manifest("t", days)


def test_a_session_the_target_did_not_complete_is_excluded_for_that_target():
    m = fx.experiment_manifest("t", _days(gaps={"ES": ["2026-01-05", "2026-08-03"], "VIX": ["2026-01-05"]}))
    dev = m["split"]["development"]["sessions"]
    assert m["data"]["excluded"]["NQ"] == [] and m["data"]["excluded"]["ES"] == ["2026-01-05", "2026-08-03"]
    assert m["data"]["excluded"]["RTY"] == [d for d in dev if d < "2025-09-18"]   # scored from its own start
    assert "2026-01-05" in dev                                                    # a gap never moves the split


def test_every_instrument_has_a_group_and_the_manifest_is_registrable():
    days = _days()
    days["GC"] = days["NQ"]
    with pytest.raises(ValueError, match="GC must belong to exactly one instrument group"):
        fx.experiment_manifest("t", days)
    m = fx.experiment_manifest("t", _days())
    rec = defs._record("t", "fan_experiment", m)
    assert rec["definition_hash"] == defs._record("t", "fan_experiment", fx.experiment_manifest("t", _days()))[
        "definition_hash"]
    assert "2YY" in m["instruments"]["removed"] and "2YY" not in m["instruments"]["availability"]
    assert m["horizons"]["primary"] == {"target": "NQ", "minutes": 15, "origins": "every minute"}
    assert m["horizons"]["secondary"]["pre_open"]["minutes"] == [16, 31, 61]
    assert "supersedes" not in m
    v2 = fx.experiment_manifest("t", _days(), supersedes=fx.SUPERSEDES)
    assert v2["supersedes"] == {"version": fx.SUPERSEDES, "why": fx.SUPERSEDES_WHY}


# --------------------------------------------------------------------------
# Database: registration and the sealed holdout
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conn():
    from database.connection import get_db_connection, reset_database
    reset_database(DSN)
    c = get_db_connection(DSN)
    yield c
    c.close()


def _model(name, experiment, training):
    return defs._record(name, "fan_model", {"experiment": {"name": experiment["version"],
                                                           "definition_hash": experiment["definition_hash"]},
                                            "training_sessions": training})


@needs_db
def test_the_holdout_stays_sealed_until_a_frozen_model_opens_it(conn, capsys):
    from database import journal_store as store
    from scripts.fan import main
    m = fx.experiment_manifest("fan_test_v1", _days())
    assert fx.register_experiment(conn, m) is True and fx.register_experiment(conn, m) is False
    with pytest.raises(store.VersionConflict):
        fx.register_experiment(conn, fx.experiment_manifest("fan_test_v1", _days({"IWM": "2025-08-28"})))
    exp = fx.load_experiment(conn, "fan_test_v1")
    dev, hold = m["split"]["development"], m["split"]["holdout"]

    fx.guard(conn, dev["first"], dev["last"])                                   # development is free
    with pytest.raises(fx.HoldoutSealed, match="sealed holdout of fan_test_v1"):
        fx.guard(conn, dev["first"], hold["first"])
    assert main(["--db", DSN, "score", "--symbol", "NQ", "--start", "2026-07-01", "--end", "2026-08-01"]) == 1
    assert "Not scored" in capsys.readouterr().out
    with pytest.raises(fx.HoldoutSealed, match="no registered model"):
        fx.holdout_sessions(conn, "fan_test_v1", "fan_model_none")

    peeked = _model("fan_model_peeked", exp, dev["sessions"] + hold["sessions"][:1])
    store.register_version(conn, peeked)
    assert "outside development" in fx.why_sealed(exp, store.get_version(conn, "fan_model_peeked"))
    with pytest.raises(fx.HoldoutSealed, match="outside development"):
        fx.holdout_sessions(conn, "fan_test_v1", "fan_model_peeked")
    assert fx.frozen_model(conn, exp) is None

    store.register_version(conn, _model("fan_model_ok", exp, dev["sessions"]))
    assert fx.frozen_model(conn, exp)["version"] == "fan_model_ok"
    fx.guard(conn, dev["first"], hold["last"])                                  # opened
    assert fx.holdout_sessions(conn, "fan_test_v1", "fan_model_ok") == hold["sessions"]
    assert main(["--db", DSN, "experiment-show", "--name", "fan_test_v1"]) == 0
    assert "holdout open - frozen model fan_model_ok" in capsys.readouterr().out


@needs_db
def test_a_new_version_takes_the_seal_over(conn):
    from database import journal_store as store
    v1 = fx.experiment_manifest("fan_sup_v1", _days())
    fx.register_experiment(conn, v1)
    with pytest.raises(ValueError, match="not a registered fan experiment"):
        fx.register_experiment(conn, fx.experiment_manifest("fan_sup_v2", _days(), supersedes="fan_nothing"))
    v2 = fx.experiment_manifest("fan_sup_v2", _days({"IWM": "2025-08-11"}), supersedes="fan_sup_v1")
    fx.register_experiment(conn, v2)
    current = [e["version"] for e in fx.current_experiments(conn)]
    assert "fan_sup_v2" in current and "fan_sup_v1" not in current
    assert fx.superseded_by(conn, "fan_sup_v1") == "fan_sup_v2" and fx.superseded_by(conn, "fan_sup_v2") is None
    hold = v2["split"]["holdout"]
    with pytest.raises(fx.HoldoutSealed, match="sealed holdout of fan_sup_v2"):
        fx.guard(conn, hold["first"], hold["last"])
    exp2 = fx.load_experiment(conn, "fan_sup_v2")
    store.register_version(conn, _model("fan_model_sup", exp2, v2["split"]["development"]["sessions"]))
    fx.guard(conn, hold["first"], hold["last"])          # v2's model opens it; v1, superseded, never holds it shut
    with pytest.raises(ValueError, match="opened by a frozen model"):
        fx.register_experiment(conn, fx.experiment_manifest("fan_sup_v3", _days(), supersedes="fan_sup_v2"))
