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
Each day is judged from its bar counts by the collector's current rule
(collector/coverage.py ``day_expectation``) - so a forecast target whose regular
session has gaps is not "complete" even if it was stored under an older rule.
Several contracts can hold the same futures day (warm-up days before a roll):
the best of them counts.

Right of the weeks, the last ``RECENT_DAYS`` scheduled trading days that have
ended are drawn one dot per day, coloured by the same rule over that day alone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from collector.coverage import day_expectation, expected_trading_days
from config import Config
from dashboard import theme
from database.queries import derive_day_status
from features.session_windows import NY_TZ

MAX_WEEKS = 78            # about eighteen months; older weeks are not drawn
SHOWN_WEEKS = 16          # the session bar's compact map: the last four months
CELL = 11                 # px per week column and per instrument row
RECENT_DAYS = 10          # the day dots: the last two weeks of trading days
DAY_PITCH, DAY_GAP = 13, 18   # px per day dot, px between the weeks and the dots

# (category, label, colour) - the category is the value the heatmap colours by: how much is stored is one grey
# ramp, darkest when complete; a week with nothing stored is signal red, the one alarm on the map.
BANDS = (
    (5, "complete", theme.INK2),
    (4, "≥ 90 %", "#8C979E"),
    (3, "≥ 50 %", "#B9C2BF"),
    (2, "> 0 %", "#D9DFDC"),
    (1, "none", theme.SIGNAL),
    (0, "no sessions yet", "#EEF1EF"),
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


def _judge(row: Dict[str, Any], inst, day: date) -> tuple:
    """``(status, score)`` of one stored day under the collector's current rule."""
    if row["status"] == "EMPTY" or not row["bar_count"]:
        return "EMPTY", 0.0
    expected, expected_rth = day_expectation(inst, day) if inst else (row.get("expected_bar_count"), None)
    have, rth = row["bar_count"] or 0, row.get("rth_bar_count") or 0
    status = derive_day_status(have, expected, row.get("open_bar_count") or 0, rth, expected_rth)
    if status == "COMPLETE":
        return status, 1.0
    shares = [have / expected] if expected else [0.5]
    if expected_rth:
        shares.append(rth / expected_rth)
    return status, min(0.99, *shares)


def coverage_weeks(conn, symbols: Sequence[str], today: Optional[date] = None,
                   max_weeks: int = MAX_WEEKS) -> Dict[str, Any]:
    """
    ``{'weeks': [monday, ...], 'symbols': [...], 'cells': {(symbol, monday): {...}}, 'newest': date,
    'days': [day, ...], 'day_cells': {(symbol, day): {...}}}``
    from the first stored week (at most ``max_weeks`` back) to the current one.
    Each cell holds the score, colour category, day counts and a tooltip text;
    ``newest`` is the latest trading day any of the symbols holds bars for;
    ``days`` are the last ``RECENT_DAYS`` scheduled trading days that have ended,
    with a colour category and tooltip per symbol in ``day_cells``.
    """
    today = today or datetime.now(NY_TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    oldest = this_monday - timedelta(weeks=max_weeks - 1)

    rows = conn.execute(
        "SELECT c.symbol, s.trading_day, s.price_type, s.status, s.bar_count, s.expected_bar_count, "
        "       s.rth_bar_count, s.open_bar_count "
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
        status, score = _judge(r, inst, key[1])
        if key not in best or score > best[key]["score"]:
            best[key] = {**r, "status": status, "score": score}

    if not best:
        return {"weeks": [], "symbols": list(symbols), "cells": {}, "newest": None, "days": [], "day_cells": {}}
    first = min(d for _, d in best)
    newest = max((d for (_, d), r in best.items() if r["score"] > 0), default=None)
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
    days = expected_trading_days(today - timedelta(days=3 * RECENT_DAYS), today - timedelta(days=1))[-RECENT_DAYS:]
    day_cells = {}
    for symbol in symbols:
        inst = Config.instrument(symbol)
        name = f"{symbol} · {inst.name}" if inst else symbol
        for d in days:
            stored = best.get((symbol, d))
            if stored is None:
                score, detail = 0.0, "not collected"
            else:
                score = stored["score"]
                detail = {"COMPLETE": "complete", "EMPTY": "empty at source"}.get(
                    stored["status"], f"partial ({score * 100:.0f} %)") + f", {stored['bar_count'] or 0:,} bars"
            cat = band(score, stored is not None and stored["status"] == "COMPLETE")
            label = next(lbl for c, lbl, _ in BANDS if c == cat)
            day_cells[(symbol, d)] = {
                "category": cat,
                "tip": f"<b>{name}</b><br>{d:%a %Y-%m-%d}<br>{detail}<br>Status: {label}",
            }
    return {"weeks": weeks, "symbols": list(symbols), "cells": cells, "newest": newest,
            "days": days, "day_cells": day_cells}


def chart_options(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    ECharts options for ``coverage_weeks`` output: the weeks as a heatmap, then
    (when there are any) the recent days as dots in a second grid on the same rows.
    """
    weeks, symbols, days = data["weeks"], data["symbols"], data.get("days", [])
    # About four week labels, counted back from the newest week so it is always labelled.
    step = max(1, -(-len(weeks) // 4))
    colours = {c: col for c, _, col in BANDS}
    week_points = [
        {"value": [x, y, data["cells"][(s, w)]["category"]], "tip": data["cells"][(s, w)]["tip"]}
        for y, s in enumerate(symbols) for x, w in enumerate(weeks)
    ]
    day_points = [
        {"value": [x, y], "tip": data["day_cells"][(s, d)]["tip"],
         "itemStyle": {"color": colours[data["day_cells"][(s, d)]["category"]]}}
        for y, s in enumerate(symbols) for x, d in enumerate(days)
    ]
    # Each day strip label sits under the last day of its week.
    week_ends = [i for i, d in enumerate(days)
                 if i == len(days) - 1 or days[i + 1].isocalendar()[1] != d.isocalendar()[1]]
    strip = DAY_PITCH * len(days)

    def x_axis(labels, grid, interval):
        return {"type": "category", "data": labels, "gridIndex": grid,
                "axisLabel": {"color": theme.INK2, "fontSize": 9, ":interval": interval},
                "axisLine": {"show": False}, "axisTick": {"show": False}, "splitArea": {"show": False}}

    def y_axis(grid, labelled):
        return {"type": "category", "data": list(symbols), "inverse": True, "gridIndex": grid,
                "axisLabel": {"show": labelled, "color": theme.INK, "fontSize": 8, "interval": 0},
                "axisLine": {"show": False}, "axisTick": {"show": False}}

    grids = [{"left": 34, "right": 8 + (strip + DAY_GAP if days else 0), "top": 2, "bottom": 18}]
    x_axes = [x_axis([f"{w:%m-%d}" for w in weeks], 0, f"(i) => ({len(weeks) - 1} - i) % {step} === 0")]
    y_axes = [y_axis(0, True)]
    series: List[Dict[str, Any]] = [{
        "type": "heatmap", "data": week_points,
        "itemStyle": {"borderColor": theme.SHEET, "borderWidth": 1},
        "emphasis": {"itemStyle": {"borderColor": theme.ARM_COLOR["B"], "borderWidth": 1}},
    }]
    if days:
        grids.append({"right": 8, "width": strip, "top": 2, "bottom": 18})
        x_axes.append(x_axis([f"{d:%m-%d}" for d in days], 1, f"(i) => {week_ends}.includes(i)"))
        y_axes.append(y_axis(1, False))
        series.append({"type": "scatter", "data": day_points, "xAxisIndex": 1, "yAxisIndex": 1,
                       "symbol": "circle", "symbolSize": 6,
                       "emphasis": {"itemStyle": {"borderColor": theme.ARM_COLOR["B"], "borderWidth": 1}}})
    return {
        "backgroundColor": "transparent",
        "animation": False,
        "grid": grids,
        "tooltip": {"confine": True, **theme.ECHART_TOOLTIP, ":formatter": "p => p.data.tip"},
        "xAxis": x_axes,
        "yAxis": y_axes,
        "visualMap": {
            "type": "piecewise", "dimension": 2, "seriesIndex": 0, "orient": "horizontal", "left": "center",
            "show": False,                     # the colours' meaning is in every cell's tooltip
            "bottom": 0, "itemWidth": 9, "itemHeight": 9, "itemGap": 8, "textGap": 3,
            "textStyle": {"color": theme.INK2, "fontSize": 9},
            "pieces": [{"value": c, "label": lbl, "color": col} for c, lbl, col in BANDS],
        },
        "series": series,
    }


def coverage_map(conn, symbols: Optional[List[str]] = None) -> None:
    """Draws the compact map - the last ``SHOWN_WEEKS`` weeks, ``CELL`` px a cell - where it is called."""
    from nicegui import ui

    symbols = symbols or Config.collect_symbols()
    data = coverage_weeks(conn, symbols, max_weeks=SHOWN_WEEKS)
    with ui.column().classes("gap-0"):
        newest = f", newest {data['newest']:%a %d %b}" if data["newest"] else ""
        recent = f", then the last {len(data['days'])} days" if data["days"] else ""
        ui.label(f"Stored by week{recent}{newest}").classes("text-xs").style(theme.MUTED)
        if not data["weeks"]:
            ui.label("No bars stored yet.").classes("text-xs").style(theme.MUTED)
            return
        width = 42 + CELL * len(data["weeks"]) + (DAY_GAP + DAY_PITCH * len(data["days"]) if data["days"] else 0)
        ui.echart(chart_options(data)).style(f"width:{width}px;height:{20 + CELL * len(symbols)}px")
