# tests/test_structure_rules.py
"""The rule-based pre-open structure annotation (forecaster/structure_rules.py, nq_structure_rules_v3)."""

from datetime import datetime, timedelta, timezone

import pytest

from contracts import nq_preopen as pre
from forecaster import structure_rules as sr
from tests.preopen_paths import ALL_SOURCES, MINUTES, event, minute_of, piecewise, prior_sessions, snapshot

N = MINUTES
UP = piecewise([(0, 100), (N, 300)], wiggle=4, period=60)
V = piecewise([(0, 300), (450, 100), (650, 220), (720, 170), (N, 260)], wiggle=5, period=60)


def fields(price, **kw):
    return sr.annotate(snapshot(price, **kw))["fields"]


def values(price, *names, **kw):
    f = fields(price, **kw)
    return tuple(f[n]["value"] for n in names)


def test_swings_need_two_bars_each_side_and_the_first_of_equal_highs_wins():
    t0 = datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc)
    bars = [sr.Bar(t0 + timedelta(minutes=5 * i), 5, h, h, h - 1, h)
            for i, h in enumerate([1, 2, 5, 5, 2, 1, 3, 6, 4])]
    highs = [(s.index, s.confirmed_at) for s in sr.swings(bars) if s.kind == "high"]
    assert highs == [(2, bars[4].end)]          # index 3 equals 2 on its left; 7 has one bar after it
    assert sr._sign_changes([1, 0, -1, -2, 0, 3]) == [2, 5]


def test_a_steady_rise_is_an_uptrend_on_every_timeframe():
    assert values(UP, "Overnight Structure", "Premarket Pattern", "Short-Term Structure", "5-Minute Trend",
                  "15-Minute Trend", "Price vs Long MA", "Long MA Slope", "Fast MA Alignment", "Chop Score",
                  T=4) == ("Uptrend", "Bullish continuation", "Higher highs / Higher Lows", "Bullish", "Bullish",
                           "Above", "Rising", "Bullish", 0)


def test_reversals_need_the_break_and_the_higher_low():
    assert values(V, "Overnight Structure", T=4) == ("V-reversal",)
    assert values(lambda m: 400 - V(m), "Overnight Structure", T=4) == ("Inverted-V",)
    # the same decline and recovery without the pullback to a higher low: no confirmed reversal
    no_higher_low = piecewise([(0, 300), (450, 100), (N, 260)])
    assert values(no_higher_low, "Overnight Structure", T=4) != ("V-reversal",)
    # the low within the first 15% of the night does not define the overnight path
    early = piecewise([(0, 300), (100, 100), (250, 220), (300, 170), (N, 260)], wiggle=5, period=60)
    assert values(early, "Overnight Structure", T=4) != ("V-reversal",)


def test_sideways_rotation_is_a_range_and_thin_coverage_is_unavailable():
    rotation = piecewise([(0, 110), (N, 110)], wiggle=10, period=N / 5)
    f = fields(rotation, T=4)
    assert f["Overnight Structure"]["value"] == "Range" and "alternations" in f["Overnight Structure"]["basis"]
    thin = fields(rotation, T=4, coverage="0.5000")["Overnight Structure"]
    assert thin["value"] is None and thin["status"] == "unavailable"


def test_a_premarket_reversal_needs_the_context_structure_broken():
    def path(hour_end):
        return piecewise([(0, 200), (minute_of("06:29"), 200), (minute_of("07:49"), 180), (minute_of("07:54"), 185),
                          (minute_of("08:29"), 150), (N, hour_end)], wiggle=1, period=20)
    assert values(path(195), "Premarket Pattern", T=4) == ("Bullish reversal",)
    mixed = fields(path(170), T=4)["Premarket Pattern"]
    assert mixed["value"] == "Mixed" and "intact opposite structure" in mixed["basis"]


