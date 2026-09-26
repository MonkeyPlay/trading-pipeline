# tests/test_nq_v2.py
"""The nq_features_v2 builder against synthetic markets with known answers."""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features import calendar as cal
from features import catalogue as catv2
from features.market_data import FrameMarketData
from features.nq_v2 import SnapshotError, build_snapshot
from tests.synthetic import ES_CID, NQ_CID, make_market

DAY = "2026-06-10"
ONE = pd.Timedelta(minutes=1)


@pytest.fixture(scope="module")
def market():
    md, sessions = make_market(last_day=DAY, n_sessions=90)
    return md, sessions


@pytest.fixture(scope="module")
def snap(market):
    return build_snapshot(market[0], DAY)


def _nq(md):
    return md._bars[(NQ_CID, "TRADES")]


def _close_at(df, ts):
    return float(df.loc[df["bar_start_at"] == ts, "close"].iloc[0])


def _daily_atr(df, day, n=14, warm=5):
    """Independent Wilder ATR from RTH minute bars (single contract)."""
    sessions = cal.sessions_before(day, warm * n + 1)
    hlc = []
    for s in sessions:
        w = df[(df["bar_start_at"] >= s.rth_open_at) & (df["bar_start_at"] < s.scheduled_close_at)]
        hlc.append((w["high"].max(), w["low"].min(), w["close"].iloc[-1]))
    trs = [max(h - l, abs(h - hlc[i - 1][2]), abs(l - hlc[i - 1][2])) for i, (h, l, _) in enumerate(hlc) if i]
    atr = np.mean(trs[:n])
    for t in trs[n:]:
        atr = (13 * atr + t) / 14
    return atr, hlc[-1]


def test_catalogue_complete_and_json_safe(snap):
    assert list(snap.features) == list(catv2.FEATURE_NAMES)
    assert set(snap.feature_status) == set(catv2.FEATURE_NAMES)
    json.dumps(snap.features, allow_nan=False)
    json.dumps(snap.reference_values, allow_nan=False)
    assert all(v is None for k, v in snap.features.items() if snap.feature_status[k] != "valid")


def test_anchor_values(market, snap):
    md, _ = market
    df = _nq(md)
    s = cal.session(DAY)
    P = _close_at(df, s.cutoff_at - ONE)                       # the bar starting 09:28
    A, (pdh, pdl, cprev) = _daily_atr(df, DAY)
    assert snap.reference_values["P"] == pytest.approx(P)
    assert snap.reference_values["A"] == pytest.approx(A)
    assert snap.features["gap_signed_atr"] == pytest.approx((P - cprev) / A)
    assert snap.features["daily_atr_fraction"] == pytest.approx(A / cprev)
    assert snap.features["prior_range_position"] == pytest.approx((P - pdl) / (pdh - pdl))
    c0828 = _close_at(df, s.cutoff_at - 61 * ONE)
    assert snap.features["return_60m_atr"] == pytest.approx((P - c0828) / A)
    assert snap.features["nq_preopen_return"] == pytest.approx(P / cprev - 1)
    on = df[(df["bar_start_at"] >= s.overnight_start_at) & (df["bar_start_at"] < s.cutoff_at)]
    assert snap.features["overnight_range_atr"] == pytest.approx((on["high"].max() - on["low"].min()) / A)
    assert snap.features["distance_onh_atr"] <= 0 <= snap.features["distance_onl_atr"]


def test_status_semantics(snap):
    fs = snap.feature_status
    assert fs["daily_volatility_ratio"] == "insufficient_history"   # ATR63 needs 316 sessions
    assert fs["us10y_change_bps"] == "stale"                        # TNX prints in RTH only
    assert snap.source_status["us10y"]["observation"]["age_minutes"] > 30
    assert fs["us2y_change_bps"] == "missing" and snap.source_status["us2y"]["validity"] == "unmapped"
    assert fs["dxy_preopen_return"] == "missing"                    # no cash DXY; never the future
    assert fs["remaining_event_risk"] == "missing"                  # no calendar != no event
    assert snap.pit_availability_status == "unverified_historical"
    assert snap.data_quality_status == "partial"


def test_no_lookahead(market, snap):
    """Changing anything at or after T - the 09:29 bar, the whole RTH session - changes nothing."""
    md, _ = market
    s = cal.session(DAY)
    bars = pd.concat([g.assign(contract_id=k[0], price_type=k[1]) for k, g in md._bars.items()])
    after = bars["bar_start_at"] >= s.cutoff_at
    bars.loc[after, ["open", "high", "low", "close"]] *= 1.05
    bars.loc[after, "volume"] += 1000
    md2 = FrameMarketData(bars, active=md._active, contracts=md._contracts, fetched_at=md._fetched)
    snap2 = build_snapshot(md2, DAY)
    assert snap2.features == snap.features
    assert snap2.source_revision_id == snap.source_revision_id


def test_missing_p_is_null_not_substituted(market):
    s = cal.session(DAY)
    md, _ = make_market(last_day=DAY, n_sessions=90, drop=[(NQ_CID, s.cutoff_at - ONE)])
    snap = build_snapshot(md, DAY)
    assert snap.reference_values["P"] is None
    for name in ("gap_signed_atr", "nq_preopen_return", "return_15m_atr", "distance_onh_atr"):
        assert snap.features[name] is None and snap.feature_status[name] == "missing"
    assert snap.feature_status["daily_atr_fraction"] == "valid"
    assert snap.data_quality_status == "invalid"


