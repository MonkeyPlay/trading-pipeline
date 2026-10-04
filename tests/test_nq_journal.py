# tests/test_nq_journal.py
"""
Evidence snapshots (features/nq_evidence.py), the journal store (migration 0009,
database/journal_store.py) and the CLI (scripts/nq_journal.py).

The pure tests need nothing; the rest need a disposable PostgreSQL + TimescaleDB
database whose name contains "test", which they RESET (see
tests/test_dashboard_data.py for TEST_DATABASE_URL).
"""

import hashlib
import json
import os
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pandas as pd
import psycopg
import pytest

from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from features.nq_evidence import aggregate, build_snapshot, dec, wilder_atr

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DSN or "test" not in (psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") or ""),
    reason="set TEST_DATABASE_URL to a disposable database whose name contains 'test'",
)
UTC = timezone.utc
DAY, PREV = "2026-06-12", "2026-06-11"


# --------------------------------------------------------------------------
# Pure
# --------------------------------------------------------------------------

def _minute_bars(day, first_et, last_et):
    t, end = cal.ny_instant(date.fromisoformat(day), first_et), cal.ny_instant(date.fromisoformat(day), last_et)
    out = []
    while t <= end:
        out.append((t, 1.0, 2.0, 0.5, 1.5, 1))
        t += timedelta(minutes=1)
    return out


def test_latest_complete_buckets_at_each_cutoff():
    bars = _minute_bars(DAY, time(9, 0), time(9, 28))
    research = cal.ny_instant(date.fromisoformat(DAY), time(9, 29))
    last2 = aggregate(bars, 2, research)[-1]
    assert last2[0].astimezone(cal.NY_TZ).strftime("%H:%M") == "09:26" and last2[6] == 2     # 09:26-09:28
    operational = cal.ny_instant(date.fromisoformat(DAY), time(9, 27))
    ops = [b for b in bars if b[0] + timedelta(minutes=1) <= operational]
    assert ops[-1][0].astimezone(cal.NY_TZ).strftime("%H:%M") == "09:26"                        # last 1m bar
    assert aggregate(ops, 2, operational)[-1][0].astimezone(cal.NY_TZ).strftime("%H:%M") == "09:24"
    assert aggregate(bars, 15, research)[-1][0].astimezone(cal.NY_TZ).strftime("%H:%M") == "09:00"


def test_wilder_atr_is_exact():
    assert wilder_atr([Decimal(1)] * 14 + [Decimal(15)]) == 2
    assert wilder_atr([Decimal(1)] * 14 + [Decimal(2)]) == Fraction(15, 14)         # no decimal rounding


@pytest.mark.parametrize("day, utc_hour", [("2026-03-06", 14),     # EST
                                           ("2026-03-10", 13),     # EDT; the UK is still on GMT until 03-29
                                           ("2026-03-30", 13),
                                           ("2026-11-02", 14)])    # back on EST
def test_profile_cutoffs_follow_new_york_dst(day, utc_hour):
    for name, p in defs.PROFILES.items():
        cutoff = cal.ny_instant(date.fromisoformat(day), p.cutoff)
        assert (cutoff.hour, cutoff.minute) == (utc_hour, p.cutoff.minute)


