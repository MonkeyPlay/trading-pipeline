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
from features.nq_evidence import SnapshotError, aggregate, build_snapshot, dec, wilder_atr

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


def iso_utc(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    assert all(len(r) == 8 and r[7] is True for tf in ("2m", "5m", "15m") for r in p["bars"][tf])
    assert p["bars"]["coverage"]["complete"] is True and p["bars"]["coverage"]["duplicates"] == 0
    refs = p["references"]
    assert {k: v["status"] for k, v in refs.items()} == {
        "overnight_open": "valid", "on_high": "valid", "on_low": "valid", "cutoff_price": "valid",
        "price_at_0929": "not_observed", "premarket_high": "valid", "premarket_low": "valid", "vwap": "valid",
        "prev_rth_high": "valid", "prev_rth_low": "valid", "prev_rth_close": "valid"}
    nq = bars[NQ_CID]
    prev_rth = nq[(nq["bar_start_at"] >= cal.session(PREV).rth_open_at)
                  & (nq["bar_start_at"] < cal.session(PREV).scheduled_close_at)]
    assert refs["prev_rth_close"]["value"] == dec(prev_rth["close"].iloc[-1])
    pm = nq[(nq["bar_start_at"] >= cal.ny_instant(date.fromisoformat(DAY), time(8, 0)))
            & (nq["bar_start_at"] < snap.cutoff_at)]                        # 08:00-09:28: complete by 09:29
    assert len(pm) == 89 and refs["premarket_high"]["window_minutes"] == 89
    assert refs["premarket_high"]["value"] == dec(pm["high"].max()) and refs["premarket_low"]["value"] == dec(pm["low"].min())
    assert refs["prev_rth_high"]["value"] == dec(prev_rth["high"].max())
    assert len(p["previous_rth_bars"]["1m"]) == 390

    daily = p["atr"]["daily"]
    assert daily["status"] == "valid" and daily["true_ranges"] == 70
    assert daily["exact"] == _expected_daily_atr(nq, DAY)
    assert p["thresholds"]["B"] == defs.threshold_b(daily["exact"])
    assert p["thresholds"]["T"] == defs.threshold_t(p["atr"]["two_minute"]["exact"])
    assert p["atr"]["two_minute"]["last_bucket_end"] == "2026-06-12T13:28:00Z"

    # the frozen first-level candidates: every reference as stored, the Long MA from the 2m history
    long_ma = p["moving_averages"]["long_ma_at_cutoff"]
    assert long_ma["status"] == "valid" and long_ma["first_bucket"] == iso_utc(cal.session(DAY).overnight_start_at)
    fl = p["first_level_candidates"]
    assert fl["ids"] == list(defs.FIRST_LEVEL_CANDIDATES) and set(fl["levels"]) == set(defs.FIRST_LEVEL_CANDIDATES)
    assert all(c["status"] == "valid" for c in fl["levels"].values())
    assert fl["levels"]["long_ma"]["value"] == long_ma["value"] and fl["levels"]["on_high"]["value"] == refs["on_high"]["value"]
    from forecaster.structure_rules import annotate                  # the annotation's EMA(100) is the same line
    ema = annotate({"payload": p})["measurements"]["moving_averages_2m"]["ema100"]
    assert abs(float(long_ma["value"]) - ema) < 0.01

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


class _RolledBack(Exception):
    """Raised to roll a test's temporary change back."""


@needs_db
def test_catch_up_takes_a_session_once_its_pre_open_is_stored(market):
    """A session in progress gets its snapshot, annotation and analogue set once its bars past the cutoff are
    stored - never from bars fetched before the cutoff - and its outcome once final."""
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster.journal import PREOPEN_SETTLE, catch_up, snapshot_pending
    from tests.synthetic import ES_CID, NQ_CID
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    stored = lambda: {str(s["session_date"]): s for s in store.list_snapshots(conn, "2000-01-01", DAY, version)}
    had = stored()
    assert DAY not in had
    first = min(had)
    d = date.fromisoformat(DAY)
    ready = cal.ny_instant(d, time(9, 29)) + PREOPEN_SETTLE                  # 09:31 ET
    midday, final = cal.ny_instant(d, time(12, 0)), cal.ny_instant(d, time(18, 0))
    earlier = {s.session_date.isoformat() for s in cal.sessions_between(first, PREV)}
    fetched = conn.execute("SELECT contract_id, fetched_at FROM session_days WHERE trading_day = %s;",
                           (DAY,)).fetchall()
    try:
        catch_up(conn, now=ready - timedelta(seconds=1))                    # DAY's pre-open is not over
        assert set(stored()) == set(had) | earlier
        assert snapshot_pending(conn, DAY, now=ready - timedelta(seconds=1)) == \
            "not before 09:31 ET (its pre-open runs to the 09:29 cutoff)"

        # an instrument's bars of the day fetched before the cutoff: never frozen from those
        conn.execute("UPDATE session_days SET fetched_at = %s WHERE trading_day = %s AND contract_id = %s;",
                     (ready - timedelta(hours=3), DAY, ES_CID))
        assert "ES bars were fetched before the 09:29 ET cutoff" in snapshot_pending(conn, DAY, now=midday)
        assert catch_up(conn, now=midday)["snapshots"] == 0 and DAY not in stored()

        # every bar fetched after it: taken mid-session with its annotation and analogues, no outcome yet
        conn.execute("UPDATE session_days SET fetched_at = %s WHERE trading_day = %s;", (ready, DAY))
        # ... but not before NQ's bar closing at the cutoff is stored (a delayed feed: fetched late, stored early)
        with pytest.raises(_RolledBack):
            with conn:
                conn.execute("DELETE FROM bars WHERE trading_day = %s AND contract_id = %s AND timestamp_utc >= %s;",
                             (DAY, NQ_CID, cal.ny_instant(d, time(9, 20))))
                why = snapshot_pending(conn, DAY, now=midday)
                assert "bar closing at the 09:29 ET cutoff is not stored and confirmed yet (newest: 09:19 ET" in why
                raise _RolledBack
        # ... nor while the bar closing at the cutoff is the newest stored: it may still be forming
        with pytest.raises(_RolledBack):
            with conn:
                conn.execute("DELETE FROM bars WHERE trading_day = %s AND contract_id = %s AND timestamp_utc >= %s;",
                             (DAY, NQ_CID, cal.ny_instant(d, time(9, 29))))
                assert "(newest: 09:28 ET; a later bar confirms it)" in snapshot_pending(conn, DAY, now=midday)
                raise _RolledBack
        assert snapshot_pending(conn, DAY, now=midday) is None
        result = catch_up(conn, now=midday)
        snap = stored()[DAY]
        assert result["snapshots"] == 1 and result["failed"] == []
        assert store.latest_annotation(conn, snap["snapshot_id"], pre.RULES_PROTOCOL_VERSION) is not None
        assert store.latest_analogue_set(conn, snap["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                         pre.RULES_PROTOCOL_VERSION) is not None
        assert store.latest_outcome(conn, snap["snapshot_id"], defs.LABEL_VERSION) is None

        # final: the outcome, the snapshot as frozen mid-session, nothing taken twice
        result = catch_up(conn, now=final)
        snaps = stored()
        assert result["snapshots"] == 0 and snaps[DAY]["snapshot_id"] == snap["snapshot_id"]
        assert set(snaps) == earlier | {DAY}
        assert all(store.latest_outcome(conn, s["snapshot_id"], defs.LABEL_VERSION) for s in snaps.values())
        assert all(snaps[d]["snapshot_id"] == s["snapshot_id"] for d, s in had.items())   # stored ones stay frozen
        assert snapshot_pending(conn, "2026-06-13") == "no regular session that day"            # a Saturday
    finally:
        for r in fetched:
            conn.execute("UPDATE session_days SET fetched_at = %s WHERE trading_day = %s AND contract_id = %s;",
                         (r["fetched_at"], DAY, r["contract_id"]))


@needs_db
def test_a_preview_forecasts_from_the_data_so_far_and_stores_nothing(market, tmp_path):
    """Forecast now (forecaster/preview.py): the evidence as of the preview's minute, the same pipeline in memory,
    nothing in the journal - and with the whole pre-open the official forecast."""
    from database import journal_store as store
    from forecaster import preview as pv
    from forecaster.forecast_service import forecast_session
    from contracts import nq_forecast as fc
    from scripts.nq_journal import main
    conn = market[0]
    d = date.fromisoformat(DAY)
    tables = ("snapshots", "structure_annotations", "analogue_sets", "forecast_runs", "forecast_predictions")
    rows = lambda: [conn.execute(f"SELECT count(*) FROM journal.{t};").fetchone()[0] for t in tables]
    before = rows()

    early = pv.preview(conn, now=cal.ny_instant(d, time(7, 45, 30)))
    assert early["status"] == "ok" and early["session_date"] == DAY and not early["complete"]
    assert early["as_of"] == "2026-06-12 11:45:00" and early["data_through"] == "2026-06-12T11:45:00Z"
    payload = early["snapshot"]["payload"]
    assert payload["cutoff"]["preview"] is True and payload["cutoff"]["data_mode"] == "preview"
    assert max(b[0] for b in payload["bars"]["1m"]) == "2026-06-12T11:44:00Z"         # nothing after the minute
    assert payload["references"]["premarket_high"]["detail"].startswith("the premarket starts at 08:00 ET")
    assert [r["algorithm_version"] for r in early["runs"]] == list(fc.RULE_ALGORITHMS)
    assert all(r["lifecycle_status"] in ("preview", "unavailable") and r["run_id"] == "preview" for r in early["runs"])

    whole = pv.preview(conn, now=cal.ny_instant(d, time(9, 30)))                      # the cutoff has passed
    assert whole["complete"] and whole["as_of"] == "2026-06-12 13:29:00"
    for run in whole["runs"]:
        official = forecast_session(conn, DAY, algorithm=run["algorithm_version"])[0]
        assert run["lifecycle_status"] == "preview" and official["lifecycle_status"] == "issued"
        assert {t: (p["predicted_label"], p["distribution"]) for t, p in run["predictions"].items()} == \
            {t: (p["predicted_label"], p["distribution"]) for t, p in official["predictions"].items()}
    assert rows() == before                                                             # nothing stored

    with pytest.raises(ValueError, match="never stored"):
        store.save_snapshot(conn, build_snapshot(conn, DAY, as_of=cal.ny_instant(d, time(8, 0))))
    with pytest.raises(SnapshotError, match="preview cutoff"):
        build_snapshot(conn, DAY, as_of=cal.ny_instant(d, time(9, 45)))                # past the profile's cutoff

    out = tmp_path / "preview.json"
    assert pv.save(early, str(out)) == str(out) and pv.load(str(out)) == early
    assert pv.load(str(tmp_path / "none.json")) is None
    assert main(["--db", DSN, "preview", "--out", str(out)]) == 0 and pv.load(str(out))["kind"] == "forecast_preview"
    assert rows() == before


@needs_db
def test_the_explorer_previews_a_session_as_of_its_last_stored_bar(market):
    """The Session Explorer's analogue preview of a day in progress (forecaster/preview.preview_session): any
    session, as of the end of its last stored NQ bar - never a stale price - by the cutoff."""
    from forecaster import preview as pv
    conn = market[0]
    d = date.fromisoformat(DAY)
    assert pv.latest_as_of(conn, DAY) == cal.ny_instant(d, time(9, 29))          # the whole pre-open is stored
    assert pv.latest_as_of(conn, "2026-06-15") is None                            # no bar of a later session
    now, as_of = cal.ny_instant(d, time(7, 45, 30)), cal.ny_instant(d, time(7, 45))
    assert pv.preview_session(conn, DAY, as_of, now=now) == pv.preview(conn, now=now)


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
    aset = store.latest_analogue_set(conn, target["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                     pre.RULES_PROTOCOL_VERSION)
    assert aset is not None and aset["pool_size"] == len(snaps) - 1                 # catch-up made it
    assert 1 <= len(aset["members"]) <= pre.TOP_ANALOGUES
    assert all(m["session_date"] < DAY and float(m["comparable_weight"]) >= 75 for m in aset["members"])
    assert [m["rank"] for m in aset["members"]] == list(range(1, len(aset["members"]) + 1))
    assert set(aset["outcome_summary"]["targets"]) == set(pre.OUTCOME_TARGETS)
    prior = aset["outcome_summary"]["prior"]
    assert aset["prior_digest"] == prior["digest"] is not None and prior["known_as_of"].startswith("reconstruction")
    assert prior["sessions"] == len(prior["manifest"]) == len(snaps) - 1 and prior["excluded"] == {"not_earlier": 1}
    assert aset["protocol_version"] == pre.RULES_PROTOCOL_VERSION
    assert store.get_analogue_set(conn, aset["set_id"])["members"] == aset["members"]
    assert store.get_analogue_set(conn, "not-a-uuid") is None
    assert match(conn) == 0                                                          # nothing changed: nothing new
    # the lookup is per annotation protocol: no set over another protocol's annotation exists, so none is returned
    assert store.latest_analogue_set(conn, target["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                     "nq_structure_rules_v1") is None
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM journal.analogue_members;")


@needs_db
def test_a_revised_outcome_outside_the_analogues_makes_a_new_set(market):
    """The prior manifest (nq_match_p1_v2): more sessions than analogues, then a revised outcome of one that is not
    an analogue - the prior changes, so the target gets a new set with the same analogues."""
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster.journal import match
    from scripts.nq_journal import main
    conn = market[0]
    assert main(["--db", DSN, "backfill", "--start", "2026-06-01", "--end", "2026-06-09"]) == 0
    assert main(["--db", DSN, "annotate", "--start", "2026-06-01", "--end", "2026-06-09"]) == 0
    match(conn)
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    snaps = store.list_snapshots(conn, "2000-01-01", DAY, version)
    target = snaps[-1]
    lookup = lambda: store.latest_analogue_set(conn, target["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                               pre.RULES_PROTOCOL_VERSION)
    before = lookup()
    members = {m["snapshot_id"] for m in before["members"]}
    other = next(s for s in snaps[:-1] if s["snapshot_id"] not in members)
    last = store.latest_outcome(conn, other["snapshot_id"], defs.LABEL_VERSION)
    assert store.save_outcome(conn, other["snapshot_id"], defs.LABEL_VERSION,
                              {"labels": last["labels"], "measurements": last["measurements"],
                               "digest": "vendor revision"})[1]
    assert match(conn) > 0
    after = lookup()
    assert after["set_id"] != before["set_id"] and after["prior_digest"] != before["prior_digest"]
    assert (after["outcome_digest"], [m["snapshot_id"] for m in after["members"]]) == \
        (before["outcome_digest"], [m["snapshot_id"] for m in before["members"]])
    assert match(conn) == 0


@needs_db
def test_baseline_forecasts_are_issued_once_from_explicit_evidence(market):
    """Stage 3 (guideline revision 2): catch-up issued a historical-replay run per session; a rerun is idempotent, a
    run reproduces from its frozen evidence and reads back into P1's record, bad evidence ids are rejected."""
    from contracts import nq_forecast as fc
    from contracts import nq_preopen as pre
    from database import journal_store as store
    from forecaster.forecast_baseline import baseline_forecast
    from forecaster.forecast_display import percent
    from forecaster.forecast_service import ForecastInputError, forecast_all, forecast_session, run_forecast
    from forecaster.preopen_display import p1_record
    conn = market[0]
    forecast_all(conn)                                   # the revised prior of the previous test: new runs
    assert forecast_all(conn) == 0                       # nothing new on a rerun
    run, created = forecast_session(conn, DAY)
    assert not created and run["lifecycle_status"] == "issued" and run["issued_at"] is not None
    assert run["mode"] == "historical_replay" and run["deadline_at"] is None and run["code_revision"]
    assert set(run["predictions"]) == {t for _, t in fc.FORECAST_TARGETS}
    assert run["evidence"]["data_mode"] == "historical_reconstruction"
    assert run["evidence"]["prior"]["known_as_of"].startswith("reconstruction")

    # reproducible: the baseline of the stored evidence is the stored forecast
    again = baseline_forecast(run["evidence"])
    for target, p in again["predictions"].items():
        stored = run["predictions"][target]
        assert (p["status"], p["predicted_label"], p["distribution"]) == \
            (stored["status"], stored["predicted_label"], stored["distribution"])

    # P1's record from the run and the evidence it was issued on - never another set
    snap, ann = store.get_snapshot(conn, run["snapshot_id"]), store.get_annotation(conn, run["annotation_id"])
    aset = store.get_analogue_set(conn, run["analogue_set_id"])
    provenance, rows = p1_record(snap, ann, aset, run)
    values = {prop: value for prop, value, _ in rows}
    dist = run["predictions"]["direction_15m"]["distribution"]
    assert run["run_id"] in provenance
    assert [values[p] for p in fc.PROBABILITY_PROPERTIES] == [percent(dist[c]) for c in fc.PROBABILITY_PROPERTIES.values()]
    assert values["Forecast Confidence"] == "Unavailable" and values["Historical Analogue Count"] == str(
        len(aset["members"]))
    with pytest.raises(ValueError, match="other evidence"):
        p1_record(snap, ann, None, run)

    # malformed, unknown or mismatched evidence ids: rejected, nothing stored
    runs = lambda: conn.execute("SELECT count(*) FROM journal.forecast_runs;").fetchone()[0]
    before = runs()
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    other = store.list_snapshots(conn, PREV, PREV, version)[0]
    other_ann = store.latest_annotation(conn, other["snapshot_id"], pre.RULES_PROTOCOL_VERSION)
    other_set = store.latest_analogue_set(conn, other["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                          pre.RULES_PROTOCOL_VERSION)
    for ids in [("not-a-uuid", run["annotation_id"], run["analogue_set_id"]),
                (run["snapshot_id"], other_ann["annotation_id"], run["analogue_set_id"]),
                (run["snapshot_id"], run["annotation_id"], other_set["set_id"]),
                (run["snapshot_id"], run["annotation_id"], "0" * 32)]:
        with pytest.raises(ForecastInputError):
            run_forecast(conn, *ids)
    with pytest.raises(ForecastInputError, match="live_capture"):
        run_forecast(conn, run["snapshot_id"], run["annotation_id"], run["analogue_set_id"], mode="live")
    assert runs() == before

    # the database keeps the forecast contract and the history
    with pytest.raises(psycopg.Error, match="not in the vocabulary"):
        conn.execute("INSERT INTO journal.forecast_predictions (run_id, target, status, predicted_label, "
                     "estimation_status, eligible, without_label, prior_sessions, prior_without_label) "
                     "VALUES (%s, 'first_move_5m', 'predicted', 'sideways', 'analogues', 1, 0, 1, 0);",
                     (run["run_id"],))
    with pytest.raises(psycopg.Error, match="sum to"):
        conn.execute("INSERT INTO journal.forecast_predictions (run_id, target, status, predicted_label, "
                     "estimation_status, distribution, eligible, without_label, prior_sessions, prior_without_label) "
                     "VALUES (%s, 'first_move_5m', 'predicted', 'up_first', 'analogues', "
                     "'{\"up_first\": \"1/2\", \"down_first\": \"1/4\", \"neither\": \"1/2\"}', 1, 0, 1, 0);",
                     (run["run_id"],))
    for sql in ("UPDATE journal.forecast_runs SET lifecycle_status = 'issued'", "DELETE FROM journal.forecast_predictions",
                "DELETE FROM journal.forecast_evidence"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)


@needs_db
def test_a_revised_outcome_is_a_new_run_and_the_issued_one_stays(market):
    """A vendor correction changes an analogue's outcome: the next forecast is a new run superseding the old one,
    whose predictions and evidence do not change."""
    from database import journal_store as store
    from forecaster.forecast_service import forecast_all, forecast_session
    from forecaster.journal import match
    conn = market[0]
    old, _ = forecast_session(conn, DAY)
    member = old["evidence"]["members"][0]
    last = store.latest_outcome(conn, member["snapshot_id"], defs.LABEL_VERSION)
    assert store.save_outcome(conn, member["snapshot_id"], defs.LABEL_VERSION,
                              {"labels": last["labels"], "measurements": last["measurements"],
                               "digest": "another vendor revision"})[1]
    assert match(conn) > 0 and forecast_all(conn) > 0
    new, created = forecast_session(conn, DAY)
    assert not created and new["run_id"] != old["run_id"] and new["supersedes_run_id"] == old["run_id"]
    assert new["evidence"]["members"][0]["outcome_revision"] == member["outcome_revision"] + 1
    unchanged = store.get_forecast_run(conn, old["run_id"])
    assert (unchanged["predictions"], unchanged["evidence"], unchanged["issued_at"]) == \
        (old["predictions"], old["evidence"], old["issued_at"])


@needs_db
def test_a_registered_experiment_is_frozen_scored_and_immutable(market, tmp_path):
    """Stage 4: the manifest is registered before any score, cases freeze once, both arms are scored on the
    sessions they share, results and the report are stored, and nothing can be rewritten."""
    from contracts import nq_forecast as fc
    from database import journal_store as store
    from forecaster import experiments as ex
    from forecaster.forecast_service import forecast_all
    from scripts.nq_journal import main
    conn = market[0]
    forecast_all(conn)
    prior_runs = store.issued_runs(conn, "2026-06-01", DAY, defs.DEFAULT_PROFILE, "historical_replay",
                                   fc.PRIOR_VERSION, defs.LABEL_VERSION)
    assert prior_runs and all(r["algorithm_version"] == fc.PRIOR_VERSION for r in prior_runs)

    manifest = ex.experiment_manifest("t_exp", "2026-06-01", DAY)
    assert ex.register_experiment(conn, manifest) and not ex.register_experiment(conn, manifest)
    with pytest.raises(store.VersionConflict):                                   # a manifest never changes
        ex.register_experiment(conn, ex.experiment_manifest("t_exp", "2026-06-02", DAY))
    results = ex.score_experiment(conn, "t_exp")
    cases = store.experiment_cases(conn, "t_exp")
    sessions = cal.sessions_between("2026-06-01", DAY)
    assert len(cases) == 2 * len(sessions) and {c["arm"] for c in cases} == {"A", "B"}
    assert all(c["status"] == "case" and c["outcome_revision"] >= 1 for c in cases)
    assert ex.freeze_cases(conn, "t_exp") == (len(cases), False)                 # frozen once

    first = next(c for c in cases if c["arm"] == "A" and c["session_date"] == sessions[1].session_date.isoformat())
    run = store.get_forecast_run(conn, first["run_id"])
    aset = store.get_analogue_set(conn, run["analogue_set_id"])
    prior = aset["outcome_summary"]["targets"]["direction_15m"]["prior"]
    assert {c: float(Fraction(v)) for c, v in run["predictions"]["direction_15m"]["distribution"].items()} == \
        pytest.approx({c: float(v) for c, v in prior.items()}, abs=1e-6)          # arm A is the prior

    paired = results["primary"]["paired"]["B-A"]
    assert results["primary"]["target"] == "direction_15m" and paired["common"] > 0
    assert set(results["targets"]) == {t for _, t in fc.FORECAST_TARGETS}
    result_id = ex.store_results(conn, "t_exp", results)
    path = ex.write_report(manifest, results, str(tmp_path), result_id)
    report = open(path).read()
    assert "# Experiment t_exp" in report and "development data" in report and "Profitability" in report
    assert os.path.exists(tmp_path / "experiment_t_exp.csv")
    stored = store.experiment_results(conn, "t_exp")[0]
    assert stored["result_id"] == result_id and stored["results"]["manifest_hash"] == results["manifest_hash"]
    assert main(["--db", DSN, "experiment-list"]) == 0
    for sql in ("DELETE FROM journal.experiment_cases", "UPDATE journal.experiment_results SET code_revision = 'x'"):
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute(sql)


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


@needs_db
def test_daily_atr_needs_every_session_of_its_window_complete(market):
    """Strict continuity (nq_conv_v5): one RTH minute missing in one of the 70 sessions leaves the daily ATR
    unavailable - it never skips to an older session. The day is restored afterwards."""
    conn = market[0]
    from database.queries import get_day_bars, save_trading_day
    from tests.synthetic import NQ_CID
    gap_day = cal.sessions_before(DAY, 30)[0]
    rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, gap_day.session_date.isoformat())]
    noon = iso_utc(cal.ny_instant(gap_day.session_date, time(12, 0))).replace("T", " ").rstrip("Z")
    assert any(r["timestamp_utc"] == noon for r in rows)
    try:
        save_trading_day(conn, NQ_CID, gap_day.session_date.isoformat(), [r for r in rows if r["timestamp_utc"] != noon])
        daily = build_snapshot(conn, DAY).payload["atr"]["daily"]
        assert daily["value"] is None and daily["status"] == "incomplete_history"
        assert gap_day.session_date.isoformat() in daily["detail"]
    finally:
        save_trading_day(conn, NQ_CID, gap_day.session_date.isoformat(), rows)
    assert build_snapshot(conn, DAY).payload["atr"]["daily"]["status"] == "valid"


@needs_db
def test_a_live_forecast_is_issued_only_by_the_database_clock(market):
    """Last in the module (it adds live-capture snapshots). The deadline comes from the issue policy and the
    session date, never the client; a run inserted after it is late, with no issued_at."""
    from contracts import nq_forecast as fc
    from database import journal_store as store
    from forecaster.forecast_service import timely, utc
    conn = market[0]
    version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
    source = store.list_snapshots(conn, DAY, DAY, version)[0]

    def live_copy(day, built):
        return str(conn.execute(
            "INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, snapshot_version, "
            "convention_version, cutoff_at, rth_open_at, data_mode, pit_availability_status, source_payload_hash, "
            "payload, built_at) SELECT gen_random_uuid(), symbol, contract_id, %s::date, snapshot_version, "
            "convention_version, cutoff_at + (%s::date - session_date) * interval '1 day', "
            "rth_open_at + (%s::date - session_date) * interval '1 day', 'live_capture', 'unverified_historical', "
            "%s, payload, %s FROM journal.snapshots WHERE snapshot_id = %s RETURNING snapshot_id;",
            (day, day, day, f"live:{day}", built, source["snapshot_id"])).fetchone()[0])

    def issue(snapshot_id, day):
        snap = store.get_snapshot(conn, snapshot_id)
        now = datetime.now(UTC)
        run = {"idempotency_key": f"live-test:{snapshot_id}", "symbol": "NQ", "session_date": day,
               "contract_id": snap["contract_id"], "profile": defs.DEFAULT_PROFILE, "snapshot_id": snapshot_id,
               "annotation_id": None, "analogue_set_id": None, "label_version": defs.LABEL_VERSION,
               "algorithm_version": fc.BASELINE_VERSION, "schema_version": fc.FORECAST_SCHEMA_VERSION,
               "issue_policy": fc.ISSUE_POLICIES["live"], "code_revision": "test", "mode": "live",
               "input_cutoff_at": snap["cutoff_at"], "deadline_at": datetime(2100, 1, 1, tzinfo=UTC),  # ignored
               "generation_started_at": now, "generation_completed_at": now, "lifecycle_status": "issued",
               "supersedes_run_id": None, "failure_reason": None, "evidence_digest": "-", "outputs": {}}
        run_id, _ = store.save_forecast_run(conn, run, {}, [])
        return store.get_forecast_run(conn, run_id)

    # a past session captured before its open: whatever the client sends, the run is late
    past = live_copy(DAY, cal.ny_instant(date.fromisoformat(DAY), time(9, 28)))
    late = issue(past, DAY)
    assert late["lifecycle_status"] == "late" and late["issued_at"] is None and "after the deadline" in late[
        "failure_reason"]
    assert utc(late["deadline_at"]) == cal.ny_instant(date.fromisoformat(DAY), fc.LIVE_DEADLINE_ET)
    assert not timely(late)

    # a future session: issued at the database clock, timely once acknowledged before the deadline
    future = "2099-06-12"
    on_time = issue(live_copy(future, datetime.now(UTC)), future)
    assert on_time["lifecycle_status"] == "issued" and not timely(on_time)
    store.add_forecast_event(conn, on_time["run_id"], "acknowledged")
    assert timely(store.get_forecast_run(conn, on_time["run_id"]))

    # a live run on a historical reconstruction is refused by the database itself
    with pytest.raises(psycopg.Error, match="live_capture"):
        issue(source["snapshot_id"], DAY)


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class _LiveIB:
    """IB in the test: the stored bars of the session that started before the request (the current minute in
    progress, as IB serves it); ``drop`` leaves the bar ending at the cutoff out, as if it had not arrived."""
    def __init__(self, conn, day, drop=False):
        from database.queries import get_day_bars
        from tests.synthetic import NQ_CID
        self.rows = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, day)]
        self.cutoff_bar = (cal.ny_instant(date.fromisoformat(day), time(9, 28))).strftime("%Y-%m-%d %H:%M:%S")
        self.drop, self.calls = drop, 0

    def fetch_historical_bars(self, contract, end, duration, what_to_show=None):
        self.calls += 1
        out = []
        for r in self.rows:
            stamp = str(r["timestamp_utc"])[:19]
            if datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) >= end:
                continue
            if self.drop and stamp == self.cutoff_bar:
                continue
            out.append({"timestamp_utc": stamp, "trading_day": str(r["trading_day"]), "open": r["open"],
                        "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"],
                        "wap": r.get("wap"), "bar_count": r.get("bar_count"), "session_scope": r["session_scope"]})
        return out


@needs_db
def test_a_restarted_live_capture_reuses_its_snapshot_and_a_passed_deadline_is_late(market):
    """Restart recovery (3D): the live snapshot frozen for DAY by the previous test is reused, never rebuilt; the
    capture annotates, matches and issues both arms live - after the deadline, so the database makes them late."""
    from contracts import nq_forecast as fc
    from database import journal_store as store
    from forecaster import live_capture as live
    conn = market[0]
    clock = _Clock(cal.ny_instant(date.fromisoformat(DAY), time(9, 29, 5)))
    ib = _LiveIB(conn, DAY)
    result = live.capture(conn, ib, DAY, clock=clock, sleep=clock.sleep)
    events = [e["event"] for e in store.capture_events(conn, result["capture_id"])]
    assert ib.calls == 0 and events[:4] == ["snapshot_reused", "annotated", "matched", "forecast"]
    assert [(r["algorithm"], r["status"], r["timely"]) for r in result["runs"]] == \
        [(fc.BASELINE_VERSION, "late", False), (fc.PRIOR_VERSION, "late", False)]
    assert result["status"] == "not timely"
    snap = store.get_snapshot(conn, result["snapshot_id"])
    assert snap["data_mode"] == "live_capture"
    capture = store.live_captures(conn, DAY, DAY)[-1]
    assert any("forecast" in line and "late" in line for line in live.timing(conn, capture))


@needs_db
def test_a_live_capture_keeps_receipts_and_verifies_what_was_known(market):
    """The fresh path (3D) on PREV: bars requested after the cutoff, stored with a receipt each; the snapshot built
    from them is verified only when every input was known; the database refuses to store a live snapshot after the
    open; without the bar ending at the cutoff the capture is stale. PREV's bars are restored afterwards."""
    from database import journal_store as store
    from database.queries import get_day_bars, save_trading_day
    from forecaster import live_capture as live
    from tests.synthetic import NQ_CID
    conn = market[0]
    original = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, PREV)]
    try:
        clock = _Clock(cal.ny_instant(date.fromisoformat(PREV), time(9, 28, 50)))
        result = live.capture(conn, _LiveIB(conn, PREV), PREV, clock=clock, sleep=clock.sleep)
        events = [e["event"] for e in store.capture_events(conn, result["capture_id"])]
        assert events == ["bars_requested", "bars_received", "failed"] and result["status"] == "failed"
        assert "CheckViolation" in result["reason"]             # built now, after PREV's open: refused
        receipts = store.capture_receipts(conn, result["capture_id"])
        cutoff_bar = cal.ny_instant(date.fromisoformat(PREV), time(9, 28)).strftime("%Y-%m-%d %H:%M:%S")
        in_progress = cal.ny_instant(date.fromisoformat(PREV), time(9, 29)).strftime("%Y-%m-%d %H:%M:%S")
        assert cutoff_bar in receipts and in_progress not in receipts   # only bars complete when received

        snap = build_snapshot(conn, PREV, live_capture_id=result["capture_id"])
        available = snap.payload["cutoff"]["availability"]
        assert snap.data_mode == "live_capture" and snap.payload["cutoff"]["last_received_bar"]["bar_start_at"] == \
            cutoff_bar.replace(" ", "T") + "Z"
        assert available["overnight_bars"]["received"] == available["overnight_bars"]["used"] > 0
        assert not available["verified"] and snap.pit_availability_status == "unverified_historical"
        assert available["sessions"]["stored_after_cutoff_or_unknown"]   # this test stored them after the cutoff
        with conn:                                                       # as if collected before the cutoff
            conn.execute("UPDATE session_days SET fetched_at = '2026-01-01 00:00:00+00';")
            conn.execute("UPDATE economic_events SET recorded_at = '2026-01-01 00:00:00+00';")
            conn.execute("UPDATE economic_event_coverage SET recorded_at = '2026-01-01 00:00:00+00';")
        snap = build_snapshot(conn, PREV, live_capture_id=result["capture_id"])
        assert snap.pit_availability_status == "verified" and snap.payload["cutoff"]["availability"]["verified"]

        stale_clock = _Clock(cal.ny_instant(date.fromisoformat(PREV), time(9, 28, 50)))
        stale = live.capture(conn, _LiveIB(conn, PREV, drop=True), PREV, clock=stale_clock, sleep=stale_clock.sleep)
        stale_events = [e["event"] for e in store.capture_events(conn, stale["capture_id"])]
        assert stale["status"] == "stale" and stale_events[-1] == "stale" and "bars_received" not in stale_events
        for sql in ("DELETE FROM journal.bar_receipts", "UPDATE journal.live_capture_events SET event = 'failed'"):
            with pytest.raises(psycopg.Error, match="append-only"):
                conn.execute(sql)
    finally:
        save_trading_day(conn, NQ_CID, PREV, original)


# --------------------------------------------------------------------------
# The forecast in force (forecaster/delivery.py) and the 09:15 candidate's capture
# --------------------------------------------------------------------------

def _live_run(algorithm, status="issued", ack_s=-5.0, issued_s=-10.0, mode="live"):
    """A run as stored, its issue and acknowledgement ``*_s`` seconds from the deadline (None: none)."""
    due = datetime(2027, 6, 14, 13, 29, 50, tzinfo=UTC)
    return {"run_id": algorithm, "algorithm_version": algorithm, "mode": mode, "lifecycle_status": status,
            "deadline_at": due if mode == "live" else None, "failure_reason": None if status == "issued" else "why",
            "issued_at": None if issued_s is None or status != "issued" else due + timedelta(seconds=issued_s),
            "events": [] if ack_s is None else [{"event": "acknowledged", "at": due + timedelta(seconds=ack_s)}]}


def test_the_forecast_in_force_is_the_first_usable_in_the_delivery_order():
    from contracts import nq_forecast as fc
    from contracts import nq_ml as ml
    from forecaster.delivery import delivered
    name = lambda v: ml.ARM_LABELS[v]
    m, n, b, a = ml.ML_MULTI_VERSION, ml.ML_NQ_VERSION, fc.BASELINE_VERSION, fc.PRIOR_VERSION
    assert ml.delivery_order() == [b, a]                                  # the ML forecasts are experimental
    run, why = delivered([_live_run(b), _live_run(a), _live_run(m)], ml.delivery_order(), name)
    assert run["algorithm_version"] == b and why == "B (analogues)"       # an experimental model is never delivered
    order = [m, n, b, a]                                                  # once promoted
    run, why = delivered([_live_run(m, "unavailable", ack_s=None), _live_run(n), _live_run(b)], order, name)
    assert run["algorithm_version"] == n and why == "ML NQ-only (fallback: ML multi-instrument unavailable: why)"
    run, why = delivered([_live_run(m, ack_s=+1.0), _live_run(b)], order, name)    # acknowledged after the deadline
    assert run["algorithm_version"] == b and "issued, not acknowledged by the deadline" in why
    run, why = delivered([_live_run(b, "late", ack_s=None), _live_run(a, "late", ack_s=None)], [b, a], name)
    assert run is None and why == "no usable forecast (B (analogues) late: why; A (frequencies) late: why)"
    replay = [_live_run(b, ack_s=None, mode="historical_replay"), _live_run(a, ack_s=None, mode="historical_replay")]
    assert delivered(replay, [b, a], name)[0]["algorithm_version"] == b   # a replay needs no acknowledgement


CANDIDATE = "candidate_0915"


@pytest.fixture(scope="module")
def candidate(market):
    """The 09:15 candidate's pool, built as Auto keeps it once started (journal.catch_up for the profile)."""
    from forecaster.journal import catch_up
    conn = market[0]
    catch_up(conn, CANDIDATE, now=cal.ny_instant(date.fromisoformat(DAY), time(20, 0)))
    return conn


def _live_copy(conn, day, built_at=None, profile=CANDIDATE):
    """A live snapshot of ``day``: DAY's historical ``profile`` snapshot moved to it, built ``built_at`` (default now:
    a session after today, whose runs the database then issues in time) - and the session's contract."""
    from database.queries import set_active_contracts
    from tests.synthetic import NQ_CID
    with conn:
        row = conn.execute(
            "INSERT INTO journal.snapshots (snapshot_id, symbol, contract_id, session_date, snapshot_version, "
            "convention_version, cutoff_at, rth_open_at, data_mode, pit_availability_status, source_payload_hash, "
            "payload, built_at) SELECT gen_random_uuid(), symbol, contract_id, %s::date, snapshot_version, "
            "convention_version, cutoff_at + (%s::date - session_date) * interval '1 day', "
            "rth_open_at + (%s::date - session_date) * interval '1 day', 'live_capture', 'unverified_historical', "
            "%s, payload, COALESCE(%s::timestamptz, clock_timestamp()) FROM journal.snapshots WHERE session_date = %s "
            "AND snapshot_version = %s AND data_mode <> 'live_capture' RETURNING snapshot_id;",
            (day, day, day, f"live:{profile}:{day}", built_at, DAY,
             defs.PROFILES[profile].snapshot_version)).fetchone()
    set_active_contracts(conn, "NQ", {day: NQ_CID}, "test")
    return str(row[0])


def _capture(conn, day, at, **kw):
    from forecaster import live_capture as live
    clock = _Clock(cal.ny_instant(date.fromisoformat(day), at))
    return live.capture(conn, None, day, CANDIDATE, clock=clock, sleep=clock.sleep, **kw)


def _steps(conn, result):
    from database import journal_store as store
    return [e["event"] for e in store.capture_events(conn, result["capture_id"])]


def _capture_row(conn, capture_id):
    return conn.execute("SELECT * FROM journal.live_captures WHERE capture_id = %s;", (capture_id,)).fetchone()


@needs_db
def test_a_live_capture_records_the_forecast_in_force_and_its_settings(candidate):
    """A later session's capture on the candidate profile: A and B issued in time, B in force, recorded as the
    capture's last step with its settings on the capture row; DAY's capture (its runs late by the database clock):
    nothing usable, and the record says why."""
    from contracts import nq_forecast as fc
    conn = candidate
    _live_copy(conn, "2027-06-14")
    result = _capture(conn, "2027-06-14", time(9, 29, 5))
    assert _steps(conn, result)[-1] == "delivered" and result["status"] == "issued"
    assert result["delivered"]["arm"] == "B (analogues)" and result["delivered"]["reason"] == "B (analogues)"
    row = _capture_row(conn, result["capture_id"])
    assert (row["issue_policy"], row["wait_limit_s"], row["reserve_s"]) == (fc.ISSUE_POLICIES["live"],
                                                                           fc.LIVE_DEFAULT_WAIT_S, fc.LIVE_RESERVE_S)
    _live_copy(conn, DAY, built_at=cal.ny_instant(date.fromisoformat(DAY), time(9, 20)))
    late = _capture(conn, DAY, time(9, 29, 5))
    assert late["delivered"]["run_id"] is None and late["delivered"]["reason"].startswith("no usable forecast")


@needs_db
def test_a_capture_whose_wait_leaves_no_time_to_issue_never_starts(candidate, capsys):
    """The data wait plus the reserve for issuing must end by the deadline: after 09:15 at most 14.7 minutes, after
    09:29 at most 40 seconds. Nothing is recorded; the command stops before it connects to IB."""
    from contracts import nq_forecast as fc
    from forecaster import live_capture as live
    from scripts.nq_journal import main
    conn = candidate
    count = lambda: conn.execute("SELECT count(*) FROM journal.live_captures;").fetchone()[0]
    before = count()
    assert fc.LIVE_RESERVE_S == 10
    with pytest.raises(live.LiveCaptureError, match="at most 14.7 minutes fit"):
        _capture(conn, "2027-06-23", time(9, 10), wait_s=15 * 60)
    with pytest.raises(live.LiveCaptureError, match="at most 0.7 minutes fit"):
        live.capture(conn, None, "2027-06-23", "research_0929", wait_s=60)
    live.check_wait("2027-06-23", CANDIDATE, 13 * 60)                      # the candidate wait fits
    live.check_wait("2027-06-23", "research_0929", 20)
    assert main(["--db", DSN, "live", "--date", "2027-06-23", "--wait-minutes", "1"]) == 1
    assert "Not started" in capsys.readouterr().out
    assert count() == before


class _DelayedIB(_LiveIB):
    """IB on a delayed feed: a bar is served ``delay`` after it starts."""
    def __init__(self, conn, day, delay):
        super().__init__(conn, day)
        self.delay = delay

    def fetch_historical_bars(self, contract, end, duration, what_to_show=None):
        return super().fetch_historical_bars(contract, end - self.delay, duration, what_to_show)


@needs_db
def test_a_long_wait_keeps_the_0915_evidence_cutoff_and_a_bar_too_late_is_a_missed_opportunity(candidate):
    """On PREV (its bars restored afterwards), the 09:15 candidate with a 13-minute wait: on a feed 11 minutes late
    the cutoff bar arrives within the wait and is taken (the database then refuses the snapshot: it is built now,
    after PREV's open); 14 minutes late, the capture is stale - a missed opportunity recorded with the newest bar
    and the wait it used. Started late, with bars past the cutoff already in, the evidence still ends at 09:15:
    the later bars are stored with receipts and never used."""
    from contracts import nq_forecast as fc
    from database import journal_store as store
    from database.queries import get_day_bars, save_trading_day
    from forecaster import live_capture as live
    from tests.synthetic import NQ_CID
    conn = candidate
    prev = date.fromisoformat(PREV)
    cutoff = cal.ny_instant(prev, time(9, 15))
    cutoff_bar = (cutoff - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    original = [dict(zip(r.keys(), r)) for r in get_day_bars(conn, NQ_CID, PREV)]

    def run(delay_min, start):
        save_trading_day(conn, NQ_CID, PREV, original)              # a capture stores the bars it was served
        clock = _Clock(cal.ny_instant(prev, start))
        result = live.capture(conn, _DelayedIB(conn, PREV, timedelta(minutes=delay_min)), PREV, CANDIDATE,
                              clock=clock, sleep=clock.sleep, wait_s=13 * 60)
        return result, store.capture_events(conn, result["capture_id"])

    try:
        taken, events = run(11, time(9, 14))
        found = next(e for e in events if e["event"] == "bars_received")
        assert taken["status"] == "failed" and "CheckViolation" in taken["reason"] and found
        row = _capture_row(conn, taken["capture_id"])
        assert (row["issue_policy"], row["wait_limit_s"], row["reserve_s"]) == (
            fc.ISSUE_POLICIES["live"], 780, fc.LIVE_RESERVE_S)
        requested = [e["detail"] for e in events if e["event"] == "bars_requested"]
        assert requested[0]["newest"] < cutoff_bar and requested[-1]["newest"] == cutoff_bar
        assert 16 < len(requested) < 60                                  # fast at first, then every 15 s

        missed, events = run(14, time(9, 14))
        stale = events[-1]
        assert missed["status"] == "stale" and stale["event"] == "stale" and "bars_received" not in \
            [e["event"] for e in events]
        assert stale["detail"]["wait_limit_s"] == 780 and stale["detail"]["newest"] < cutoff_bar
        assert stale["detail"]["attempts"] == len([e for e in events if e["event"] == "bars_requested"])

        late_start, _ = run(0, time(9, 24))
        receipts = store.capture_receipts(conn, late_start["capture_id"])
        after = [k for k in receipts if k >= cutoff.strftime("%Y-%m-%d %H:%M:%S")]
        assert after and cutoff_bar in receipts                            # bars past the cutoff were received
        snap = build_snapshot(conn, PREV, CANDIDATE, live_capture_id=late_start["capture_id"])
        historical = build_snapshot(conn, PREV, CANDIDATE)
        bars = snap.payload["bars"]
        assert bars["window"][1] == cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert bars["1m"][-1][0] == cutoff_bar.replace(" ", "T") + "Z"     # the last bar ends at the cutoff
        assert {k: bars[k] for k in ("1m", "2m", "5m", "15m")} == {k: historical.payload["bars"][k]
                                                                   for k in ("1m", "2m", "5m", "15m")}
        assert snap.payload["cutoff"]["last_received_bar"]["bar_start_at"] == cutoff_bar.replace(" ", "T") + "Z"
    finally:
        save_trading_day(conn, NQ_CID, PREV, original)


