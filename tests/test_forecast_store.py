# tests/test_forecast_store.py
"""
Integration tests for migration 0004 and database/forecast_store.py.

They need a disposable PostgreSQL + TimescaleDB database whose name contains
"test", which they RESET:

    TEST_DATABASE_URL=postgresql://trading:trading@localhost:5432/trading_pipeline_test pytest
"""

import os
import re
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import psycopg
import pytest

from database import forecast_store as store
from database.connection import get_db_connection, reset_database
from database.queries import get_day_bars, save_bars_by_day, save_trading_day, set_active_contracts, upsert_contract
from features import calendar as cal
from features import catalogue as catv2
from features.market_data import DbMarketData
from features.nq_v2 import build_snapshot
from features.session_windows import enrich_candle_timezones
from forecaster import labels_v2, models_v2
from tests.synthetic import NQ_CID, make_market

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)

DAY = "2026-06-10"


@pytest.fixture(scope="module")
def conn():
    reset_database(DSN)
    c = get_db_connection(DSN)
    md, sessions = make_market(last_day="2026-06-12", n_sessions=75)
    upsert_contract(c, NQ_CID, "NQ", "20260918", "CME")
    df = md._bars[(NQ_CID, "TRADES")]
    df = enrich_candle_timezones(df.assign(timestamp_utc=df["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
    save_bars_by_day(c, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                            "low", "close", "volume"]].to_dict("records"))
    set_active_contracts(c, "NQ", {s.session_date.isoformat(): NQ_CID for s in sessions}, "test")
    store.register_feature_version(c, catv2.registry_record())
    store.register_label_version(c, labels_v2.label_registry_record())
    store.register_model_version(c, models_v2.registry_record(models_v2.CLIMATOLOGY))
    yield c
    c.close()


def test_feature_matrix_view_matches_catalogue(conn):
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'forecast' "
        "AND table_name = 'feature_matrix_nq_v2' ORDER BY ordinal_position;")]
    assert tuple(cols[cols.index("daily_atr_fraction"):]) == catv2.FEATURE_NAMES


def test_versions_are_immutable(conn):
    assert store.register_feature_version(conn, catv2.registry_record()) is False
    changed = dict(catv2.registry_record(), definition_hash="different")
    with pytest.raises(store.VersionConflict):
        store.register_feature_version(conn, changed)


def test_snapshot_idempotent_and_append_only(conn):
    snap = build_snapshot(DbMarketData(conn), DAY)
    sid, created = store.save_feature_snapshot(conn, snap)
    assert created
    assert store.save_feature_snapshot(conn, snap) == (sid, False)
    for sql in ("UPDATE forecast.feature_snapshots SET correction_reason = 'x'",
                "DELETE FROM forecast.feature_snapshots", "TRUNCATE forecast.source_revisions CASCADE"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)
    stored = store.get_feature_snapshot(conn, sid)
    assert stored["features"] == snap.features and stored["source_revision_id"] == snap.source_revision_id


def test_live_timing_is_enforced(conn):
    s = cal.session("2026-06-11")
    base = build_snapshot(DbMarketData(conn), "2026-06-11")
    late = replace(base, data_mode="live_capture", features_frozen_at=s.rth_open_at)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.save_feature_snapshot(conn, late)

    live = replace(base, data_mode="live_capture", features_frozen_at=s.cutoff_at + timedelta(seconds=20))
    sid, _ = store.save_feature_snapshot(conn, live)
    run = {"snapshot_id": sid, "model_version": "nq_climatology_v1", "label_version": labels_v2.LABEL_VERSION,
           "calibration": {}, "input_quality_status": "invalid"}
    abstain = [{"target_id": t, "abstained": True, "abstention_reason": "test"} for t in labels_v2.TARGETS]
    with pytest.raises(psycopg.Error, match="before 09:30"):
        store.save_forecast_run(conn, dict(run, generated_at=s.rth_open_at + timedelta(seconds=1)), abstain)
    with pytest.raises(psycopg.Error, match="precedes"):
        store.save_forecast_run(conn, dict(run, generated_at=s.cutoff_at), abstain)
    store.save_forecast_run(conn, dict(run, generated_at=s.cutoff_at + timedelta(seconds=40)), abstain)


