# tests/test_matching.py
"""Structural analogue selection (matching/structural.py, matcher nq_match_p1_v2): P1 section 7."""

from fractions import Fraction

from contracts import nq_preopen as pre
from matching import structural as ms

FULL = {"price:prev_rth_high": "Above", "price:prev_rth_low": "Above", "price:prev_rth_close": "Above",
        "price:on_high": "Below", "price:on_low": "Above", "Overnight Structure": "Uptrend",
        "Short-Term Structure": "Higher highs / Higher Lows", "5-Minute Trend": "Bullish",
        "15-Minute Trend": "Bullish", "Price vs Long MA": "Above", "Long MA Slope": "Rising",
        "Premarket Pattern": "Bullish continuation", "Chop Score": 1, "Event Risk": "Normal"}


def rec(day, sid=None, symbol="NQ", version="v2", protocol="rules", integrity="ok", **changes):
    feats = {**FULL, **{k.replace("_", " ") if k in ("Overnight_Structure",) else k: v for k, v in changes.items()}}
    return ms.Record(sid or f"s-{day}", day, symbol, version, f"a-{day}", protocol, integrity, feats)


def test_the_weights_are_p1s_and_sum_to_exactly_100():
    assert sum(pre.MATCH_WEIGHTS.values()) == 100
    groups = {g: sum(pre.MATCH_WEIGHTS[f] for f in fs) for g, fs in pre.MATCH_GROUPS.items()}
    assert groups == {"Price location": 30, "Structure": 25, "Trends and MA": 25, "Final-hour condition": 10,
                      "Events": 10}


def test_similarity_over_the_comparable_weight_only():
    t = rec("2026-06-12")
    assert ms.score(ms.compare(t, rec("2026-06-11"))) == (100, 100)
    one_off = rec("2026-06-11", **{"Overnight Structure": "Range"})
    assert ms.score(ms.compare(t, one_off)) == (Fraction(175, 2), 100)               # 87.5
    unknown = rec("2026-06-11", **{"Event Risk": None, "Overnight Structure": "Range"})
    sim, comparable = ms.score(ms.compare(t, unknown))
    assert comparable == 90 and sim == Fraction(100 * (90 - Fraction(25, 2)), 90)  # missing is neither match nor miss
    chop = ms.compare(t, rec("2026-06-11", **{"Chop Score": 3}))["Chop Score"]
    assert chop["score"] == Fraction(1, 3)                                          # max(0, 1 - 2/3)


def test_ranking_rules_and_exclusions():
    t = rec("2026-06-12")
    pool = [
        t,
        rec("2026-06-15"),                                                 # later
        rec("2026-06-01", symbol="ES"),
        rec("2026-06-02", protocol="llm"),
        rec("2026-06-03", integrity="contaminated"),
        rec("2026-06-04", **{k: None for k in list(FULL)[:8]}),            # 63% comparable
        rec("2026-06-05"), rec("2026-06-08"),                              # both 100 at full coverage: recent first
        rec("2026-06-09", **{"Event Risk": None}),                         # 100 but 90% coverage: after them
        rec("2026-06-10", **{"Overnight Structure": "Range"}),             # 87.5
        rec("2026-06-11", **{"Chop Score": 0}),                            # chop 2/3 x 5: 96.67
    ]
    out = ms.rank(t, pool)
    assert out["excluded"] == {"contaminated": 1, "incompatible_version": 1, "low_coverage": 1, "not_earlier": 2,
                               "other_symbol": 1}
    assert out["pool_size"] == 6
    picked = [m["record"].session_date for m in out["selected"]]
    assert picked == ["2026-06-08", "2026-06-05", "2026-06-09", "2026-06-11", "2026-06-10"]
    assert ms.rank(t, list(reversed(pool))) == out                              # deterministic
    assert ms.rank(rec("2026-01-01"), pool)["selected"] == []                    # nothing earlier: no analogues


def test_a_session_counts_once():
    # nq_match_p1_v2: two records of one session (two snapshots, or two annotations) would be two votes for one
    # day; the session is left out entirely rather than letting either record stand for it
    t = rec("2026-06-12")
    pool = [rec("2026-06-10"), rec("2026-06-11", sid="a"), rec("2026-06-11", sid="b"),
            rec("2026-06-11", sid="c", protocol="llm")]                          # another protocol: not eligible
    out = ms.rank(t, pool)
    assert [m["record"].session_date for m in out["selected"]] == ["2026-06-10"]
    assert out["excluded"] == {"duplicate_session": 2, "incompatible_version": 1} and out["pool_size"] == 1
    assert out["pool_hash"] == ms.rank(t, [rec("2026-06-10")])["pool_hash"]


