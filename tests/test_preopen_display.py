# tests/test_preopen_display.py
"""P1's 47-field pre-open record (forecaster/preopen_display.py, guideline Appendix B)."""

from forecaster import structure_rules as sr
from forecaster.preopen_display import P1_FIELDS, UNAVAILABLE, p1_record
from tests.preopen_paths import MINUTES, piecewise, snapshot

UP = piecewise([(0, 100), (MINUTES, 300)], wiggle=4, period=60)


def record(snap, analogue_set=None, annotation="rules"):
    ann = sr.annotate(snap) if annotation == "rules" else annotation
    provenance, rows = p1_record(snap, ann, analogue_set)
    return provenance, {prop: (value, basis) for prop, value, basis in rows}, rows


def test_the_47_fields_in_p1s_order_with_their_owners():
    snap = snapshot(UP, T=4, refs={"prev_rth_high": 250, "on_high": 301.25})
    provenance, r, rows = record(snap)
    assert [row[0] for row in rows] == P1_FIELDS and len(rows) == 47
    assert "actual cutoff 09:29 ET" in provenance and "no forecast" in provenance
    assert r["Day"][0] == "2026-06-12" and r["Weekday"][0] == "Friday" and r["Contract"][0] == "NQM6"
    assert r["Previous RTH High"][0] == "250.00" and r["ON High"][0] == "301.25"
    assert r["Price at 09:29"][0] == UNAVAILABLE                            # never the cutoff price renamed
    assert r["14-Day Daily ATR"][0] == "300.50" and r["2-Min ATR at 09:29"][0] == "12.35"
    assert r["Overnight Structure"][0] == "Uptrend" and r["Chop Score"][0] == "0"
    for prop in P1_FIELDS[24:36] + P1_FIELDS[41:45]:
        assert r[prop] == (UNAVAILABLE, "no forecast run given")
    assert r["Historical Analogue Count"] == (UNAVAILABLE, "the journal was not searched")


def test_a_0927_profile_has_no_two_minute_atr_at_0929():
    snap = snapshot(UP, T=4)
    snap["payload"]["cutoff"]["cutoff_et"] = "09:27"
    _, r, _ = record(snap)
    assert r["2-Min ATR at 09:29"] == (UNAVAILABLE, "this profile's cutoff is 09:27, not 09:29")


def test_analogue_fields_follow_p1s_formats():
    aset = {"pool_size": 12, "mean_similarity": "86.666667", "matcher_version": "nq_match_p1_v2",
            "members": [{"session_date": "2026-06-08", "similarity": "93.750000"},
                        {"session_date": "2026-05-29", "similarity": "79.583333"}]}
    _, r, _ = record(snapshot(UP, T=4), aset)
    assert r["Historical Analogue Count"][0] == "2"
    assert r["Analogue Sessions"][0] == "2026-06-08 (Monday); 2026-05-29 (Friday)"
    assert r["Analogue Session Similarity Scores"][0] == "2026-06-08 — 94%; 2026-05-29 — 80%"
    assert r["Analogue Similarity Score"][0] == "87%"
    assert r["Analogue Sessions Relation"][0] == UNAVAILABLE                 # no Notion write is claimed
    _, none, _ = record(snapshot(UP, T=4), {"pool_size": 3, "mean_similarity": None, "members": [],
                                            "matcher_version": "nq_match_p1_v2"})
    assert none["Historical Analogue Count"][0] == "0" and none["Analogue Similarity Score"][0] == UNAVAILABLE


def test_a_contaminated_snapshot_has_no_record():
    late = [["2026-06-12T13:29:00Z", 1, 1, 1, 1, 1]]
    provenance, _, rows = record(snapshot(UP, extra_1m=late))
    assert rows == [] and "CONTAMINATED" in provenance
