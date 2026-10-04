# dashboard/views/forecast.py
"""
Forecast (guideline revision 2, 3F): one stored forecast run, read by its run id -
never recomputed, never silently switched to a newer run. ``/forecast?run=<id>``
opens a run directly; picking a session lists its runs, newest first, each with its
status, so a superseded or late run stays visible.

  provenance     run id, lifecycle (issued at the database clock, or why not), mode,
                 versions, code revision, evidence ids and digest, the run it
                 supersedes
  chart          the snapshot's own frozen 2m buckets, moving averages and levels
                 (dashboard/components/preopen.frozen_preopen_spec) - not the bars
                 table, which a vendor revision can change
  per target     P1's predicted property, the class or why there is none, the exact
                 distribution as percentages, and its denominators: analogues with a
                 label (and without), prior sessions with a label (and without)
  P1 record      the 47 fields from the snapshot, annotation, analogue set and this
                 run, each with its basis
  outcome        hidden until "Show realised outcome": P2's record of the session's
                 latest outcome revision, beside the run's classes - a view, not a
                 score (stage 4 scores registered runs)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from nicegui import ui

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.preopen import frozen_preopen_spec
from database import journal_store as store
from forecaster.forecast_display import target_rows
from forecaster.outcome_display import p2_record
from forecaster.preopen_display import p1_record

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs"
_STATUS_COLOR = {"issued": "#26a69a", "unavailable": "#787b86", "late": "#ffa726", "failed": "#ef5350",
                 "invalid": "#ef5350"}
_FORECAST_FIELDS = set(range(25, 37)) | set(range(42, 46))


class ForecastPage:
    def __init__(self, conn, run_id: Optional[str] = None) -> None:
        self.conn = conn
        self.runs = store.list_forecast_runs(conn, "2000-01-01", "2100-01-01", profile=defs.DEFAULT_PROFILE)
        self.by_day: Dict[str, List[Dict[str, Any]]] = {}
        for r in self.runs:
            self.by_day.setdefault(r["session_date"], []).append(r)
        self.days = sorted(self.by_day, reverse=True)
        self.requested = store.get_forecast_run(conn, run_id) if run_id else None
        self.run: Optional[Dict[str, Any]] = None
        self.outcome_shown = False

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        with ui.column().classes("w-full p-4 gap-3"):
            ui.label("Forecast").classes("text-2xl font-medium")
            if not self.days:
                ui.label("No forecast runs yet. They are issued by catch-up (the collector's journal step), or: "
                         "python scripts/nq_journal.py forecast --start 2025-09-01 --end 2026-10-02").style(_MUTED)
                return
            ui.label(f"Deterministic forecasts from the run's frozen evidence: the baseline ({fc.BASELINE_VERSION}, "
                     f"stage 4 arm B) smooths the selected analogues with the earlier sessions; the prior "
                     f"({fc.PRIOR_VERSION}, arm A) is the earlier sessions alone. A historical replay is research on "
                     f"reconstructed evidence, never a timely live forecast. The realised outcome stays hidden "
                     f"until you show it.").classes("text-sm").style(_MUTED)
            first = self.requested["session_date"] if self.requested else self.days[0]
            with ui.row().classes("w-full items-center gap-4"):
                self.day_select = ui.select(self.days, value=first, label="Session", with_input=True,
                                            on_change=lambda e: self.pick_day(e.value)).classes("w-52")
                self.run_select = ui.select({}, label="Run", on_change=lambda e: self.show(e.value)).classes("w-[30rem]")
                ui.switch("Show realised outcome", value=False, on_change=self.toggle_outcome)
            self.provenance = ui.column().classes("w-full gap-0")
            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=520)
                with ui.card().classes("w-[620px] shrink-0").style("background:#1c212e"):
                    self.targets = ui.column().classes("w-full gap-0")
            self.outcome = ui.column().classes("w-full gap-0")
            with ui.expansion("P1 record (47 fields)", icon="list_alt", value=True).classes("w-full").style(
                    "background:#1c212e"):
                self.record = ui.column().classes("w-full gap-0")
        self.pick_day(first, self.requested["run_id"] if self.requested else None)

    def _run_label(self, r: Dict[str, Any]) -> str:
        arm = {fc.PRIOR_VERSION: "prior (arm A)", fc.BASELINE_VERSION: "baseline (arm B)"}.get(
            r["algorithm_version"], r["algorithm_version"])
        return (f"{r['run_id'][:8]} · {arm} · {r['lifecycle_status']} · {r['mode'].replace('_', ' ')} · "
                f"{str(r['created_at'])[:16]} UTC")

    def pick_day(self, day: Optional[str], run_id: Optional[str] = None) -> None:
        if not day:
            return
        runs = self.by_day.get(day, [])
        self.run_select.set_options({r["run_id"]: self._run_label(r) for r in runs},
                                    value=run_id or (runs[0]["run_id"] if runs else None))

    def toggle_outcome(self, event) -> None:
        self.outcome_shown = bool(event.value)
        self._render_outcome()

    def show(self, run_id: Optional[str]) -> None:
        if not run_id:
            return
        self.run = store.get_forecast_run(self.conn, run_id)
        self.snapshot = store.get_snapshot(self.conn, self.run["snapshot_id"])
        self.annotation = store.get_annotation(self.conn, self.run["annotation_id"]) if self.run["annotation_id"] else None
        self.aset = store.get_analogue_set(self.conn, self.run["analogue_set_id"]) if self.run["analogue_set_id"] else None
        self._render_provenance()
        self.chart.apply(frozen_preopen_spec(self.snapshot))
        self._render_targets()
        self._render_record()
        self._render_outcome()

    # -- rendering --------------------------------------------------------------

    def _render_provenance(self) -> None:
        r = self.run
        self.provenance.clear()
        with self.provenance:
            status = (f"issued {str(r['issued_at'])[:19]} UTC (database clock)" if r["issued_at"]
                      else f"{r['lifecycle_status']}: {r['failure_reason']}")
            ui.html(f"<b>Run {r['run_id']}</b> - <span style='color:{_STATUS_COLOR[r['lifecycle_status']]}'>"
                    f"{status}</span> · {r['mode'].replace('_', ' ')} · cutoff {str(r['input_cutoff_at'])[:16]} UTC"
                    ).classes("text-sm")
            ui.label(f"{r['algorithm_version']} · {r['schema_version']} · {r['issue_policy']} · labels "
                     f"{r['label_version']} · code {r['code_revision'][:12]}").classes("text-xs").style(_MUTED)
            ev = r["evidence"] or {}
            members = ", ".join(f"{m['session_date']} ({float(m['similarity']):.0f}%)" for m in ev.get("members") or [])
            mode = f" ({ev['data_mode'].replace('_', ' ')})" if ev.get("data_mode") else ""
            protocol = f" ({ev['protocol_version']})" if ev.get("protocol_version") else ""
            ui.label(f"evidence: snapshot {r['snapshot_id'][:8]}{mode}, annotation "
                     f"{(r['annotation_id'] or 'none')[:8]}{protocol}, analogue set {(r['analogue_set_id'] or 'none')[:8]}"
                     f"; digest {r['evidence_digest'][:12]}"
                     + (f"; supersedes {r['supersedes_run_id'][:8]}" if r["supersedes_run_id"] else "")
                     ).classes("text-xs").style(_MUTED)
            if ev.get("prior"):
                ui.label(f"analogues {members or 'none'}; prior {ev['prior']['sessions']} earlier session(s) - "
                         f"{ev['prior']['known_as_of']}").classes("text-xs").style(_MUTED)

    def _render_targets(self) -> None:
        self.targets.clear()
        with self.targets:
            ui.label("Per target (baseline distribution and its denominators)").classes("text-sm font-medium")
            rows = target_rows(self.run)
            if not rows:
                ui.label(f"No predictions: {self.run['failure_reason']}").classes("text-xs").style(_MUTED)
                return
            with ui.grid(columns="minmax(0,1.1fr) minmax(0,0.9fr) minmax(0,1.6fr) minmax(0,0.9fr)").classes(
                    "w-full gap-x-2 gap-y-1"):
                for head in ("P1 property", "class", "distribution", "n / prior"):
                    ui.label(head).classes(_CELL).style(_MUTED)
                for r in rows:
                    ui.label(r["property"]).classes(_CELL).style(_MUTED)
                    ui.label(r["class"] if r["status"] == "predicted" else f"{r['class']} - {r['reason']}").classes(
                        _CELL + " break-words")
                    ui.label(", ".join(f"{k} {v}" for k, v in r["distribution"].items()) or "-").classes(
                        _CELL + " break-words")
                    ui.label(f"{r['eligible']} (+{r['without_label']}) / {r['prior_sessions']} "
                             f"(+{r['prior_without_label']})").classes(_CELL)
            ui.label("n: analogues with a label (+ without); prior: earlier sessions with a label (+ without). "
                     "Estimates are conditional on classifiable outcomes.").classes("text-[10px]").style(_MUTED)

    def _render_record(self) -> None:
        self.record.clear()
        provenance, rows = p1_record(self.snapshot, self.annotation, self.aset, self.run)
        with self.record:
            ui.label(provenance).classes("text-xs").style(_MUTED)
            with ui.grid(columns="2.5rem minmax(0,1fr) minmax(0,1fr) minmax(0,2fr)").classes("w-full gap-x-2 gap-y-0"):
                for i, (prop, value, basis) in enumerate(rows, 1):
                    style = "background:rgba(41,98,255,0.10)" if i in _FORECAST_FIELDS else ""
                    ui.label(str(i)).classes(_CELL).style(_MUTED + ";" + style)
                    ui.label(prop).classes(_CELL).style(_MUTED + ";" + style)
                    ui.label(value).classes(_CELL + " break-words").style(style)
                    ui.label(basis).classes(_CELL + " break-words text-[10px]").style(_MUTED + ";" + style)

    def _render_outcome(self) -> None:
        self.outcome.clear()
        if not self.outcome_shown or self.run is None:
            return
        outcome = store.latest_outcome(self.conn, self.run["snapshot_id"], self.run["label_version"])
        with self.outcome:
            if outcome is None:
                ui.label("No realised outcome recorded yet.").style(_MUTED)
                return
            ui.label(f"Realised outcome r{outcome['outcome_revision']} ({self.run['label_version']}) - a view, not "
                     f"a score").classes("text-sm font-medium")
            with ui.grid(columns="minmax(0,1.2fr) minmax(0,1fr) minmax(0,1fr)").classes("w-full gap-x-2 gap-y-0"):
                for head in ("target", "forecast class", "realised"):
                    ui.label(head).classes(_CELL).style(_MUTED)
                for name, target in fc.FORECAST_TARGETS:
                    p = self.run["predictions"].get(target)
                    realised = outcome["labels"][target]
                    ui.label(name).classes(_CELL).style(_MUTED)
                    ui.label(defs.display(target, p["predicted_label"], "predicted") if p and p["predicted_label"]
                             else "Unavailable").classes(_CELL)
                    ui.label(defs.display(target, realised["label"]) if realised["label"]
                             else f"Unavailable ({realised['reason']})").classes(_CELL)
            with ui.expansion("P2 outcome record (40 fields)", value=False).classes("w-full"):
                for prop, value in p2_record(self.snapshot, outcome, self.run["label_version"]):
                    with ui.row().classes("w-full gap-2 no-wrap"):
                        ui.label(prop).classes(_CELL + " w-72 shrink-0").style(_MUTED)
                        ui.label(value).classes(_CELL + " break-words")


def show_forecast_page(conn, run_id: Optional[str] = None) -> None:
    ForecastPage(conn, run_id).build()