def _run(conn, sid):
    return {"snapshot_id": sid, "model_version": "nq_climatology_v1", "label_version": labels_v2.LABEL_VERSION,
            "generated_at": pd.Timestamp.now(tz="UTC").to_pydatetime(), "calibration": {},
            "input_quality_status": "valid"}


@pytest.mark.parametrize("probabilities, label, message", [
    ({"down": 0.2, "flat": 0.3, "up": 0.5}, "sideways", "not in the vocabulary"),
    ({"down": 0.2, "flat": 0.3}, "up", "exactly the vocabulary"),
    ({"down": 0.2, "flat": 0.3, "up": 0.6}, "up", "sum to"),
    ({"down": 0.2, "flat": 0.3, "up": 0.5, "big_up": 0.0}, "up", "exactly the vocabulary"),
])
def test_prediction_vocabulary_enforced(conn, probabilities, label, message):
    sid = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[0]["snapshot_id"]
    preds = [{"target_id": "first_hour_direction", "predicted_label": label,
              "probabilities": probabilities, "abstained": False}]
    with pytest.raises(psycopg.Error, match=message):
        store.save_forecast_run(conn, _run(conn, sid), preds)


def test_outcome_revisions_and_explicit_selection(conn):
    md = DbMarketData(conn)
    snap = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[-1]
    sid = snap["snapshot_id"]
    preds = [{"target_id": t, "predicted_label": "flat", "abstained": False,
              "probabilities": {"down": 0.25, "flat": 0.5, "up": 0.25}} for t in labels_v2.TARGETS]
    store.save_forecast_run(conn, _run(conn, sid), preds)

    def record():
        out = labels_v2.compute_outcome(md, snap)
        mrev, _ = store.save_outcome_metrics(conn, sid, labels_v2.METRIC_VERSION, out["metrics"],
                                             out["metric_status"], out["available_at"], out["digest"])
        return [store.save_realised_outcome(conn, sid, labels_v2.LABEL_VERSION, t, lab, why, at, out["digest"],
                                            labels_v2.METRIC_VERSION, mrev)
                for t, (lab, why, at) in out["labels"].items()], mrev

    first, mrev = record()
    assert snap["reference_values"]["A"] is not None
    assert mrev == 1 and all(r == (1, True) for r in first)
    again, mrev = record()
    assert mrev == 1 and all(r == (1, False) for r in again)

    # The vendor revises the session's closing minute: outcomes get revision 2,
    # revision 1 stays exactly as it was.
    s = cal.session(DAY)
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, DAY)]
    for r in rows:
        if pd.Timestamp(r["timestamp_utc"], tz="UTC") == s.scheduled_close_at - timedelta(minutes=1):
            r["close"] += 500.0
            r["high"] = max(r["high"], r["close"])
    save_trading_day(conn, NQ_CID, DAY, rows)
    md = DbMarketData(conn)
    revised, mrev = record()
    assert mrev == 2 and all(rev == 2 for rev, _ in revised)

    with pytest.raises(ValueError):
        store.get_prediction_outcomes(conn)
    v1 = store.get_prediction_outcomes(conn, outcome_revision=1)
    v2 = store.get_prediction_outcomes(conn, outcome_revision=2)
    assert {r["outcome_revision"] for r in v1} == {1} and {r["outcome_revision"] for r in v2} == {2}
    assert [r for r in v2 if r["target_id"] == "session_direction"][0]["actual_label"] == "up"
    as_of = store.get_prediction_outcomes(conn, outcomes_as_of=pd.Timestamp.now(tz="UTC").to_pydatetime())
    assert {r["outcome_revision"] for r in as_of} == {2}