def test_definitions_hash_stably_and_differ_by_profile():
    a, b = defs.snapshot_record("research_0929"), defs.snapshot_record("operational_0927")
    assert a["definition_hash"] != b["definition_hash"] and a["version"] != b["version"]
    assert defs.label_record() == defs.label_record()
    assert defs.canonical_json({"x": Decimal("1.50"), "y": Fraction(1, 3)}) == '{"x":"1.50","y":"1/3"}'


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
def market():
    from database.connection import get_db_connection, reset_database
    from database.queries import set_active_contracts, upsert_contract
    from tests.synthetic import ES_CID, NQ_CID, make_market
    reset_database(DSN)
    conn = get_db_connection(DSN)
    bars, sessions = make_market(last_day=DAY, n_sessions=80)
    upsert_contract(conn, NQ_CID, "NQ", "20260918", "CME", local_symbol="NQU6")
    upsert_contract(conn, ES_CID, "ES", "20260918", "CME", local_symbol="ESU6")
    _store(conn, bars[NQ_CID], NQ_CID)
    _store(conn, bars[ES_CID], ES_CID)
    days = [s.session_date.isoformat() for s in sessions]
    set_active_contracts(conn, "NQ", {d: NQ_CID for d in days}, "test")
    set_active_contracts(conn, "ES", {d: ES_CID for d in days}, "test")
    with conn:
        conn.execute("INSERT INTO economic_event_coverage (source, covered_from, covered_to) "
                     "VALUES ('bls', '2026-06-01', '2026-06-30');")
        conn.execute("INSERT INTO economic_events (source, event_key, scheduled_at, name, tier) "
                     "VALUES ('bls', 'cpi:2026-05', %s, 'Consumer Price Index', 'high');",
                     (cal.ny_instant(date(2026, 6, 12), time(8, 30)),))
    yield conn, bars, sessions
    conn.close()


def _expected_daily_atr(nq: pd.DataFrame, before_day: str) -> Fraction:
    """The daily ATR recomputed from the synthetic bars: RTH H / L / C, Wilder over the last 70 TRs."""
    ny = nq["bar_start_at"].dt.tz_convert("America/New_York")
    rth = nq[(ny.dt.strftime("%H:%M") >= "09:30") & (ny.dt.strftime("%H:%M") < "16:00")].assign(
        day=ny.dt.strftime("%Y-%m-%d"))
    daily = rth.groupby("day").agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    daily = daily[daily.index < before_day]
    trs = [max(dec(h), dec(pc)) - min(dec(lo), dec(pc))
           for (h, lo), pc in zip(daily[["high", "low"]].values[1:], daily["close"].values[:-1])]
    return wilder_atr(trs[-70:])


@needs_db
def test_research_snapshot_contents(market):
    conn, bars, _ = market
    from tests.synthetic import NQ_CID
    snap = build_snapshot(conn, DAY, "research_0929")
    p = snap.payload
    assert snap.cutoff_at == datetime(2026, 6, 12, 13, 29, tzinfo=UTC) and snap.cutoff_at < snap.rth_open_at
    assert (snap.data_mode, snap.pit_availability_status) == ("historical_reconstruction", "unverified_historical")
    assert p["cutoff"]["last_completed_bar"] == {"bar_start_at": "2026-06-12T13:28:00Z",
                                                 "bar_end_at": "2026-06-12T13:29:00Z"}
    assert p["bars"]["1m"][-1][0] == "2026-06-12T13:28:00Z" and p["bars"]["2m"][-1][0] == "2026-06-12T13:26:00Z"
    refs = p["references"]
    assert {k: v["status"] for k, v in refs.items()} == {
        "overnight_open": "valid", "on_high": "valid", "on_low": "valid", "cutoff_price": "valid",
        "price_at_0929": "not_observed", "premarket_high": "not_defined", "premarket_low": "not_defined",
        "prev_rth_high": "valid", "prev_rth_low": "valid", "prev_rth_close": "valid"}
    nq = bars[NQ_CID]
    prev_rth = nq[(nq["bar_start_at"] >= cal.session(PREV).rth_open_at)
                  & (nq["bar_start_at"] < cal.session(PREV).scheduled_close_at)]
    assert refs["prev_rth_close"]["value"] == dec(prev_rth["close"].iloc[-1])
    assert refs["prev_rth_high"]["value"] == dec(prev_rth["high"].max())
    assert len(p["previous_rth_bars"]["1m"]) == 390

    daily = p["atr"]["daily"]
    assert daily["status"] == "valid" and daily["true_ranges"] == 70
    assert daily["exact"] == _expected_daily_atr(nq, DAY)
    assert p["thresholds"]["B"] == defs.threshold_b(daily["exact"])
    assert p["thresholds"]["T"] == defs.threshold_t(p["atr"]["two_minute"]["exact"])
    assert p["atr"]["two_minute"]["last_bucket_end"] == "2026-06-12T13:28:00Z"

    prior = p["prior_sessions"]
    assert prior["contract_id"] == NQ_CID and [x["status"] for x in prior["sessions"]] == ["valid"] * 5
    first = cal.sessions_before(DAY, 5)[0]
    rth = nq[(nq["bar_start_at"] >= first.rth_open_at) & (nq["bar_start_at"] < first.scheduled_close_at)]
    assert prior["sessions"][0]["session_date"] == first.session_date.isoformat()
    assert prior["sessions"][0]["open"] == dec(rth["open"].iloc[0]) and prior["sessions"][0]["high"] == dec(rth["high"].max())
    assert prior["sessions"][-1]["session_date"] == PREV

    assert p["events"]["covered_sources"] == ["bls"]
    assert p["events"]["events"][0]["time_et"] == "08:30" and p["events"]["events"][0]["before_cutoff"]
    assert p["intermarket"]["es"]["status"] == "valid" and p["intermarket"]["es"]["age_minutes"] == 0
    assert p["intermarket"]["vix"]["status"] == "no_contract"


