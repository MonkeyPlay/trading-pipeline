# tests/test_ml.py
"""
The scikit-learn forecaster (contracts/nq_ml.py, forecaster/ml_*.py, forecast_summary.py,
delivery.py) on a synthetic journal: NQ and ES bars (tests/synthetic.py) - RTY, VIX, the
10-year and DX absent, as an instrument with no data must be handled - their snapshots,
outcomes and A/B runs, and models trained into a temporary directory.

The pure tests need nothing; the rest need a disposable PostgreSQL + TimescaleDB database
whose name contains "test" (TEST_DATABASE_URL), which they RESET.
"""

import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction

import numpy as np
import psycopg
import pytest

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
UTC = timezone.utc
LAST = "2026-06-12"


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def test_exact_probabilities_cover_every_class_and_sum_to_one():
    from forecaster import ml_model as mm
    rng = np.random.default_rng(3)
    for _ in range(200):
        p = rng.dirichlet(np.ones(3))
        d = mm.exact(p)
        assert set(d) == set(ml.CLASSES) and sum(mm.fraction(v) for v in d.values()) == 1
        assert all(mm.fraction(v) >= 0 for v in d.values())
        assert max(abs(float(mm.fraction(d[c])) - p[i] / p.sum()) for i, c in enumerate(ml.CLASSES)) < 2e-6
    assert mm.exact([1, 1, 1]) == {c: v for c, v in zip(ml.CLASSES, ("333334/1000000", "333333/1000000",
                                                                    "333333/1000000"))}


def test_a_market_closed_at_the_cutoff_is_known_from_its_schedule():
    from forecaster.ml_features import closed_at
    ny = lambda d, h, m: cal.ny_instant(date.fromisoformat(d), time(h, m))
    assert closed_at("VIX", ny("2026-06-12", 9, 28))            # VIX's global hours end 09:15, RTH starts 09:30
    assert not closed_at("VIX", ny("2026-06-12", 9, 10)) and not closed_at("ES", ny("2026-06-12", 9, 28))
    assert closed_at("ES", ny("2026-06-12", 17, 30)) and closed_at("DX", ny("2026-06-12", 18, 30))
    assert closed_at("VIX", ny("2026-03-10", 9, 20))             # New York time across the DST change


def test_the_contract_is_complete_and_experimental():
    specs = ml.feature_specs()
    assert set(ml.CONFIGS["nq_only"]) <= set(ml.CONFIGS["multi"]) and all(c in specs for c in ml.CONFIGS["multi"])
    assert ml.CONFIGS["pooled"] == ml.POOLED_FEATURES and ml.TARGET == "direction_15m"
    assert set(ml.INSTRUMENTS) == {"ES", "RTY", "VIX", "10Y", "DX"} and "QQQ" in ml.EXCLUDED
    assert all(ml.STATUS[v] == "experimental" for v in ml.ALGORITHMS)
    assert ml.delivery_order() == [fc.BASELINE_VERSION, fc.PRIOR_VERSION]   # nothing experimental is delivered
    assert {ml.arm_of(v) for v in ml.ALGORITHMS} == {"N", "M", "P"}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def _store(conn, df, cid):
    from database.queries import save_bars_by_day
    from features.session_windows import enrich_candle_timezones
    df = df.assign(contract_id=cid)
    df = enrich_candle_timezones(df.assign(timestamp_utc=df["bar_start_at"].dt.strftime("%Y-%m-%d %H:%M:%S")))
    save_bars_by_day(conn, df[["contract_id", "timestamp_utc", "trading_day", "session_scope", "open", "high",
                               "low", "close", "volume"]].to_dict("records"))