def test_the_prior_manifest_names_every_session_and_revision_it_uses():
    t = rec("2026-06-12")
    sessions = [ms.SessionRef("s-2026-06-09", "2026-06-09", "NQ"), ms.SessionRef("s-2026-06-10", "2026-06-10", "NQ"),
                ms.SessionRef("x", "2026-06-11", "NQ"), ms.SessionRef("y", "2026-06-11", "NQ"),
                ms.SessionRef("s-2026-06-12", "2026-06-12", "NQ"), ms.SessionRef("s-2026-06-05", "2026-06-05", "ES"),
                ms.SessionRef("s-2026-06-08", "2026-06-08", "NQ")]          # never annotated: still in the prior
    out = lambda label, rev=1: {"labels": {"direction_15m": {"label": label}}, "outcome_revision": rev}
    outcomes = {"s-2026-06-09": out("bullish"), "s-2026-06-10": None, "x": out("bearish"), "y": out("bearish"),
                "s-2026-06-12": out("bullish"), "s-2026-06-05": out("bullish"), "s-2026-06-08": out(None)}
    prior = ms.prior_manifest(t, sessions, outcomes.get)
    assert prior["manifest"] == [["s-2026-06-08", "2026-06-08", 1], ["s-2026-06-09", "2026-06-09", 1]]
    assert prior["excluded"] == {"duplicate_session": 2, "no_outcome_known": 1, "not_earlier": 1, "other_symbol": 1}
    summary = ms.outcome_summary([], {}, prior["labels"])["targets"]["direction_15m"]
    assert (summary["prior_sessions"], summary["prior_without_label"]) == (1, 1)    # a null label: this target only
    revised = ms.prior_manifest(t, sessions, {**outcomes, "s-2026-06-09": out("bearish", 2)}.get)
    assert revised["digest"] != prior["digest"]             # a revised prior session is a new set, analogue or not
    assert ms.prior_manifest(t, list(reversed(sessions)), outcomes.get) == prior


def test_a_live_target_knows_only_the_outcomes_computed_by_its_cutoff():
    from datetime import datetime, timezone
    from forecaster.journal import _known_as_of
    at = lambda h: datetime(2026, 6, 12, h, tzinfo=timezone.utc)
    history = {"a": [{"outcome_revision": 1, "computed_at": at(1)}, {"outcome_revision": 2, "computed_at": at(15)}]}
    live = _known_as_of(history, {"data_mode": "live_capture", "cutoff_at": at(13)})
    rebuilt = _known_as_of(history, {"data_mode": "historical_reconstruction", "cutoff_at": at(13)})
    assert live("a")["outcome_revision"] == 1 and rebuilt("a")["outcome_revision"] == 2
    assert live("b") is None


def test_outcomes_attach_after_selection_with_smoothing():
    t = rec("2026-06-12")
    selected = ms.rank(t, [rec("2026-06-10"), rec("2026-06-11"), rec("2026-06-09")])["selected"]
    lab = lambda **kw: {tg: {"label": kw.get(tg)} for tg in pre.OUTCOME_TARGETS}
    outcomes = {"s-2026-06-11": lab(direction_15m="bullish"), "s-2026-06-10": lab(direction_15m="bearish"),
                "s-2026-06-09": None}                                         # no outcome: never replaced
    prior = [lab(direction_15m=x, first_move_5m="up_first")
             for x in ["bullish"] * 6 + ["bearish"] * 2 + ["neutral_band"] * 2]
    s = ms.outcome_summary(selected, outcomes, prior)
    d = s["targets"]["direction_15m"]
    assert (s["analogues"], d["eligible"], d["without_label"], d["status"]) == (3, 2, 1, "analogues")
    assert d["counts"] == {"bullish": 1, "bearish": 1, "neutral_band": 0}
    assert d["raw"]["bullish"] == "0.500000" and d["prior"]["bullish"] == "0.600000"
    assert d["smoothed"]["bullish"] == "0.571429"                                 # (1 + 5 x 0.6) / (2 + 5)
    assert sum(float(v) for v in d["smoothed"].values()) == 1.0
    fm = s["targets"]["first_move_5m"]
    assert fm["status"] == "prior_only" and fm["eligible"] == 0 and fm["raw"] is None
    assert fm["smoothed"]["up_first"] == "1.000000"                               # (0 + 5 x 1) / (0 + 5)
    assert s["targets"]["opening_type_15m"]["status"] == "none"                   # no label anywhere
    assert s["mean_similarity"] == "100.000000"


def test_the_coverage_floor_is_exactly_75_percent():
    t = rec("2026-06-12")
    at_floor = rec("2026-06-11", **{"Overnight Structure": None, "Short-Term Structure": None})       # 100 - 25
    below = rec("2026-06-10", **{"Overnight Structure": None, "Short-Term Structure": None, "Chop Score": None})
    assert ms.score(ms.compare(t, at_floor))[1] == 75 and ms.score(ms.compare(t, below))[1] == 70
    out = ms.rank(t, [at_floor, below])
    assert [m["record"].session_date for m in out["selected"]] == ["2026-06-11"]
    assert out["excluded"] == {"low_coverage": 1}
