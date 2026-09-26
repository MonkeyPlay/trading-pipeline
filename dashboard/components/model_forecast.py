# dashboard/components/model_forecast.py
"""
The v2 model forecast of a session (forecaster/models_v2.py): the five
nq_schema_v2 targets with their status, predicted label, full probability
distribution and - once the session is over and labelled - what happened.

It shows the newest stored run of the chosen model for the selected day (the
live capture's when there is one). "Run model" computes one from the stored
bars: a historical-reconstruction snapshot and a walk-forward fit on the
outcomes of earlier sessions, exactly as ``scripts/nq_forecast_v2.py forecast``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from nicegui import run, ui

from database import forecast_store as store
from features import catalogue as catv2
from features.market_data import DbMarketData
from features.nq_v2 import SnapshotError, build_snapshot
from forecaster import labels_v2, models_v2

TARGET_NAMES = {
    "first_move_5m": "First move · 5 min",
    "opening_type_15m": "Opening type · 15 min",
    "direction_15m": "Direction · 15 min",
    "direction_rth": "Direction · RTH",
    "session_type_rth": "Session type · RTH",
}

_MUTED = "color:#787b86"
_LABEL_COLOURS = {
    "up": "#26a69a", "up_first": "#26a69a", "drive_up": "#26a69a", "bull_trend": "#26a69a",
    "sweep_low_rebound": "#80cbc4",
    "down": "#ef5350", "down_first": "#ef5350", "drive_down": "#ef5350", "bear_trend": "#ef5350",
    "sweep_high_reverse": "#ef9a9a",
    "flat": "#787b86", "neither": "#787b86", "range": "#787b86",
    "two_sided": "#ab47bc", "two_sided_volatile": "#ab47bc", "reversal": "#ffa726",
    "mixed": "#5c6bc0",
}
_STATUS_COLOURS = {"issued": "#d1d4dc", "abstained": "#ffa726", "unavailable": "#787b86"}


def _colour(label: str) -> str:
    return _LABEL_COLOURS.get(label, "#90a4ae")


def register_versions(conn) -> None:
    """Feature, label and model definitions must be registered before a run is stored."""
    store.register_feature_version(conn, catv2.registry_record())
    store.register_label_version(conn, labels_v2.label_registry_record())
    for model in models_v2.MODELS.values():
        store.register_model_version(conn, models_v2.registry_record(model))


def compute_forecast(conn, session_date: str, model_version: str) -> str:
    """Snapshot (reused when identical) + walk-forward fit + stored run; returns the run id."""
    register_versions(conn)
    snap = build_snapshot(DbMarketData(conn), session_date)
    snapshot_id, _ = store.save_feature_snapshot(conn, snap)
    snapshot = store.get_feature_snapshot(conn, snapshot_id)
    run_rec, predictions = models_v2.predict(conn, snapshot, models_v2.MODELS[model_version])
    return store.save_forecast_run(conn, run_rec, predictions)


class ModelForecastPanel:
    """A card showing the selected day's v2 model forecast."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.symbol: Optional[str] = None
        self.date: Optional[str] = None
        self.model = models_v2.DEFAULT_MODEL

    def build(self) -> None:
        with ui.card().classes("w-full mt-2").style("background:#1c212e"):
            with ui.row().classes("items-center w-full"):
                with ui.column().classes("gap-0"):
                    ui.label("Model forecast").classes("text-lg font-medium")
                    ui.label("Five opening and session targets from the trained model, with the full "
                             "probability distribution of each.").classes("text-sm").style(_MUTED)
                ui.space()
                ui.select({m: m for m in models_v2.MODELS}, value=self.model, label="Model",
                          on_change=self._on_model).classes("w-48")
                self.run_button = ui.button("Run model", icon="model_training",
                                            on_click=self._run).props("color=primary outline")
            self.body = ui.column().classes("w-full gap-1")

    # ------------------------------------------------------------------

    def show(self, symbol: Optional[str], date: Optional[str]) -> None:
        self.symbol, self.date = symbol, date
        self._render()

    def _on_model(self, event) -> None:
        self.model = event.value
        self._render()

    async def _run(self) -> None:
        if self.symbol != catv2.TARGET_SYMBOL or not self.date:
            return
        self.run_button.props("loading")
        try:
            await run.io_bound(compute_forecast, self.conn, self.date, self.model)
        except SnapshotError as e:
            ui.notify(f"No snapshot for {self.date}: {e}", type="warning")
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            ui.notify(f"Model run failed: {e}", type="negative")
        finally:
            if not self.run_button.is_deleted:
                self.run_button.props(remove="loading")
        self._render()

    def _render(self) -> None:
        if self.body.is_deleted:        # the page was closed while a run was computing
            return
        self.body.clear()
        is_target = self.symbol == catv2.TARGET_SYMBOL
        self.run_button.set_enabled(is_target and bool(self.date))
        with self.body:
            if not is_target:
                ui.label(f"The model forecasts {catv2.TARGET_SYMBOL} only; select an "
                         f"{catv2.TARGET_SYMBOL} contract.").classes("text-sm").style(_MUTED)
                return
            if not self.date:
                return
            day = store.get_day_forecast(self.conn, self.date, self.model, catv2.FEATURE_VERSION,
                                         catv2.TARGET_SYMBOL)
            if day is None:
                ui.label(f"No {self.model} forecast stored for {self.date}. Run model computes one "
                         f"from the stored bars, trained on earlier sessions only."
                         ).classes("text-sm").style(_MUTED)
                return
            self._render_run(day)

    def _render_run(self, day: Dict[str, Any]) -> None:
        r = day["run"]
        mode = "live capture" if r["data_mode"] == "live_capture" else "historical reconstruction"
        ui.label(
            f"Run {str(r['generated_at'])[:16]} UTC · {mode} · input {r['input_quality_status']} · "
            f"{r['pit_availability_status'].replace('_', ' ')}"
        ).classes("text-xs").style(_MUTED)
        training = (r.get("calibration") or {}).get("training", {})
        with ui.grid(columns="190px 190px minmax(0,1fr) 170px").classes("w-full items-center gap-x-4 gap-y-2"):
            for head in ("Target", "Forecast", "Probabilities", "Actual"):
                ui.label(head).classes("text-xs uppercase").style(_MUTED)
            for target in labels_v2.TARGETS:
                p = day["predictions"].get(target)
                if p is None:
                    continue
                self._render_target(target, p, day["outcomes"].get(target), training.get(target, {}))

    def _render_target(self, target: str, p: Dict[str, Any], outcome, fit: Dict[str, Any]) -> None:
        method = fit.get("selected")
        with ui.column().classes("gap-0"):
            ui.label(TARGET_NAMES.get(target, target)).classes("text-sm")
            if method:
                ui.label(f"{method} · n={fit.get('n')}").classes("text-xs").style(_MUTED)

        status = p.get("prediction_status") or ("abstained" if p["abstained"] else "issued")
        with ui.column().classes("gap-0"):
            if status == "issued":
                ui.label(p["predicted_label"]).classes("text-sm font-medium").style(
                    f"color:{_colour(p['predicted_label'])}")
            else:
                ui.label(status).classes("text-sm").style(f"color:{_STATUS_COLOURS[status]}")
                reason = (p.get("decision_reason") or "").replace("_", " ")
                ui.label(reason).classes("text-xs").style(_MUTED).tooltip(p.get("abstention_reason") or "")

        probs = p.get("probabilities") or {}
        with ui.column().classes("gap-0 w-full"):
            if probs:
                ranked = sorted(probs.items(), key=lambda kv: -kv[1])
                with ui.row().classes("w-full no-wrap gap-0").style(
                        "height:10px;border-radius:3px;overflow:hidden;background:#2a2e39"):
                    # Vocabulary order: JSONB does not keep the stored key order.
                    for label in labels_v2.TARGETS[target]["labels"]:
                        value = probs.get(label, 0.0)
                        ui.element("div").style(
                            f"width:{value * 100:.2f}%;background:{_colour(label)};height:100%"
                        ).tooltip(f"{label}: {value * 100:.1f} %")
                ui.label(" · ".join(f"{lab} {v * 100:.0f} %" for lab, v in ranked)).classes(
                    "text-xs").style("color:#b2b5be")
            else:
                ui.label("no distribution").classes("text-xs").style(_MUTED)

        with ui.row().classes("items-center gap-1"):
            if outcome is None:
                ui.label("pending").classes("text-xs").style(_MUTED)
            elif outcome["actual_label"] is None:
                ui.label(f"ineligible · {(outcome['ineligibility_reason'] or '').replace('_', ' ')}"
                         ).classes("text-xs").style(_MUTED)
            else:
                actual = outcome["actual_label"]
                ui.label(actual).classes("text-sm").style(f"color:{_colour(actual)}")
                if status == "issued":
                    hit = actual == p["predicted_label"]
                    ui.icon("check" if hit else "close").style(
                        f"color:{'#26a69a' if hit else '#ef5350'};font-size:16px")