@needs_db
def test_operational_snapshot_is_a_different_version_with_earlier_bars(market):
    conn, _, _ = market
    snap = build_snapshot(conn, DAY, "operational_0927")
    assert snap.snapshot_version == defs.PROFILES["operational_0927"].snapshot_version \
        != defs.PROFILES["research_0929"].snapshot_version
    assert snap.payload["bars"]["1m"][-1][0] == "2026-06-12T13:26:00Z"
    assert snap.payload["bars"]["2m"][-1][0] == "2026-06-12T13:24:00Z"


@needs_db
def test_bars_after_the_cutoff_cannot_change_the_snapshot(market):
    conn, _, _ = market
    from database.queries import get_day_bars, save_trading_day
    from tests.synthetic import NQ_CID
    before = build_snapshot(conn, DAY)
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, DAY)]
    cutoff = before.cutoff_at.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        if r["timestamp_utc"] >= cutoff:
            r["close"] += 300.0
            r["high"] += 300.0
    save_trading_day(conn, NQ_CID, DAY, rows)
    assert build_snapshot(conn, DAY).source_payload_hash == before.source_payload_hash
    for r in rows:                                     # a pre-cutoff change does change it
        if r["timestamp_utc"] == "2026-06-12 13:00:00":
            r["close"] = r["high"] = r["high"] + 1.0
    save_trading_day(conn, NQ_CID, DAY, rows)
    assert build_snapshot(conn, DAY).source_payload_hash != before.source_payload_hash


@needs_db
def test_store_is_versioned_idempotent_and_append_only(market):
    conn, _, _ = market
    from database import journal_store as store
    for rec in defs.all_records():
        store.register_version(conn, rec)
    assert store.register_version(conn, defs.label_record()) is False
    changed = dict(defs.label_record(), definition_hash="different")
    with pytest.raises(store.VersionConflict):
        store.register_version(conn, changed)

    snap = build_snapshot(conn, PREV)
    sid, created = store.save_snapshot(conn, snap)
    assert created and store.save_snapshot(conn, snap) == (sid, False)
    stored = store.get_snapshot(conn, sid)
    assert hashlib.sha256(defs.canonical_json(stored["payload"]).encode()).hexdigest() == snap.source_payload_hash
    for sql in ("UPDATE journal.snapshots SET data_mode = 'live_capture'", "DELETE FROM journal.snapshots",
                "TRUNCATE journal.snapshots CASCADE", "DELETE FROM journal.definition_versions"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)
    late = replace(snap, cutoff_at=snap.rth_open_at, source_payload_hash="late")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.save_snapshot(conn, late)
    claimed = replace(snap, pit_availability_status="verified", source_payload_hash="claimed")
    with pytest.raises(psycopg.errors.CheckViolation):                 # only a live capture can be verified
        store.save_snapshot(conn, claimed)


