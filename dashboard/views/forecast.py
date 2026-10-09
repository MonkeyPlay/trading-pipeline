# dashboard/views/forecast.py
"""
Forecast (guideline revision 2, 3F), at the bottom of the Session Explorer: the NQ
forecast of the session bar's day. A stored run is read by its run id - never
recomputed, never silently switched to a newer run; ``/?run=<id>`` opens a run
directly (on its day). The day's runs are listed newest first, each with its status,
so a superseded or late run stays visible.

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

For the session in progress, a target whose window has ended by the newest bar
stored (the first move at 09:35, the 15-minute targets at 09:45, the opening bias
at 10:00) is marked as observed: what the run said about it is a record, no longer
a forecast of something still to come.

Beside the stored runs, the Forecast now tab (``/?view=preview``) shows the latest
preview (forecaster/preview.py): the next session's forecast from the data so far -
whatever day the bar shows - made by its own button at any time from the session's
Globex open: the latest bars collected, then the evidence as of now, its rule-based
annotation, analogues and both arms, all in memory and never stored. The same chart,
per-target view and P1 record as a stored run, with what is not known yet shown as
unavailable.

After a job runs from the header, the day's runs are read again with the bar
(``show_day(keep=True)``): the run shown stays shown - a run never changes once
stored. ``reload`` reads the preview again; a Forecast now job switches to its tab.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nicegui import ui

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs
from dashboard.components.fan import current_session
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.preopen import frozen_preopen_spec
from dashboard.jobs import RUNNER
from database import journal_store as store
from features import calendar as cal
from forecaster import preview as pv
from forecaster.forecast_display import target_rows
from forecaster.grading import BENCHMARK, compare, current_runs, grade
from forecaster.outcome_display import p2_record
from forecaster.preopen_display import p1_record
from forecaster.rth_analogues import newest_bar_end, windows_over

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs"
_STATUS_COLOR = {"issued": "#26a69a", "unavailable": "#787b86", "late": "#ffa726", "failed": "#ef5350",
                 "invalid": "#ef5350"}
_FORECAST_FIELDS = set(range(25, 37)) | set(range(42, 46))
_ARMS = {v: f"{fc.ARM_NAMES[a]} (arm {a})" for a, v in fc.ARMS.items()}
# The arms on the grading radar: A, the benchmark, a dashed neutral grey; B-D the first three categorical slots of
# the dark palette (validated all-pairs against #1c212e and #131722: CVD dE 9.4, normal-vision dE 20.9, >= 3:1).
_ARM_COLOR = {"A": "#9598a1", "B": "#3987e5", "C": "#d95926", "D": "#199e70"}
_SHORT = {"opening_bias_30m": "30-min bias", "first_move_5m": "First move", "opening_type_15m": "Opening type",
          "direction_15m": "15-min direction", "session_type_rth": "Session type", "close_direction_rth": "RTH close",
          "first_level_tested": "First level"}
_EMPTY_SPEC = {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []}


def _et(value: str, fmt: str = "%H:%M") -> str:
    """A journal timestamp ('YYYY-MM-DD HH:MM:SS' or ISO, UTC) in New York time."""
    t = datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T")).replace(tzinfo=timezone.utc)
    return f"{t.astimezone(cal.NY_TZ):{fmt}} ET"


def _targets_grid(run: Dict[str, Any], title: str, over: Optional[Dict[str, str]] = None) -> None:
    """A run's per-target view where it is called: class, distribution and denominators; ``over`` - target -> the
    ET end of its window - marks the targets of a session in progress already observed."""
    ui.label(title).classes("text-sm font-medium")
    over = over or {}
    rows = target_rows(run)
    if not rows:
        ui.label(f"No predictions: {run['failure_reason']}").classes("text-xs").style(_MUTED)
        return
    with ui.grid(columns="minmax(0,1.1fr) minmax(0,0.9fr) minmax(0,1.6fr) minmax(0,0.9fr)").classes(
            "w-full gap-x-2 gap-y-1"):
        for head in ("P1 property", "class", "distribution", "n / prior"):
            ui.label(head).classes(_CELL).style(_MUTED)
        for r in rows:
            done = over.get(r["target"])
            ui.label(r["property"] + (f" — window over at {done} ET: observed, no longer a forecast" if done else "")
                     ).classes(_CELL + " break-words").style(_MUTED + (";color:#ffa726" if done else ""))
            ui.label(r["class"] if r["status"] == "predicted" else f"{r['class']} - {r['reason']}").classes(
                _CELL + " break-words")
            ui.label(", ".join(f"{k} {v}" for k, v in r["distribution"].items()) or "-").classes(
                _CELL + " break-words")
            ui.label(f"{r['eligible']} (+{r['without_label']}) / {r['prior_sessions']} "
                     f"(+{r['prior_without_label']})").classes(_CELL)
    ui.label("n: analogues with a label (+ without); prior: earlier sessions with a label (+ without). "
             "Estimates are conditional on classifiable outcomes.").classes("text-[10px]").style(_MUTED)


def _record_grid(snapshot, annotation, aset, run) -> None:
    """P1's 47-field record where it is called, the forecast's fields shaded."""
    provenance, rows = p1_record(snapshot, annotation, aset, run)
    ui.label(provenance).classes("text-xs").style(_MUTED)
    with ui.grid(columns="2.5rem minmax(0,1fr) minmax(0,1fr) minmax(0,2fr)").classes("w-full gap-x-2 gap-y-0"):
        for i, (prop, value, basis) in enumerate(rows, 1):
            style = "background:rgba(41,98,255,0.10)" if i in _FORECAST_FIELDS else ""
            ui.label(str(i)).classes(_CELL).style(_MUTED + ";" + style)
            ui.label(prop).classes(_CELL).style(_MUTED + ";" + style)
            ui.label(value).classes(_CELL + " break-words").style(style)
            ui.label(basis).classes(_CELL + " break-words text-[10px]").style(_MUTED + ";" + style)


class ForecastPanel:
    """
    Built at the bottom of the Session Explorer: ``show_day(day)`` follows the
    session bar's day; ``reload`` reads the preview again after a job.
    """

    def __init__(self, conn, panel=None) -> None:
        self.conn = conn
        self.day: Optional[str] = None
        self.runs: List[Dict[str, Any]] = []     # the day's runs, newest first
        self.run: Optional[Dict[str, Any]] = None
        self.outcome_shown = False
        self.arm: Optional[str] = None           # the arm shown for the day
        self.current: Dict[str, Dict[str, Any]] = {}   # per arm the day's newest issued run, in full
        self.panel = panel                       # the header's job control: its Forecast now starts the preview
        self.preview: Optional[Dict[str, Any]] = None

    def reload(self) -> None:
        """After a job: the preview read again, and the tab of a Forecast now or LLM job shown (the day's runs
        follow the session bar)."""
        self.preview = pv.load()
        self._render_preview()
        if RUNNER.job is not None and RUNNER.job.key == "preview":
            self.tabs.set_value("preview")
        elif RUNNER.job is not None and RUNNER.job.key == "llm":
            self.tabs.set_value("stored")

    def show_preview(self) -> None:
        """The Forecast now tab, scrolled to."""
        self.tabs.set_value("preview")
        self.box.set_value(True)
        self.scroll_into_view()

    def scroll_into_view(self) -> None:
        ui.timer(0.5, lambda: ui.run_javascript(
            f"document.getElementById('c{self.box.id}')?.scrollIntoView({{behavior: 'smooth'}})"), once=True)

    # -- layout ---------------------------------------------------------------

    def build(self, view: Optional[str] = None) -> None:
        view = "preview" if view == "preview" else "stored"
        self.box = ui.expansion("Forecast", icon="insights", value=True).classes("w-full")
        with self.box:
            with ui.tabs(value=view).props("dense no-caps inline-label align=left") as self.tabs:
                ui.tab("stored", label="Stored runs", icon="inventory_2")
                ui.tab("preview", label="Forecast now", icon="bolt")
            with ui.tab_panels(self.tabs, value=view).props("keep-alive").classes("w-full").style(
                    "background:transparent"):
                with ui.tab_panel("stored").classes("p-0 pt-3 gap-3"):
                    self._build_stored()
                with ui.tab_panel("preview").classes("p-0 pt-3 gap-3"):
                    self._build_preview()

    def _build_stored(self) -> None:
        ui.label(f"Forecasts of the session day's NQ session from each run's frozen evidence, by arm (stage 4): A "
                 f"the prior ({fc.PRIOR_VERSION}, the earlier sessions alone, the benchmark); B the baseline "
                 f"({fc.BASELINE_VERSION}, the rule-based analogues smoothed with the prior); C the restricted LLM "
                 f"({fc.RESTRICTED_VERSION}, Claude's overnight and premarket structure, matched and smoothed like "
                 f"B); D the synthesis ({fc.SYNTHESIS_VERSION}, Claude's own forecast from B's evidence). C and D run "
                 f"only when started by hand (Update data, Run LLM forecast). A historical replay is research on "
                 f"reconstructed evidence, never a timely live forecast. The realised outcome and the grading stay "
                 f"hidden until you show them.").classes("text-sm").style(_MUTED)
        with ui.row().classes("w-full items-center gap-4"):
            self.run_select = ui.select({}, label="Run", on_change=lambda e: self.show(e.value)).classes("w-[30rem]")
            ui.switch("Show realised outcome", value=False, on_change=self.toggle_outcome)
        with ui.card().classes("w-full gap-3").style("background:#1c212e"):
            with ui.row().classes("w-full items-center gap-3"):
                self.arm_title = ui.label().classes("text-base font-medium")
                ui.label("one tile per arm: the one shown below is highlighted - click another to show it").classes(
                    "text-xs").style(_MUTED)
                ui.space()
                self.llm_day_button = ui.button("Run C and D for this day", icon="psychology",
                                                on_click=self._run_llm_for_day).props("dense outline no-caps")
            self.arm_row = ui.element("div").classes("w-full grid gap-3").style(
                "grid-template-columns:repeat(4,minmax(0,1fr))")
            ui.separator().style("background:#2a2e39")
            self.grading = ui.column().classes("w-full gap-2")
        with ui.column().classes("w-full gap-3") as self.run_body:
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

    def _run_label(self, r: Dict[str, Any]) -> str:
        arm = _ARMS.get(r["algorithm_version"], r["algorithm_version"])
        return (f"{r['run_id'][:8]} · {arm} · {r['lifecycle_status']} · {r['mode'].replace('_', ' ')} · "
                f"{str(r['created_at'])[:16]} UTC")

    def show_day(self, day: Optional[str], run_id: Optional[str] = None, keep: bool = False) -> None:
        """
        The day's runs, read again: ``run_id`` opened when it is one of them; ``keep`` keeps the run shown (a run
        never changes once stored) - after a job, which may have added runs.
        """
        self.day = day
        self.box.text = f"Forecast of NQ {day}" if day else "Forecast"
        self.runs = store.list_forecast_runs(self.conn, day, day, profile=defs.DEFAULT_PROFILE) if day else []
        self.pick_day(self.run_select.value if keep else run_id)

    def pick_day(self, run_id: Optional[str] = None) -> None:
        """The day's arms (the tiles and their comparison), then the runs of the arm shown - the requested
        run's, the arm shown before when the day has it, else arm B, else the first the day has."""
        runs = self.runs
        have = {fc.arm_of(r["algorithm_version"]) for r in runs}
        requested = next((r for r in runs if r["run_id"] == run_id), None)
        if requested is not None:
            self.arm = fc.arm_of(requested["algorithm_version"])
        elif self.arm not in have:
            self.arm = "B" if "B" in have else next((a for a in fc.ARMS if a in have), None)
        self.current = {a: store.get_forecast_run(self.conn, r["run_id"]) for a, r in current_runs(runs).items()}
        self._render_arms(self.day, runs)
        self._render_grading()
        mine = [r for r in runs if fc.arm_of(r["algorithm_version"]) == self.arm]
        self.run_select.set_options({r["run_id"]: self._run_label(r) for r in mine},
                                    value=run_id if requested is not None else (mine[0]["run_id"] if mine else None))
        self.run_select.set_enabled(bool(mine))
        self.run_body.set_visibility(bool(mine))
        if not mine:
            self.run = None
            self.chart.apply(dict(_EMPTY_SPEC))

    def pick_arm(self, arm: str) -> None:
        self.arm = arm
        self.pick_day()

    def _render_arms(self, day: str, runs: List[Dict[str, Any]]) -> None:
        """One tile per arm: whether it ran for the session (issued, failed or no run) and what it rests on; the
        arm shown below highlighted. A tile with runs shows that arm when clicked."""
        shown = f"arm {self.arm} · {fc.ARM_NAMES[self.arm]}" if self.arm else "no run"
        self.arm_title.set_text(f"Arms for {day} - shown: {shown}")
        done = all(a in self.current for a in "CD")
        self.llm_day_button.set_text("C and D issued for this day" if done else f"Run C and D for {day}")
        self.llm_day_button.set_enabled(not done and self.panel is not None)
        self.arm_row.clear()
        with self.arm_row:
            for arm in fc.ARMS:
                mine = [r for r in runs if fc.arm_of(r["algorithm_version"]) == arm]
                is_shown = arm == self.arm
                tile = ui.element("div").classes("rounded px-3 py-2 flex flex-col gap-1"
                                                 + (" cursor-pointer" if mine else ""))
                tile.style(f"border-top:3px {'dashed' if arm == BENCHMARK else 'solid'} {_ARM_COLOR[arm]};"
                           f"background:{'rgba(41,98,255,0.16)' if is_shown else '#131722'};"
                           f"outline:{'1px solid #2962ff' if is_shown else 'none'};{'' if mine else 'opacity:0.55'}")
                if mine:
                    tile.on("click", lambda a=arm: self.pick_arm(a))
                with tile:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.label(f"{arm} · {fc.ARM_NAMES[arm]}").classes("text-sm font-medium")
                        if arm == BENCHMARK:
                            ui.label("benchmark").classes("text-xs").style(_MUTED)
                        ui.space()
                        if is_shown:
                            ui.badge("shown", color="primary")
                    status, detail = self._arm_status(arm, mine, self.current.get(arm))
                    ui.label(status).classes("text-xs")
                    ui.label(detail).classes("text-xs break-words").style(_MUTED)

    async def _run_llm_for_day(self) -> None:
        """Arms C and D for the day shown: the same plan and confirmation as Update data (Claude requests are
        sent only after its Send)."""
        if self.panel is None:
            ui.notify("Arms C and D run from the Update data control in the header.", type="warning")
            return
        await self.panel.llm_forecast(days=[self.day], arms="CD", batch=False)

    @staticmethod
    def _arm_status(arm: str, mine: List[Dict[str, Any]], current: Optional[Dict[str, Any]]):
        """``(status, detail)`` of one arm's tile."""
        if current is not None:
            ev = current.get("evidence") or {}
            n = len(ev.get("members") or [])
            detail = {"A": f"the prior of {(ev.get('prior') or {}).get('sessions', '?')} earlier session(s)",
                      "B": f"{n} rule-based analogue(s)",
                      "C": f"{n} analogue(s) from a pool of {ev.get('pool_size', 0)} annotated the same way",
                      "D": f"confidence {(current.get('outputs') or {}).get('confidence', '?')} of 5, from arm B's "
                           f"evidence"}[arm]
            return f"issued {str(current['issued_at'])[11:16]} UTC · {current['algorithm_version']}", detail
        if mine:
            return (f"{mine[0]['lifecycle_status']} - {len(mine)} attempt(s), none issued",
                    (mine[0]["failure_reason"] or "")[:110])
        return "no run for this session", ("Run C and D for this day (above), or Update data" if arm in "CD" else
                                           "Update data, Run forecaster")

    def toggle_outcome(self, event) -> None:
        self.outcome_shown = bool(event.value)
        self._render_outcome()
        self._render_grading()

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
            arm = fc.arm_of(r["algorithm_version"])
            ui.html((f"<b>Arm {arm} · {fc.ARM_NAMES[arm]}</b> · " if arm else "")
                    + f"<b>Run {r['run_id']}</b> - <span style='color:{_STATUS_COLOR[r['lifecycle_status']]}'>"
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
            attempt = ev.get("attempt") or {}
            if attempt:
                usage = attempt.get("usage") or {}
                ui.label(f"Claude: {attempt.get('model') or 'no answer'}"
                         + (f", {usage.get('input_tokens', 0):,} input / {usage.get('output_tokens', 0):,} output "
                            f"tokens" if usage else "") + f"; request {str(attempt.get('request_id'))[:8]}"
                         ).classes("text-xs").style(_MUTED)

    def _render_targets(self) -> None:
        self.targets.clear()
        day = str(self.run["session_date"])
        over = (windows_over(day, newest_bar_end(self.conn, day), [t for _, t in fc.FORECAST_TARGETS])
                if day == current_session() else {})
        with self.targets:
            _targets_grid(self.run, f"Per target ({_ARMS.get(self.run['algorithm_version'], self.run['algorithm_version'])}"
                                    f": distribution and denominators)", over)

    def _render_record(self) -> None:
        self.record.clear()
        with self.record:
            _record_grid(self.snapshot, self.annotation, self.aset, self.run)

    # -- forecast now (the preview) ----------------------------------------------

    def _build_preview(self) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            with ui.column().classes("gap-0"):
                self.preview_button = ui.button("Forecast now", icon="bolt", on_click=self._forecast_now).props(
                    "no-caps")
            self.preview_status = ui.html().classes("text-sm")
        self.preview_note = ui.label().classes("text-xs")
        ui.label("A preview of the next session's forecast from the data so far, at any time from its Globex open "
                 "(18:00 ET the evening before): the button collects the latest bars, then builds the evidence as of "
                 "now, annotates it by the rules, finds its analogues among the stored sessions and runs both arms - "
                 "in memory, never stored. What is not known yet is unavailable, never filled in, so the preview can "
                 "differ from the official forecast, which comes from the 09:31 ET snapshot (Stored runs)."
                 ).classes("text-sm").style(_MUTED)
        self.preview_meta = ui.column().classes("w-full gap-0")
        with ui.row().classes("w-full no-wrap gap-4 items-start"):
            with ui.column().classes("grow gap-1 min-w-0"):
                self.preview_chart = LightweightChart(height=520)
            with ui.card().classes("w-[620px] shrink-0").style("background:#1c212e"):
                self.preview_arm = ui.toggle({v: _ARMS[v] for v in fc.RULE_ALGORITHMS}, value=fc.BASELINE_VERSION,
                                             on_change=lambda e: self._render_preview_run()).props("dense no-caps")
                self.preview_targets = ui.column().classes("w-full gap-0")
        with ui.expansion("P1 record (47 fields)", icon="list_alt", value=False).classes("w-full").style(
                "background:#1c212e"):
            self.preview_record = ui.column().classes("w-full gap-0")
        self.preview = pv.load()
        self._render_preview()
        self._preview_button_state()
        ui.timer(2.0, self._preview_button_state)

    def _forecast_now(self) -> None:
        if self.panel is None:
            ui.notify("Forecast now runs from the Update data control in the header.", type="warning")
            return
        self.panel.forecast_now()

    def _preview_button_state(self) -> None:
        possible = self.panel is not None and self.panel.preview_possible
        self.preview_button.set_enabled(possible and not RUNNER.busy)
        note = ("" if self.panel is None else
                f"Forecast now is running ({RUNNER.job.title})" if RUNNER.busy else
                ("Possible " if possible else "Not possible now: ") + self.panel.preview_why)
        if note != self.preview_note.text:
            self.preview_note.set_text(note)
            self.preview_note.style(f"color:{'#26a69a' if possible else '#787b86'}")

    def _preview_run(self) -> Optional[Dict[str, Any]]:
        return next((r for r in (self.preview or {}).get("runs") or []
                     if r["algorithm_version"] == self.preview_arm.value), None)

    def _render_preview(self) -> None:
        r = self.preview
        self.preview_meta.clear()
        if r is None or r["status"] != "ok":
            text = ("No preview yet - press Forecast now." if r is None else
                    f"No preview ({_et(r['made_at'], '%a %H:%M')}): {r['reason']}")
            self.preview_status.set_content(f"<span style='{_MUTED}'>{text}</span>")
            self.preview_chart.apply(dict(_EMPTY_SPEC))
            self.preview_targets.clear()
            self.preview_record.clear()
            return
        made = datetime.fromisoformat(r["made_at"].replace(" ", "T")).replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - made).total_seconds() // 60)
        scope = ("the whole pre-open" if r["complete"] else f"the pre-open so far, to the {r['cutoff_et']} cutoff")
        self.preview_status.set_content(
            f"<b>{r['session_date']}</b> as of {r['as_of_et']} - <span style='color:"
            f"{'#26a69a' if r['complete'] else '#ffa726'}'>{scope}</span> <span style='{_MUTED}'>· data through "
            f"{_et(r['data_through'])} · made {_et(r['made_at'])} ({age} min ago) · not stored</span>")
        payload = r["snapshot"]["payload"]
        missing = sorted(k for k, v in (payload.get("references") or {}).items()
                         if v["status"] != "valid" and k != "price_at_0929")
        aset = r["analogue_set"]
        with self.preview_meta:
            ui.label("not known yet or unavailable: " + (", ".join(missing) or "nothing")).classes("text-xs").style(
                _MUTED)
            members = ", ".join(f"{m['session_date']} ({float(m['similarity']):.0f}%)" for m in aset["members"]) \
                if aset and aset["members"] else "none"
            ui.label(f"analogues {members}" + (f"; prior {aset['outcome_summary']['prior']['sessions']} earlier "
                                               f"session(s)" if aset else "")).classes("text-xs").style(_MUTED)
        self.preview_chart.apply(frozen_preopen_spec(r["snapshot"]))
        self._render_preview_run()

    def _render_preview_run(self) -> None:
        run = self._preview_run()
        self.preview_targets.clear()
        self.preview_record.clear()
        if run is None:
            return
        with self.preview_targets:
            _targets_grid(run, f"Per target ({_ARMS[run['algorithm_version']]}, preview)")
        with self.preview_record:
            r = self.preview
            _record_grid(r["snapshot"], r["annotation"], r["analogue_set"], run)

    def _render_grading(self) -> None:
        """
        The arms of the session side by side (forecaster/grading.py): before the outcome is shown and recorded,
        their forecasts - how sure each arm is of its predicted class, against the benchmark; with it, their
        grades - the probability each gave what happened, hits, and the difference to the benchmark.
        """
        self.grading.clear()
        with self.grading:
            if not self.current:
                ui.label("No issued forecast to compare for this session.").classes("text-xs").style(_MUTED)
                return
            first = next(iter(self.current.values()))
            outcome = (store.latest_outcome(self.conn, first["snapshot_id"], first["label_version"])
                       if self.outcome_shown else None)
            if outcome is not None:
                self._render_scored(outcome)
            else:
                self._render_compared()

    def _render_compared(self) -> None:
        g = compare(self.current)
        ui.label(f"Forecasts compared: how sure each arm is of its predicted class, against the benchmark (arm "
                 f"{BENCHMARK})").classes("text-sm font-medium")
        if not g["targets"]:
            ui.label("No arm forecast any target for this session.").classes("text-xs").style(_MUTED)
            return
        targets = [t for _, t in g["targets"]]
        with ui.row().classes("w-full no-wrap gap-6 items-start"):
            if len(targets) >= 3:
                ui.echart(self._radar_options(targets, {a: [x["cells"][t]["p"] for t in targets]
                                                        for a, x in g["arms"].items()},
                                              [g["chance"][t] for t in targets])).style(
                    "width:540px;height:460px").classes("shrink-0")
            with ui.column().classes("grow gap-1 min-w-0 max-w-[60rem]"):
                arms = list(g["arms"])
                with ui.grid(columns=f"minmax(0,1.1fr) repeat({len(arms)}, minmax(0,1fr))").classes(
                        "w-full gap-x-3 gap-y-1"):
                    ui.label("target").classes(_CELL).style(_MUTED)
                    for arm in arms:
                        a = g["arms"][arm]
                        with ui.column().classes(_CELL + " gap-0"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.element("span").style(
                                    f"display:inline-block;width:14px;height:0;border-top:2px "
                                    f"{'dashed' if arm == BENCHMARK else 'solid'} {_ARM_COLOR[arm]}")
                                ui.label(f"{arm} · {fc.ARM_NAMES[arm]}")
                            ui.label("benchmark" if arm == BENCHMARK else
                                     f"same class as {BENCHMARK} on {a['agrees']} of {a['comparable']}").classes(
                                "text-[10px]").style(_MUTED)
                    for _, t in g["targets"]:
                        ui.label(_SHORT[t]).classes(_CELL).style(_MUTED)
                        for arm in arms:
                            c = g["arms"][arm]["cells"][t]
                            if c["p"] is None:
                                ui.label("-").classes(_CELL).style(_MUTED).tooltip(c.get("why") or "")
                                continue
                            name = (defs.display(t, c["cls"], "predicted") if c["cls"] else
                                    "tie: " + " / ".join(defs.display(t, x, "predicted") for x in c["top"]))
                            with ui.row().classes(_CELL + " items-center gap-1 no-wrap"):
                                ui.label(f"{name} {100 * c['p']:.0f}%").classes("break-words")
                                if c["agrees"] is False:
                                    ui.label(f"≠{BENCHMARK}").classes("text-[10px] px-1 rounded").style(
                                        "border:1px solid #787b86;color:#d1d4dc").tooltip(
                                        f"a different class from arm {BENCHMARK}'s")
        why = ("Show realised outcome to grade the arms against what happened." if not self.outcome_shown else
               "The realised outcome is recorded once the session is final, two hours after its close; then the "
               "arms are graded here.")
        ui.label(f"Each axis: the probability an arm gives its own predicted class (the top of a tie). Arm "
                 f"{BENCHMARK}, the earlier-session prior, is the benchmark (dashed); chance is 1 / the number of "
                 f"classes (dotted). Higher is surer, not better - only the outcome says which was right. "
                 f"{why}").classes("text-[10px]").style(_MUTED)

    def _render_scored(self, outcome: Dict[str, Any]) -> None:
        g = grade(self.current, outcome["labels"])
        ui.label(f"Graded against the realised outcome (revision {outcome['outcome_revision']}): the probability "
                 f"each arm gave what happened").classes("text-sm font-medium")
        targets = [t for _, t in g["targets"]]
        with ui.row().classes("w-full no-wrap gap-6 items-start"):
            if len(targets) >= 3:
                ui.echart(self._radar_options(targets, {a: [x["cells"][t]["p"] for t in targets]
                                                        for a, x in g["arms"].items()},
                                              [g["chance"][t] for t in targets])).style(
                    "width:540px;height:460px").classes("shrink-0")
            with ui.column().classes("grow gap-2 min-w-0 max-w-[52rem]"):
                self._scorecard(g)
        ui.label(f"p(realised): the probability the arm gave the class that happened; a hit: its predicted class "
                 f"was it. Arm {BENCHMARK} (the earlier-session prior) is the benchmark; chance is 1 / the number of "
                 f"classes. One session is a view, not a score - experiments score many (Evaluation).").classes(
            "text-[10px]").style(_MUTED)

    def _radar_options(self, targets: List[str], values: Dict[str, List[Optional[float]]],
                       chance: List[float]) -> Dict[str, Any]:
        """A radar over ``targets`` (0-100 %): one polygon per arm, the benchmark dashed, chance dotted."""
        series = []
        for arm, vals in values.items():
            color, bench = _ARM_COLOR[arm], arm == BENCHMARK
            series.append({
                "name": f"{arm} · {fc.ARM_NAMES[arm]}" + (" (benchmark)" if bench else ""),
                "value": [round(100 * (v or 0), 1) for v in vals],
                "symbol": "circle", "symbolSize": 8,
                "lineStyle": {"width": 2, "color": color, "type": "dashed" if bench else "solid"},
                "itemStyle": {"color": color, "borderColor": "#1c212e", "borderWidth": 2},
                "areaStyle": {"color": color, "opacity": 0 if bench else 0.08}})
        series.append({"name": "chance", "value": [round(100 * c, 1) for c in chance],
                       "symbol": "none", "lineStyle": {"width": 1, "type": "dotted", "color": "#5d606b"},
                       "itemStyle": {"color": "#5d606b"}, "areaStyle": {"opacity": 0}})
        return {
            "backgroundColor": "transparent", "animation": False,
            "legend": {"top": 0, "left": "center", "itemWidth": 14, "itemHeight": 8, "itemGap": 12,
                       "textStyle": {"color": "#b2b5be", "fontSize": 11}},
            "tooltip": {"trigger": "item", "backgroundColor": "#1c212e", "borderColor": "#2a2e39",
                        "textStyle": {"color": "#d1d4dc", "fontSize": 11}},
            "radar": {"indicator": [{"name": _SHORT[t], "max": 100} for t in targets], "radius": "66%",
                      "center": ["50%", "56%"], "splitNumber": 4, "shape": "polygon",
                      "axisName": {"color": "#b2b5be", "fontSize": 11},
                      "splitLine": {"lineStyle": {"color": "#2a2e39", "width": 1}},
                      "splitArea": {"show": False}, "axisLine": {"lineStyle": {"color": "#2a2e39"}}},
            "series": [{"type": "radar", "data": series, "emphasis": {"lineStyle": {"width": 3}}}]}

    def _scorecard(self, g: Dict[str, Any]) -> None:
        """Per arm: hits, mean p(realised) and its difference to the benchmark; then per target each arm's
        p(realised) with a hit or a miss, under the realised class."""
        arms = list(g["arms"])
        with ui.grid(columns="minmax(0,1.6fr) repeat(3, minmax(0,1fr))").classes("w-full gap-x-3 gap-y-1"):
            for head in ("arm", "hits", "mean p(realised)", f"vs arm {BENCHMARK}"):
                ui.label(head).classes(_CELL).style(_MUTED)
            for arm in arms:
                a = g["arms"][arm]
                with ui.row().classes(_CELL + " items-center gap-2 no-wrap"):
                    ui.element("span").style(f"display:inline-block;width:14px;height:0;border-top:2px "
                                             f"{'dashed' if arm == BENCHMARK else 'solid'} {_ARM_COLOR[arm]}")
                    ui.label(f"{arm} · {fc.ARM_NAMES[arm]}")
                ui.label(f"{a['hits']} of {a['graded']}").classes(_CELL)
                ui.label("-" if a["mean_p"] is None else f"{100 * a['mean_p']:.0f}%").classes(_CELL)
                vs = a["vs_benchmark"]
                ui.label("benchmark" if arm == BENCHMARK else "-" if vs is None else
                         f"{'+' if vs >= 0 else '−'}{abs(100 * vs):.0f} pts").classes(_CELL).style(
                    _MUTED if arm == BENCHMARK or vs is None else "")
        targets = g["targets"]
        with ui.grid(columns=f"minmax(0,1.3fr) repeat({len(arms)}, minmax(0,1fr))").classes(
                "w-full gap-x-3 gap-y-0 mt-2"):
            ui.label("target - realised").classes(_CELL).style(_MUTED)
            for arm in arms:
                ui.label(f"arm {arm}").classes(_CELL).style(_MUTED)
            for name, t in targets:
                ui.label(f"{_SHORT[t]} - {defs.display(t, g['realised'][t])}").classes(_CELL + " break-words")
                for arm in arms:
                    c = g["arms"][arm]["cells"][t]
                    if c["p"] is None:
                        ui.label("-").classes(_CELL).style(_MUTED).tooltip(c.get("why") or "")
                        continue
                    with ui.row().classes(_CELL + " items-center gap-1 no-wrap"):
                        ui.icon("check_circle" if c["hit"] else "cancel", size="14px").style(
                            f"color:{'#0ca30c' if c['hit'] else '#787b86'}")
                        ui.label(f"{100 * c['p']:.0f}%" + (" hit" if c["hit"] else ""))
        if g["ungraded"]:
            ui.label("not graded: " + ", ".join(f"{_SHORT[t]} ({why})" for _, t, why in g["ungraded"])).classes(
                "text-[10px]").style(_MUTED)

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



def run_day(conn, run_id: Optional[str]) -> Optional[str]:
    """The session day of the stored run ``run_id``, or None for an unknown or malformed id."""
    run = store.get_forecast_run(conn, run_id) if run_id else None
    return run["session_date"] if run is not None else None
