# tests/test_snapshot_completeness.py
"""
Strict completeness (nq_conv_v5, guideline revision 2 section 1B): the fixtures
the revision asks for, on the pure evidence functions of features/nq_evidence.py
and the structure rules (nq_structure_rules_v4). No database needed; the daily
ATR fixture, which reads stored sessions, is in tests/test_nq_journal.py.
"""

from datetime import date, time, timedelta
from decimal import Decimal

from features import calendar as cal
from features.nq_evidence import _moving_averages, _overnight, _two_minute_atr, aggregate
from forecaster import structure_rules as sr
from tests.preopen_paths import CUTOFF, MINUTES, piecewise, snapshot

DAY = date(2026, 6, 12)
SESSION = cal.session(DAY.isoformat())
MIN = timedelta(minutes=1)


def at(et: str):
    h, m = map(int, et.split(":"))
    day = DAY - timedelta(days=1) if h >= 18 else DAY
    return cal.ny_instant(day, time(h, m))


PEAK = at("02:00")


def night(skip=(), dup=()):
    """Every overnight minute [18:00, 09:29) once; the night's true high (200) in the 02:00 minute."""
    out, t, i = [], SESSION.overnight_start_at, 0
    while t < CUTOFF:
        p = 100 + i % 7
        bar = (t, p, 200 if t == PEAK else p + 1, p - 1, p, 10)
        if t not in skip:
            out.append(bar)
            if t in dup:
                out.append(bar)
        t, i = t + MIN, i + 1
    return out


def evidence(bars):
    two = aggregate(bars, 2, CUTOFF)
    return (_overnight(bars, SESSION, CUTOFF), _two_minute_atr(two, CUTOFF),
            _moving_averages(two, SESSION, CUTOFF)["long_ma_at_cutoff"])


def test_a_complete_night_freezes_every_window():
    refs, atr, ma = evidence(night())
    assert refs["on_high"] == {"value": Decimal(200), "status": "valid", "window_minutes": MINUTES}
    assert refs["premarket_high"]["status"] == refs["vwap"]["status"] == "valid"
    assert refs["_coverage"]["complete"] and refs["_coverage"]["duplicates"] == 0
    assert atr["status"] == "valid" and ma["status"] == "valid" and ma["buckets"] == MINUTES // 2
    assert all(b[7] for b in aggregate(night(), 5, CUTOFF))


def test_without_the_minute_of_the_true_high_the_on_extremes_are_provisional():
    refs, atr, ma = evidence(night(skip={PEAK}))
    for name in ("on_high", "on_low"):
        assert refs[name]["value"] is None and refs[name]["status"] == "incomplete"
    assert refs["on_high"]["provisional_high"] == Decimal(107)                   # what is left is not the high
    assert refs["on_high"]["detail"] == f"{MINUTES - 1} of {MINUTES} overnight minutes: the window is incomplete"
    assert refs["vwap"]["status"] == "incomplete" and not refs["_coverage"]["complete"]
    assert refs["premarket_high"]["status"] == "valid"                           # 02:00 is outside [08:00, 09:29)
    assert ma["status"] == "incomplete_history"                                  # its 2m bucket lost a minute
    assert atr["status"] == "valid"                                              # 02:00 is outside its 71 buckets


