# dashboard/views/backtests.py
"""
Backtests: how each forecast has done, every one against a simple guess on the
same sessions, so skill can be told from luck.

  First-hour model         forecaster/first_hour_model.scorecard - walk-forward over
                           every complete session: the predicted moves at 09:45 /
                           10:00 / 10:30 against no move, the 15 / 30 / 60 minute
                           ranges and each minute's size against the usual, and the
                           generated path against a flat line.
  First hour forecast      forecaster/first_hour.backtest - the pre-open-match forecast
  (pre-open matches)       the first-hour model replaced, kept for comparison: the
                           first-hour range (two estimators) against the usual range,
                           the opening-range break against the base rates, and the
                           fan's coverage.
  Opening scenario         forecaster/analogue.backtest - walk-forward: the first-hour
  generator                up / flat / down probabilities and the bias against the
                           base rates.
  Trained model            the stored forecasts of the default model against the
                           climatology baseline on the same sessions
                           (forecaster/scoring_v2.paired_comparison), from the
                           outcomes recorded so far.

The same numbers as ``nq_forecast_v2.py first-hour-model``, ``first-hour-backtest``,
``scenario-backtest`` and ``evaluate``. Each section runs when the page opens and again on Rerun.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from nicegui import run, ui

from database import forecast_store as store
from forecaster import analogue, first_hour, first_hour_model, models_v2, scoring_v2

_MUTED = "color:#787b86"
_VERDICT_SLOT = '''
<q-td :props="props">
  <span :style="{color: props.value === 'better' ? '#26a69a' : props.value === 'worse' ? '#ef5350' : '#b2b5be'}">
    {{ props.value }}
  </span>
</q-td>
'''


def verdict(gain: float, se: float) -> str:
    """'better' / 'worse' beyond two standard errors of the gain, else 'no difference'."""
    if any(isinstance(v, float) and math.isnan(v) for v in (gain, se)):
        return "too few sessions"
    return "better" if gain > 2 * se else "worse" if gain < -2 * se else "no difference"


def _row(key: str, label: str, s: Dict[str, float], fmt: str = "{:.4f}") -> Dict[str, Any]:
    return {"key": key, "test": label, "forecast": fmt.format(s["model"]), "simple": fmt.format(s["base"]),
            "gain": f"{s['gain']:+.4f} ± {s['gain_se']:.4f}", "verdict": verdict(s["gain"], s["gain_se"])}


def first_hour_model_rows(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    times = {15: "09:45", 30: "10:00", 60: "10:30"}
    rows = [_row(f"move_{k}", f"Move at {times[k]}, squared error vs no move (right {d['hit_rate'] * 100:.0f} %, "
                              f"up {d['up_share'] * 100:.0f} %)", d["move"]) for k, d in r["direction"].items()]
    rows += [_row(f"range_{k}", f"Range of the first {k} min, |log error| vs the usual", s)
             for k, s in r["ranges"].items()]
    return rows + [_row("candles", "Each minute's candle size, |log error| vs the usual", r["candles"]),
                   _row("path", "Generated path, mean |error| vs a flat line at P", r["path"])]


def first_hour_rows(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [_row("range_matches", "First-hour range, |log error| - matches' median", r["range_matches"]),
            _row("range_regression", "First-hour range, |log error| - regression", r["range_regression"]),
            _row("first_break", "First break side, log loss (3-way)", r["first_break"]),
            _row("breakout", "Break vs no break, log loss", r["breakout"])]


def scenario_rows(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    p, h = r["probabilities"], r["hits"]
    return [_row("probabilities", "First hour up / flat / down, log loss", p),
            {"key": "bias", "test": "Bias, hit rate", "forecast": f"{r['hit_rate'] * 100:.1f} %",
             "simple": f"{r['base_hit_rate'] * 100:.1f} %",
             "gain": f"{(r['hit_rate'] - r['base_hit_rate']) * 100:+.1f} pp ± {h['gain_se'] * 100:.1f}",
             "verdict": verdict(h["gain"], h["gain_se"])}]


def model_rows(conn) -> List[Dict[str, Any]]:
    """The default model against the climatology on the same sessions, per target, from the outcomes so far."""
    model, base = models_v2.DEFAULT_MODEL, models_v2.CLIMATOLOGY["model_version"]
    rows = [r for r in store.get_prediction_outcomes(conn, outcomes_as_of=datetime.now(timezone.utc))
            if r["model_version"] in (model, base)]
    out = []
    for c in scoring_v2.paired_comparison(rows, base, [model]):
        out.append({"key": f"{c['target']}/{c['mode']}", "test": c["target"], "n": c["n"],
                    "forecast": f"{c['log_loss']:.4f}", "simple": f"{c['baseline_log_loss']:.4f}",
                    "gain": f"{c['gain']:+.4f} ± {c['gain_se']:.4f}", "verdict": verdict(c["gain"], c["gain_se"])})
    return out


_COLUMNS = [
    {"name": "test", "label": "Test", "field": "test", "align": "left"},
    {"name": "forecast", "label": "Forecast", "field": "forecast", "align": "right"},
    {"name": "simple", "label": "Simple guess", "field": "simple", "align": "right"},
    {"name": "gain", "label": "Gain ± SE", "field": "gain", "align": "right"},
    {"name": "verdict", "label": "Verdict", "field": "verdict", "align": "left"},
]


class _Section:
    """One backtest: a card with its description, a Rerun button and its result table."""

    def __init__(self, title: str, description: str, compute: Callable[[], Dict[str, Any]],
                 render: Callable[[Dict[str, Any]], None]) -> None:
        self.compute, self.render = compute, render
        with ui.card().classes("w-full").style("background:#1c212e"):
            with ui.row().classes("items-center w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(title).classes("text-lg font-medium")
                    ui.label(description).classes("text-sm").style(_MUTED)
                ui.space()
                self.button = ui.button("Rerun", icon="refresh", on_click=self.refresh).props("flat no-caps")
            self.body = ui.column().classes("w-full gap-1")

    async def refresh(self) -> None:
        self.button.disable()
        self.body.clear()
        with self.body:
            ui.spinner(size="lg")
        try:
            result = await run.io_bound(self.compute)
        except Exception as e:  # noqa: BLE001 - shown on the page, not swallowed
            self.body.clear()
            with self.body:
                ui.label(f"Failed: {e}").style("color:#ef5350")
            return
        finally:
            self.button.enable()
        self.body.clear()
        with self.body:
            self.render(result)
            ui.label(f"Run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC").classes("text-xs").style(_MUTED)


def _table(rows: List[Dict[str, Any]], columns=_COLUMNS) -> None:
    table = ui.table(columns=columns, rows=rows, row_key="key").classes("w-full").props("dense flat")
    table.style("background:#1c212e")
    table.add_slot("body-cell-verdict", _VERDICT_SLOT)


def show_backtests_page(conn) -> None:
    with ui.column().classes("w-full p-4 gap-3"):
        ui.label("Backtests").classes("text-2xl font-medium")
        ui.label("Each forecast against a simple guess on the same sessions, using only what was known "
                 "before each session. Gain > 0 means the forecast did better; 'better' / 'worse' need more "
                 "than two standard errors - anything less is not distinguishable from luck.").classes(
            "text-sm").style(_MUTED)

        def render_first_hour_model(r):
            if not r["sessions"]:
                ui.label("No sessions to score yet: the model needs about 100 complete sessions of history.").style(
                    _MUTED)
                return
            ui.label(f"{r['sessions']} sessions from {r['first_day']}, each forecast before its open from the "
                     f"sessions before it. Simple guesses: no move, and the usual range (the median of the previous "
                     f"40 sessions).").classes("text-xs").style(_MUTED)
            _table(first_hour_model_rows(r))

        def render_first_hour(r):
            if not r["sessions"]:
                ui.label("No sessions to score yet: backfill the NQ history first.").style(_MUTED)
                return
            ui.label(f"{r['sessions']} sessions, {r['k']} pre-open matches each. Simple guess: the median range "
                     f"of the last 40 sessions, and the base rates.").classes("text-xs").style(_MUTED)
            _table(first_hour_rows(r))
            ui.label(f"10:29 close inside the fan (ideal 80 % / 50 %): as built {r['fan_80'] * 100:.0f} % / "
                     f"{r['fan_50'] * 100:.0f} %, calibrated {r['fan_80_calibrated'] * 100:.0f} % / "
                     f"{r['fan_50_calibrated'] * 100:.0f} % ({r['calibrated_sessions']} sessions)").classes("text-sm")

        def render_scenario(r):
            if not r["sessions"]:
                ui.label("No sessions to score yet: backfill the NQ history first.").style(_MUTED)
                return
            ui.label(f"{r['sessions']} sessions, {r['k']} analogues each; first hour up / down beyond "
                     f"±{r['band']:.2f} ATR, else flat. Simple guess: the base rates of all earlier sessions, "
                     f"and always calling the most common outcome.").classes("text-xs").style(_MUTED)
            _table(scenario_rows(r))

        def render_model(rows):
            if not rows:
                ui.label(f"No stored {models_v2.DEFAULT_MODEL} forecasts with outcomes yet: run "
                         f"nq_forecast_v2.py backfill.").style(_MUTED)
                return
            _table(rows, [_COLUMNS[0], {"name": "n", "label": "Sessions", "field": "n", "align": "right"}]
                   + _COLUMNS[1:])

        sections = [
            _Section("First-hour model",
                     "The Session Explorer's generated first hour, replayed walk-forward on every complete NQ session.",
                     lambda: first_hour_model.scorecard(first_hour_model.load_history(conn, "NQ")),
                     render_first_hour_model),
            _Section("First hour forecast from pre-open matches · 09:30–10:30",
                     "The forecast the first-hour model replaced, replayed walk-forward on every stored NQ session - "
                     "kept for comparison.",
                     lambda: first_hour.backtest(conn, "NQ"), render_first_hour),
            _Section("Opening scenario generator",
                     "The analogue forecast of the first hour, replayed walk-forward.",
                     lambda: analogue.backtest(conn), render_scenario),
            _Section(f"Trained model · {models_v2.DEFAULT_MODEL}",
                     f"Its stored forecasts against the {models_v2.CLIMATOLOGY['model_version']} baseline on the "
                     f"same sessions, log loss per target, from the outcomes recorded so far.",
                     lambda: model_rows(conn), render_model),
        ]
    for section in sections:
        ui.timer(0.1, section.refresh, once=True)