def test_a_choppy_final_half_hour():
    flat = piecewise([(0, 100), (N, 100)])
    chop = lambda m: flat(m) + (3 if (m // 2) % 2 else -3) * (m >= N - 30)
    f = fields(chop, T=4)
    assert (f["Price vs Long MA"]["value"], f["Long MA Slope"]["value"], f["Fast MA Alignment"]["value"]) == \
        ("Crossing", "Flat", "Mixed")
    assert f["Chop Score"]["value"] >= 2


def test_price_location_uses_one_point_for_at():
    close = float(snapshot(UP)["payload"]["references"]["cutoff_price"]["value"])
    a = sr.annotate(snapshot(UP, T=4, refs={"prev_rth_high": close - 1, "prev_rth_low": close - 1.25,
                                            "prev_rth_close": close + 1, "on_high": close + 1.25}))
    assert a["price_location"] == {"prev_rth_high": "At", "prev_rth_low": "Above", "prev_rth_close": "At",
                                   "on_high": "Below", "on_low": None}


# --------------------------------------------------------------------------
# Event Risk (EV-v1)
# --------------------------------------------------------------------------

def risk(events=(), covered=ALL_SOURCES):
    f = fields(UP, T=4, events=events, covered=covered)
    return f["Event Risk"], f["Event Notes"]


def test_event_risk_levels():
    cpi = event("bls", "cpi:2026-05", "Consumer Price Index", "high", "08:30")
    ppi = event("bls", "ppi:2026-05", "Producer Price Index", "moderate", "08:30")
    fomc = event("fed", "fomc-decision:2026-06", "FOMC rate decision", "high", "14:00")
    aapl = event("sec_earnings", "AAPL:1", "AAPL earnings release (8-K 2.02)", "moderate", "16:30", day="2026-06-11")
    assert risk()[0]["value"] == "Normal" and risk()[1]["value"].startswith("No material")
    r, notes = risk([cpi, fomc])
    assert r["value"] == "High-risk" and r["evidence_ids"] == ["event:bls:cpi:2026-05", "event:fed:fomc-decision:2026-06"]
    assert notes["value"] == ("08:30 ET Consumer Price Index (released pre-open); "
                              "14:00 ET FOMC rate decision (upcoming)")
    assert risk([ppi])[0]["value"] == "Reduced-confidence"
    r, notes = risk([aapl])
    assert r["value"] == "Reduced-confidence" and notes["value"].startswith("Thu 16:30 ET AAPL earnings")


def test_event_risk_needs_every_source_covered():
    r, notes = risk(covered=[s for s in ALL_SOURCES if s != "sec_earnings"])
    assert r["value"] is None and r["reason"] == "no calendar coverage for sec_earnings" and notes["value"] is None


def test_a_release_after_the_close_does_not_count():
    late = event("fed", "x", "Late statement", "high", "16:30")
    assert risk([late])[0]["value"] == "Normal"


# --------------------------------------------------------------------------
# Integrity and shape
# --------------------------------------------------------------------------

def test_items_after_the_cutoff_contaminate_the_annotation():
    late_bar = [["2026-06-12T13:29:00Z", 1, 1, 1, 1, 1]]
    a = sr.annotate(snapshot(UP, extra_1m=late_bar))
    assert a["integrity_status"] == "contaminated" and a["fields"] == {}
    late_earnings = event("sec_earnings", "NVDA:1", "NVDA earnings release (8-K 2.02)", "moderate", "09:30")
    assert sr.annotate(snapshot(UP, events=[late_earnings]))["integrity_status"] == "contaminated"


def test_every_field_in_the_vocabulary_and_the_output_deterministic():
    snap = snapshot(V, T=4)
    a, b = sr.annotate(snap), sr.annotate(snap)
    assert a["output_hash"] == b["output_hash"] and a["protocol_version"] == pre.RULES_PROTOCOL_VERSION
    assert list(a["fields"]) == list(pre.FIELDS)
    htb = a["fields"]["Higher-Timeframe Bias"]
    assert htb["status"] == "unavailable" and htb["reason"].startswith("0 of 5 prior sessions verified")
    for name, f in a["fields"].items():
        assert f["value"] is None or pre.FIELDS[name]["values"] == "text" or f["value"] in pre.FIELDS[name]["values"]
        assert all(i.startswith(("bar:", "ref:", "event:", "prior:")) for i in f["evidence_ids"])
    assert a["measurements"]["swings_5m"] and a["measurements"]["moving_averages_2m"]["ema100"]


@pytest.mark.parametrize("missing", ["T", "cutoff"])
def test_missing_inputs_leave_fields_unavailable(missing):
    snap = snapshot(UP, T=None if missing == "T" else 4)
    if missing == "cutoff":
        snap["payload"]["references"]["cutoff_price"] = {"value": None, "status": "stale"}
    f = sr.annotate(snap)["fields"]
    if missing == "T":
        assert f["Premarket Pattern"]["value"] is None and f["Long MA Slope"]["value"] is None
        assert f["Overnight Structure"]["value"] == "Uptrend"
    else:
        assert all(v["value"] is None for k, v in f.items() if k not in ("Event Risk", "Event Notes"))



# --------------------------------------------------------------------------
# Higher-Timeframe Bias (HTB-v1): H5 = 120, L5 = 90, A = 30; upper third >= 110, lower <= 100
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cutoff, first_open, expected", [
    (121, 100, "Bullish"),                   # above the 5-session high
    (110, 95, "Bullish"),                    # upper third (exactly 2/3) and momentum exactly +0.5 ATR
    (110, 101, "Neutral-bullish"),           # upper third, momentum +0.3
    (110, 105.2, "Neutral-bullish"),         # momentum +0.16: just above the neutral band
    (110, 105.5, "Neutral"),                 # momentum exactly +0.15: within +/-0.15
    (105, 80, "Neutral"),                    # middle third, whatever the momentum
    (100, 115, "Bearish"),                   # lower third (exactly 1/3) and momentum exactly -0.5
    (100, 109, "Neutral-bearish"),           # lower third, momentum -0.3
    (89, 100, "Bearish"),                    # below the 5-session low
])
def test_higher_timeframe_bias(cutoff, first_open, expected):
    f = fields(UP, T=4, refs={"cutoff_price": cutoff}, prior=prior_sessions(first_open), A="30")
    htb = f["Higher-Timeframe Bias"]
    assert htb["value"] == expected, htb["basis"]
    assert htb["evidence_ids"][0] == "ref:cutoff_price" and len(htb["evidence_ids"]) == 6


