# tests/test_labels_prompt_v2.py
"""NQ-v2 outcome labels (forecaster/labels_prompt_v2.py) on hand-built sessions:
every threshold boundary, the ordered rules and their unresolved cases, and the
invariances the guideline asks for. No database needed."""

import random
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from forecaster.labels_prompt_v2 import RBar, compute_outcome

DAY = "2026-06-10"                 # a full Wednesday session (EDT)
EARLY = "2026-11-27"               # the 13:00 early close after Thanksgiving
MIN = timedelta(minutes=1)


def snapshot(day=DAY, T=5, B=10, A="200", vwap=None, **refs):
    """A frozen snapshot with the thresholds and references the labels read. ``vwap`` is
    given through the archived overnight bars, as a real snapshot provides it."""
    names = [n for n in defs.FIRST_LEVEL_CANDIDATES if n != "vwap"]
    return {"snapshot_id": "test", "payload": {
        "identity": {"session_date": day},
        "thresholds": {"T": T, "B": B, "A": A},
        "references": {n: {"value": None if refs.get(n) is None else str(refs[n]),
                           "status": "valid" if refs.get(n) is not None else "missing"} for n in names},
        "bars": {"coverage": {"ratio": "1" if vwap is not None else "0"},
                 "1m": [] if vwap is None else [["2026-06-10T12:00:00Z", vwap, vwap, vwap, vwap, 1]]},
    }}


