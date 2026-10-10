# tests/test_evaluation_view.py
"""
The Evaluation page's presentation (dashboard/components/forest.py, fan_experiment_panel.py and
views/evaluation.py): the forest plot's axis, signs and readings; the fan experiment's holdout as
forest rows, what else its stored result shows, where the experiment stands, the forward record's
classes and timing; and the P1 decision and per-target rows. Pure - no database, no browser.
"""

from datetime import datetime, timezone

from dashboard.components import fan_experiment_panel as fep
from dashboard.components import forest
from dashboard.views import evaluation as ev

M = forest.MINUS


def test_the_axis_covers_the_data_and_zero_on_a_round_step():
    assert forest.scale([-1.925, 1.51]) == (-2.0, 2.0, 1.0)
    lo, hi, step = forest.scale([-0.0785, 0.0248])
    assert (round(lo, 6), round(hi, 6), round(step, 6)) == (-0.08, 0.04, 0.02)
    assert forest.scale([0.3, 0.9])[0] == 0.0                     # zero is always on the axis
    assert forest.scale([None, 0.0]) == (-1.0, 1.0, 0.5)           # nothing to show: a unit either side
    assert forest.scale([979.0, 60.0], ticks=5) == (0.0, 1000.0, 200.0)
    assert [forest.decimals(s) for s in (0.5, 0.02, 1.0, 200.0)] == [1, 2, 0, 0]


def test_values_carry_a_typographic_sign_and_intervals_a_reading():
    assert forest.signed(-0.1433, 2, " %") == f"{M}0.14 %"
    assert forest.signed(0.006, 4) == "+0.0060" and forest.signed(0.0, 4) == "0.0000" and forest.signed(None, 2) == "-"
    say = lambda lo, hi: forest.reading(lo, hi, "B better", "A better")
    assert [say(-0.08, -0.02), say(0.0004, 0.02), say(-0.01, 0.02), say(None, None)] == [
        "B better", "A better", "No difference shown", "No interval"]


def test_the_plot_draws_a_solid_bar_only_where_the_interval_clears_zero():
    rows = [{"label": "15 min", "role": "primary", "point": -0.14, "lo": -0.28, "hi": -0.01, "value": "a", "ci": "b",
             "reading": "Pass", "primary": True},
            {"label": "20 min", "role": "", "point": -0.16, "lo": -0.32, "hi": 0.01, "value": "c", "ci": "d",
             "reading": "Inconclusive"},
            {"label": "1 min", "point": None, "lo": None, "hi": None, "value": "-", "reading": "Not scored"}]
    html = forest.html(rows, ("Horizon", "Difference", "Verdict"), "Model better", "v2 better", unit=" %")
    assert html.count("tp-ivl solid") == 1 and html.count("tp-ivl open") == 1 and html.count('class="tp-pt"') == 2
    assert html.count("tp-frow primary") == 1 and "Model better" in html and f"{M}0.4 %" in html


HOLDOUT = {"kind": "holdout", "verdict": "pass", "sessions": {"wanted": 60},
           "results": {"h5": {"sessions": 60, "base_crps_bps": 3.282, "other_crps_bps": 3.2778, "diff_bps": -0.00419,
                              "diff_share": -0.00419 / 3.282, "interval": [-0.00699, -0.0012]},
                       "h15": {"sessions": 60, "base_crps_bps": 5.7284, "other_crps_bps": 5.7202,
                               "diff_bps": -0.00821, "diff_share": -0.00821 / 5.7284, "interval": [-0.01619, -0.00003]},
                       "pre_open_16": {"sessions": 60, "base_crps_bps": 20.3069, "other_crps_bps": 20.2863,
                                       "diff_bps": -0.02059, "diff_share": -0.02059 / 20.3069, "interval": None},
                       "h15:overnight": {"diff_share": -0.0024}, "h5:overnight": {"diff_share": -0.0023},
                       "h15:after_close": {"diff_share": 0.018}, "h15:midday": {"diff_share": -0.0018}},
           "verdicts": {"h5": "better", "h15": "better", "pre_open_16": "no interval"},
           "roles": {"h5": "secondary", "h15": "primary", "pre_open_16": "secondary"},
           "concentration": {"improved": 0.62, "top5_share": 0.69, "weeks": 13,
                             "leave_week_out": {"weeks_flipping_sign": 0}},
           "calibration": {"base|h1|all": {"coverage": {"0.90": 0.895}}, "cand|h1|all": {"coverage": {"0.90": 0.882}},
                           "base|h15|all": {"coverage": {"0.90": 0.896}},
                           "cand|h15|all": {"coverage": {"0.90": 0.887}}}}


def test_the_holdout_is_a_forest_of_shares_with_the_primary_in_the_rules_words():
    rows = fep.holdout_forest(HOLDOUT)
    assert [r["label"] for r in rows] == ["5 min", "15 min", "09:29 + 16"]
    p = rows[1]
    assert p["primary"] and p["reading"] == "Pass" and p["value"] == f"{M}0.14 %"
    assert round(p["lo"], 2) == -0.28 and p["hi"] < 0                # the interval in bps over v2's CRPS, in %
    assert rows[0]["reading"] == "Better" and rows[2]["role"] == "pre-open" and rows[2]["ci"] == "no interval"


