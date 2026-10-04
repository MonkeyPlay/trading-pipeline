# tests/test_review_set.py
"""Choosing the stage-1 review set (forecaster/review_set.py) and the review page's chart levels."""

from forecaster.review_set import select_sessions, session_items


def _labels(**over):
    base = {"first_move_5m": {"label": "up_first", "reason": None}, "session_type_rth": {"label": "range_day", "reason": None}}
    base.update({t: ({"label": v, "reason": None} if not v.startswith(":") else {"label": None, "reason": v[1:]})
                 for t, v in over.items()})
    return base


def test_session_items():
    items = session_items(_labels(session_type_rth=":uncovered"), "early_close", True, "2025-11-28")
    assert items == {"first_move_5m=up_first", "session_type_rth:uncovered", "schedule=early_close", "contract_roll",
                     "quarter=2025Q4"}
    assert "contract_roll" not in session_items(_labels(), "full", False, "2026-01-05")


def candidates():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-04-01", "2026-07-01"]
    out = []
    for i, d in enumerate(days):
        labels = _labels(first_move_5m="down_first" if i == 2 else "up_first")
        out.append({"snapshot_id": f"s{i}", "session_date": d, "items": session_items(labels, "full", False, d)})
    return out


def test_rare_classes_first_then_spread_in_time():
    chosen, coverage = select_sessions(candidates(), size=5)
    assert chosen[0]["session_date"] == "2026-01-07"        # the only down_first: rarest item first
    assert coverage["uncovered"] == [] and coverage["covered"] == coverage["items"]
    assert len({c["snapshot_id"] for c in chosen}) == 5
    assert select_sessions(candidates(), size=5) == (chosen, coverage)     # deterministic


def test_size_and_reasons():
    chosen, coverage = select_sessions(candidates(), size=2)
    assert len(chosen) == 2
    assert "first_move_5m=down_first" in chosen[0]["reasons"]
    assert all(set(c["reasons"]) for c in chosen)                        # each pick covered something new
    assert coverage["covered"] < coverage["items"]                       # two sessions cannot cover every quarter


def test_review_chart_levels():
    from dashboard.views.review import chart_levels
    snap = {"payload": {"references": {"prev_rth_close": {"value": "100.25"}, "on_high": {"value": "110"},
                                       "on_low": {"value": None}, "prev_rth_high": {"value": "120"},
                                       "prev_rth_low": {"value": "90"}}}}
    levels, extra = chart_levels(snap, {"O": "105", "T": "6", "vwap": "103.5"})
    assert levels == {"previous_rth_close": 100.25, "overnight_high": 110.0, "overnight_low": None}
    assert {e["key"]: e["value"] for e in extra} == {"prev_rth_high": 120.0, "prev_rth_low": 90.0, "vwap": 103.5,
                                                     "o_plus_t": 111.0, "o_minus_t": 99.0}
