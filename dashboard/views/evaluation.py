# dashboard/views/evaluation.py
"""
Model evaluation and analytics view for the NQ Opening Forecast System.
Compares historical forecasts against realized session outcomes to measure accuracy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from nicegui import ui

from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import build_accuracy_spec
from database.queries import get_evaluations

# Below this fraction of the open, a session is called flat rather than directional.
_NEUTRAL_BAND = 0.0005


def _load_json(value) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _actual_bias(rth_open, rth_close) -> str:
    if rth_open is None or rth_close is None:
        return "UNKNOWN"
    threshold = abs(rth_open) * _NEUTRAL_BAND
    if rth_close - rth_open > threshold:
        return "BULLISH"
    if rth_open - rth_close > threshold:
        return "BEARISH"
    return "NEUTRAL"


def _metric(label: str, value: str, accent: str = "#d1d4dc") -> None:
    with ui.card().classes("grow").style("background:#1c212e"):
        ui.label(label).classes("text-xs uppercase tracking-wide").style("color:#787b86")
        ui.label(value).classes("text-2xl font-medium").style(f"color:{accent}")


def _build_records(rows) -> List[Dict[str, Any]]:
    records = []
    for row in rows:
        probabilities = _load_json(row["probabilities"])
        raw_outcomes = _load_json(row["raw_outcomes"])

        rth_close = row["rth_close"]
        actual = _actual_bias(raw_outcomes.get("rth_open"), rth_close)
        predicted = row["predicted_bias"] or "UNKNOWN"
        correct = predicted == actual and actual != "UNKNOWN"

        records.append({
            # A session can carry more than one prediction, so the row key is the
            # prediction, not the date.
            "key": row["prediction_id"],
            "date": (row["session_date"] or "")[:10],
            "predicted": predicted,
            "actual": actual,
            "correct": correct,
            "result": "Match" if correct else "Miss",
            "rth_close": f"{rth_close:,.2f}" if rth_close is not None else "—",
            "bullish": f"{probabilities.get('bullish_continuation_pct', 0.0):.1f}%",
            "gap_fill": f"{probabilities.get('mean_reversion_gap_fill_pct', 0.0):.1f}%",
            "bearish": f"{probabilities.get('bearish_rejection_pct', 0.0):.1f}%",
        })
    return records


def _accuracy_trajectory(records: List[Dict[str, Any]]) -> tuple[List[str], List[float]]:
    """
    Cumulative directional accuracy over time, oldest session first.

    Records arrive newest-first and a session may hold several predictions. The
    chart's time axis needs one strictly increasing point per date, so a session
    contributes a single point carrying the running accuracy once all of that
    day's predictions have been counted.
    """
    dates: List[str] = []
    running: List[float] = []
    hits = 0

    for index, record in enumerate(reversed(records), start=1):
        if record["correct"]:
            hits += 1
        accuracy = hits / index * 100
        if not record["date"]:
            # Counted, but there is no point on a time axis to put it at.
            if running:
                running[-1] = accuracy
            continue
        if dates and dates[-1] == record["date"]:
            running[-1] = accuracy
        else:
            dates.append(record["date"])
            running.append(accuracy)

    return dates, running


def show_evaluation_page(conn) -> None:
    with ui.column().classes("w-full p-4 gap-2"):
        _evaluation_body(conn)


def _evaluation_body(conn) -> None:
    ui.label("Predictive accuracy").classes("text-2xl font-medium")
    ui.label(
        "How the opening forecasts held up against what the sessions actually did."
    ).classes("text-sm").style("color:#787b86")

    rows = get_evaluations(conn)
    if not rows:
        with ui.card().classes("w-full mt-4").style("background:#1c212e"):
            ui.label("No evaluated predictions yet.").classes("text-lg")
            ui.label(
                "Generate and save forecasts in the Session Explorer, then let the post-close "
                "evaluator fill in realized outcomes — scripts/daily_forecast.py does both."
            ).classes("text-sm").style("color:#787b86")
        return

    records = _build_records(rows)
    total = len(records)
    correct = sum(1 for r in records if r["correct"])
    accuracy = (correct / total * 100) if total else 0.0

    with ui.row().classes("w-full gap-4 mt-2"):
        _metric("Evaluated sessions", f"{total}")
        _metric(
            "Directional accuracy", f"{accuracy:.1f}%",
            "#26a69a" if accuracy >= 50 else "#ef5350",
        )
        _metric("Correct calls", f"{correct}/{total}")

    dates, running = _accuracy_trajectory(records)

    ui.label("Performance trajectory").classes("text-lg font-medium mt-4")
    LightweightChart(height=340, show_volume=False, show_candles=False).apply(
        build_accuracy_spec(dates, running)
    )

    ui.label("Forecast history").classes("text-lg font-medium mt-4")
    ui.table(
        columns=[
            {"name": "date", "label": "Date", "field": "date", "align": "left", "sortable": True},
            {"name": "predicted", "label": "Predicted", "field": "predicted", "align": "left"},
            {"name": "actual", "label": "Actual", "field": "actual", "align": "left"},
            {"name": "result", "label": "Result", "field": "result", "align": "left"},
            {"name": "rth_close", "label": "RTH close", "field": "rth_close", "align": "right"},
            {"name": "bullish", "label": "Bullish", "field": "bullish", "align": "right"},
            {"name": "gap_fill", "label": "Gap fill", "field": "gap_fill", "align": "right"},
            {"name": "bearish", "label": "Bearish", "field": "bearish", "align": "right"},
        ],
        rows=records,
        row_key="key",
    ).classes("w-full").props("dense flat").style("background:#1c212e")