@needs_db
def test_outcomes_are_revisioned_and_checked_against_the_vocabulary(market):
    conn, _, _ = market
    from database import journal_store as store
    from database.queries import get_day_bars, save_trading_day
    from forecaster import labels_prompt_v2 as labels
    from tests.synthetic import NQ_CID
    for rec in defs.all_records():
        store.register_version(conn, rec)
    sid, _ = store.save_snapshot(conn, build_snapshot(conn, PREV))
    snap = store.get_snapshot(conn, sid)
    out = labels.compute_outcome(snap, labels.load_realised_bars(conn, snap))
    # the frozen cutoff VWAP, from the stored snapshot's archive, against the raw overnight bars
    nq, s = market[1][NQ_CID], cal.session(PREV)
    on = nq[(nq["bar_start_at"] >= s.overnight_start_at) & (nq["bar_start_at"] < s.cutoff_at)]
    pv = sum((Fraction(dec(h)) + Fraction(dec(lo)) + Fraction(dec(c))) / 3 * int(v)
             for h, lo, c, v in on[["high", "low", "close", "volume"]].values)
    expected = pv / int(on["volume"].sum())
    assert Decimal(out["measurements"]["vwap"]) == (Decimal(expected.numerator) / Decimal(expected.denominator)
                                                    ).quantize(Decimal("0.000001"))
    assert set(out["labels"]) == set(defs.TARGETS)
    assert store.save_outcome(conn, sid, defs.LABEL_VERSION, out) == (1, True)
    assert store.save_outcome(conn, sid, defs.LABEL_VERSION, out) == (1, False)

    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, PREV)]
    for r in rows:                                     # the vendor revises the closing minute
        if r["timestamp_utc"] == "2026-06-11 19:59:00":
            r["close"] += 0.25
            r["high"] = max(r["high"], r["close"])
    save_trading_day(conn, NQ_CID, PREV, rows)
    # the snapshot of PREV is unchanged (its inputs end before 09:29), the outcome is not
    assert build_snapshot(conn, PREV).source_payload_hash == snap["source_payload_hash"]
    revised = labels.compute_outcome(snap, labels.load_realised_bars(conn, snap))
    assert store.save_outcome(conn, sid, defs.LABEL_VERSION, revised) == (2, True)
    assert store.latest_outcome(conn, sid, defs.LABEL_VERSION)["outcome_revision"] == 2

    bad = {k: dict(v) for k, v in revised["labels"].items()}
    bad["first_move_5m"] = {"label": "sideways", "reason": None, "detail": None}
    with pytest.raises(psycopg.Error, match="not in the vocabulary"):
        store.save_outcome(conn, sid, defs.LABEL_VERSION, dict(revised, labels=bad))
    bad["first_move_5m"] = {"label": None, "reason": None, "detail": None}
    with pytest.raises(psycopg.Error, match="registered reason"):
        store.save_outcome(conn, sid, defs.LABEL_VERSION, dict(revised, labels=bad))
    del bad["first_move_5m"]
    with pytest.raises(psycopg.Error, match="exactly those"):
        store.save_outcome(conn, sid, defs.LABEL_VERSION, dict(revised, labels=bad))
    view = conn.execute("SELECT count(*) FROM journal.outcome_labels WHERE snapshot_id = %s "
                        "AND outcome_revision = 2;", (sid,)).fetchone()[0]
    assert view == len(defs.TARGETS)


@needs_db
def test_cli_backfill_and_show(market, capsys):
    from scripts.nq_journal import main
    assert main(["--db", DSN, "backfill", "--date", "2026-06-10"]) == 0
    assert main(["--db", DSN, "show", "--date", "2026-06-10"]) == 0
    printed = capsys.readouterr().out
    assert "Realised First Move" in printed and defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version in printed
    assert main(["--db", DSN, "show", "--date", "2026-06-10", "--profile", "operational_0927"]) == 1