def test_revision_changes_with_input_data(market, snap):
    s = cal.session(DAY)
    md, _ = make_market(last_day=DAY, n_sessions=90, drop=[(ES_CID, s.cutoff_at - 30 * ONE)])
    assert build_snapshot(md, DAY).source_revision_id != snap.source_revision_id


def test_early_close_previous_session():
    md, _ = make_market(last_day="2025-12-01", n_sessions=90, seed=3)
    snap = build_snapshot(md, "2025-12-01")
    prev = cal.session("2025-11-28")
    df = _nq(md)
    assert prev.schedule == "early_close"
    cprev = _close_at(df, prev.scheduled_close_at - ONE)             # the 12:59 ET bar
    assert snap.reference_values["Cprev"] == pytest.approx(cprev)
    assert snap.source_status["nq"]["reference"]["bar_end_at"] == "2025-11-28T18:00:00Z"
    assert snap.features["weekday"] == "mon"


def test_closed_session_refused(market):
    with pytest.raises(SnapshotError):
        build_snapshot(market[0], "2026-06-13")


def test_live_capture_must_freeze_before_the_open(market):
    s = cal.session(DAY)
    with pytest.raises(SnapshotError):
        build_snapshot(market[0], DAY, "live_capture", frozen_at=s.cutoff_at - timedelta(seconds=5))
    with pytest.raises(SnapshotError):
        build_snapshot(market[0], DAY, "live_capture", frozen_at=s.rth_open_at)
    live = build_snapshot(market[0], DAY, "live_capture", frozen_at=s.cutoff_at + timedelta(seconds=30))
    # Synthetic ledgers say every day was written two days later: not verifiable.
    assert live.pit_availability_status == "unverified_historical"


def _receipts_for_observations(md, s, received_at, bump=0.0):
    """A real-time receipt of every source's latest bar ending by T, as the streamer writes."""
    out = {}
    for (cid, _), g in md._bars.items():
        last = g[g["bar_start_at"] < s.cutoff_at].iloc[-1]
        out[(cid, last["bar_start_at"])] = {"received_at": received_at, "revision": 1,
                                             "finalised_by": "confirm_fetch",
                                             "close": float(last["close"]) + bump}
    return out


def test_live_capture_pit_needs_bar_receipts(market):
    md, _ = market
    s = cal.session(DAY)
    fetched = {k: s.cutoff_at for k in md._fetched}
    bars = pd.concat([g.assign(contract_id=k[0], price_type=k[1]) for k, g in md._bars.items()])
    frozen = s.cutoff_at + timedelta(seconds=30)

    def live(receipts):
        m = FrameMarketData(bars, active=md._active, contracts=md._contracts, fetched_at=fetched,
                            receipts=receipts)
        return build_snapshot(m, DAY, "live_capture", frozen_at=frozen)

    # Day-level ledger times alone are not per-bar proof.
    ledger_only = live(None)
    assert ledger_only.pit_availability_status == "unverified_historical"
    assert ledger_only.source_status["nq"]["observation"]["evidence"] == "day_ledger"

    ok = live(_receipts_for_observations(md, s, s.cutoff_at + timedelta(seconds=3)))
    assert ok.pit_availability_status == "verified"
    obs = ok.source_status["nq"]["observation"]
    assert obs["evidence"] == "bar_receipt" and obs["available_at"] == "2026-06-10T13:29:03Z"
    assert ok.source_status["nq"]["reference"]["evidence"] == "day_ledger"

    # A receipt of a different value (the store was rewritten since) proves nothing.
    assert live(_receipts_for_observations(md, s, s.cutoff_at, bump=0.25)).pit_availability_status \
        == "unverified_historical"
    # Nor does one received after the freeze.
    assert live(_receipts_for_observations(md, s, frozen + timedelta(seconds=1))).pit_availability_status \
        == "unverified_historical"


def test_event_features_need_a_calendar(market):
    md, _ = market
    s = cal.session(DAY)
    bars = pd.concat([g.assign(contract_id=k[0], price_type=k[1]) for k, g in md._bars.items()])
    events = [{"source": "t", "event_key": "fomc", "name": "FOMC", "tier": "high",
               "scheduled_at": cal.ny_instant(date(2026, 6, 10), pd.Timestamp("14:00").time())},
              {"source": "t", "event_key": "cpi", "name": "CPI", "tier": "high",
               "scheduled_at": cal.ny_instant(date(2026, 6, 10), pd.Timestamp("08:30").time())}]
    md2 = FrameMarketData(bars, active=md._active, contracts=md._contracts, fetched_at=md._fetched,
                          events=events, event_coverage=("2026-01-01", "2026-12-31"))
    snap = build_snapshot(md2, DAY)
    assert snap.features["has_future_high_event"] is True               # CPI at 08:30 is before T
    assert snap.features["remaining_event_risk"] == "high"
    assert snap.features["minutes_to_high_event"] == pytest.approx(271.0)   # 09:29 -> 14:00
    md3 = FrameMarketData(bars, active=md._active, contracts=md._contracts, fetched_at=md._fetched,
                          events=[], event_coverage=("2026-01-01", "2026-12-31"))
    none = build_snapshot(md3, DAY)
    assert none.features["has_future_high_event"] is False
    assert none.features["remaining_event_risk"] == "none"
    assert none.feature_status["minutes_to_high_event"] == "not_applicable"