@pytest.fixture(scope="module")
def journal(tmp_path_factory):
    """A synthetic journal of the research profile (snapshots, outcomes, annotations, analogue sets, A and B runs)
    and the three models trained on all but its last 8 sessions into a temporary directory."""
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from forecaster import ml_model as mm
    from forecaster import ml_train
    from forecaster.journal import catch_up, register, take_snapshot
    from tests.synthetic import ES_CID, NQ_CID, make_market
    reset_database(DSN)
    conn = get_db_connection(DSN)
    bars, sessions = make_market(last_day=LAST, n_sessions=110)
    upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME", local_symbol="NQU6")
    upsert_contract(conn, ES_CID, "ES", "20260918", "CME", local_symbol="ESU6")
    _store(conn, bars[NQ_CID], NQ_CID)
    _store(conn, bars[ES_CID], ES_CID)
    days = [s.session_date.isoformat() for s in sessions]
    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    set_active_contracts(conn, "ES", {d: ES_CID for d in days}, "test")
    root = str(tmp_path_factory.mktemp("models"))
    mp = pytest.MonkeyPatch()
    mp.setattr(mm, "MODELS_DIR", root)
    register(conn)
    take_snapshot(conn, days[30], ml.PROFILE)
    catch_up(conn, ml.PROFILE, now=cal.ny_instant(date.fromisoformat(LAST), time(20, 0)))
    pool = [str(s["session_date"]) for s in ml_train.pool(conn)]
    until = pool[-9]
    manifests = ml_train.train(conn, until=until)
    register(conn)                                     # as the next journal step does once the artifacts exist
    yield {"conn": conn, "root": root, "pool": pool, "until": until, "manifests": manifests}
    mp.undo()
    conn.close()


def _snap(conn, day):
    from forecaster.ml_train import pool
    return next(s for s in pool(conn) if str(s["session_date"]) == day)


@needs_db
def test_models_are_trained_offline_with_their_manifest_and_reproducibly(journal, tmp_path):
    from forecaster import ml_model as mm
    from forecaster import ml_train
    conn, man = journal["conn"], journal["manifests"]
    assert set(man) == set(ml.ALGORITHMS)
    for v, m in man.items():
        assert m["family"] == ml.FAMILY[v] and m["training"]["to"] == journal["until"]
        assert m["features"] == ml.CONFIGS[ml.CONFIG_OF[v]] and m["software"]["scikit-learn"]
        assert re.fullmatch(r"[0-9a-f]{64}", m["sha256"]) and m["path"].endswith(f"{v}/model.joblib")
        assert m["training"]["class_counts"] and len(m["tuning"]) == len(mm.grid(m["family"]))
    assert man[ml.ML_POOLED_VERSION]["training"]["rows"]["ES"] > 0          # pooled: ES's own rows too
    again = ml_train.train(conn, root=str(tmp_path), until=journal["until"])  # the same data, the same model
    for v in ml.ALGORITHMS:
        a, b = mm.load(v), mm.load(v, str(tmp_path))
        X = np.zeros((2, len(ml.CONFIGS[ml.CONFIG_OF[v]])))
        assert np.array_equal(mm.probabilities(a["model"], X), mm.probabilities(b["model"], X))
        assert again[v]["sha256"] == man[v]["sha256"]
    with pytest.raises(mm.ArtifactError, match="never|new version"):     # an artifact is never overwritten
        ml_train.train(conn, root=str(tmp_path), until=journal["until"])


