# tests/test_migration_0029.py
"""
Migration 0029 (the LLM system's removal) on a database built to v28 and seeded the way
production was: shared records (snapshots, rule-based annotations and their analogue
set, arm A and B runs, an outcome, an A/B experiment's cases, an annotation review) beside
the LLM ones (Claude annotations and the set built on them, arm C and D runs with their
evidence, predictions and an event, inference requests, an annotation attempt, a batch,
and an experiment with arm D).

Needs a disposable database whose name contains "test" (TEST_DATABASE_URL); it RESETS it.
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from contracts import nq_forecast as fc
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
CID = 9001
UTC = timezone.utc
DIST = json.dumps({"bearish": "1/3", "bullish": "1/3", "neutral_band": "1/3"})
SHARED = ("snapshots", "structure_annotations", "analogue_sets", "analogue_members", "forecast_runs",
          "forecast_predictions", "forecast_evidence", "forecast_run_events", "outcomes", "experiment_cases",
          "annotation_review_sets", "annotation_review_members", "annotation_review_verdicts", "definition_versions")


def _id() -> str:
    return str(uuid.uuid4())


def _record(version, kind, definition):
    return defs._record(version, kind, definition)


def seed(conn, superseding=False):
    """The v28 database's records; returns the ids of the LLM ones. ``superseding``: a kept run supersedes an LLM run
    (the migration must stop)."""
    from database import journal_store as store
    from database.queries import upsert_contract
    upsert_contract(conn, CID, "NQ", "20260918", "CME", local_symbol="NQU6")
    for rec in (defs.all_records() + [pre.rules_record(), pre.matcher_record(), fc.forecast_schema_record(),
                                      _record(fc.BASELINE_VERSION, "forecast_algorithm", fc.BASELINE),
                                      _record(fc.PRIOR_VERSION, "forecast_algorithm", fc.PRIOR),
                                      _record("nq_issue_replay_v1", "issue_policy",
                                              fc.ISSUE_POLICY_DEFINITIONS["historical_replay"]),
                                      _record("nq_structure_restricted_v3", "annotation", {"annotator": "llm"}),
                                      _record("nq_structure_llm_v4", "annotation", {"annotator": "llm"}),
                                      _record("nq_restricted_p1_v3", "forecast_algorithm", {"arm": "C"}),
                                      _record("nq_synthesis_p1_v5", "forecast_algorithm", {"arm": "D"}),
                                      _record("nq_forecast_schema_v2", "forecast_schema", {"arm": "D"}),
                                      _record("nq_issue_live_v3", "issue_policy",
                                              {"deadline_et": "09:29:50", "arms": "A, B and arm D"}),
                                      _record("hist_dev_t", "experiment", {"arms": {
                                          "A": {"algorithm": fc.PRIOR_VERSION}, "B": {"algorithm": fc.BASELINE_VERSION}}}),
                                      _record("p1_d_t", "experiment", {"arms": {
                                          "A": {"algorithm": fc.PRIOR_VERSION}, "D": {"algorithm": "nq_synthesis_p1_v5"}}})]):
        store.register_version(conn, rec)
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    snaps = {}
    with conn:
        for i, day in enumerate(("2026-06-11", "2026-06-12")):
            sid = _id()
            cutoff = datetime(2026, 6, 11 + i, 13, 29, tzinfo=UTC)
            conn.execute("INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, "
                         "snapshot_version, convention_version, cutoff_at, rth_open_at, data_mode, "
                         "pit_availability_status, source_payload_hash, payload) VALUES (%s, 'NQ', %s, %s, %s, %s, %s, "
                         "%s, 'historical_reconstruction', 'unverified_historical', %s, '{}');",
                         (sid, CID, day, version, defs.CONVENTION_VERSION, cutoff, cutoff + timedelta(minutes=1),
                          f"h{i}"))
            snaps[day] = sid
        s1, s2 = snaps["2026-06-11"], snaps["2026-06-12"]

        def annotation(sid, protocol, annotator):
            aid = _id()
            conn.execute("INSERT INTO journal.structure_annotations (annotation_id, snapshot_id, protocol_version, "
                         "annotator, model, integrity_status, fields, price_location, measurements, output_hash) VALUES "
                         "(%s, %s, %s, %s, %s, 'ok', '{}', '{}', '{}', %s);",
                         (aid, sid, protocol, annotator, "claude-x" if annotator == "llm" else None, aid))
            return aid
        rules1, rules2 = annotation(s1, pre.RULES_PROTOCOL_VERSION, "rules"), annotation(s2, pre.RULES_PROTOCOL_VERSION,
                                                                                         "rules")
        llm1, llm2 = annotation(s1, "nq_structure_restricted_v3", "llm"), annotation(s2, "nq_structure_restricted_v3",
                                                                                     "llm")

        def analogue_set(target_sid, target_aid, member_aid):
            set_id = _id()
            conn.execute("INSERT INTO journal.analogue_sets (set_id, target_snapshot_id, target_annotation_id, "
                         "matcher_version, label_version, data_mode, pool_size, pool_hash, excluded, outcome_digest, "
                         "outcome_summary) VALUES (%s, %s, %s, %s, %s, 'historical_reconstruction', 1, %s, '[]', 'd', "
                         "'{}');", (set_id, target_sid, target_aid, pre.MATCHER_VERSION, defs.LABEL_VERSION, set_id))
            conn.execute("INSERT INTO journal.analogue_members (set_id, rank, snapshot_id, annotation_id, session_date, "
                         "similarity, comparable_weight, components) VALUES (%s, 1, %s, %s, '2026-06-11', 50, 100, "
                         "'{}');", (set_id, s1, member_aid))
            return set_id
        rules_set, llm_set = analogue_set(s2, rules2, rules1), analogue_set(s2, llm2, llm1)

        def request(protocol):
            rid = _id()
            conn.execute("INSERT INTO journal.inference_requests (request_id, snapshot_id, protocol_version, model, "
                         "request, request_hash, prompt_sha256, schema_sha256, evidence_sha256, code_revision, mode) "
                         "VALUES (%s, %s, %s, 'claude-x', '{}', 'r', 'p', 's', 'e', 'test', 'live');",
                         (rid, s2, protocol))
            return rid
        c_request, d_request = request("nq_structure_restricted_v3"), request("nq_synthesis_p1_v5")
        conn.execute("INSERT INTO journal.annotation_attempts (attempt_id, snapshot_id, protocol_version, model, "
                     "request_hash, status, annotation_id, started_at, finished_at, request_id) VALUES (%s, %s, "
                     "'nq_structure_restricted_v3', 'claude-x', 'r', 'ok', %s, now(), now(), %s);",
                     (_id(), s2, llm2, c_request))
        conn.execute("INSERT INTO journal.inference_batches (batch_id, submitted_at, request_count) VALUES "
                     "('batch_1', now(), 1);")
        conn.execute("INSERT INTO journal.inference_batch_requests (batch_id, request_id) VALUES ('batch_1', %s);",
                     (c_request,))

        def run(algorithm, aid, set_id, schema=fc.FORECAST_SCHEMA_VERSION, status="issued", request_id=None,
                supersedes=None):
            run_id = _id()
            now = datetime.now(UTC)
            conn.execute("INSERT INTO journal.forecast_runs (run_id, idempotency_key, symbol, session_date, contract_id, "
                         "profile, snapshot_id, annotation_id, analogue_set_id, label_version, algorithm_version, "
                         "schema_version, issue_policy, code_revision, mode, input_cutoff_at, generation_started_at, "
                         "generation_completed_at, lifecycle_status, supersedes_run_id, failure_reason, "
                         "evidence_digest, outputs, request_id) VALUES (%s, %s, 'NQ', '2026-06-12', %s, %s, %s, %s, %s, "
                         "%s, %s, %s, 'nq_issue_replay_v1', 'test', 'historical_replay', %s, %s, %s, %s, %s, %s, 'e', "
                         "'{}', %s);",
                         (run_id, run_id, CID, defs.DEFAULT_PROFILE, s2, aid, set_id, defs.LABEL_VERSION, algorithm,
                          schema, datetime(2026, 6, 12, 13, 29, tzinfo=UTC), now, now, status, supersedes,
                          None if status == "issued" else "invalid answer", request_id))
            conn.execute("INSERT INTO journal.forecast_evidence (run_id, evidence, evidence_digest) VALUES (%s, '{}', "
                         "'e');", (run_id,))
            if status == "issued":
                conn.execute("INSERT INTO journal.forecast_predictions (run_id, target, status, predicted_label, "
                             "estimation_status, distribution, eligible, without_label, prior_sessions, "
                             "prior_without_label) VALUES (%s, 'direction_15m', 'predicted', 'bullish', %s, %s, 1, 0, "
                             "1, 0);", (run_id, "judgement" if algorithm == "nq_synthesis_p1_v5" else "analogues",
                                        DIST))
            return run_id
        a_run = run(fc.PRIOR_VERSION, rules2, rules_set)
        b_run = run(fc.BASELINE_VERSION, rules2, rules_set)
        c_run = run("nq_restricted_p1_v3", llm2, llm_set)
        d_run = run("nq_synthesis_p1_v5", rules2, rules_set, "nq_forecast_schema_v2", request_id=d_request)
        d_invalid = run("nq_synthesis_p1_v5", rules2, rules_set, "nq_forecast_schema_v2", status="invalid")
        conn.execute("INSERT INTO journal.forecast_run_events (run_id, event) VALUES (%s, 'acknowledged');", (d_run,))
        if superseding:
            run(fc.PRIOR_VERSION, rules2, rules_set, supersedes=d_run)
        conn.execute("INSERT INTO journal.outcomes (snapshot_id, label_version, outcome_revision, labels, measurements, "
                     "source_digest) SELECT %s, %s, 1, (SELECT jsonb_object_agg(t, jsonb_build_object('label', NULL, "
                     "'reason', 'missing_bars')) FROM jsonb_object_keys(definition -> 'targets') t), '{}', 'd' FROM "
                     "journal.definition_versions WHERE version = %s;", (s1, defs.LABEL_VERSION, defs.LABEL_VERSION))
        for arm, run_id in (("A", a_run), ("B", b_run)):
            conn.execute("INSERT INTO journal.experiment_cases (experiment, session_date, arm, run_id, snapshot_id, "
                         "status, detail) VALUES ('hist_dev_t', '2026-06-12', %s, %s, %s, 'no_outcome', 'x');",
                         (arm, run_id, s2))
        for arm, run_id in (("A", a_run), ("D", d_run)):
            conn.execute("INSERT INTO journal.experiment_cases (experiment, session_date, arm, run_id, snapshot_id, "
                         "status, detail) VALUES ('p1_d_t', '2026-06-12', %s, %s, %s, 'no_outcome', 'x');",
                         (arm, run_id, s2))
        conn.execute("INSERT INTO journal.experiment_results (result_id, experiment, results, results_hash, "
                     "code_revision) VALUES (%s, 'p1_d_t', '{}', 'h', 'test');", (_id(),))
        conn.execute("INSERT INTO journal.annotation_review_sets (review_set, protocol_version, snapshot_version, "
                     "selection) VALUES ('review_t', %s, %s, '{}');", (pre.RULES_PROTOCOL_VERSION, version))
        conn.execute("INSERT INTO journal.annotation_review_members (review_set, snapshot_id, annotation_id, "
                     "position, reasons) VALUES ('review_t', %s, %s, 1, '[]');", (s1, rules1))
    return {"annotations": {llm1, llm2}, "sets": {llm_set}, "runs": {c_run, d_run, d_invalid},
            "kept_runs": {a_run, b_run}}


def digest(conn, table, where="TRUE"):
    """A table's rows (or those matching ``where``) as one hash - to show a kept record unchanged (the column 0029
    drops left out)."""
    rows = conn.execute(f"SELECT (to_jsonb(t) - 'request_id')::text FROM journal.{table} t WHERE {where} "
                        f"ORDER BY 1;").fetchall()
    return len(rows), hashlib.sha256("\n".join(r[0] for r in rows).encode()).hexdigest()


@pytest.fixture
def v28():
    from database.connection import get_db_connection, reset_database
    reset_database(DSN, upto=28)
    conn = get_db_connection(DSN)
    yield conn
    conn.close()


def test_0029_removes_every_llm_record_and_keeps_every_shared_one(v28):
    from database.migrations import apply_migrations, get_user_version
    conn = v28
    ids = seed(conn)
    llm = ("'nq_structure_restricted_v3', 'nq_structure_llm_v4', 'nq_restricted_p1_v3', 'nq_synthesis_p1_v5', "
           "'nq_forecast_schema_v2', 'nq_issue_live_v3', 'p1_d_t'")
    kept_runs = ", ".join(f"'{r}'" for r in ids["kept_runs"])
    keep = {  # every record that stays, as it is before
        "snapshots": digest(conn, "snapshots"), "outcomes": digest(conn, "outcomes"),
        "rules annotations": digest(conn, "structure_annotations", "annotator = 'rules'"),
        "rules sets": digest(conn, "analogue_sets", f"target_annotation_id NOT IN "
                                                    f"({', '.join(repr(a) for a in ids['annotations'])})"),
        "A/B runs": digest(conn, "forecast_runs", f"run_id IN ({kept_runs})"),
        "A/B predictions": digest(conn, "forecast_predictions", f"run_id IN ({kept_runs})"),
        "A/B evidence": digest(conn, "forecast_evidence", f"run_id IN ({kept_runs})"),
        "A/B experiment": digest(conn, "experiment_cases", "experiment = 'hist_dev_t'"),
        "review": digest(conn, "annotation_review_members"),
        "definitions": digest(conn, "definition_versions", f"version NOT IN ({llm})"),
    }
    assert get_user_version(conn) == 28
    apply_migrations(conn, upto=29)
    assert get_user_version(conn) == 29

    # gone: the LLM records, definitions and tables
    q = lambda sql: conn.execute(sql).fetchone()[0]
    assert q("SELECT count(*) FROM journal.structure_annotations WHERE annotator = 'llm'") == 0
    assert q(f"SELECT count(*) FROM journal.definition_versions WHERE version IN ({llm})") == 0
    assert q("SELECT count(*) FROM journal.forecast_runs") == 2
    assert q("SELECT count(*) FROM journal.forecast_predictions WHERE estimation_status = 'judgement'") == 0
    assert q("SELECT count(*) FROM journal.forecast_run_events") == 0
    assert q("SELECT count(*) FROM journal.analogue_sets") == 1 and q("SELECT count(*) FROM journal.analogue_members") == 1
    assert q("SELECT count(*) FROM journal.experiment_cases WHERE experiment = 'p1_d_t'") == 0
    assert q("SELECT count(*) FROM journal.experiment_results") == 0
    assert q("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'journal' AND table_name IN "
             "('inference_requests', 'inference_batches', 'inference_batch_requests', 'annotation_attempts')") == 0
    assert q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'journal' AND "
             "column_name IN ('request_id', 'with_d')") == 0
    # kept: every shared record exactly as it was
    after = {
        "snapshots": digest(conn, "snapshots"), "outcomes": digest(conn, "outcomes"),
        "rules annotations": digest(conn, "structure_annotations", "annotator = 'rules'"),
        "rules sets": digest(conn, "analogue_sets"),
        "A/B runs": digest(conn, "forecast_runs"), "A/B predictions": digest(conn, "forecast_predictions"),
        "A/B evidence": digest(conn, "forecast_evidence"),
        "A/B experiment": digest(conn, "experiment_cases", "experiment = 'hist_dev_t'"),
        "review": digest(conn, "annotation_review_members"), "definitions": digest(conn, "definition_versions"),
    }
    assert after == keep
    # the append-only guards are back, and the LLM vocabularies are refused
    for sql in ("DELETE FROM journal.forecast_runs", "DELETE FROM journal.definition_versions",
                "UPDATE journal.structure_annotations SET fields = '{}'"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)
    with pytest.raises(psycopg.Error, match="annotator_check"):
        conn.execute("INSERT INTO journal.structure_annotations (annotation_id, snapshot_id, protocol_version, "
                     "annotator, model, integrity_status, fields, price_location, measurements, output_hash) SELECT "
                     "gen_random_uuid(), snapshot_id, protocol_version, 'llm', 'm', 'ok', '{}', '{}', '{}', 'x' FROM "
                     "journal.structure_annotations LIMIT 1;")


def test_0029_stops_when_a_kept_record_depends_on_an_llm_one(v28):
    from database.migrations import apply_migrations, get_user_version
    conn = v28
    seed(conn, superseding=True)
    with pytest.raises(psycopg.Error, match="supersede an LLM run"):
        apply_migrations(conn, upto=29)
    assert get_user_version(conn) == 28                                     # nothing applied
    assert conn.execute("SELECT count(*) FROM journal.inference_requests").fetchone()[0] == 2
