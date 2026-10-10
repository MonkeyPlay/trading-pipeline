# tests/test_ml_bundle.py
"""
The seven-target ML bundles (contracts/nq_ml_bundle.py, contracts/market_labels.py, forecaster/ml_bundle*.py,
forecaster/market_labels.py) and the per-arm scheduling of forecaster/ml_service.issue_pending.

The pure tests need nothing. The rest build a synthetic journal - NQ, ES and RTY bars (tests/synthetic.py), the NQ
snapshots, outcomes and A/B runs - in a disposable PostgreSQL + TimescaleDB database whose name contains "test"
(TEST_DATABASE_URL), which they RESET, and train the bundles into a temporary directory.
"""

import os
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction

import numpy as np
import pandas as pd
import psycopg
import pytest

from contracts import market_labels as mlab
from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_ml_bundle as mb
from contracts import nq_prompt_v2 as defs
from features import calendar as cal

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'")
UTC = timezone.utc
LAST = "2026-06-12"
COLUMNS = sorted({c for t in mb.TARGETS for c in mb.head_features(t, "M")})


# --------------------------------------------------------------------------
# Pure: the registry, label conventions, masks, vocabularies, structural zeros
# --------------------------------------------------------------------------

def test_the_registry_is_the_forecast_contracts_seven_targets_in_order():
    assert mb.TARGETS == tuple(t for _, t in fc.FORECAST_TARGETS) and len(mb.TARGETS) == 7
    for t in mb.TARGETS:
        assert mb.CLASSES[t] == tuple(defs.TARGETS[t]["labels"])          # the label version's order
    assert mb.CLASSES["first_level_tested"] == tuple(defs.FIRST_LEVEL_CANDIDATES) and \
        "none_tested" not in mb.CLASSES["first_level_tested"]               # an unavailable outcome, not a class
    assert set(mb.DIRECTION) == {"opening_bias_30m", "direction_15m", "close_direction_rth"}
    assert mb.DIRECTION["close_direction_rth"]["threshold"] == "B"
    for t in mb.TARGETS:                                                   # N and P: the same NQ-own information
        n, m, p = (mb.head_features(t, a) for a in ("N", "M", "P"))
        assert n == p and set(n) < set(m) and not set(n) & set(mb.FEATURE_GROUPS["context"]["features"])
    assert set(mb.VERSIONS.values()).isdisjoint(ml.ALGORITHMS) and ml.ARMS == {**fc.ARMS, "N": ml.ML_NQ_VERSION,
                                                                                "M": ml.ML_MULTI_VERSION,
                                                                                "P": ml.ML_POOLED_VERSION}
    assert all(v == "shadow" for st in mb.STATUS.values() for v in st.values())
    assert ml.delivery_order() == [fc.BASELINE_VERSION, fc.PRIOR_VERSION]   # never delivered


def test_market_thresholds_reproduce_nq_and_round_es_rty_to_their_tick():
    rng = np.random.default_rng(4)
    for _ in range(5000):
        atr = Fraction(int(rng.integers(1, 10 ** 6)), int(rng.integers(1, 10 ** 4)))
        assert mlab.threshold_t("NQ", atr) == defs.threshold_t(atr)
        assert mlab.threshold_b("NQ", atr) == defs.threshold_b(atr)
        for m in ("ES", "RTY"):
            t = Fraction(str(mlab.threshold_t(m, atr)))
            inc = Fraction(mlab.MARKETS[m].threshold_increment)
            assert t % inc == 0 and t >= atr / 2 and t - atr / 2 < inc or t == inc
    assert mlab.threshold_t("ES", "13/4") == "1.75" and mlab.threshold_t("RTY", "1.37") == "0.7"
    assert mlab.threshold_t("ES", None) is None


def _payload(schedule="full", T=10, B=20, A="100", prices=None):
    prices = prices or {c: str(100 + i) for i, c in enumerate(mb.CANDIDATES)}
    return {"schedule": {"schedule": schedule}, "thresholds": {"T": T, "B": B, "A": A},
            "first_level_candidates": {"levels": {c: ({"status": "valid", "value": v} if v is not None else
                                                      {"status": "missing", "value": None})
                                                  for c, v in prices.items()}}}


def _entry(d, s, rng, labels, payload=None, measurements=None, candidates=None):
    from forecaster import ml_bundle_features as bf
    return {"date": d, "instrument": s, "payload": payload or _payload(),
            "features": {c: float(rng.normal()) for c in COLUMNS},
            "candidates": candidates or bf.candidate_row(None), "labels": labels,
            "measurements": measurements or {}}