@needs_db
def test_features_are_as_of_the_cutoff_and_flag_what_is_missing(journal):
    from forecaster import ml_features as mf
    conn = journal["conn"]
    day = journal["pool"][-3]
    fs = mf.build(conn, [day], {day: _snap(conn, day)})
    st = fs.instruments[day]
    assert st["NQ"]["status"] == st["ES"]["status"] == "reconstructed"     # stored after the fact: no live receipt
    assert all(st[s]["status"] == "no_session" for s in ("RTY", "VIX", "10Y", "DX"))     # no data: not zero
    row = fs.rows[day]
    assert all(math.isnan(row[f]) for f in ("rty_ret_on", "vix_chg_on", "y10_chg_on", "dx_ret_on"))
    assert row["rty_missing"] == row["vix_missing"] == row["10y_missing"] == row["dx_missing"] == 1.0
    assert all(not math.isnan(row[f"nq_{f.name}"]) for f in ml.OWN_FEATURES) and not math.isnan(row["es_ret_on"])
    cutoff = cal.ny_instant(date.fromisoformat(day), defs.PROFILES[ml.PROFILE].cutoff)
    assert st["NQ"]["market_ts"] == (cutoff - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = fs.provenance[day]["nq_es_resid_on"]
    assert p["sources"] == ["NQ", "ES"] and p["status"]["ES"] == "reconstructed" and p["age_min"]["ES"] == 0
    assert fs.observations[day]["NQ"]["unit"] == "%" and fs.observations[day]["NQ"]["since_close"] is not None


@needs_db
def test_later_bars_cannot_change_the_features_and_a_frozen_forecast_never_changes(journal):
    """Bars after the cutoff (and their revisions) are never read; a revision of an earlier bar changes what a
    rebuild sees, but the stored forecast keeps the features it was issued with."""
    from database.queries import get_day_bars, save_trading_day
    from forecaster import ml_features as mf
    from forecaster import ml_service
    from tests.synthetic import ES_CID, NQ_CID
    conn = journal["conn"]
    day = journal["pool"][-2]
    snap = _snap(conn, day)
    before = mf.build(conn, [day], {day: snap}).rows[day]
    run, created = ml_service.issue(conn, snap, ml.PROFILE, "historical_replay", ml.ML_MULTI_VERSION)
    assert created and run["lifecycle_status"] == "issued"
    cutoff = cal.ny_instant(date.fromisoformat(day), defs.PROFILES[ml.PROFILE].cutoff)
    originals = {cid: [dict(zip(r.keys(), r)) for r in get_day_bars(conn, cid, day)] for cid in (NQ_CID, ES_CID)}
    try:
        for cid, rows in originals.items():                       # every bar after the cutoff revised
            revised = [dict(r, close=float(r["close"]) * 1.05, high=float(r["high"]) * 1.05)
                       if str(r["timestamp_utc"]) >= cutoff.strftime("%Y-%m-%d %H:%M:%S") else r for r in rows]
            save_trading_day(conn, cid, day, revised)
        assert mf.build(conn, [day], {day: snap}).rows[day] == before
        rows = originals[NQ_CID]                                  # a bar before the cutoff revised
        revised = [dict(r, close=float(r["close"]) * 1.01) if str(r["timestamp_utc"]) ==
                   (cutoff - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S") else r for r in rows]
        save_trading_day(conn, NQ_CID, day, revised)
        assert mf.build(conn, [day], {day: snap}).rows[day]["nq_ret_on"] != before["nq_ret_on"]
        again, created = ml_service.issue(conn, snap, ml.PROFILE, "historical_replay", ml.ML_MULTI_VERSION)
        assert not created and again["run_id"] == run["run_id"] and again["evidence"] == run["evidence"]
    finally:
        for cid, rows in originals.items():
            save_trading_day(conn, cid, day, rows)
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("UPDATE journal.forecast_evidence SET evidence = '{}'")


@needs_db
def test_receipt_times_decide_what_was_known_stale_versus_delayed(journal):
    """As of an issue time, only received bars count: ES's last bars not yet received but an earlier one within its
    5-minute limit - delayed, used; nothing received within it - stale, missing, and the multi-instrument model
    (ES is required) abstains while the NQ-only one is issued."""
    from forecaster import ml_features as mf
    from forecaster import ml_service
    from tests.synthetic import ES_CID, NQ_CID
    conn = journal["conn"]
    day = journal["pool"][-1]
    snap = _snap(conn, day)
    cutoff = cal.ny_instant(date.fromisoformat(day), defs.PROFILES[ml.PROFILE].cutoff)
    as_of = cutoff + timedelta(minutes=10)
    with conn:                     # live receipts: NQ 9 minutes late; ES 11, its last 3 bars 31 minutes late
        conn.execute("UPDATE bars SET first_stored_at = timestamp_utc + interval '9 minutes' "
                     "WHERE contract_id = %s AND trading_day = %s;", (NQ_CID, day))
        conn.execute("UPDATE bars SET first_stored_at = timestamp_utc + interval '11 minutes' "
                     "WHERE contract_id = %s AND trading_day = %s;", (ES_CID, day))
        conn.execute("UPDATE bars SET first_stored_at = timestamp_utc + interval '31 minutes' WHERE contract_id = %s "
                     "AND trading_day = %s AND timestamp_utc >= %s;", (ES_CID, day, cutoff - timedelta(minutes=3)))
    fs = mf.build(conn, [day], {day: snap}, as_of=as_of)
    es = fs.instruments[day]["ES"]
    assert fs.instruments[day]["NQ"]["status"] == "observed"             # received live, in time
    assert es["status"] == "delayed" and es["age_min"] == 3 and not math.isnan(fs.rows[day]["es_ret_on"])
    with conn:
        conn.execute("UPDATE bars SET first_stored_at = timestamp_utc + interval '31 minutes' "
                     "WHERE contract_id = %s AND trading_day = %s AND timestamp_utc >= %s;",
                     (ES_CID, day, cutoff - timedelta(minutes=8)))
    fs = mf.build(conn, [day], {day: snap}, as_of=as_of)
    assert fs.instruments[day]["ES"]["status"] == "stale" and math.isnan(fs.rows[day]["es_ret_on"])
    assert mf.required_missing(fs, day) == ["ES"]
    multi = ml_service.predict(conn, snap, ml.ML_MULTI_VERSION, as_of=as_of, fs=fs)
    nq = ml_service.predict(conn, snap, ml.ML_NQ_VERSION, as_of=as_of, fs=fs)
    assert multi["status"] == "unavailable" and "ES stale" in multi["reason"] and "abstains" in multi["reason"]
    assert nq["status"] == "issued" and sum(mm_fraction(v) for v in nq["distribution"].values()) == 1
    assert not ml_service.context_ready(conn, snap, cutoff + timedelta(minutes=12))    # still waiting for ES
    assert ml_service.context_ready(conn, snap, cutoff + timedelta(minutes=ml.MAX_WAIT_MINUTES))


def mm_fraction(v):
    from forecaster.ml_model import fraction
    return fraction(v)


@needs_db
def test_ml_runs_are_issued_after_the_training_window_only_once_with_exact_probabilities(journal):
    from database import journal_store as store
    from forecaster import ml_service
    conn = journal["conn"]
    # issued now: the synthetic bars were stored today, so an earlier issue time would rightly find them unreceived
    first = ml_service.issue_pending(conn, ml.PROFILE)
    assert ml_service.issue_pending(conn, ml.PROFILE)["runs"] == 0                # idempotent
    after = [d for d in journal["pool"] if d > journal["until"]]
    runs = store.list_forecast_runs(conn, after[0], after[-1], ml.PROFILE, "historical_replay")
    ml_runs = [r for r in runs if r["algorithm_version"] in ml.ALGORITHMS]
    assert {r["session_date"] for r in ml_runs} == set(after) and first["deliveries"] >= len(after) - 2
    run = store.get_forecast_run(conn, next(r["run_id"] for r in ml_runs if r["lifecycle_status"] == "issued"))
    p = run["predictions"][ml.TARGET]
    assert p["estimation_status"] == "model" and sum(Fraction(v) for v in p["distribution"].values()) == 1
    assert p["status"] in ("predicted", "ambiguous_prediction") and run["schema_version"] == ml.SCHEMA_VERSION
    others = [v for t, v in run["predictions"].items() if t != ml.TARGET]
    assert all(v["status"] == "unavailable" and "B's forecast stands" in v["reason"] for v in others)
    ev = run["evidence"]
    assert ev["model"]["sha256"] == journal["manifests"][run["algorithm_version"]]["sha256"]
    assert set(ev["features"]) == set(ml.CONFIGS[ml.CONFIG_OF[run["algorithm_version"]]])
    # a session inside the training window: unavailable, never an in-sample forecast
    inside = ml_service.issue(conn, _snap(conn, journal["until"]), ml.PROFILE, "historical_replay",
                              ml.ML_NQ_VERSION)[0]
    assert inside["lifecycle_status"] == "unavailable" and "in-sample" in inside["failure_reason"]


@needs_db
def test_a_tampered_or_unregistered_artifact_never_forecasts(journal, tmp_path):
    import shutil
    from forecaster import ml_model as mm
    from forecaster import ml_service
    conn = journal["conn"]
    root = str(tmp_path / "models")
    shutil.copytree(journal["root"], root)
    path = os.path.join(root, ml.ML_NQ_VERSION, "model.joblib")
    with open(path, "ab") as f:
        f.write(b"tampered")
    with pytest.raises(mm.ArtifactError, match="differs from its manifest"):
        mm.load(ml.ML_NQ_VERSION, root)
    snap = _snap(conn, journal["pool"][-20])                  # no stored ML run: the artifact has to be loaded
    run, created = ml_service.issue(conn, snap, ml.PROFILE, "historical_replay", ml.ML_NQ_VERSION, root)
    assert created and run["lifecycle_status"] == "failed" and "differs from its manifest" in run["failure_reason"]
    assert run["predictions"] == {} or all(p["status"] == "unavailable" for p in run["predictions"].values())
    with pytest.raises(mm.ArtifactError, match="registered"):
        mm.load(ml.ML_NQ_VERSION, journal["root"], expected_sha256="0" * 64)


@needs_db
def test_the_forecast_in_force_falls_back_explicitly(journal, monkeypatch):
    """Promoted ML models are delivered first; the multi-instrument model unavailable -> the NQ-only one, with the
    fallback recorded; every ML forecast unavailable -> B. While experimental, B is delivered whatever they say."""
    from database import journal_store as store
    from forecaster import ml_service
    conn = journal["conn"]
    day = [d for d in journal["pool"] if d > journal["until"]][0]
    exp = ml_service.record_delivery(conn, day, ml.PROFILE, "historical_replay")
    assert exp["algorithm"] == fc.BASELINE_VERSION and exp["reason"] == "B (analogues)"
    monkeypatch.setitem(ml.STATUS, ml.ML_MULTI_VERSION, "production")
    monkeypatch.setitem(ml.STATUS, ml.ML_NQ_VERSION, "production")
    runs = {r["algorithm_version"]: r for r in store.list_forecast_runs(conn, day, day, ml.PROFILE,
                                                                        "historical_replay")}
    if runs[ml.ML_MULTI_VERSION]["lifecycle_status"] == "issued":
        got = ml_service.record_delivery(conn, day, ml.PROFILE, "historical_replay")
        assert got["algorithm"] == ml.ML_MULTI_VERSION and got["reason"] == "ML multi-instrument"
    from forecaster.delivery import delivered
    fake = [dict(store.get_forecast_run(conn, runs[ml.ML_MULTI_VERSION]["run_id"]), lifecycle_status="unavailable",
                 failure_reason="required instrument(s) not usable at the cutoff: ES stale")] + \
        [store.get_forecast_run(conn, runs[v]["run_id"]) for v in (ml.ML_NQ_VERSION, fc.BASELINE_VERSION)]
    run, why = delivered(fake, ml.delivery_order(), lambda v: ml.ARM_LABELS[v])
    assert run["algorithm_version"] == ml.ML_NQ_VERSION and "ML multi-instrument unavailable" in why
    run, why = delivered(fake[2:], ml.delivery_order(), lambda v: ml.ARM_LABELS[v])
    assert run["algorithm_version"] == fc.BASELINE_VERSION and "ML NQ-only not run" in why
    rec = store.latest_delivery(conn, day, ml.PROFILE, "historical_replay")
    assert rec["delivery_order"] and rec["decided_at"]
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM journal.forecast_deliveries")


@needs_db
def test_the_summary_states_measurements_and_sources_only(journal):
    from forecaster import forecast_summary as fsum
    conn = journal["conn"]
    day = [d for d in journal["pool"] if d > journal["until"]][1]
    s = fsum.build(conn, day)
    assert s["in_force"]["label"] == "B (analogues)" and set(s["arms"]) >= {"A", "B", "N", "M", "P"}
    m = s["arms"]["M"]
    if m["status"] == "issued":
        assert set(m["minus_A_pp"]) == set(m["minus_B_pp"]) == set(ml.CLASSES) and m["experimental"]
        assert m["model"]["sha256"] and s["instruments"]["ES"]["status"] == "reconstructed"
    assert s["sources"]["direction_15m"]["source"] == "B (analogues)"     # experimental ML is never the source
    assert s["sources"]["close_direction_rth"]["source"] == "B (analogues)"
    text = " ".join(fsum.lines(s))
    assert "In force: B (analogues)" in text and "Sources:" in text and "Observed to the cutoff" in text
    assert not re.search(r"\b(because|due to|driven by|suggests|likely because|confiden)", text, re.I)


@needs_db
def test_the_development_comparison_runs_on_identical_opportunities(journal, monkeypatch):
    from forecaster import ml_eval
    conn = journal["conn"]
    monkeypatch.setitem(ml.DEV_EVALUATION, "initial_train_sessions", 50)
    monkeypatch.setitem(ml.DEV_EVALUATION, "test_block_sessions", 20)
    res = ml_eval.run(conn, progress=lambda *a: None)
    assert set(res["summary"]) == {"A", "B", "nq_only/logit", "nq_only/gbm", "multi/logit", "multi/gbm",
                                   "pooled/logit", "pooled/gbm"}
    assert all(v["available"] <= v["scheduled"] for v in res["summary"].values())
    assert res["common"] > 0 and all(v["n"] == res["common"] for v in res["pairs"].values())
    for f in res["folds"]:                                   # training ends before the embargo and the test block
        assert f["train"][1] < f["test"][0]
    text = ml_eval.report(res, {"nq_only": "logit", "multi": "logit", "pooled": "gbm"})
    assert "Development data, not a test" in text and "Paired differences" in text


@needs_db
def test_the_forward_evaluation_registers_without_issuing_anything(journal):
    from database import journal_store as store
    from forecaster import experiments as ex
    from forecaster.journal import register
    conn = journal["conn"]
    register(conn)                                           # the ML definitions, from the artifacts' manifests
    for v in ml.ALGORITHMS:
        d = store.get_version(conn, v)["definition"]
        assert d["artifact"]["sha256"] == journal["manifests"][v]["sha256"] and d["status"] == "experimental"
    before = conn.execute("SELECT count(*) FROM journal.forecast_runs").fetchone()[0]
    m = ml.forward_manifest("2026-06-15", "2026-12-31")
    assert ex.register_experiment(conn, m)
    assert m["primary"]["target"] == "direction_15m" and "unhalved" in m["primary"]["metric"]
    assert ["M", "N"] in m["pairs"] and m["arms"]["delivered"]["delivered"] == ml.delivery_order()
    assert conn.execute("SELECT count(*) FROM journal.forecast_runs").fetchone()[0] == before


@needs_db
def test_the_cli_shows_the_summary_and_writes_the_inventory(journal, capsys, tmp_path):
    from scripts.nq_journal import main
    conn = journal["conn"]
    day = [d for d in journal["pool"] if d > journal["until"]][0]
    assert main(["--db", DSN, "summary", "--date", day]) == 0
    assert "In force:" in capsys.readouterr().out
    out = tmp_path / "inventory.md"
    assert main(["--db", DSN, "instrument-inventory", "--since", "2026-01-01", "--out", str(out)]) == 0
    text = out.read_text()
    assert "| NQ |" in text and "| ES |" in text and "**QQQ: excluded.**" not in text   # only stored instruments
    assert "ES: included (required)" in text


@needs_db
def test_a_live_capture_issues_the_ml_forecasts_live_and_b_stays_in_force(journal):
    """A later session captured live on the research profile (a live copy of the last session's snapshot): A and B
    issued in time, the three ML forecasts issued live beside them - unavailable here, saying why (no bars of that
    session in the store) - and B, the forecast in force, recorded as a delivery."""
    from database import journal_store as store
    from database.queries import set_active_contracts
    from forecaster import live_capture as live
    from tests.synthetic import NQ_CID
    conn = journal["conn"]
    day = "2027-06-14"
    with conn:
        conn.execute(
            "INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, snapshot_version, "
            "convention_version, cutoff_at, rth_open_at, data_mode, pit_availability_status, source_payload_hash, "
            "payload, built_at) SELECT gen_random_uuid(), symbol, contract_id, %s::date, snapshot_version, "
            "convention_version, cutoff_at + (%s::date - session_date) * interval '1 day', "
            "rth_open_at + (%s::date - session_date) * interval '1 day', 'live_capture', 'unverified_historical', "
            "'live:ml', payload, clock_timestamp() FROM journal.snapshots WHERE snapshot_id = %s;",
            (day, day, day, _snap(conn, LAST)["snapshot_id"]))
    set_active_contracts(conn, "NQ", {day: NQ_CID}, "test")
    clock = lambda: cal.ny_instant(date.fromisoformat(day), time(9, 29, 5))       # noqa: E731
    result = live.capture(conn, None, day, ml.PROFILE, clock=clock, sleep=lambda s: None)
    ml_runs = [r for r in result["runs"] if r["algorithm"] in ml.ALGORITHMS]
    assert len(ml_runs) == 3 and all(r["experimental"] for r in ml_runs)
    runs = [store.get_forecast_run(conn, r["run_id"]) for r in ml_runs]
    assert all(r["mode"] == "live" and r["lifecycle_status"] == "unavailable" and r["failure_reason"] for r in runs)
    assert result["status"] == "issued" and result["delivered"]["arm"] == "B (analogues)"
    rec = store.latest_delivery(conn, day, ml.PROFILE, "live")
    b = next(r for r in result["runs"] if r["algorithm"] == fc.BASELINE_VERSION)
    assert rec["run_id"] == b["run_id"] and rec["algorithm"] == fc.BASELINE_VERSION
