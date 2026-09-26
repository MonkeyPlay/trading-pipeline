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
    for model in models_v2.MODELS.values():
        store.register_model_version(c, models_v2.registry_record(model))
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
    run = {"snapshot_id": sid, "model_version": "nq_climatology_v2", "label_version": labels_v2.LABEL_VERSION,
           "calibration": {}, "input_quality_status": "invalid"}
    abstain = [{"target_id": t, "abstained": True, "abstention_reason": "test", "prediction_status": "unavailable",
                "decision_reason": "data_quality", "calibration_status": "unvalidated"} for t in labels_v2.TARGETS]
    with pytest.raises(psycopg.Error, match="before 09:30"):
        store.save_forecast_run(conn, dict(run, generated_at=s.rth_open_at + timedelta(seconds=1)), abstain)
    with pytest.raises(psycopg.Error, match="precedes"):
        store.save_forecast_run(conn, dict(run, generated_at=s.cutoff_at), abstain)
    store.save_forecast_run(conn, dict(run, generated_at=s.cutoff_at + timedelta(seconds=40)), abstain)


def _run(conn, sid):
    return {"snapshot_id": sid, "model_version": "nq_climatology_v2", "label_version": labels_v2.LABEL_VERSION,
            "generated_at": pd.Timestamp.now(tz="UTC").to_pydatetime(), "calibration": {},
            "input_quality_status": "valid"}


def _issued(target, label, probabilities):
    return {"target_id": target, "predicted_label": label, "probabilities": probabilities, "abstained": False,
            "prediction_status": "issued", "decision_reason": "none", "calibration_status": "unvalidated"}


@pytest.mark.parametrize("probabilities, label, message", [
    ({"down": 0.2, "flat": 0.3, "up": 0.5}, "sideways", "not in the vocabulary"),
    ({"down": 0.2, "flat": 0.3}, "up", "exactly the vocabulary"),
    ({"down": 0.2, "flat": 0.3, "up": 0.6}, "up", "sum to"),
    ({"down": 0.2, "flat": 0.3, "up": 0.5, "big_up": 0.0}, "up", "exactly the vocabulary"),
    ({"down": 0.2, "flat": 0.3, "up": 0.5}, "flat", "not the most probable"),
    ({"down": 0.4, "flat": 0.2, "up": 0.4}, "down", "not the most probable"),   # tie -> vocabulary order: up
])
def test_prediction_vocabulary_enforced(conn, probabilities, label, message):
    sid = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[0]["snapshot_id"]
    with pytest.raises(psycopg.Error, match=message):
        store.save_forecast_run(conn, _run(conn, sid), [_issued("direction_15m", label, probabilities)])


@pytest.mark.parametrize("pred, constraint", [
    (dict(prediction_status="unavailable", decision_reason="data_quality", abstained=True,
          abstention_reason="x", probabilities={"down": 0.2, "flat": 0.3, "up": 0.5}),
     "unavailable_has_no_probabilities"),
    (dict(prediction_status="issued", decision_reason="uncertainty", predicted_label="up", abstained=False,
          probabilities={"down": 0.2, "flat": 0.3, "up": 0.5}), "issued_iff_no_reason"),
    (dict(prediction_status="abstained", decision_reason="made_up", abstained=True, abstention_reason="x"),
     "decision_reason_vocabulary"),
    (dict(prediction_status="abstained", decision_reason="uncertainty", abstained=True, abstention_reason="x",
          calibration_status=None), "status_all_or_none"),
])
def test_prediction_status_enforced(conn, pred, constraint):
    sid = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[0]["snapshot_id"]
    row = {"target_id": "direction_15m", "calibration_status": "unvalidated", **pred}
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        store.save_forecast_run(conn, _run(conn, sid), [row])