def test_a_bucket_missing_one_of_its_minutes_is_flagged_not_filled():
    bars = night(skip={at("09:00")})
    two = {b[0]: b for b in aggregate(bars, 2, CUTOFF)}
    assert two[at("09:00")][6:] == (1, False) and two[at("09:02")][6:] == (2, True)
    _, atr, ma = evidence(bars)
    assert atr == {"value": None, "status": "incomplete_history",
                   "detail": "incomplete bucket(s) " + at("09:00").strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert ma["status"] == "incomplete_history"
    f = sr.annotate(snapshot(piecewise([(0, 100), (MINUTES, 300)]), T=4))["fields"]
    assert f["Price vs Long MA"]["value"] is not None                             # the same path, complete


def test_a_gap_in_the_moving_average_warm_up():
    bars = night(skip={at("19:00"), at("19:01")})                                # a whole 2m bucket gone
    refs, atr, ma = evidence(bars)
    assert ma["status"] == "incomplete_history" and ma["detail"].startswith("a gap after")
    assert atr["status"] == "valid"                                              # its window is complete
    assert refs["on_high"]["status"] == "incomplete"


def test_the_last_required_minutes():
    refs, atr, _ = evidence(night(skip={at("09:28")}))
    assert refs["on_high"]["status"] == refs["premarket_high"]["status"] == refs["vwap"]["status"] == "incomplete"
    assert atr["status"] == "valid"                     # the 09:28 bucket ends 09:30: no 2m bucket needs 09:28
    _, atr, ma = evidence(night(skip={at("09:27")}))
    assert atr["status"] == ma["status"] == "incomplete_history"                 # the last bucket, 09:26, is not whole
    _, atr, _ = evidence(night(skip={at("09:26"), at("09:27")}))
    assert atr["detail"].startswith("the last bucket before the cutoff")


def test_a_duplicate_timestamp_is_not_a_complete_minute():
    bars = night(dup={at("03:00")})
    refs, _, ma = evidence(bars)
    assert refs["_coverage"]["duplicates"] == 1 and not refs["_coverage"]["complete"]
    assert refs["on_high"]["status"] == "incomplete" and "1 duplicate(s)" in refs["on_high"]["detail"]
    assert {b[0]: b for b in aggregate(bars, 2, CUTOFF)}[at("03:00")][6:] == (3, False)
    assert ma["status"] == "incomplete_history"


# --------------------------------------------------------------------------
# The structure annotation (nq_structure_rules_v4): each field needs its whole window
# --------------------------------------------------------------------------

UP = piecewise([(0, 100), (MINUTES, 300)], wiggle=4, period=60)


def drop(snap, tf, *starts):
    """Mark the ``tf`` buckets starting at the given ET times incomplete, as a missing minute would."""
    ids = {at(s).strftime("%Y-%m-%dT%H:%M:%SZ") for s in starts}
    rows = snap["payload"]["bars"][tf]
    snap["payload"]["bars"][tf] = [r[:7] + [False] if r[0] in ids else r for r in rows]
    return snap


def annotated(snap):
    return sr.annotate(snap)["fields"]


def test_every_field_classifies_a_complete_snapshot():
    f = annotated(snapshot(UP, T=4))
    assert all(v["value"] is not None for k, v in f.items() if k != "Higher-Timeframe Bias")


def test_an_incomplete_bucket_leaves_only_the_fields_whose_window_holds_it():
    early = annotated(drop(snapshot(UP, T=4), "5m", "20:00"))                    # overnight only
    assert early["Overnight Structure"]["value"] is None
    assert "incomplete 5m bucket(s)" in early["Overnight Structure"]["reason"]
    assert early["Short-Term Structure"]["value"] is not None and early["5-Minute Trend"]["value"] is not None

    late = annotated(drop(snapshot(UP, T=4), "5m", "08:40"))
    for name in ("Overnight Structure", "Premarket Pattern", "Short-Term Structure", "5-Minute Trend"):
        assert late[name]["value"] is None, name
    assert late["15-Minute Trend"]["value"] is not None

    lookback = annotated(drop(snapshot(UP, T=4), "5m", "06:20"))                # the swing look-back
    assert lookback["Short-Term Structure"]["value"] is None and lookback["5-Minute Trend"]["value"] is not None

    ma = annotated(drop(snapshot(UP, T=4), "2m", "18:30"))
    for name in ("Price vs Long MA", "Long MA Slope", "Fast MA Alignment", "Chop Score"):
        assert ma[name]["value"] is None and "2m history is incomplete" in ma[name]["reason"], name
    assert ma["Overnight Structure"]["value"] is not None

    fifteen = annotated(drop(snapshot(UP, T=4), "15m", "07:15"))
    assert fifteen["15-Minute Trend"]["value"] is None and fifteen["5-Minute Trend"]["value"] is not None


def test_a_missing_bucket_is_named():
    snap = snapshot(UP, T=4)
    gone = at("09:20").strftime("%Y-%m-%dT%H:%M:%SZ")
    snap["payload"]["bars"]["5m"] = [r for r in snap["payload"]["bars"]["5m"] if r[0] != gone]
    trend = annotated(snap)["5-Minute Trend"]
    assert trend["value"] is None and trend["reason"].endswith("1 of 12 5m buckets from 12:25Z missing (first 13:20Z)")


def test_older_rows_judge_completeness_by_their_minute_count():
    snap = snapshot(UP, T=4)
    snap["payload"]["bars"]["5m"] = [r[:7] for r in snap["payload"]["bars"]["5m"]]      # nq_conv_v4 rows
    assert annotated(snap)["5-Minute Trend"]["value"] is not None
    nine = at("09:00").strftime("%Y-%m-%dT%H:%M:%SZ")
    snap["payload"]["bars"]["5m"] = [r[:6] + [4] if r[0] == nine else r for r in snap["payload"]["bars"]["5m"]]
    assert annotated(snap)["5-Minute Trend"]["value"] is None