@needs_db
def test_review_set_cli_and_append_only_verdicts(market, capsys):
    from database import journal_store as store
    from scripts.nq_journal import main
    conn = market[0]
    assert main(["--db", DSN, "review-set", "--name", "t_review", "--size", "2"]) == 0
    assert main(["--db", DSN, "review-set", "--name", "t_review"]) == 1            # immutable
    members = store.review_members(conn, "t_review")
    assert [m["position"] for m in members] == [1, 2] and all(m["reasons"] for m in members)
    sid = members[0]["snapshot_id"]
    store.save_verdicts(conn, "t_review", sid, 1, [
        {"field": "Realised First Move", "shown_value": "Up", "verdict": "disagree", "note": "both in one bar"},
        {"field": "RTH Close", "shown_value": "100.00", "verdict": "agree"}])
    store.save_verdicts(conn, "t_review", sid, 1, [
        {"field": "Realised First Move", "shown_value": "Up", "verdict": "agree", "note": ""}])   # a re-review
    latest = {v["field"]: v for v in store.latest_verdicts(conn, "t_review")}
    assert latest["Realised First Move"]["verdict"] == "agree" and latest["RTH Close"]["verdict"] == "agree"
    for sql in ("UPDATE journal.review_verdicts SET verdict = 'agree'", "DELETE FROM journal.review_members"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.save_verdicts(conn, "t_review", sid, 1, [{"field": "x", "shown_value": "y", "verdict": "maybe"}])
    capsys.readouterr()
    assert main(["--db", DSN, "review-report", "--name", "t_review"]) == 0
    assert "1 of 2 sessions reviewed" in capsys.readouterr().out


@needs_db
def test_catch_up_takes_every_final_session_once(market):
    from database import journal_store as store
    from forecaster.journal import catch_up
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    stored = lambda: {str(s["session_date"]): s for s in store.list_snapshots(conn, "2000-01-01", DAY, version)}
    had = stored()
    first = min(had)
    final = cal.ny_instant(date.fromisoformat(DAY), time(18, 0))      # two hours after DAY's close

    catch_up(conn, now=final - timedelta(minutes=1))                     # DAY is not final yet
    earlier = {s.session_date.isoformat() for s in cal.sessions_between(first, PREV)}
    assert set(stored()) == set(had) | earlier

    result = catch_up(conn, now=final)
    snaps = stored()
    assert set(snaps) == earlier | {DAY} and result["failed"] == []
    assert result["snapshots"] == (0 if DAY in had else 1)
    assert all(store.latest_outcome(conn, s["snapshot_id"], defs.LABEL_VERSION) for s in snaps.values())
    assert all(snaps[d]["snapshot_id"] == s["snapshot_id"] for d, s in had.items())   # stored ones stay frozen
    assert catch_up(conn, now=final)["snapshots"] == 0                  # nothing taken twice


@needs_db
def test_rules_annotations_are_stored_once(market):
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster.structure_rules import annotate
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    snap = store.list_snapshots(conn, DAY, DAY, version)[0]
    stored = store.latest_annotation(conn, snap["snapshot_id"], pre.RULES_PROTOCOL_VERSION)   # catch-up wrote it
    assert stored is not None and stored["integrity_status"] == "ok" and set(stored["fields"]) == set(pre.FIELDS)
    out = annotate(snap)
    assert store.save_annotation(conn, snap["snapshot_id"], out) == (stored["annotation_id"], False)
    with pytest.raises(store.VersionConflict):
        store.save_annotation(conn, snap["snapshot_id"], {**out, "output_hash": "changed rules"})
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM journal.structure_annotations;")


@needs_db
def test_analogue_sets_are_stored_once_per_pool_and_outcomes(market):
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster.journal import match
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    snaps = store.list_snapshots(conn, "2000-01-01", DAY, version)
    target = snaps[-1]
    aset = store.latest_analogue_set(conn, target["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION)
    assert aset is not None and aset["pool_size"] == len(snaps) - 1                 # catch-up made it
    assert 1 <= len(aset["members"]) <= pre.TOP_ANALOGUES
    assert all(m["session_date"] < DAY and float(m["comparable_weight"]) >= 75 for m in aset["members"])
    assert [m["rank"] for m in aset["members"]] == list(range(1, len(aset["members"]) + 1))
    assert set(aset["outcome_summary"]["targets"]) == set(pre.OUTCOME_TARGETS)
    assert match(conn) == 0                                                          # nothing changed: nothing new
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM journal.analogue_members;")


class _Usage:
    def to_json(self):
        return '{"input_tokens": 1000, "output_tokens": 500}'


class _Message:
    def __init__(self, answer, model, stop_reason="end_turn"):
        self.content = [type("Text", (), {"type": "text", "text": json.dumps(answer)})()]
        self.model, self.stop_reason, self.usage, self.stop_details = model, stop_reason, _Usage(), None


class _Client:
    def __init__(self, message):
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return message
        self.beta = type("Beta", (), {"messages": _Messages()})()


@needs_db
def test_claude_annotation_attempts_are_kept_and_only_valid_ones_stored(market):
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster import structure_llm as llm
    from tests.test_structure_llm import answer_from_rules
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    snap = store.list_snapshots(conn, DAY, DAY, version)[0]
    answer = answer_from_rules(snap)
    attempts = lambda: conn.execute("SELECT status, model, error FROM journal.annotation_attempts "
                                    "ORDER BY finished_at;").fetchall()

    fallback = llm.annotate_live(conn, _Client(_Message(answer, "claude-opus-4-8")), snap)
    assert fallback["status"] == "invalid" and "fallback" in fallback["error"]
    assert store.latest_annotation(conn, snap["snapshot_id"], pre.LLM_PROTOCOL_VERSION) is None
    refused = llm.annotate_live(conn, _Client(_Message({}, pre.LLM_MODEL, stop_reason="refusal")), snap)
    assert refused["status"] == "refused"

    client = _Client(_Message(answer, pre.LLM_MODEL))
    ok = llm.annotate_live(conn, client, snap)
    assert ok["status"] == "ok" and client.calls[0]["fallbacks"] == "default"
    assert client.calls[0]["betas"] == [llm.FALLBACK_BETA]
    stored = store.latest_annotation(conn, snap["snapshot_id"], pre.LLM_PROTOCOL_VERSION)
    assert stored["annotator"] == "llm" and stored["model"] == pre.LLM_MODEL
    assert [r["status"] for r in attempts()] == ["invalid", "refused", "ok"]


@needs_db
def test_annotation_review_set_cli(market, capsys):
    from database import journal_store as store
    from scripts.nq_journal import main
    conn = market[0]
    assert main(["--db", DSN, "annotation-review-set", "--name", "t_pre", "--size", "2"]) == 0
    assert main(["--db", DSN, "annotation-review-set", "--name", "t_pre"]) == 1      # immutable
    members = store.annotation_review_members(conn, "t_pre")
    assert len(members) == 2 and all(m["reasons"] for m in members)
    store.save_annotation_verdicts(conn, "t_pre", members[0]["snapshot_id"], members[0]["annotation_id"], [
        {"field": "Overnight Structure", "shown_value": "Uptrend", "verdict": "disagree", "note": "a V"}])
    capsys.readouterr()
    assert main(["--db", DSN, "annotation-review-report", "--name", "t_pre"]) == 0
    out = capsys.readouterr().out
    assert "1 of 2 sessions reviewed" in out and "disagree (shown 'Uptrend') - a V" in out
    assert main(["--db", DSN, "analogues", "--date", DAY]) == 0
    assert "outcomes hidden" in capsys.readouterr().out


def test_earnings_rows_from_edgar_filings():
    from database.earnings import covered_to, earnings_rows
    filings = {"form": ["8-K", "4", "8-K", "8-K"], "items": ["2.02,9.01", "", "7.01", "2.02"],
               "acceptanceDateTime": ["2026-07-30T20:30:28.000Z", "2026-07-29T21:00:00.000Z",
                                      "2026-07-01T12:00:00.000Z", "2025-05-01T20:30:21.000Z"],
               "accessionNumber": ["a1", "a2", "a3", "a4"]}
    rows = earnings_rows("AAPL", filings, date(2025, 6, 1))
    assert [(r["event_key"], r["scheduled_at"], r["source"]) for r in rows] == [
        ("AAPL:a1", datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC), "sec_earnings")]   # 16:30 ET
    assert covered_to(datetime(2026, 10, 2, 13, 28, tzinfo=UTC)) == date(2026, 10, 1)   # 09:28 ET: before the cutoff
    assert covered_to(datetime(2026, 10, 2, 13, 29, tzinfo=UTC)) == date(2026, 10, 2)


@needs_db
def test_snapshot_events_run_from_the_previous_close_and_earnings_stop_at_the_cutoff(market):
    from database.earnings import load
    conn = market[0]
    at = lambda day, hhmm: cal.ny_instant(date.fromisoformat(day), time(*map(int, hhmm.split(":"))))
    rows = [{"source": "sec_earnings", "event_key": key, "scheduled_at": at(day, hhmm), "name": f"{key} earnings",
             "tier": "moderate", "country": "US"}
            for key, day, hhmm in (("AAPL:prev-evening", PREV, "16:30"), ("NVDA:after-cutoff", DAY, "16:05"),
                                   ("MSFT:before-close", PREV, "15:00"))]
    assert load(conn, rows, at(DAY, "18:00"), since=date(2026, 6, 1)) == {"events": 3, "coverage": 1}
    events = build_snapshot(conn, DAY).payload["events"]
    assert "sec_earnings" in events["covered_sources"]
    assert [e["event_key"] for e in events["events"] if e["source"] == "sec_earnings"] == ["AAPL:prev-evening"]
    assert load(conn, rows, at(DAY, "18:00"), since=date(2026, 6, 1)) == {"events": 0, "coverage": 0}


@needs_db
def test_a_roll_day_takes_every_reference_from_the_new_contract(market):
    """Last in the module: it adds a December contract and rolls DAY onto it, then restores the roll."""
    conn, bars, sessions = market
    from database.queries import set_active_contracts, upsert_contract
    from tests.synthetic import NQ_CID
    dec_cid = 301
    upsert_contract(conn, dec_cid, "NQ", "20261218", "CME", local_symbol="NQZ6")
    nq = bars[NQ_CID]
    warm = nq[nq["bar_start_at"] >= sessions[-4].overnight_start_at]      # warm-up sessions and DAY itself
    dec_bars = warm.assign(**{c: warm[c] + 50.0 for c in ("open", "high", "low", "close")})
    _store(conn, dec_bars, dec_cid)
    set_active_contracts(conn, "NQ", {DAY: dec_cid}, "roll")
    try:
        snap = build_snapshot(conn, DAY)
        assert snap.contract_id == dec_cid and snap.payload["identity"]["local_symbol"] == "NQZ6"
        # every reference from the December bars alone (earlier tests revised some September bars)
        p, d = cal.session(PREV), cal.session(DAY)
        t = dec_bars["bar_start_at"]
        prev_rth = dec_bars[(t >= p.rth_open_at) & (t < p.scheduled_close_at)]
        on = dec_bars[(t >= d.overnight_start_at) & (t < snap.cutoff_at)]
        expected = {"prev_rth_close": prev_rth["close"].iloc[-1], "prev_rth_high": prev_rth["high"].max(),
                    "on_high": on["high"].max(), "on_low": on["low"].min(), "overnight_open": on["open"].iloc[0],
                    "cutoff_price": on["close"].iloc[-1]}
        refs = snap.payload["references"]
        for name, value in expected.items():
            assert refs[name]["value"] == dec(value), name
        # earlier sessions' true ranges stay on the contract that was active on each of them
        assert {row[1] for row in snap.payload["daily_atr_inputs"]} == {NQ_CID}
        # the five prior sessions stay on the snapshot contract: those it holds no bars for are incomplete
        prior = snap.payload["prior_sessions"]
        assert prior["contract_id"] == dec_cid
        assert [x["status"] for x in prior["sessions"]] == ["incomplete", "incomplete", "valid", "valid", "valid"]
        assert prior["sessions"][-1]["high"] == dec(prev_rth["high"].max())
    finally:
        set_active_contracts(conn, "NQ", {DAY: NQ_CID}, "test")