def test_higher_timeframe_bias_unavailable_and_uncovered():
    uncovered = fields(UP, T=4, refs={"cutoff_price": 110}, prior=prior_sessions(120), A="30")["Higher-Timeframe Bias"]
    assert uncovered["value"] is None and uncovered["reason"].startswith("uncovered")   # upper third, momentum -0.33
    short = fields(UP, T=4, refs={"cutoff_price": 110}, prior=prior_sessions(100, incomplete=True), A="30")
    assert short["Higher-Timeframe Bias"]["reason"].startswith("4 of 5 prior sessions verified")
    no_atr = fields(UP, T=4, refs={"cutoff_price": 110}, prior=prior_sessions(100), A=None)
    assert no_atr["Higher-Timeframe Bias"]["reason"] == "daily ATR unavailable"



def test_htb_bands_are_exclusive_except_a_flat_breakout():
    from fractions import Fraction
    momenta = [Fraction(k, 100) for k in range(-80, 81)]
    for k in range(13):                                          # positions inside the range, 0 .. 1 in twelfths
        for m in momenta:
            assert len(sr.htb_bands(False, False, Fraction(k, 12), m)) <= 1, (k, m)
    for m in momenta:                                            # beyond the range: the breakout band, and with
        bands = sr.htb_bands(True, False, Fraction(5, 4), m)     # |m| <= 0.15 Neutral too - the breakout wins
        assert bands[0] == "Bullish" and (len(bands) == 2) == (abs(m) <= Fraction(15, 100))
        assert sr.htb_label(True, False, Fraction(5, 4), m) == "Bullish"
    assert sr.htb_bands(False, False, Fraction(5, 6), Fraction(-20, 100)) == []      # the gap: no band at all
    assert sr.htb_bands(False, False, Fraction(1, 6), Fraction(20, 100)) == []