def test_what_else_the_holdout_shows_comes_from_the_stored_result():
    notes = fep.holdout_notes(HOLDOUT, "fan_rw_v2")
    assert notes[0].startswith("The gain sits overnight") and "after the close it loses" in notes[0]
    assert notes[1] == ("Better than fan_rw_v2 in 62 % of sessions, and its five best sessions carry 69 % of the "
                        "gain; with any one of its 13 weeks left out it stays a gain.")
    assert notes[2] == "Too narrow: its 90 % bands covered 88.2 to 88.7 % at 1 to 60 minutes, against fan_rw_v2's " \
                       "89.5 to 89.6 %."


EXP = {"registered_at": "2026-10-06 16:10:00", "definition": {
    "horizons": {"primary": {"minutes": 15}},
    "split": {"development": {"count": 243, "first": "2025-07-21", "last": "2026-07-10"},
              "holdout": {"count": 60, "first": "2026-07-13", "last": "2026-10-05"}}}}
MODEL = {"registered_at": "2026-10-06 18:40:00", "definition_hash": "cb831885906a", "definition": {
    "candidate": "lin_pois_ivx"}}


def test_the_rail_marks_the_first_step_not_done_as_current():
    states = lambda *a: [s for _, _, s in fep.stages(EXP, *a)]
    assert states(None, None, []) == ["done", "now", "todo", "todo", "todo"]
    assert states(MODEL, None, []) == ["done", "done", "done", "now", "todo"]
    held = {"computed_at": "2026-10-06 19:02", "results": {"verdict": "pass"}}
    rules = [{"version": "forward_v2", "active_from": datetime(2026, 10, 6, 20, 34, tzinfo=timezone.utc)}]
    steps = fep.stages(EXP, MODEL, held, rules)
    assert [s for _, _, s in steps] == ["done", "done", "done", "done", "now"]
    assert steps[2][1] == "lin_pois_ivx, 6 Oct, definition cb831885" and steps[3][1] == "6 Oct: pass at 15 minutes"


def test_the_forward_record_counts_classes_and_times_the_busiest_trigger():
    g = {"horizons": [{"class": "expired", "issued": 4}, {"class": "late", "issued": 3}, {"class": "late", "issued": 3},
                      {"class": "delayed_origin", "issued": 2}],
         "timing": [{"trigger": "auto", "issued": 2, "deadline_s": 60.0, "latency_s": {"median": 979.0},
                     "arrival_s": {"median": 930.0}, "compute_s": {"median": 8.0}},
                    {"trigger": "manual", "issued": 0, "deadline_s": 60.0, "latency_s": None},
                    {"trigger": "unrecorded", "issued": 2, "deadline_s": 60.0, "latency_s": {"median": 671.0},
                     "arrival_s": {"median": 653.0}, "compute_s": None}]}
    assert fep.class_counts(g) == [("Live", 0), ("Delayed origin", 2), ("Late", 6), ("Expired", 4)]
    t = fep.timing_block(g)
    assert (t["trigger"], t["median"], t["feed"], t["own"], t["compute"], t["deadline"], t["top"]) == (
        "Auto mode", 979.0, 930.0, 49.0, 8.0, 60.0, 1000.0)
    assert fep.timing_block({"timing": []}) is None


def _paired(diff, iv, common=200):
    return {"B-A": {"common": common, "brier": {"diff": diff, "interval": iv},
                    "log_loss": {"diff": diff, "interval": iv}}}


def test_the_p1_decision_reads_the_primary_metrics_interval():
    say = lambda iv: ev.decision({"target": "direction_15m", "paired": _paired(0.001, iv)}, "log_loss")[0]
    assert say([-0.03, 0.04]).startswith("No reliable difference between arm B and arm A was established. Primary: "
                                         "15-min direction, log loss, arm B minus arm A.")
    assert say([-0.03, -0.01]).startswith("Arm B forecast better than arm A")
    assert say([0.01, 0.03]).startswith("Arm A forecast better than arm B")
    assert say(None).startswith("Not decided: no interval on 200 common sessions.")
    assert ev.primary_metric({"primary": {"metric": "multiclass log loss (natural log), lower is better"}}) == "log_loss"


def test_every_target_is_a_forest_row_in_the_order_the_session_resolves_them():
    r = {"targets": {"direction_15m": {"paired": _paired(0.0014, [-0.0143, 0.017], 217)},
                     "first_level_tested": {"paired": _paired(-0.0473, [-0.0785, -0.0161], 154)},
                     "first_move_5m": {"paired": _paired(-0.0328, [-0.0712, 0.0145], 57)},
                     "session_type_rth": {"paired": _paired(None, None, 0)}}}
    rows = ev.target_rows(r, "B-A", "direction_15m")
    assert [x["label"] for x in rows] == ["First move", "15-min direction", "Session type", "First level"]
    assert [x["reading"] for x in rows] == ["No difference shown", "No difference shown", "Not scored", "B better"]
    assert rows[1]["primary"] and rows[1]["n"] == 217 and rows[3]["value"] == f"{M}0.0473"
