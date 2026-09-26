# dashboard/components/coverage_map.py
"""
A compact week-by-instrument map of what the database holds.

One cell per (instrument, week): how much of that week's scheduled trading
days the collector has stored, read from its day ledger (``session_days``) -
the same calendar and completeness judgement the collector plans with.

Colour rule, over the week's scheduled trading days that have already ended:

  complete   every day COMPLETE                                  green
  ≥ 90 %     nearly all of it                                     light green
  ≥ 50 %     partial                                              yellow
  > 0 %      sparse                                               orange
  0 %        nothing stored (or the source returned no data)      red
  (none)     no finished trading day in the week yet              grey

A day scores 1 when COMPLETE, its share of the expected bars (capped at 0.99)
when PARTIAL, and 0 when EMPTY or not stored; the week's score is the mean.
Several contracts can hold the same futures day (warm-up days before a roll):
the best of them counts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from collector.coverage import expected_trading_days
from config import Config
from features.session_windows import NY_TZ

MAX_WEEKS = 78            # about eighteen months; older weeks are not drawn

# (category, label, colour) - the category is the value the heatmap colours by.
BANDS = (
    (5, "complete", "#26a69a"),
    (4, "≥ 90 %", "#9ccc65"),
    (3, "≥ 50 %", "#fdd835"),
    (2, "> 0 %", "#ff9800"),
    (1, "none", "#ef5350"),
    (0, "no sessions yet", "#3a3f4b"),
)


def band(score: Optional[float], complete: bool) -> int:
    """The colour category of a week (see the module docstring)."""
    if score is None:
        return 0
    if complete:
        return 5
    if score >= 0.9:
        return 4
    if score >= 0.5:
        return 3
    if score > 0:
        return 2
    return 1


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _day_score(row: Dict[str, Any], expected_bars: int) -> float:
    if row["status"] == "COMPLETE":
        return 1.0
    if row["status"] == "PARTIAL":
        expected = row.get("expected_bar_count") or expected_bars
        return min(0.99, (row["bar_count"] or 0) / expected) if expected else 0.5
    return 0.0


def coverage_weeks(conn, symbols: Sequence[str], today: Optional[date] = None,
                   max_weeks: int = MAX_WEEKS) -> Dict[str, Any]:
    """
    ``{'weeks': [monday, ...], 'symbols': [...], 'cells': {(symbol, monday): {...}}}``
    from the first stored week (at most ``max_weeks`` back) to the current one.
    Each cell holds the score, colour category, day counts and a tooltip text.
    """
    today = today or datetime.now(NY_TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    oldest = this_monday - timedelta(weeks=max_weeks - 1)

    rows = conn.execute(
        "SELECT c.symbol, s.trading_day, s.price_type, s.status, s.bar_count, s.expected_bar_count "
        "FROM session_days s JOIN contracts c ON c.contract_id = s.contract_id "
        "WHERE s.interval = '1m' AND c.symbol = ANY(%s) AND s.trading_day >= %s;",
        (list(symbols), oldest),
    ).fetchall()

    best: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        r = dict(zip(r.keys(), r))
        inst = Config.instrument(r["symbol"])
        if inst is not None and r["price_type"] != inst.what_to_show:
            continue
        key = (r["symbol"], _day(r["trading_day"]))
        score = _day_score(r, inst.expected_bars if inst else 0)
        if key not in best or score > best[key]["score"]:
            best[key] = {**r, "score": score}

    if not best:
        return {"weeks": [], "symbols": list(symbols), "cells": {}}
    first = min(d for _, d in best)
    weeks = []
    monday = max(oldest, first - timedelta(days=first.weekday()))
    while monday <= this_monday:
        weeks.append(monday)
        monday += timedelta(weeks=1)

    cells = {}
    for symbol in symbols:
        inst = Config.instrument(symbol)
        for monday in weeks:
            # Scheduled days that have ended: today's session is still running.
            days = [d for d in expected_trading_days(monday, monday + timedelta(days=6)) if d < today]
            stored = [best.get((symbol, d)) for d in days]
            complete = sum(1 for s in stored if s and s["status"] == "COMPLETE")
            partial = sum(1 for s in stored if s and s["status"] == "PARTIAL")
            empty = sum(1 for s in stored if s and s["status"] == "EMPTY")
            missing = sum(1 for s in stored if s is None)
            score = sum(s["score"] for s in stored if s) / len(days) if days else None
            cat = band(score, bool(days) and complete == len(days))
            name = f"{symbol} · {inst.name}" if inst else symbol
            if not days:
                detail = "no finished trading day yet"
            else:
                parts = [f"{complete}/{len(days)} days complete"]
                parts += [f"{n} {what}" for n, what in ((partial, "partial"), (empty, "empty at source"),
                                                         (missing, "not collected")) if n]
                detail = ", ".join(parts) + f" ({score * 100:.0f} %)"
            label = next(lbl for c, lbl, _ in BANDS if c == cat)
            cells[(symbol, monday)] = {
                "score": score, "category": cat, "days": len(days), "complete": complete,
                "partial": partial, "empty": empty, "missing": missing,
                "tip": f"<b>{name}</b><br>Week of {monday:%a %Y-%m-%d}<br>{detail}<br>Status: {label}",
            }
    return {"weeks": weeks, "symbols": list(symbols), "cells": cells}


def chart_options(data: Dict[str, Any]) -> Dict[str, Any]:
    """ECharts heatmap options for ``coverage_weeks`` output."""
    weeks, symbols = data["weeks"], data["symbols"]
    points = [
        {"value": [x, y, data["cells"][(s, w)]["category"]], "tip": data["cells"][(s, w)]["tip"]}
        for y, s in enumerate(symbols) for x, w in enumerate(weeks)
    ]
    return {
        "backgroundColor": "transparent",
        "animation": False,
        "grid": {"left": 40, "right": 8, "top": 6, "bottom": 40},
        "tooltip": {"confine": True, "backgroundColor": "#1c212e", "borderColor": "#363a45",
                    "textStyle": {"color": "#d1d4dc", "fontSize": 11},
                    ":formatter": "p => p.data.tip"},
        "xAxis": {"type": "category", "data": [f"{w:%m-%d}" for w in weeks],
                  "axisLabel": {"color": "#787b86", "fontSize": 9},
                  "axisLine": {"show": False}, "axisTick": {"show": False}, "splitArea": {"show": False}},
        "yAxis": {"type": "category", "data": list(symbols), "inverse": True,
                  "axisLabel": {"color": "#b2b5be", "fontSize": 9},
                  "axisLine": {"show": False}, "axisTick": {"show": False}},
        "visualMap": {
            "type": "piecewise", "dimension": 2, "orient": "horizontal", "left": "center", "bottom": 0,
            "itemWidth": 9, "itemHeight": 9, "itemGap": 8, "textGap": 3,
            "textStyle": {"color": "#787b86", "fontSize": 9},
            "pieces": [{"value": c, "label": lbl, "color": col} for c, lbl, col in BANDS],
        },
        "series": [{
            "type": "heatmap", "data": points,
            "itemStyle": {"borderColor": "#161a25", "borderWidth": 1},
            "emphasis": {"itemStyle": {"borderColor": "#d1d4dc", "borderWidth": 1}},
        }],
    }


def coverage_map(conn, symbols: Optional[List[str]] = None) -> None:
    """Draws the map (about 480 x 150 px) where it is called."""
    from nicegui import ui

    symbols = symbols or Config.collect_symbols()
    data = coverage_weeks(conn, symbols)
    with ui.column().classes("gap-0"):
        ui.label("Database coverage by week").classes("text-xs uppercase tracking-wide").style(
            "color:#787b86")
        if not data["weeks"]:
            ui.label("No bars stored yet.").classes("text-xs").style("color:#787b86")
            return
        height = 46 + 12 * len(symbols)
        ui.echart(chart_options(data)).style(f"width:480px;height:{height}px")