def test_abstained_prediction_keeps_probabilities(conn):
    sid = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[0]["snapshot_id"]
    store.save_forecast_run(conn, _run(conn, sid), [
        {"target_id": "direction_15m", "prediction_status": "abstained", "decision_reason": "out_of_distribution",
         "calibration_status": "validated_raw", "abstained": True, "abstention_reason": "far",
         "probabilities": {"up": 0.2, "down": 0.3, "flat": 0.5}}])


def test_outcome_revisions_and_explicit_selection(conn):
    md = DbMarketData(conn)
    snap = store.find_snapshots(conn, DAY, catv2.FEATURE_VERSION)[-1]
    sid = snap["snapshot_id"]
    preds = []
    for t, labels in labels_v2.vocabulary().items():
        probs = {lab: 0.5 / (len(labels) - 1) for lab in labels}
        probs[labels[-1]] = 0.5
        preds.append(_issued(t, labels[-1], probs))
    store.save_forecast_run(conn, _run(conn, sid), preds)

    def record():
        out = labels_v2.compute_outcome(md, snap)
        mrev, _ = store.save_outcome_metrics(conn, sid, labels_v2.METRIC_VERSION, out["metrics"],
                                             out["metric_status"], out["available_at"], out["digest"])
        return [store.save_realised_outcome(conn, sid, labels_v2.LABEL_VERSION, t, o["label"],
                                            None if o["label"] else o["status"], o["available_at"], out["digest"],
                                            labels_v2.METRIC_VERSION, mrev, o["window_start_at"],
                                            o["window_end_at"])
                for t, o in out["labels"].items()], mrev

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
    rth = [r for r in v2 if r["target_id"] == "direction_rth"][0]
    assert rth["actual_label"] == "up" and rth["prediction_status"] == "issued"
    assert pd.Timestamp(rth["outcome_window_end_at"], tz="UTC") == cal.ny_instant(s.session_date, labels_v2.RTH_END)
    as_of = store.get_prediction_outcomes(conn, outcomes_as_of=pd.Timestamp.now(tz="UTC").to_pydatetime())
    assert {r["outcome_revision"] for r in as_of} == {2}


def test_sklearn_forecast_end_to_end(conn):
    """Snapshots -> realised outcomes -> a walk-forward sklearn fit -> a stored run
    that the database's vocabulary, arg-max and status checks accept."""
    md = DbMarketData(conn)
    days = [s.session_date.isoformat() for s in cal.sessions_between("2026-06-08", "2026-06-12")]
    for d in days:
        sid, _ = store.save_feature_snapshot(conn, build_snapshot(md, d))
        snap = store.get_feature_snapshot(conn, sid)
        out = labels_v2.compute_outcome(md, snap)
        mrev, _ = store.save_outcome_metrics(conn, sid, labels_v2.METRIC_VERSION, out["metrics"],
                                             out["metric_status"], out["available_at"], out["digest"])
        for t, o in out["labels"].items():
            store.save_realised_outcome(conn, sid, labels_v2.LABEL_VERSION, t, o["label"],
                                        None if o["label"] else o["status"], o["available_at"], out["digest"],
                                        labels_v2.METRIC_VERSION, mrev, o["window_start_at"], o["window_end_at"])

    small = dict(models_v2.SKLEARN, model_version="nq_sklearn_test")
    small["parameters"] = dict(small["parameters"], min_training_sessions=3)
    store.register_model_version(conn, models_v2.registry_record(small))
    target = store.find_snapshots(conn, days[-1], catv2.FEATURE_VERSION)[0]
    run, preds = models_v2.predict(conn, target, small)
    for p in preds:
        n = run["calibration"]["training"][p["target_id"]]["n"]
        assert n >= 3, "earlier sessions' outcomes are the training set"
        assert run["calibration"]["training"][p["target_id"]]["last_session"] < days[-1]
        assert p["prediction_status"] == "issued"
    run_id = store.save_forecast_run(conn, run, preds)
    assert run_id

    run, default = models_v2.predict(conn, target, models_v2.SKLEARN)
    assert {p["prediction_status"] for p in default} == {"unavailable"}   # < 60 training sessions
    store.save_forecast_run(conn, run, default)