def _days(n, start=date(2020, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _synthetic(n=160, instruments=("NQ",), seed=0, missing=None, rule=None):
    """BundleData with random features and labels of every target drawn per row (``missing``: target -> function of
    the row index giving the reason of a missing label, or None)."""
    from forecaster import ml_bundle_data as bd
    rng = np.random.default_rng(seed)
    days = _days(n)
    entries = []
    for i, d in enumerate(days):
        for s in instruments:
            labels = {}
            for t in mb.TARGETS:
                why = (missing or {}).get(t, lambda i: None)(i)
                lab = None if why else (rule(t, i, s) if rule else mb.CLASSES[t][int(rng.integers(0, len(
                    mb.CLASSES[t])))])
                labels[t] = {"label": lab, "reason": why}
            entries.append(_entry(d, s, rng, labels))
    return bd.assemble(entries, days, {})


def test_eligibility_follows_the_label_version():
    from forecaster import ml_bundle_data as bd
    ok = bd.eligible(_payload())
    assert all(v is None for v in ok.values())
    early = bd.eligible(_payload(schedule="early_close"))
    assert early["session_type_rth"].startswith("shortened_session") and \
        early["close_direction_rth"].startswith("shortened_session")
    assert early["direction_15m"] is None and early["first_level_tested"] is None
    no_t = bd.eligible(_payload(T=None))
    assert all(no_t[t].startswith("missing_threshold") for t in ("opening_bias_30m", "first_move_5m",
                                                                  "opening_type_15m", "direction_15m"))
    assert no_t["close_direction_rth"] is None and no_t["first_level_tested"] is None   # B and the levels suffice
    no_a = bd.eligible(_payload(A=None))
    assert no_a["session_type_rth"].startswith("missing_threshold") and no_a["close_direction_rth"] is None
    prices = {c: str(100 + i) for i, c in enumerate(mb.CANDIDATES)}
    prices["vwap"] = None
    lvl = bd.eligible(_payload(prices=prices))
    assert lvl["first_level_tested"].startswith("missing_reference") and lvl["direction_15m"] is None


def test_training_masks_are_per_target_and_count_every_reason():
    """A missing first-move label (ambiguous intrabar) never drops the row's close-direction label; uncovered
    sessions, none_tested and shortened sessions are excluded with their counts, never relabelled."""
    data = _synthetic(60, missing={"first_move_5m": lambda i: "ambiguous_intrabar" if i % 4 == 0 else None,
                                   "session_type_rth": lambda i: "uncovered" if i % 5 == 0 else None,
                                   "first_level_tested": lambda i: "none_tested" if i % 3 == 0 else None,
                                   "close_direction_rth": lambda i: "shortened_session" if i == 7 else None})
    fm, cd = data.mask("first_move_5m"), data.mask("close_direction_rth")
    assert fm.sum() == 45 and cd.sum() == 59 and (cd & ~fm).sum() == 15     # every ambiguous row keeps it
    cov = data.coverage()
    assert cov["first_move_5m"]["NQ"]["without_label"] == {"ambiguous_intrabar": 15}
    assert cov["session_type_rth"]["NQ"]["without_label"] == {"uncovered": 12}
    assert cov["first_level_tested"]["NQ"]["without_label"] == {"none_tested": 20}
    assert "range_day" not in cov["session_type_rth"]["NQ"]["without_label"]
    assert sum(cov["session_type_rth"]["NQ"]["classes"].values()) == 48


def test_a_class_missing_from_training_keeps_the_fixed_vocabulary_with_a_smoothed_share():
    from forecaster import ml_bundle as mbun
    data = _synthetic(160, rule=lambda t, i, s: mb.CLASSES[t][i % (len(mb.CLASSES[t]) - 1)])   # last class never
    head = mbun.fit_head(data, "opening_type_15m", "N", data.days[:150])
    assert head.status == "trained" and head.unseen == ["range"] and head.class_counts["range"] == 0
    P = head.predict(data.rows.iloc[[-1]])
    assert P.shape == (1, 6) and abs(P.sum() - 1) < 1e-12 and P[0, -1] > 0
    assert P[0, -1] <= mb.SMOOTHING["pseudo_count"] / (head.n + mb.SMOOTHING["pseudo_count"] * 6) + 1e-12
    assert all(s["family"] in mb.FAMILIES for s in head.selection) and head.folds


def test_first_level_candidates_the_contract_rules_out_get_exactly_zero():
    """Candidates at one price are one level named by the precedence: the others there can never be the label, so
    they get probability 0 - exactly 0 in the stored fraction - and the smoothing goes to the possible ones."""
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_features as bf
    prices = {c: str(100 + i) for i, c in enumerate(mb.CANDIDATES)}
    prices["premarket_high"] = prices["on_high"]                 # ON High comes first in the precedence
    payload = {"references": {"cutoff_price": {"status": "valid", "value": "104.5"}},
               "atr": {"two_minute": {"exact": "2"}, "daily": {"exact": "40"}}, **_payload(prices=prices)}
    table = bf.candidate_table(payload)
    assert table["on_high"]["possible"] == 1.0 and table["premarket_high"]["possible"] == 0.0
    assert table["on_high"]["coincident"] == 1.0 and table["vwap"]["dist_atr"] == (106 - 104.5) / 2
    assert table["overnight_open"]["nearest_side"] == 1.0 and table["overnight_open"]["above"] == 1.0
    X = pd.DataFrame([bf.candidate_row(payload)])
    P = mbun.finish("first_level_tested", np.full((1, 10), 0.1), X, n=100)
    j = mb.CLASSES["first_level_tested"].index("premarket_high")
    assert P[0, j] == 0.0 and abs(P.sum() - 1) < 1e-12 and (np.delete(P[0], j) > 0).all()
    ex = mbun.exact(P[0], mb.CLASSES["first_level_tested"])
    assert ex["premarket_high"] == "0/1000000" and sum(Fraction(v) for v in ex.values()) == 1


def test_exact_fractions_never_break_a_tie_or_move_a_structural_zero():
    """A symmetric head's equal bullish and bearish probabilities stay exactly equal (an ambiguous prediction, never a
    class made by rounding); a zero stays zero; the fractions sum to 1 and stay within a millionth or two."""
    from forecaster import ml_bundle as mbun
    cls = mb.CLASSES["direction_15m"]
    tie = mbun.exact([0.4013605442176871, 0.4013605442176871, 0.19727891156462582], cls)
    assert tie["bullish"] == tie["bearish"] and sum(Fraction(v) for v in tie.values()) == 1
    low = mbun.exact([0.1, 0.1, 0.8], cls)
    assert low["bullish"] == low["bearish"] and sum(Fraction(v) for v in low.values()) == 1
    assert mbun.exact([1, 1, 1], cls) == {c: "1/3" for c in cls}          # all tied: exact thirds
    assert mbun.exact([0.5, 0.5, 0.0], cls) == {"bullish": "1/2", "bearish": "1/2", "neutral_band": "0/1000000"}
    rng = np.random.default_rng(0)
    for _ in range(2000):
        k = int(rng.integers(2, 11))
        p = rng.dirichlet(np.ones(k))
        if rng.random() < 0.3:
            p[int(rng.integers(0, k))] = 0.0
        if rng.random() < 0.3 and k >= 3:
            p[1] = p[0]
        p = p / p.sum()
        names = [str(i) for i in range(k)]
        ex = mbun.exact(p, names)
        f = [Fraction(ex[n]) for n in names]
        assert sum(f) == 1 and all(x >= 0 for x in f)
        assert all((f[i] == 0) == (p[i] == 0) for i in range(k))
        assert max(abs(float(f[i]) - p[i]) for i in range(k)) <= 2.5e-6
        top = [i for i in range(k) if p[i] == p.max()]
        assert len({f[i] for i in top}) == 1                    # the top tie (the predicted class) never broken
        assert max(f) == f[top[0]]


def test_a_pooled_head_without_enough_other_market_rows_is_never_shown_as_pooled():
    from forecaster import ml_bundle as mbun
    nq_only = _synthetic(120, instruments=("NQ",))
    head = mbun.fit_head(nq_only, "direction_15m", "P", nq_only.days[:110])
    assert head.status == "unavailable" and "no pooled training" in head.reason
    pooled = _synthetic(120, instruments=("NQ", "ES", "RTY"))
    head = mbun.fit_head(pooled, "direction_15m", "P", pooled.days[:110])
    assert head.status == "trained" and head.rows == {"NQ": 110, "ES": 110, "RTY": 110}
    assert head.n == 110                                         # the smoothing and coverage count NQ's sessions


def test_selection_scores_nq_validation_rows_of_later_dates_only(monkeypatch):
    """The inner folds are by session date: every instrument's training rows precede the embargo and the validation
    dates, and only NQ's validation rows are scored."""
    from forecaster import ml_bundle as mbun
    data = _synthetic(150, instruments=("NQ", "ES", "RTY"))
    seen = []
    real = mbun._fit

    def spy(fam, params, target, arm, d, idx):
        seen.append(("fit", sorted({d.dates[i] for i in idx})))
        return real(fam, params, target, arm, d, idx)
    monkeypatch.setattr(mbun, "_fit", spy)
    real_brier = mbun.brier
    monkeypatch.setattr(mbun, "brier", lambda P, y, classes: real_brier(P, y, classes))
    chosen, scores, folds, _ = mbun.select(data, "direction_15m", "P", data.days[:140])
    assert chosen is not None and folds
    for f in folds:
        assert f["train"][1] < f["embargo"][0] < f["test"][0]
    ends = {f["train"][1] for f in folds}
    assert len(folds) == 3 and {max(ds) for kind, ds in seen} == ends   # each fit ends at its fold's training end,
    # before that fold's embargo and validation dates
    assert all(s["n"] == sum(f["test"][2] for f in folds) for s in scores if "n" in s)   # NQ rows only


def test_every_target_gets_a_prediction_row_and_unavailable_ones_say_why():
    from forecaster import ml_bundle_service as mbs
    result = {"status": "issued", "reason": None, "heads": {
        "direction_15m": {"status": "predicted", "distribution": {"bullish": "1/2", "bearish": "1/4",
                                                                  "neutral_band": "1/4"}},
        "first_move_5m": {"status": "predicted", "distribution": {"up_first": "2/5", "down_first": "2/5",
                                                                  "neither": "1/5"}},
        "session_type_rth": {"status": "unavailable", "reason": "shortened_session: early close"}}}
    rows = {p["target"]: p for p in mbs._predictions(result, {"heads": {"direction_15m": {
        "n": 90, "training": {"sessions": 100}}}})}
    assert list(rows) == [t for _, t in fc.FORECAST_TARGETS]
    assert rows["direction_15m"]["status"] == "predicted" and rows["direction_15m"]["predicted_label"] == "bullish"
    assert rows["direction_15m"]["eligible"] == 90 and rows["direction_15m"]["without_label"] == 10
    assert rows["first_move_5m"]["status"] == "ambiguous_prediction" and "tie" in rows["first_move_5m"]["reason"]
    assert rows["session_type_rth"]["reason"].startswith("shortened_session")
    assert all(p["reason"] for p in rows.values() if p["status"] != "predicted")
    assert all(p["estimation_status"] == ("model" if p["distribution"] else "none") for p in rows.values())


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
def world(tmp_path_factory):
    """A synthetic journal with NQ, ES and RTY bars, the v1 models and the three bundles trained on all but the last
    8 sessions into a temporary directory."""
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_data as bd
    from forecaster import ml_model as mm
    from forecaster import ml_train
    from forecaster.journal import catch_up, register, take_snapshot
    from tests.synthetic import ES_CID, NQ_CID, RTY_CID, make_instrument, make_market
    reset_database(DSN)
    conn = get_db_connection(DSN)
    bars, sessions = make_market(last_day=LAST, n_sessions=110)
    bars[RTY_CID] = make_instrument(sessions, RTY_CID, 2100.0, 0.0005, seed=11)
    days = [s.session_date.isoformat() for s in sessions]
    for sym, cid, local in (("NQ", NQ_CID, "NQU6"), ("ES", ES_CID, "ESU6"), ("RTY", RTY_CID, "RTYU6")):
        upsert_contract(conn, cid, sym, "20260918", "CME", local_symbol=local)
        _store(conn, bars[cid], cid)
        set_active_contracts(conn, sym, {d: cid for d in days}, "test")
    root = str(tmp_path_factory.mktemp("models"))
    mp = pytest.MonkeyPatch()
    mp.setattr(mm, "MODELS_DIR", root)
    register(conn)
    take_snapshot(conn, days[30], ml.PROFILE)
    catch_up(conn, ml.PROFILE, now=cal.ny_instant(date.fromisoformat(LAST), time(20, 0)))
    pool = [str(s["session_date"]) for s in ml_train.pool(conn)]
    until = pool[-9]
    legacy = ml_train.train(conn, until=until)
    data = bd.build(conn)
    bundles = mbun.train(conn, data=data, until=until, progress=lambda *a: None)
    register(conn)
    yield {"conn": conn, "root": root, "pool": pool, "until": until, "data": data, "bundles": bundles,
           "legacy": legacy}
    mp.undo()
    conn.close()


def _snap(conn, day):
    from forecaster.ml_train import pool
    return next(s for s in pool(conn) if str(s["session_date"]) == day)


@needs_db
def test_each_market_is_labelled_from_its_own_snapshot_and_nq_matches_the_stored_labels(world):
    """Parity: the parameterised NQ path (its convention's thresholds, compute_outcome on its bars) gives exactly the
    stored outcome of every target. ES and RTY get their own thresholds, levels and outcomes - never NQ's."""
    from database import journal_store as store
    from forecaster import market_labels as mlf
    conn, data = world["conn"], world["data"]
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    checked = 0
    for d in world["pool"][-20:]:
        snap = _snap(conn, d)
        mine = mlf.with_thresholds(snap, "NQ")
        assert mine["payload"]["thresholds"] == snap["payload"]["thresholds"]
        stored = history[snap["snapshot_id"]][-1]["labels"]
        got = mlf.market_outcome(conn, mine)["labels"]
        assert {t: got[t]["label"] for t in defs.TARGETS} == {t: stored[t]["label"] for t in defs.TARGETS}
        assert {t: got[t]["reason"] for t in defs.TARGETS} == {t: stored[t]["reason"] for t in defs.TARGETS}
        checked += 1
    assert checked == 20
    es = mlf.market_snapshot(conn, "ES", world["pool"][-1])
    nq = _snap(conn, world["pool"][-1])
    assert es["payload"]["identity"]["symbol"] == "ES" and es["contract_id"] != nq["contract_id"]
    t = Fraction(str(es["payload"]["thresholds"]["T"]))
    assert t % Fraction("0.25") == 0 and es["payload"]["thresholds"] != nq["payload"]["thresholds"]
    assert es["payload"]["first_level_candidates"] != nq["payload"]["first_level_candidates"]
    cov = data.coverage()
    for target in mb.TARGETS:                                     # per-target, per-instrument usable counts
        assert set(cov[target]) == {"NQ", "ES", "RTY"}
        assert all(c["rows"] == c["labelled"] + sum(c["without_label"].values()) for c in cov[target].values())
    nq_rows, es_rows = data.instruments == "NQ", data.instruments == "ES"
    same = sum(a == b for a, b in zip(data.labels["direction_15m"][nq_rows], data.labels["direction_15m"][es_rows]))
    assert same < nq_rows.sum()                                   # ES's labels are its own


@needs_db
def test_bundles_are_trained_with_a_complete_manifest_and_one_head_per_target(world):
    from forecaster import ml_bundle as mbun
    for arm, v in mb.VERSIONS.items():
        m = world["bundles"][v]
        assert m["targets"] == list(mb.TARGETS) and m["classes"] == {t: list(c) for t, c in mb.CLASSES.items()}
        assert m["feature_version"] == mb.FEATURE_VERSION and m["label_version"] == defs.LABEL_VERSION
        assert m["market_labels"] == mlab.VERSION and len(m["sha256"]) == 64 and len(m["data_digest"]) == 64
        assert m["training"]["to"] == world["until"] and m["software"]["scikit-learn"]
        assert set(m["heads"]) == set(mb.TARGETS)
        for t, h in m["heads"].items():
            assert h["status"] in ("trained", "unavailable") and (h["status"] == "trained") == (h["reason"] is None)
            assert set(h["class_counts"]) == set(mb.CLASSES[t])
            if h["status"] == "trained":
                assert h["family"] in mb.FAMILIES and h["selection"] and h["folds"] and h["n"] >= 40
        art = mbun.load(v)
        assert art["arm"] == arm and set(art["heads"]) == set(mb.TARGETS)
    p = world["bundles"][mb.VERSIONS["P"]]["heads"]["direction_15m"]
    assert p["pooled"] and p["rows"]["ES"] > 0 and p["rows"]["RTY"] > 0
    with pytest.raises(Exception):                                # never overwritten
        mbun.save(mbun.fit_bundle(world["data"], "N", world["data"].days[:60]), world["data"])


@needs_db
def test_every_bundle_issues_all_seven_targets_with_reasons_and_the_frozen_first_level_price(world):
    from database import journal_store as store
    from forecaster import ml_bundle_service as mbs
    conn = world["conn"]
    day = world["pool"][-1]
    snap = _snap(conn, day)
    for arm, v in mb.VERSIONS.items():
        run, created = mbs.issue(conn, snap, ml.PROFILE, "historical_replay", v)
        assert created and run["algorithm_version"] == v and run["schema_version"] == mb.SCHEMA_VERSION
        assert set(run["predictions"]) == set(mb.TARGETS)
        assert mbs.issue(conn, snap, ml.PROFILE, "historical_replay", v)[1] is False          # idempotent
        for t, p in run["predictions"].items():
            if p["distribution"]:
                assert set(p["distribution"]) == set(mb.CLASSES[t])
                assert sum(Fraction(x) for x in p["distribution"].values()) == 1
                assert p["estimation_status"] == "model"
            else:
                assert p["status"] == "unavailable" and p["reason"]
        assert run["lifecycle_status"] == ("issued" if any(p["distribution"] for p in run["predictions"].values())
                                           else "unavailable")
        heads = run["outputs"]["heads"]
        assert set(heads) == set(mb.TARGETS) and run["outputs"]["status"] == "shadow"
        fl = run["predictions"]["first_level_tested"]
        if fl["status"] == "predicted":
            level = snap["payload"]["first_level_candidates"]["levels"][fl["predicted_label"]]
            assert run["outputs"]["first_level_price"] == level["value"]
        else:
            assert run["outputs"]["first_level_price"] is None
        assert run["evidence"]["model"]["sha256"] == world["bundles"][v]["sha256"]
    inside = mbs.issue(conn, _snap(conn, world["until"]), ml.PROFILE, "historical_replay", mb.VERSIONS["N"])[0]
    assert inside["lifecycle_status"] == "unavailable" and "in-sample" in inside["failure_reason"]
    assert all("in-sample" in p["reason"] for p in inside["predictions"].values())
    assert store.first_delivery(conn, day, ml.PROFILE, "historical_replay") is None or \
        store.first_delivery(conn, day, ml.PROFILE, "historical_replay")["algorithm"] not in mb.VERSIONS.values()


@needs_db
def test_one_heads_failure_or_missing_input_never_suppresses_another(world, monkeypatch):
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_service as mbs
    conn = world["conn"]
    snap = _snap(conn, world["pool"][-2])
    v = mb.VERSIONS["N"]
    base = mbs.predict(conn, snap, v)
    trained = [t for t in mb.TARGETS if base["heads"][t]["status"] == "predicted"]
    assert "direction_15m" in trained
    real = mbun.Head.predict

    def boom(self, X):
        if self.target == "direction_15m":
            raise RuntimeError("planted failure")
        return real(self, X)
    monkeypatch.setattr(mbun.Head, "predict", boom)
    res = mbs.predict(conn, snap, v)
    assert res["heads"]["direction_15m"]["status"] == "unavailable" and "planted failure" in \
        res["heads"]["direction_15m"]["reason"]
    assert [t for t in trained if res["heads"][t]["status"] == "predicted"] == [t for t in trained
                                                                                if t != "direction_15m"]
    monkeypatch.setattr(mbun.Head, "predict", real)
    # no T frozen: the T targets are unavailable, the close direction (B) and the first level still predict
    no_t = {**snap, "payload": {**snap["payload"], "thresholds": {**snap["payload"]["thresholds"], "T": None}}}
    res = mbs.predict(conn, no_t, v)
    for t in ("opening_bias_30m", "first_move_5m", "opening_type_15m", "direction_15m"):
        assert res["heads"][t]["reason"].startswith("missing_threshold")
    for t in ("close_direction_rth", "first_level_tested"):
        assert res["heads"][t]["status"] == base["heads"][t]["status"]
    # a first-level reference unavailable: only the first level is
    levels = {**snap["payload"]["first_level_candidates"]["levels"], "vwap": {"status": "incomplete", "value": None}}
    no_ref = {**snap, "payload": {**snap["payload"], "first_level_candidates": {
        **snap["payload"]["first_level_candidates"], "levels": levels}}}
    res = mbs.predict(conn, no_ref, v)
    assert res["heads"]["first_level_tested"]["reason"].startswith("missing_reference")
    assert res["heads"]["direction_15m"]["status"] == base["heads"]["direction_15m"]["status"]


@needs_db
def test_n_and_p_never_read_the_context_markets_current_data(world):
    """Removing the context instruments' bars of the session changes nothing in N's and P's forecasts (bundles and
    v1 models); M's features change, and without ES (required) M abstains."""
    from forecaster import ml_bundle_service as mbs
    from forecaster import ml_service
    conn = world["conn"]
    day = world["pool"][-3]
    snap = _snap(conn, day)
    before = {v: mbs.predict(conn, snap, v) for v in mb.VERSIONS.values()}
    legacy_before = {v: ml_service.predict(conn, snap, v, fs=ml_service.features(conn, snap, None,
                                                                                 ml_service.symbols_of(v)))
                     for v in (ml.ML_NQ_VERSION, ml.ML_POOLED_VERSION)}
    legacy_all = {v: ml_service.predict(conn, snap, v, fs=ml_service.features(conn, snap)) for v in legacy_before}
    for v in legacy_before:                                   # the NQ-only feature set leaves v1 forecasts unchanged
        assert legacy_before[v]["probabilities"] == legacy_all[v]["probabilities"]
        assert legacy_before[v]["features"] == legacy_all[v]["features"]
    with conn:
        conn.execute("CREATE TEMP TABLE saved_bars AS SELECT * FROM bars WHERE trading_day = %s AND contract_id IN "
                     "(SELECT contract_id FROM contracts WHERE symbol IN ('ES', 'RTY'));", (day,))
        conn.execute("DELETE FROM bars WHERE trading_day = %s AND contract_id IN (SELECT contract_id FROM contracts "
                     "WHERE symbol IN ('ES', 'RTY'));", (day,))
    try:
        after = {v: mbs.predict(conn, snap, v) for v in mb.VERSIONS.values()}
        for arm in ("N", "P"):
            v = mb.VERSIONS[arm]
            assert {t: h.get("probabilities") for t, h in after[v]["heads"].items()} == \
                {t: h.get("probabilities") for t, h in before[v]["heads"].items()}
        for v in legacy_before:
            assert ml_service.predict(conn, snap, v, fs=ml_service.features(conn, snap, None, ml_service.symbols_of(
                v)))["probabilities"] == legacy_before[v]["probabilities"]
        m = after[mb.VERSIONS["M"]]
        assert m["status"] == "unavailable" and "abstains" in m["reason"]
        assert all(h["status"] == "unavailable" for h in m["heads"].values())
    finally:
        with conn:
            conn.execute("INSERT INTO bars SELECT * FROM saved_bars;")
            conn.execute("DROP TABLE saved_bars;")


@needs_db
def test_auto_issues_n_and_p_at_once_m_after_its_context_and_never_delivers_a_bundle(world):
    from database import journal_store as store
    from forecaster import ml_service
    conn = world["conn"]
    day = [d for d in world["pool"] if d > world["until"]][0]
    snap = _snap(conn, day)
    cutoff = snap["cutoff_at"] if isinstance(snap["cutoff_at"], datetime) else \
        datetime.fromisoformat(str(snap["cutoff_at"]))
    cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
    early = ml_service.issue_pending(conn, ml.PROFILE, now=cutoff + timedelta(minutes=3))
    runs = {r["algorithm_version"] for r in store.list_forecast_runs(conn, day, day, ml.PROFILE, "historical_replay")}
    assert {mb.VERSIONS["N"], mb.VERSIONS["P"], ml.ML_NQ_VERSION, ml.ML_POOLED_VERSION} <= runs
    assert mb.VERSIONS["M"] not in runs and ml.ML_MULTI_VERSION not in runs and early["waiting"] >= 1
    assert early["errors"] == 0
    late = ml_service.issue_pending(conn, ml.PROFILE, now=cutoff + timedelta(minutes=ml.MAX_WAIT_MINUTES + 1))
    runs = {r["algorithm_version"] for r in store.list_forecast_runs(conn, day, day, ml.PROFILE, "historical_replay")}
    assert {mb.VERSIONS["M"], ml.ML_MULTI_VERSION} <= runs and late["errors"] == 0
    d = store.first_delivery(conn, day, ml.PROFILE, "historical_replay")
    assert d is not None and d["algorithm"] == fc.BASELINE_VERSION          # B in force; bundles are shadow


@needs_db
def test_an_arm_that_raises_is_logged_and_the_others_still_issue(world, monkeypatch):
    from database import journal_store as store
    from forecaster import ml_bundle_service as mbs
    from forecaster import ml_service
    conn = world["conn"]
    day = [d for d in world["pool"] if d > world["until"]][1]
    real = mbs.issue

    def broken(conn_, snap, profile, mode, version, *a, **k):
        if version == mb.VERSIONS["P"]:
            raise RuntimeError("planted outage")
        return real(conn_, snap, profile, mode, version, *a, **k)
    monkeypatch.setattr(mbs, "issue", broken)
    res = ml_service.issue_pending(conn, ml.PROFILE)
    assert res["errors"] >= 1
    runs = {r["algorithm_version"] for r in store.list_forecast_runs(conn, day, day, ml.PROFILE, "historical_replay")}
    assert mb.VERSIONS["N"] in runs and mb.VERSIONS["M"] in runs and ml.ML_NQ_VERSION in runs
    assert mb.VERSIONS["P"] not in runs


@needs_db
def test_a_tampered_or_unregistered_bundle_never_forecasts(world, tmp_path):
    import shutil
    from forecaster import ml_bundle as mbun
    from forecaster import ml_bundle_service as mbs
    from forecaster import ml_model as mm
    conn = world["conn"]
    v = mb.VERSIONS["N"]
    root = str(tmp_path / "copy")
    shutil.copytree(mbun.artifact_dir(v), os.path.join(root, v))
    with open(os.path.join(root, v, "bundle.joblib"), "ab") as f:
        f.write(b"tampered")
    with pytest.raises(mm.ArtifactError):
        mbun.load(v, root)
    snap = _snap(conn, world["pool"][-20])                    # no stored bundle run: the artifact has to be loaded
    run, created = mbs.issue(conn, snap, ml.PROFILE, "historical_replay", v, root)
    assert created and run["lifecycle_status"] == "failed" and "differs from its manifest" in run["failure_reason"]
    assert set(run["predictions"]) == set(mb.TARGETS)
    assert all(p["status"] == "unavailable" and p["distribution"] is None for p in run["predictions"].values())
    with pytest.raises(mm.ArtifactError):                             # the registered hash, not just the manifest
        mbun.load(v, expected_sha256="0" * 64)


@needs_db
def test_the_bundle_evaluation_freezes_predictions_then_scores_every_target_against_a_and_b(world, monkeypatch,
                                                                                            tmp_path):
    from forecaster import ml_bundle_eval as ev
    conn, data = world["conn"], world["data"]
    monkeypatch.setitem(ml.DEV_EVALUATION, "initial_train_sessions", 45)     # the synthetic pool has 80 sessions
    monkeypatch.setitem(ml.DEV_EVALUATION, "test_block_sessions", 18)
    out = str(tmp_path / "eval")
    man = ev.predict(data, out, jobs=2, progress=lambda *a: None)
    assert set(man["files"]) == {"predictions.jsonl", "folds.json"} and man["protocol"]["claims"] == 21
    assert len(man["folds"]) == 2 and all(f["embargo"] for f in man["folds"])
    res = ev.score(conn, out, str(tmp_path / "report.md"), progress=lambda *a: None)
    assert set(res["targets"]) == set(mb.TARGETS)
    for t, e in res["targets"].items():
        assert {"N - A", "N - B", "M - A", "P - B", "B - A"} <= set(e["pairs"]) and set(e["claims"]) == {"N", "M", "P"}
        assert {"M - N", "P - P_nqonly", "P - P_complete"} == set(e["ablations"])
        assert e["coverage"]["scheduled"] == res["scheduled"] and e["coverage"]["labelled"] <= res["scheduled"]
        if t in mb.DIRECTION:
            assert "N - N_sym" in e["pairs"] and "A_sym - A" in e["pairs"]
    text = (tmp_path / "report.md").read_text()
    assert "Results matrix" in text and "Development data, not a test" in text and "99.76" in text
    with open(os.path.join(out, "predictions.jsonl"), "a") as f:
        f.write("{}\n")
    with pytest.raises(RuntimeError, match="changed after the predict stage"):
        ev.score(conn, out, None, progress=lambda *a: None)


@needs_db
def test_the_summary_and_grading_show_every_bundle_target_apart_from_the_forecast_in_force(world):
    from database import journal_store as store
    from forecaster import forecast_summary as fsum
    from forecaster import ml_bundle_service as mbs
    from forecaster.grading import compare, current_runs
    conn = world["conn"]
    day = world["pool"][-1]
    snap = _snap(conn, day)
    for v in mb.VERSIONS.values():
        mbs.issue(conn, snap, ml.PROFILE, "historical_replay", v)
    s = fsum.build(conn, day)
    assert set(s["bundles"]) == {"N7", "M7", "P7"}
    for arm, b in s["bundles"].items():
        assert b["shadow"] and set(b["targets"]) == set(mb.TARGETS) and b["sha256"]
        for t, e in b["targets"].items():
            if e["probabilities"]:
                assert set(e["probabilities"]) == set(mb.CLASSES[t]) and e["label_coverage"]["labelled"] > 0
                if "A" in s["arms"] and (store.get_forecast_run(conn, next(
                        r["run_id"] for r in store.list_forecast_runs(conn, day, day, ml.PROFILE, "historical_replay")
                        if r["algorithm_version"] == fc.PRIOR_VERSION))["predictions"].get(t) or {}).get(
                        "distribution"):
                    assert set(e["minus_A_pp"]) == set(mb.CLASSES[t])
            else:
                assert e["reason"]
    assert all(v["source"] in (None, "B (analogues)", "A (frequencies)") for v in s["sources"].values())
    text = " ".join(fsum.lines(s))
    assert "N7 ML NQ-only (7 targets, shadow) [shadow: model output only, never in force]" in text
    runs = store.list_forecast_runs(conn, day, day, ml.PROFILE)
    cur = current_runs(runs)
    issued = {mb.display_arm_of(r["algorithm_version"]) for r in runs if r["lifecycle_status"] == "issued"}
    assert {"N7", "M7", "P7"} & issued <= set(cur)
    full = {a: store.get_forecast_run(conn, r["run_id"]) for a, r in cur.items()}
    cmp = compare(full)
    assert all(set(cmp["arms"][a]["cells"]) == set(mb.TARGETS) for a in cmp["arms"])


@needs_db
def test_the_release_check_verifies_bundle_artifacts_and_registered_definitions(world, capsys, tmp_path, monkeypatch):
    import importlib.util
    import shutil
    from forecaster import ml_model as mm
    spec = importlib.util.spec_from_file_location("release_check", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "release_check.py"))
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    assert rc.check_artifacts(DSN) == 0
    out = capsys.readouterr().out
    assert all(f"{v}: " in out and "= registered" in out for v in mb.VERSIONS.values())
    root = str(tmp_path / "models")
    shutil.copytree(world["root"], root)
    with open(os.path.join(root, mb.VERSIONS["M"], "bundle.joblib"), "ab") as f:
        f.write(b"tampered")
    monkeypatch.setattr(mm, "MODELS_DIR", root)
    assert rc.check_artifacts(DSN) == 1
    assert "nq_ml_bundle_multi_v1: bundle.joblib hashes to" in capsys.readouterr().out