def session(day=DAY, price=100, bars=None, drop=(), pre_close=100):
    """1m bars from 09:29 (minute -1) to the close: flat at ``price`` unless ``bars``
    {minute: (open, high, low, close)} says otherwise; ``drop`` removes minutes."""
    s = cal.session(day)
    n = int((s.scheduled_close_at - s.rth_open_at).total_seconds() // 60)
    out = []
    for m in range(-1, n):
        if m in drop:
            continue
        o, h, low, c = (bars or {}).get(m, (price, price, price, price))
        if m == -1 and pre_close is not None and m not in (bars or {}):
            o = h = low = c = pre_close
        if m == -1 and pre_close is None:
            continue
        out.append(RBar(s.rth_open_at + m * MIN, Decimal(str(o)), Decimal(str(h)), Decimal(str(low)), Decimal(str(c))))
    return out


def label(out, target):
    return out["labels"][target]["label"], out["labels"][target]["reason"]


def run(bars, snap=None):
    return compute_outcome(snap or snapshot(), bars)


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------

def test_thresholds_are_exact_ceilings_with_a_floor_of_one():
    assert defs.threshold_t(Fraction(10)) == 5                          # exactly 5: no ceiling step
    assert defs.threshold_t(Fraction(10) + Fraction(1, 10 ** 40)) == 6  # a hair above: the next point
    assert defs.threshold_t(Decimal("10.0000")) == 5
    assert defs.threshold_t(Decimal("1")) == 1 and defs.threshold_t(0) == 1
    assert defs.threshold_b(Fraction(200)) == 10 and defs.threshold_b(Fraction(201)) == 11
    assert defs.threshold_b("401/2") == 11                              # 10.025 -> 11
    assert defs.threshold_t(None) is None and defs.threshold_b(None) is None


# --------------------------------------------------------------------------
# First move
# --------------------------------------------------------------------------

def test_first_move_boundaries_and_order():
    assert label(run(session(bars={2: (100, 105, 100, 103)})), "first_move_5m") == ("up_first", None)   # == O+T
    assert label(run(session(bars={2: (100, 104.75, 100, 103)})), "first_move_5m") == ("neither", None)
    assert label(run(session(bars={1: (100, 100, 95, 97), 3: (100, 110, 100, 108)})),
                 "first_move_5m") == ("down_first", None)              # a later up move changes nothing
    assert label(run(session(bars={5: (100, 110, 100, 108)})), "first_move_5m") == ("neither", None)  # 09:35 is outside


def test_first_move_same_bar_double_touch_is_ambiguous():
    assert label(run(session(bars={0: (100, 106, 94, 101)})), "first_move_5m") == (None, "ambiguous_intrabar")


def test_first_move_needs_the_bars_before_the_first_reach():
    assert label(run(session(drop={1})), "first_move_5m") == (None, "missing_bars")
    reached = run(session(bars={0: (100, 106, 100, 105)}, drop={3}))    # reached at 09:30; 09:33 missing is irrelevant
    assert label(reached, "first_move_5m") == ("up_first", None)
    assert label(run(session(drop={0})), "first_move_5m") == (None, "missing_bars")      # no 09:30 bar: no O
    assert label(run(session(), snapshot(T=None)), "first_move_5m") == (None, "missing_threshold")


# --------------------------------------------------------------------------
# 15-minute direction and 30-minute bias
# --------------------------------------------------------------------------

@pytest.mark.parametrize("close, expected", [(105, "neutral_band"), (105.25, "bullish"), (95, "neutral_band"),
                                             (94.75, "bearish"), (100, "neutral_band")])
def test_direction_15m_band_includes_its_boundaries(close, expected):
    out = run(session(bars={14: (100, max(100, close), min(100, close), close)}))
    assert label(out, "direction_15m") == (expected, None)


def test_direction_needs_its_closing_bar():
    assert label(run(session(drop={14})), "direction_15m") == (None, "missing_bars")
    out = run(session(drop={14}))
    assert label(out, "opening_bias_30m") == ("neutral_band", None)    # 09:59 is there


def test_opening_bias_30m_uses_the_0959_close_and_t():
    out = run(session(bars={29: (100, 106, 100, 106)}))
    assert label(out, "opening_bias_30m") == ("bullish", None)
    assert defs.display("opening_bias_30m", "neutral_band", "predicted") == "Neutral"
    assert defs.display("opening_bias_30m", "neutral_band") == "Two-sided"


# --------------------------------------------------------------------------
# Opening type
# --------------------------------------------------------------------------

def climb(start=100, step=1, to=None, minutes=15):
    """A steady 1m climb from ``start`` by ``step`` a minute (wicks 0.25 outside the bodies)."""
    out, p = {}, start
    for m in range(minutes):
        o, c = p, p + step
        out[m] = (o, max(o, c) + 0.25, min(o, c) - 0.25, c)
        p = c
    return out


def test_opening_drive_up_and_down():
    assert label(run(session(bars=climb())), "opening_type_15m") == ("opening_drive_up", None)
    assert label(run(session(bars=climb(step=-1))), "opening_type_15m") == ("opening_drive_down", None)


def test_drive_efficiency_boundary_is_inclusive():
    # O 100, C15 106, H15 110, L15 100: efficiency 6/10 = 0.60 exactly -> a drive
    bars = {m: (100, 100, 100, 100) for m in range(15)}
    bars[5] = (100, 110, 100, 103)
    bars[14] = (103, 106, 103, 106)
    assert label(run(session(bars=bars)), "opening_type_15m") == ("opening_drive_up", None)
    bars[5] = (100, 110.25, 100, 103)                                   # 6/10.25 < 0.60: not a drive
    assert label(run(session(bars=bars)), "opening_type_15m") == ("range", None)


def sweep_low_bars(breach_minute=2):
    """Open 100, dip to 93 (prev RTH low 98 breached by 5 = T), then close above 98 and finish at 106."""
    bars = {m: (100, 100.5, 99.5, 100) for m in range(15)}
    bars[breach_minute] = (100, 100, 93, 96)
    for m in range(breach_minute + 1, 15):
        bars[m] = (99, 106, 98.5, 99 + (m - breach_minute))
    bars[14] = (105, 106.5, 104, 106)
    return bars


def test_sweep_low_then_rebound_wins_over_the_drive_rules():
    out = run(session(bars=sweep_low_bars(), pre_close=100), snapshot(prev_rth_low=98, on_low=90, on_high=120,
                                                                        prev_rth_high=130))
    assert label(out, "opening_type_15m") == ("sweep_low_rebound", None)


def test_breach_short_of_t_or_without_a_later_reclaim_is_no_sweep():
    refs = dict(prev_rth_low=98.25, on_low=90, on_high=120, prev_rth_high=130)   # 93 is 5.25 below 98.25... breached
    assert label(run(session(bars=sweep_low_bars()), snapshot(**refs)), "opening_type_15m")[0] == "sweep_low_rebound"
    refs["prev_rth_low"] = 97.75                                         # 93 is only 4.75 below: not breached by T
    assert label(run(session(bars=sweep_low_bars()), snapshot(**refs)), "opening_type_15m")[0] != "sweep_low_rebound"
    # breach in the last bar of the window: no later candle can reclaim
    bars = {m: (100 + m * 0.5, 100.5 + m * 0.5, 99.5 + m * 0.5, 100 + m * 0.5) for m in range(15)}
    bars[14] = (104, 106, 92, 106)
    out = run(session(bars=bars), snapshot(prev_rth_low=98, on_low=90, on_high=120, prev_rth_high=130))
    assert label(out, "opening_type_15m")[0] != "sweep_low_rebound"


def test_a_level_crossed_by_the_opening_gap_is_not_a_sweep_reference():
    refs = dict(prev_rth_low=98, on_low=90, on_high=120, prev_rth_high=130)
    gapped = run(session(bars=sweep_low_bars(), pre_close=96), snapshot(**refs))     # 96 -> open 100 crossed 98
    assert label(gapped, "opening_type_15m") == ("two_sided_whipsaw", None)
    unknown = run(session(bars=sweep_low_bars(), pre_close=None), snapshot(**refs))  # no 09:29 bar
    assert label(unknown, "opening_type_15m") == (None, "missing_bars")


def test_a_missing_reference_blocks_the_label_only_when_it_could_matter():
    refs = dict(prev_rth_low=None, on_low=90, on_high=120, prev_rth_high=130)
    out = run(session(bars=sweep_low_bars()), snapshot(**refs))         # dip below O-T, C15 > O+T: could be a sweep
    assert label(out, "opening_type_15m") == (None, "missing_reference")
    drive = run(session(bars=climb()), snapshot(**refs))                # never below O-T: no level could be swept
    assert label(drive, "opening_type_15m") == ("opening_drive_up", None)
    flat = run(session(), snapshot(**refs))                             # C15 inside the band: no sweep possible
    assert label(flat, "opening_type_15m") == ("range", None)


def test_whipsaw_range_and_zero_range():
    bars = {m: (100, 100, 100, 100) for m in range(15)}
    bars[3], bars[7] = (100, 105, 100, 102), (102, 102, 95, 98)
    assert label(run(session(bars=bars)), "opening_type_15m") == ("two_sided_whipsaw", None)
    assert label(run(session()), "opening_type_15m") == ("range", None)            # zero range, no division
    assert run(session())["measurements"]["efficiency_15m"] is None
    assert label(run(session(drop={9})), "opening_type_15m") == (None, "missing_bars")


# --------------------------------------------------------------------------
# RTH close direction and session type
# --------------------------------------------------------------------------

def rth(path, day=DAY):
    """Bars for the whole RTH following ``path(minute) -> price`` (0.25 wicks)."""
    s = cal.session(day)
    n = int((s.scheduled_close_at - s.rth_open_at).total_seconds() // 60)
    bars, prev = {}, path(0)
    for m in range(n):
        p = path(m)
        bars[m] = (prev, max(prev, p) + 0.25, min(prev, p) - 0.25, p)
        prev = p
    bars[0] = (path(0), bars[0][1], bars[0][2], bars[0][3])
    return session(day, bars=bars)


@pytest.mark.parametrize("close, expected", [(110, "neutral_band"), (110.25, "bullish"), (90, "neutral_band"),
                                             (89.75, "bearish")])
def test_close_direction_band_uses_b(close, expected):
    bars = session(bars={389: (100, max(100, close), min(100, close), close)})
    assert label(run(bars), "close_direction_rth") == (expected, None)


def test_full_session_targets_unavailable_on_an_early_close():
    out = run(session(EARLY), snapshot(EARLY))
    assert label(out, "close_direction_rth") == (None, "shortened_session")
    assert label(out, "session_type_rth") == (None, "shortened_session")
    assert label(out, "direction_15m") == ("neutral_band", None)        # the opening targets still apply


def test_session_types():
    trend = rth(lambda m: 100 + m * 0.5)                                 # +194.5, closes at the high
    assert label(run(trend), "session_type_rth") == ("bull_trend_day", None)
    assert label(run(rth(lambda m: 300 - m * 0.5)), "session_type_rth") == ("bear_trend_day", None)
    # first hour drops 60 (> A/4 = 50) then the day closes up beyond B: a reversal
    rev = rth(lambda m: 100 - m if m < 60 else 40 + (m - 60) * 0.25)
    assert label(run(rev), "session_type_rth") == ("reversal_day", None)
    # a wide day (R >= A = 200) that closes back near the open; the first hour stays within A/4
    def wide_path(m):
        if m < 60:
            return 100 + 0.5 * m
        if m < 200:
            return 130 + 1.4 * (m - 60)
        return 326 - (m - 200) * 221 / 189
    wide = rth(wide_path)
    out = run(wide, snapshot(A="200"))
    assert label(out, "close_direction_rth") == ("neutral_band", None)
    assert label(out, "session_type_rth") == ("two_sided_volatile_day", None)
    narrow = rth(lambda m: 100 + (m * 0.1 if m < 195 else 39 - m * 0.1))
    assert label(run(narrow), "session_type_rth") == ("range_day", None)
    assert label(run(session()), "session_type_rth") == ("range_day", None)        # zero-range session


def test_complete_session_no_rule_fits_is_uncovered_not_missing():
    # up to 280 by 12:10, back to 240 at the close: bullish (+140 > B), E = 140 / 180.5 = 0.78 >= 0.60 but
    # CL = 0.78 < 0.80, so no trend day; E >= 0.35, so neither volatile nor range; no reversal
    path = rth(lambda m: 100 + (m * 0.5 if m < 160 else 180 - (m - 160) * 0.2 if m < 360 else 140))
    out = run(path)
    assert label(out, "close_direction_rth") == ("bullish", None)
    assert label(out, "session_type_rth") == (None, "uncovered")


def test_session_type_needs_complete_rth_and_the_daily_atr():
    trend = rth(lambda m: 100 + m * 0.5)
    assert label(run([b for i, b in enumerate(trend) if i != 250]), "session_type_rth") == (None, "missing_bars")
    assert label(run(trend, snapshot(A=None, B=None)), "session_type_rth") == (None, "missing_threshold")


def test_reversal_boundary_uses_the_exact_daily_atr():
    # A = 201: A/4 = 50.25, so an IB low of exactly 49.75 (O - A/4) is a reversal - the test is inclusive -
    # and one tick higher is not. The close is bullish (+20 > B).
    def day(ib_low):
        return session(bars={30: (100, 100, ib_low, 100), 389: (100, 120, 100, 120)})
    assert label(run(day(49.75), snapshot(A="201")), "session_type_rth") == ("reversal_day", None)
    assert label(run(day(50.0), snapshot(A="201")), "session_type_rth") == ("range_day", None)
    assert label(run(day(49.75), snapshot(A="201/1")), "session_type_rth") == ("reversal_day", None)


# --------------------------------------------------------------------------
# Invariances
# --------------------------------------------------------------------------

def test_outcome_ignores_bar_order_and_bars_outside_the_session():
    bars = rth(lambda m: 100 + m * 0.5)
    shuffled = bars[:]
    random.Random(1).shuffle(shuffled)
    s = cal.session(DAY)
    late = [RBar(s.scheduled_close_at + k * MIN, Decimal(1), Decimal(1), Decimal(1), Decimal(1)) for k in range(5)]
    assert run(shuffled) == run(bars) == run(bars + late)


def test_bars_after_0945_cannot_change_the_opening_labels():
    a = run(session(bars=climb()))
    b = run(session(bars={**climb(), **{m: (50, 300, 10, 200) for m in range(15, 390)}}))
    for target in ("first_move_5m", "direction_15m", "opening_type_15m"):
        assert a["labels"][target] == b["labels"][target]


def test_every_target_present_and_unavailable_labels_carry_a_registered_reason():
    out = run(session(drop={0}))
    assert set(out["labels"]) == set(defs.TARGETS)
    for v in out["labels"].values():
        assert (v["label"] is None) == (v["reason"] is not None)
        assert v["reason"] is None or v["reason"] in defs.REASONS
        assert v["label"] is None or v["label"] in defs.TARGETS and True


# --------------------------------------------------------------------------
# First level tested
# --------------------------------------------------------------------------

FAR = dict(on_high=150, on_low=50, prev_rth_high=160, prev_rth_low=40, prev_rth_close=170, overnight_open=30,
           vwap=180)


def levels(**over):
    return snapshot(**{**FAR, **over})


def first(out):
    return label(out, "first_level_tested")


def test_no_candidate_reached_in_the_complete_window():
    out = run(session(), levels())
    assert first(out) == (None, "none_tested") and out["measurements"]["first_level_price"] is None


def test_first_level_is_the_first_reached_with_its_price():
    out = run(session(bars={3: (100, 104.5, 100, 102)}), levels(vwap=104))
    assert first(out) == ("vwap", None)
    assert out["measurements"]["first_level_price"] == "104.000000" and out["measurements"]["first_level_bar"] == "09:33"
    assert defs.display("first_level_tested", "prev_rth_close") == "Previous RTH Close"


def test_a_level_at_the_opening_trade_is_tested_first():
    out = run(session(bars={0: (100, 101, 97, 100)}), levels(prev_rth_close=100, overnight_open=98))
    assert first(out) == ("prev_rth_close", None)
    assert label(out, "first_level_outcome") == (None, "approach_unresolved")      # equal to O: no approach


def test_within_a_bar_the_nearest_level_on_one_side_is_first_and_both_sides_is_ambiguous():
    assert first(run(session(bars={2: (100, 106, 100, 105)}), levels(vwap=104, on_high=105))) == ("vwap", None)
    both = run(session(bars={2: (100, 106, 94, 101)}), levels(on_high=105, on_low=95))
    assert first(both) == (None, "ambiguous_intrabar")


def test_coincident_levels_keep_the_price_but_not_the_identity():
    out = run(session(bars={4: (100, 100, 94, 96)}), levels(on_low=95, prev_rth_low=95))
    assert first(out) == (None, "coincident_levels") and out["measurements"]["first_level_price"] == "95"
    assert label(out, "first_level_outcome") == (None, "upstream_unavailable")


def test_a_level_the_opening_gap_crossed_without_a_trade_is_not_tested():
    out = run(session(bars={6: (100, 101, 100, 100.5)}, pre_close=96), levels(overnight_open=98, vwap=101))
    assert first(out) == ("vwap", None)                  # 98 lay between the 09:29 close and O, never traded


def test_first_level_needs_every_candidate_and_the_bars_before_it():
    assert first(run(session(bars={3: (100, 104.5, 100, 102)}), levels(on_high=None))) == (None, "missing_reference")
    assert first(run(session(bars={3: (100, 104.5, 100, 102)}, drop={1}), levels(vwap=104))) == (None, "missing_bars")


# --------------------------------------------------------------------------
# LO-v1 level outcomes
# --------------------------------------------------------------------------

def lo(bars=None, drop=(), **over):
    return label(run(session(bars=bars, drop=drop), levels(on_low=95, **over)), "on_low_outcome")


def test_lo_not_tested_and_test_and_rejection():
    assert lo() == ("not_tested", None)
    assert label(run(session(), levels()), "on_high_outcome") == ("not_tested", None)
    assert lo({10: (100, 100, 94, 97)}) == ("test_rejection", None)               # a wick through is no breach
    assert lo({10: (100, 100, 95, 95), 11: (95, 96, 95, 96)}) == ("test_rejection", None)   # an equal close neither


def test_lo_breaks():
    after = {m: (92, 92, 92, 92) for m in range(11, 390)}
    assert lo({10: (100, 100, 90, 92), **after}) == ("break_acceptance", None)
    reclaim = {10: (100, 100, 90, 92), **{m: (97, 97, 97, 97) for m in range(20, 390)}}
    assert lo(reclaim) == ("break_reclaim_acceptance", None)
    assert lo({10: (100, 100, 90, 92), 388: (100, 100, 94, 94)}) == ("break_without_acceptance", None)


def test_lo_unresolved_cases():
    at_level = {m: (95, 95, 95, 95) for m in range(10, 390)}
    assert lo(at_level) == (None, "no_rejection")                                  # touched, never closed back
    assert lo({10: (100, 100, 90, 92)}, drop={389}) == (None, "missing_bars")      # final three not stored
    assert lo(drop={200}) == (None, "missing_bars")                                # a breach could hide there
    assert label(run(session(), levels(on_low=100)), "on_low_outcome") == (None, "approach_unresolved")


def test_lo_session_references_need_a_standard_session():
    out = run(session(EARLY), snapshot(EARLY, **FAR))
    for target in defs.LEVEL_OUTCOME_REFERENCES:
        assert label(out, target) == (None, "shortened_session")
    assert first(out) == (None, "none_tested")                                     # the opening window still applies


def test_first_level_outcome_covers_only_the_first_15_minutes():
    bars = {3: (100, 100, 94, 97), 30: (97, 97, 88, 90), **{m: (90, 90, 90, 90) for m in range(31, 390)}}
    out = run(session(bars=bars), levels(on_low=95))
    assert first(out) == ("on_low", None)
    assert label(out, "first_level_outcome") == ("test_rejection", None)            # by 09:45
    assert label(out, "on_low_outcome") == ("break_acceptance", None)               # by the close


# --------------------------------------------------------------------------
# Supplementary descriptors (P2 sections 5 and 7)
# --------------------------------------------------------------------------

def test_window_measurements_need_only_their_own_window():
    m = run(session(bars=climb()), snapshot(T=None))["measurements"]             # no threshold needed
    assert (m["H15"], m["L15"], m["C15"]) == ("115.25", "99.75", "115")
    late_gap = run(session(drop={200}))["measurements"]
    assert late_gap["IB_high"] == "100" and late_gap["RTH_high"] is None          # IB kept, RTH incomplete
    assert run(session(drop={20}))["measurements"]["first_30m_range"] is None
    assert run(session(EARLY), snapshot(EARLY))["measurements"]["RTH_close"] is None   # no early close in RTH Close


def test_close_direction_needs_the_1559_bar():
    assert label(run(session(drop={389})), "close_direction_rth") == (None, "missing_bars")


def test_ib_direction_uses_the_1029_close_and_t():
    assert label(run(session(bars={59: (100, 106, 100, 106)})), "ib_direction") == ("bullish", None)
    assert label(run(session(bars={59: (100, 105, 100, 105)})), "ib_direction") == ("neutral_band", None)


def test_opening_drive_strength():
    assert label(run(session(bars=climb())), "opening_drive_strength") == ("strong", None)        # eff 0.97
    moderate = {5: (100, 110, 100, 103), 14: (103, 107, 103, 107)}                             # eff 0.70
    assert label(run(session(bars=moderate)), "opening_drive_strength") == ("moderate", None)
    strong = {5: (100, 110, 100, 103), 14: (103, 108, 103, 108)}                               # eff 0.80 exactly
    assert label(run(session(bars=strong)), "opening_drive_strength") == ("strong", None)
    assert label(run(session()), "opening_drive_strength") == (None, "not_applicable")          # a range opening
    assert label(run(session(drop={9})), "opening_drive_strength") == (None, "upstream_unavailable")


def test_opening_confirmation_time_is_the_close_time_of_the_first_close_beyond():
    m = run(session(bars=climb()))["measurements"]                 # up first at 09:34; first close > 105 at 09:35
    assert m["opening_confirmation_time"] == "09:36"
    assert run(session())["measurements"]["opening_confirmation_time"] is None            # neither


def test_range_extensions():
    up = run(session(bars={100: (100, 101, 100, 100)}))
    assert label(up, "opening_range_extension") == ("up", None) and label(up, "ib_extension") == ("up", None)
    both = run(session(bars={100: (100, 101, 100, 100), 200: (100, 100, 99, 100)}))
    assert label(both, "opening_range_extension") == ("both_sides", None)
    assert label(run(session()), "opening_range_extension") == ("none", None)     # touching the boundary only
    assert label(run(session(bars={20: (100, 101, 100, 100)})), "ib_extension") == ("none", None)  # inside the IB
    assert label(run(session(bars={100: (100, 101, 100, 100)}, drop={300})),
                 "opening_range_extension") == (None, "missing_bars")             # a down breach could hide
    both_gap = run(session(bars={100: (100, 101, 100, 100), 200: (100, 100, 99, 100)}, drop={300}))
    assert label(both_gap, "opening_range_extension") == ("both_sides", None)    # final whatever is missing
    assert label(run(session(EARLY), snapshot(EARLY)), "ib_extension") == (None, "shortened_session")


def gap(prev_close, bars=None, drop=()):
    return label(run(session(bars=bars, drop=drop), levels(prev_rth_close=prev_close)), "gap_outcome")


def test_gap_outcome():
    assert gap(90) == ("no_material_gap", None)                                   # |100 - 90| = B
    assert gap(85, {200: (100, 100, 85, 90)}) == ("full_gap_fill", None)
    assert gap(85, {10: (100, 100, 99, 99)}) == ("partial_gap_fill", None)
    assert gap(85, {10: (100, 106, 100, 105)}) == ("gap_and_go", None)            # never below O, T above it
    assert gap(85) == (None, "uncovered")                                          # neither toward P nor T beyond
    assert gap(85, {200: (100, 100, 85, 90)}, drop={300}) == ("full_gap_fill", None)
    assert gap(85, drop={300}) == (None, "missing_bars")
    assert gap(None) == (None, "missing_reference")
    assert gap(115, {10: (100, 101, 100, 101)}) == ("partial_gap_fill", None)     # a gap down mirrors it


def test_session_high_and_low_timing_and_repeated_extrema():
    def timing(bars, which="session_high_timing"):
        return label(run(session(bars=bars)), which)[0]
    assert timing({100: (100, 110, 100, 100)}) == "morning"
    assert timing({20: (100, 110, 100, 100), 300: (100, 110, 100, 100)}) == "morning"   # first occurrence
    assert [timing({m: (100, 110, 100, 100)}) for m in (14, 15, 149, 150, 269, 270, 359, 360)] == [
        "opening_15m", "morning", "morning", "midday", "midday", "afternoon", "afternoon", "closing_30m"]
    assert timing({370: (100, 100, 90, 100)}, "session_low_timing") == "closing_30m"
    assert timing({0: (100, 100, 100, 100)}) == "opening_15m"                        # flat: the first bar
    assert label(run(session(drop={5})), "session_high_timing") == (None, "missing_bars")


def morning(pull, low_minute=50, extra=None):
    """A bearish morning: 100 until 09:40, 90, a leg low of 60 at ``low_minute``, a pullback high of
    ``pull`` at 11:10, then 61 into noon."""
    bars = {m: (100, 100, 100, 100) for m in range(10)}
    bars.update({m: (90, 90, 90, 90) for m in range(10, low_minute)})
    bars[low_minute] = (90, 90, 60, 60)
    bars.update({m: (60, 60, 60, 60) for m in range(low_minute + 1, 150)})
    if low_minute < 100:
        bars[100] = (60, pull, 60, 61)
        bars.update({m: (61, 61, 61, 61) for m in range(101, 150)})
    bars.update(extra or {})
    return run(session(bars=bars))


def test_mp_v1_retracement_bands_are_exact():
    bands = {63: "none", 64: "minor", 75.28: "moderate", 84.72: "moderate", 85: "deep"}   # leg 40: 10%, 38.2%, 61.8%
    for pull, expected in bands.items():
        assert label(morning(pull), "morning_pullback") == (expected, None), pull
    mp = morning(80)["measurements"]["mp_v1"]
    assert (mp["direction"], mp["leg_start"], mp["leg_extreme"], mp["pullback"]) == ("bearish", "100", "60", "80")


def test_mp_v1_repeated_and_late_extremes_and_two_sided_mornings():
    repeated = morning(80, extra={120: (61, 61, 60, 61)})          # a second 60 later: the first occurrence leads
    assert label(repeated, "morning_pullback") == ("moderate", None)
    assert label(morning(80, low_minute=148), "morning_pullback") == (None, "late_leg_extreme")
    assert label(run(session()), "morning_pullback") == ("none", None)                     # range 0 <= 2T
    wide = run(session(bars={30: (100, 120, 100, 100)}))
    assert label(wide, "morning_pullback") == (None, "uncovered")                          # two-sided, > 2T
    bullish = {m: (100, 100, 100, 100) for m in range(50)}
    bullish.update({50: (100, 140, 100, 140), **{m: (140, 140, 140, 140) for m in range(51, 100)},
                    100: (140, 140, 120, 139), **{m: (139, 139, 139, 139) for m in range(101, 150)}})
    assert label(run(session(bars=bullish)), "morning_pullback") == ("moderate", None)     # 20 / 40


def test_afternoon_continuation():
    up = {149: (100, 110, 100, 110), **{m: (110, 110, 110, 110) for m in range(150, 389)}, 389: (110, 125, 110, 125)}
    assert label(run(session(bars=up)), "afternoon_continuation") == ("bullish", None)
    flat_pm = {149: (100, 110, 100, 110), **{m: (110, 110, 110, 110) for m in range(150, 390)}}
    assert label(run(session(bars=flat_pm)), "afternoon_continuation") == ("none", None)
    assert label(run(session(bars=up, drop={270})), "afternoon_continuation") == (None, "missing_bars")


def test_direction_matched_session_fields():
    def matched(bars, drop=()):
        out = run(session(bars=bars, drop=drop))
        return label(out, "opening_direction_matched")[0], label(out, "direction_15m_matched")
    bull = {14: (100, 106, 100, 106), 29: (100, 106, 100, 106), 389: (100, 120, 100, 120)}
    assert matched(bull) == ("yes", ("yes", None))
    bear_close = {**bull, 389: (100, 100, 80, 80)}
    assert matched(bear_close) == ("no", ("no", None))
    assert matched({389: (100, 120, 100, 120)}) == ("mixed", ("mixed", None))           # opening neutral
    assert label(run(session(bars=bull, drop={29})), "opening_direction_matched") == (None, "upstream_unavailable")


def test_trend_persistence():
    assert label(run(rth(lambda m: 100 + m * 0.5)), "trend_persistence") == ("high", None)
    half = rth(lambda m: 100 + 0.5 * m if m < 160 else 180 - (m - 160) * 0.15)       # close 145.65
    assert label(run(half), "trend_persistence") == ("moderate", None)                  # E = 45.65 / 80.5 = 0.57
    assert label(run(session()), "trend_persistence") == ("low", None)                  # zero range
    assert label(run(session(drop={5})), "trend_persistence") == (None, "missing_bars")


# --------------------------------------------------------------------------
# The 40-field P2 record
# --------------------------------------------------------------------------

def _p2_properties():
    """The 40 property names of P2 section 9, from the source prompt itself."""
    import json
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = next(s["path"] for s in json.load(open(os.path.join(root, "prompts", "manifest.json")))["sources"]
                if s["id"] == "P2")
    text = open(os.path.join(root, path), encoding="utf-8").read()
    section = text[text.index("Include each property exactly once"):]
    return [m.group(1).strip() for m in re.finditer(r"^\d+\. (.+)$", section, re.M)][:40]


def test_p2_record_has_the_prompts_40_properties_in_order():
    from forecaster.outcome_display import p2_record
    snap = {**snapshot(), "data_mode": "historical_reconstruction", "snapshot_version": "nq_evidence_v1_r0929"}
    record = p2_record(snap, run(session(bars=climb())))
    assert [p for p, _ in record] == _p2_properties()
    values = dict(record)
    assert values["Realised Opening Type"] == "Opening drive up" and values["Opening Drive Strength"] == "Strong"
    assert values["First 15-Minute Pattern"] == "Drive continuation" and values["Realised Outcome Confidence"] == "5"
    assert values["Realised First Move"] == "Up" and values["First 30-Minute Direction"] == "Two-sided"
    assert "nq_prompt_v2_1_impl3" in values["Outcome Data Notes"]


def test_p2_record_unavailable_blank_and_confidence():
    from forecaster.outcome_display import p2_record
    snap = {**snapshot(), "data_mode": "historical_reconstruction", "snapshot_version": "nq_evidence_v1_r0929"}
    values = dict(p2_record(snap, run(session(drop={200}))))
    assert values["Opening Drive Strength"] == ""                                     # no drive: blank, per P2
    assert values["RTH High"] == "Unavailable" and values["Realised Outcome Confidence"] == "4"
    assert "Session High Timing: missing_bars" in values["Outcome Data Notes"]
    assert dict(p2_record(snap, run(session(drop={0}))))["Realised Outcome Confidence"] == "1"


def test_p2_record_prices_show_at_two_decimals():
    from forecaster.outcome_display import p2_record
    snap = {**levels(vwap=104.123456), "data_mode": "historical_reconstruction",
            "snapshot_version": "nq_evidence_v1_r0929"}
    values = dict(p2_record(snap, run(session(bars={3: (100, 104.5, 100, 102)}), snap)))
    assert values["RTH Open"] == "100.00" and values["Realised First Level Price"] == "104.12"


# --------------------------------------------------------------------------
# First 15-minute pattern (FP-v1)
# --------------------------------------------------------------------------

def pattern(bars, snap=None):
    return label(run(session(bars=bars), snap or levels()), "first_15m_pattern")


def bars_from(rows):
    return {m: row for m, row in enumerate(rows)}


def test_fp_drive_continuation_and_v_shape_by_sweep():
    assert pattern(climb()) == ("drive_continuation", None)
    sweep = snapshot(prev_rth_low=98, on_low=90, on_high=120, prev_rth_high=130, overnight_open=30,
                     prev_rth_close=170, vwap=180)
    assert pattern(sweep_low_bars(), sweep) == ("v_shape_reversal", None)


def test_fp_v_shape_without_a_sweep():
    rows = [(100, 100, 99, 99), (99, 99, 96, 96), (96, 96, 94, 94), (94, 94, 92, 92), (92, 92, 90, 90),
            (90, 90, 89, 89), (89, 89, 88, 90), (90, 92, 90, 92), (92, 95, 92, 95), (95, 98, 95, 98),
            (98, 100, 98, 100), (100, 102, 100, 102), (102, 104, 102, 104), (104, 106, 104, 106), (106, 107.5, 106, 107)]
    assert pattern(bars_from(rows)) == ("v_shape_reversal", None)      # low 12 below O at 09:36, close 7 above


def test_fp_fade_reversal():
    rows = [(100, 111, 100, 110)] + [(110 - k, 110 - k, 109 - k, 109 - k) for k in range(12)] + \
        [(98, 98, 97.5, 98), (98, 98.5, 97.5, 98)]
    assert pattern(bars_from(rows)) == ("fade_reversal", None)         # 11 up in the first bar, closes below O


def test_fp_double_top():
    rows = [(100, 101, 100, 101), (101, 103, 101, 103), (103, 105, 103, 105), (105, 108, 105, 107),
            (107, 107, 105, 105), (105, 105, 103, 103), (103, 104, 102, 102), (102, 104, 102, 104),
            (104, 106, 104, 106), (106, 108, 106, 107), (107, 107, 105, 105), (105, 105, 103, 103),
            (103, 103, 102, 102.25), (102.25, 102.25, 101, 101), (101, 101, 100.5, 101)]
    assert pattern(bars_from(rows)) == ("double_top_bottom", None)     # 108 twice, the 102 trough broken


def test_fp_spike_and_channel():
    # from the bottom fifth (5.5 below O > T, so not a drive), 17.5 of the 29-point range in bars 0-2,
    # then a channel to the high in the last bar, closing in the top fifth
    rows = [(100, 101, 94.5, 100), (100, 110, 100, 110), (110, 112, 109, 111)] + \
        [(111 + k, 112.5 + k, 110.5 + k, 112 + k) for k in range(12)]
    assert pattern(bars_from(rows)) == ("spike_and_channel", None)


def test_fp_balanced_rotation_and_choppy():
    rotation = [(100, 103, 99, 103), (103, 106, 102, 105), (105, 106.5, 103, 104), (104, 104, 99, 100),
                (100, 100, 96, 97), (97, 98, 95.5, 98), (98, 102, 98, 102), (102, 105, 101, 104),
                (104, 105, 100, 101), (101, 101, 97, 98), (98, 99, 96, 99), (99, 103, 99, 103),
                (103, 105, 102, 104), (104, 104, 100, 101), (101, 102, 100, 101)]
    assert pattern(bars_from(rotation)) == ("balanced_rotation", None)
    chop = [(100, 101.5, 99, 101 if m % 2 == 0 else 99) for m in range(14)] + [(99, 101.5, 99, 100)]
    assert pattern(bars_from(chop)) == ("choppy", None)                 # 13 reversals, range 2.5 < 2T


def test_fp_unavailable_cases():
    assert pattern({}) == (None, "uncovered")                           # zero range
    both = [(100, 102, 100, 102), (102, 105, 102, 105), (105, 107, 105, 107), (107, 110, 107, 109),
            (109, 109, 106, 106), (106, 106, 104, 104), (104, 106, 104, 106), (106, 108, 106, 108),
            (108, 109, 108, 109), (109, 110, 107, 107), (107, 107, 105, 105), (105, 105, 104, 104),
            (104, 104, 103, 103), (103, 103, 102, 102.5), (102.5, 102.5, 101.5, 102)]
    out = run(session(bars=bars_from(both)), levels())
    assert label(out, "first_15m_pattern") == (None, "ambiguous_pattern")   # a double top and a rotation
    assert "double_top_bottom" in out["labels"]["first_15m_pattern"]["detail"]
    blocked = snapshot(prev_rth_low=None, on_low=90, on_high=120, prev_rth_high=130)
    assert pattern(sweep_low_bars(), blocked) == (None, "upstream_unavailable")
    assert pattern(climb(), levels(T=None)) == (None, "missing_threshold")
