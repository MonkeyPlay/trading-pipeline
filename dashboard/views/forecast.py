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
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

from contracts import nq_forecast as fc
from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from dashboard.components.fan import current_session
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.preopen import frozen_preopen_spec
from dashboard import theme
from dashboard.jobs import RUNNER
from database import journal_store as store
from features import calendar as cal
from forecaster import preview as pv
from forecaster.forecast_display import target_rows
from forecaster.grading import BENCHMARK, compare, current_runs, grade
from forecaster.outcome_display import p2_record
from forecaster.preopen_display import p1_record
from forecaster.rth_analogues import newest_bar_end, windows_over

_MUTED = theme.MUTED
_CELL = "px-2 py-1 text-xs"
_STATUS_COLOR = {"issued": theme.INK, "unavailable": theme.INK2, "late": theme.AMBER, "failed": theme.SIGNAL,
                 "invalid": theme.SIGNAL}
_FORECAST_FIELDS = set(range(25, 37)) | set(range(42, 46))
_ARMS = {v: f"{ml.ARM_NAMES[a]} (arm {a})" for a, v in ml.ARMS.items()}
# The arms (dashboard/theme.py): A, the benchmark, a dashed ink grey; B, N and M blue, amber and teal (validated
# all-pairs against the sheet: CVD dE 11.0, normal-vision dE 19.3). P, plum, is at the CVD floor against B, so it is
# used only where its label is shown beside it (the arm tiles), never in an overlaid chart.
_ARM_COLOR = theme.ARM_COLOR
_SHORT = {"opening_bias_30m": "30-min bias", "first_move_5m": "First move", "opening_type_15m": "Opening type",
          "direction_15m": "15-min direction", "session_type_rth": "Session type", "close_direction_rth": "RTH close",
          "first_level_tested": "First level"}
_EMPTY_SPEC = {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []}


def _et(value: str, fmt: str = "%H:%M") -> str:
    """A journal timestamp ('YYYY-MM-DD HH:MM:SS' or ISO, UTC) in New York time."""
    t = datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T")).replace(tzinfo=timezone.utc)
    return f"{t.astimezone(cal.NY_TZ):{fmt}} ET"


def _span(minutes: float) -> str:
    """A duration as read: minutes, hours or days."""
    m = abs(minutes)
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h" if m < 48 * 60 else f"{m / 1440:.0f} days"


def issue_timing(run: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """``(line, over)``: when a stored run was issued against its session's cutoff and open - a forecast, or a record
    made after the fact - and the targets whose window had already ended by then (target -> its ET end)."""
    issued = run.get("issued_at") or run.get("created_at")
    if not issued:
        return "", {}
    at = datetime.fromisoformat(str(issued).replace("Z", "").replace(" ", "T")).replace(tzinfo=timezone.utc)
    day = str(run["session_date"])
    try:
        s = cal.session(day)
    except cal.CalendarCoverageError:
        return "", {}
    cutoff = datetime.fromisoformat(str(run["input_cutoff_at"]).replace("Z", "").replace(" ", "T")).replace(
        tzinfo=timezone.utc)
    after_open = (at - s.rth_open_at).total_seconds() / 60
    kind = ("issued live" if run.get("mode") == "live" else
            "a historical replay - built after the fact, a record rather than a forecast" if after_open > 24 * 60 else
            "a historical replay on the session's day")
    line = (f"Issued {at.astimezone(cal.NY_TZ):%Y-%m-%d %H:%M} ET, {_span((at - cutoff).total_seconds() / 60)} after "
            f"its {cutoff.astimezone(cal.NY_TZ):%H:%M} ET cutoff"
            + (f" and {_span(after_open)} after the open" if after_open > 0 else
               f", {_span(after_open)} before the open") + f" - {kind}.")
    return line, windows_over(day, at, [t for _, t in fc.FORECAST_TARGETS])


def _arm_name(arm: str) -> str:
    """'B' -> 'Baseline' (the contract's names, the first letter capitalised)."""
    name = ml.ARM_NAMES[arm]
    return name[:1].upper() + name[1:]


def _mark(arm: str) -> None:
    """The arm's mark where it is called: its letter in a ring of its colour, the benchmark's ring dashed."""
    ui.label(arm).classes("tp-mark").style(
        f"border-color:{_ARM_COLOR[arm]};border-style:{'dashed' if arm == BENCHMARK else 'solid'}")


def _targets_grid(run: Dict[str, Any], title: str, over: Optional[Dict[str, str]] = None) -> None:
    """A run's per-target view where it is called: class, distribution and denominators; ``over`` - target -> the
    ET end of its window - marks the targets of a session in progress already observed; a target whose window had
    ended when the run was issued is marked as such (issue_timing)."""
    ui.label(title).classes("text-sm font-medium")
    over = over or {}
    timing, at_issue = issue_timing(run)
    if timing:
        ui.label(timing).classes("text-xs").style(_MUTED)
    rows = target_rows(run)
    if not rows:
        ui.label(f"No predictions: {run['failure_reason']}").classes("text-xs").style(_MUTED)
        return
    with ui.grid(columns="minmax(0,1.1fr) minmax(0,0.9fr) minmax(0,1.6fr) minmax(0,0.9fr)").classes(
            "w-full gap-x-2 gap-y-1"):
        for head in ("P1 property", "class", "distribution", "n / prior"):
            ui.label(head).classes(_CELL).style(_MUTED)
        for r in rows:
            done, before = over.get(r["target"]), at_issue.get(r["target"])
            note = (f" — its window had ended ({before} ET) when this run was issued: a record, not a forecast"
                    if before else f" — window over at {done} ET: observed, no longer a forecast" if done else "")
            ui.label(r["property"] + note
                     ).classes(_CELL + " break-words").style(_MUTED + (f";color:{theme.AMBER}" if done or before else ""))
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
            style = "background:var(--tp-tint)" if i in _FORECAST_FIELDS else ""
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
        """After a job: the preview read again, and the tab of a Forecast now job shown (the day's runs follow the
        session bar)."""
        self.preview = pv.load()
        self._render_preview()
        if RUNNER.job is not None and RUNNER.job.key == "preview":
            self.tabs.set_value("preview")

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
        self.box = ui.expansion("Forecast", value=True).classes("min-w-0").style("flex:6 1 600px")
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
        with ui.row().classes("w-full items-center gap-3"):
            self.run_select = ui.select({}, label="Run", on_change=lambda e: self.show(e.value)).props(
                "outlined dense stack-label").classes("grow min-w-[16rem] max-w-[30rem]")
            ui.space()
            self.outcome_button = ui.button("Show the realised outcome", on_click=self.toggle_outcome).props(
                "unelevated no-caps")
        self.arm_title = ui.label().classes("text-sm").style(_MUTED)
        self.arm_row = ui.element("div").classes("w-full grid gap-1 p-1 rounded-lg").props(
            'role="group" aria-label="Arm shown"').style(
            "grid-template-columns:repeat(auto-fill,minmax(190px,1fr));background:var(--tp-tint)")
        self.grading = ui.column().classes("w-full gap-3")
        with ui.expansion("The day's summary", value=False).classes("w-full"):
            ui.label(f"Forecasts of the session day's NQ session from each run's frozen evidence, by arm: A the prior "
                     f"({fc.PRIOR_VERSION}, the earlier sessions alone, the benchmark); B the baseline "
                     f"({fc.BASELINE_VERSION}, the rule-based analogues smoothed with the prior); N and M the "
                     f"scikit-learn models of the 15-minute direction ({ml.ML_NQ_VERSION} from NQ's own features, "
                     f"{ml.ML_MULTI_VERSION} with ES, RTY, VIX, the 10-year yield and the dollar besides) - "
                     f"experimental until a forward evaluation shows they improve on B. A historical replay is "
                     f"research on reconstructed evidence, never a timely live forecast. The realised outcome and the "
                     f"grading stay hidden until you show them.").classes("text-sm").style(_MUTED)
            ui.label("From the stored numbers only: the forecast in force and why, every arm's probabilities and the "
                     "ML forecasts' differences from A and B, the reference levels, the instruments used and what "
                     "they showed at the cutoff").classes("text-xs").style(_MUTED)
            self.summary = ui.column().classes("w-full gap-0")
        with ui.expansion("The run in full", value=False, on_value_change=self._run_opened).classes(
                "w-full") as self.run_body:
            with ui.column().classes("w-full gap-3"):
                self.provenance = ui.column().classes("w-full gap-0")
                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("grow gap-1 min-w-[320px]"):
                        self.chart = LightweightChart(height=520)
                    with ui.card().classes("w-full max-w-[620px]"):
                        self.targets = ui.column().classes("w-full gap-0")
                self.outcome = ui.column().classes("w-full gap-0")
                with ui.expansion("P1 record (47 fields)", value=True).classes("w-full"):
                    self.record = ui.column().classes("w-full gap-0")

    def _run_opened(self, event) -> None:
        """The run's chart is drawn again when its section opens: a chart laid out while hidden has no width."""
        if event.value and self.run is not None:
            self.chart.apply(frozen_preopen_spec(self.snapshot))

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
        self.box.text = "NQ forecast" if day else "Forecast"
        self.runs = store.list_forecast_runs(self.conn, day, day, profile=defs.DEFAULT_PROFILE) if day else []
        self.pick_day(self.run_select.value if keep else run_id)

    def pick_day(self, run_id: Optional[str] = None) -> None:
        """The day's arms (the tiles and their comparison), then the runs of the arm shown - the requested
        run's, the arm shown before when the day has it, else arm B, else the first the day has."""
        runs = self.runs
        have = {ml.arm_of(r["algorithm_version"]) for r in runs}
        requested = next((r for r in runs if r["run_id"] == run_id), None)
        if requested is not None:
            self.arm = ml.arm_of(requested["algorithm_version"])
        elif self.arm not in have:
            self.arm = "B" if "B" in have else next((a for a in ml.ARMS if a in have), None)
        self.current = {a: store.get_forecast_run(self.conn, r["run_id"]) for a, r in current_runs(runs).items()}
        self._render_summary()
        self._render_arms(self.day, runs)
        self._render_grading()
        mine = [r for r in runs if ml.arm_of(r["algorithm_version"]) == self.arm]
        self.run_select.set_options({r["run_id"]: self._run_label(r) for r in mine},
                                    value=run_id if requested is not None else (mine[0]["run_id"] if mine else None))
        self.run_select.set_enabled(bool(mine))
        self.run_body.set_visibility(bool(mine))
        if not mine:
            self.run = None
            self.chart.apply(dict(_EMPTY_SPEC))

    def _render_summary(self) -> None:
        """The day's summary (forecaster/forecast_summary.py) as sentences - measurements and stored fields only."""
        from forecaster import forecast_summary as fsum
        self.summary.clear()
        with self.summary:
            if not self.day or not self.runs:
                ui.label("No forecast run for this session.").classes("text-sm").style(_MUTED)
                return
            try:
                text = fsum.lines(fsum.build(self.conn, self.day))
            except Exception as e:              # the page stays usable; the summary says why it is missing
                text = [f"Summary unavailable: {type(e).__name__}: {e}"]
            for line in text:
                ui.label(line).classes("text-sm break-words")

    def pick_arm(self, arm: str) -> None:
        self.arm = arm
        self.pick_day()

    def _render_arms(self, day: str, runs: List[Dict[str, Any]]) -> None:
        """One tile per arm: what it rests on and whether it ran for the session (issued, failed or no run); the
        arm shown below raised. A tile with runs shows that arm when clicked."""
        shown = f"arm {self.arm}, {ml.ARM_NAMES[self.arm]}" if self.arm else "no run"
        self.arm_title.set_text(f"Arms for {day}: {shown} shown. Click another to show it.")
        self.arm_row.clear()
        with self.arm_row:
            for arm in ml.ARMS:
                mine = [r for r in runs if ml.arm_of(r["algorithm_version"]) == arm]
                is_shown = arm == self.arm
                tile = ui.element("div").classes("rounded-md px-3 py-2 flex gap-3 items-start"
                                                 + (" cursor-pointer" if mine else ""))
                tile.props(f'role="button" tabindex="0" aria-pressed="{str(is_shown).lower()}"')
                tile.style("background:var(--tp-sheet);outline:1px solid var(--tp-ink);"
                           "box-shadow:inset 0 -3px 0 var(--tp-ink)" if is_shown else
                           f"background:transparent;{'' if mine else 'opacity:0.55'}")
                if mine:
                    tile.on("click", lambda a=arm: self.pick_arm(a))
                    tile.on("keydown.enter", lambda a=arm: self.pick_arm(a))
                with tile:
                    _mark(arm)
                    with ui.column().classes("gap-0 min-w-0"):
                        with ui.row().classes("items-baseline gap-2 no-wrap"):
                            ui.label(_arm_name(arm)).classes("text-sm font-semibold")
                            if arm == BENCHMARK:
                                ui.label("the benchmark").classes("text-xs").style(_MUTED)
                        status, detail = self._arm_status(arm, mine, self.current.get(arm))
                        ui.label(detail).classes("text-xs break-words").style(_MUTED)
                        ui.label(status).classes("text-xs break-words")

    @staticmethod
    def _arm_status(arm: str, mine: List[Dict[str, Any]], current: Optional[Dict[str, Any]]):
        """``(status, detail)`` of one arm's tile."""
        if current is not None:
            ev = current.get("evidence") or {}
            n = len(ev.get("members") or [])
            used = (current.get("outputs") or {}).get("instruments") or {}
            detail = {"A": f"the prior of {(ev.get('prior') or {}).get('sessions', '?')} earlier session(s)",
                      "B": f"{n} rule-based analogue(s)",
                      "N": f"NQ's own features · {ml.STATUS[ml.ML_NQ_VERSION]}",
                      "P": f"NQ's own features, trained on NQ, ES and RTY · {ml.STATUS[ml.ML_POOLED_VERSION]}",
                      "M": (f"{', '.join(k for k, v in used.items() if v == 'used') or 'no context instrument'} used"
                            + (f"; {', '.join(k for k, v in used.items() if v != 'used')} missing" if any(
                                v != 'used' for v in used.values()) else "")
                            + f" · {ml.STATUS[ml.ML_MULTI_VERSION]}")}[arm]
            late = " · reconstruction (after the replay deadline)" if ml.reconstruction(current) else ""
            return f"issued {str(current['issued_at'])[11:16]} UTC · {current['algorithm_version']}{late}", detail
        if mine:
            return (f"{mine[0]['lifecycle_status']} - {len(mine)} attempt(s), none issued",
                    (mine[0]["failure_reason"] or "")[:110])
        return "no run for this session", "Update data, Run forecaster"

    def toggle_outcome(self) -> None:
        self.outcome_shown = not self.outcome_shown
        self.outcome_button.set_text("Hide the realised outcome" if self.outcome_shown else
                                     "Show the realised outcome")
        self.outcome_button.props(remove="unelevated" if self.outcome_shown else "outline",
                                  add="outline" if self.outcome_shown else "unelevated")
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
            arm = ml.arm_of(r["algorithm_version"])
            ui.html((f"<b>Arm {arm} · {ml.ARM_NAMES[arm]}</b> · " if arm else "")
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
            model = ev.get("model") or {}
            if model:
                ui.label(f"model {model.get('family')} · artifact {str(model.get('sha256'))[:12]} · trained on "
                         f"{(model.get('training') or {}).get('sessions', '?')} session(s) to "
                         f"{(model.get('training') or {}).get('to', '?')} · {ml.STATUS.get(r['algorithm_version'], '')}"
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
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.column().classes("grow gap-1 min-w-[320px]"):
                self.preview_chart = LightweightChart(height=520)
            with ui.card().classes("w-full max-w-[620px]"):
                self.preview_arm = ui.toggle({v: _ARMS[v] for v in fc.RULE_ALGORITHMS}, value=fc.BASELINE_VERSION,
                                             on_change=lambda e: self._render_preview_run()).props("dense no-caps")
                self.preview_targets = ui.column().classes("w-full gap-0")
        with ui.expansion("P1 record (47 fields)", value=False).classes("w-full"):
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
            self.preview_note.style(f"color:{theme.INK if possible else theme.INK2}")

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
            f"{theme.INK if r['complete'] else theme.AMBER}'>{scope}</span> <span style='{_MUTED}'>· data through "
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
        The arms of the session side by side (forecaster/grading.py): how sure each arm is of its predicted class
        against the benchmark - or, with the realised outcome shown, what each gave to what happened - then every
        class of the arm shown, and with the outcome the grading.
        """
        self.grading.clear()
        with self.grading:
            if not self.current:
                ui.label("No issued forecast to compare for this session.").classes("text-sm").style(_MUTED)
                return
            first = next(iter(self.current.values()))
            outcome = (store.latest_outcome(self.conn, first["snapshot_id"], first["label_version"])
                       if self.outcome_shown else None)
            if self.outcome_shown and outcome is None:
                ui.label("No realised outcome recorded yet: it is recorded once the session is final, two hours "
                         "after its close.").classes("text-sm").style(_MUTED)
            compared = compare(self.current)
            graded = grade(self.current, outcome["labels"]) if outcome is not None else None
            if not compared["targets"]:
                ui.label("No arm forecast any target for this session.").classes("text-sm").style(_MUTED)
                return
            with ui.element("div").classes("w-full flex flex-wrap gap-3 items-start"):
                with ui.card().classes("gap-2 p-4 min-w-0").style("flex:5 1 380px"):
                    self._render_sureness(compared, graded)
                with ui.card().classes("gap-1 p-4 min-w-0").style("flex:6 1 420px"):
                    self._render_classes(graded)
            if graded is not None:
                with ui.card().classes("w-full gap-2 p-4"):
                    self._grading_table(graded, outcome)

    def _render_sureness(self, compared: Dict[str, Any], graded: Optional[Dict[str, Any]]) -> None:
        """Per target a 0-100 % track: each arm's dot (its letter in its colour) at the probability it gives its own
        predicted class - with the outcome, the class that happened, hollow when it predicted another - arm A's
        dashed tick, chance dotted."""
        source = graded or compared
        ui.label("What each arm gave to what happened" if graded else "How sure each arm is").classes(
            "tp-x text-lg font-bold")
        ui.label("Each dot is the probability the arm gave the class that happened. Solid: its prediction was that "
                 "class. Further right is better." if graded else
                 "Each dot is the probability an arm gives its own predicted class (the top of a tie). Further right "
                 "is surer, not more right: only the outcome says which was right.").classes("text-xs").style(_MUTED)
        arms = [a for a in ml.ARMS if a in source["arms"]]
        with ui.row().classes("items-center gap-x-4 gap-y-1"):
            for arm in arms:
                with ui.element("span").classes("tp-key"):
                    if arm == BENCHMARK:
                        ui.element("span").style(f"height:16px;border-left:2px dashed {_ARM_COLOR[arm]}")
                    else:
                        ui.element("span").style(f"width:12px;height:12px;border-radius:6px;"
                                                 f"background:{_ARM_COLOR[arm]}")
                    ui.label(f"{arm}, {ml.ARM_NAMES[arm]}")
            with ui.element("span").classes("tp-key"):
                ui.element("span").style("height:16px;border-left:2px dotted var(--tp-ink2)")
                ui.label("Chance")
            if graded:
                with ui.element("span").classes("tp-key"):
                    ui.element("span").style("width:12px;height:12px;border-radius:6px;box-sizing:border-box;"
                                             "border:2.5px solid var(--tp-ink2)")
                    ui.label("Hollow: predicted another class")
        for _, t in source["targets"]:
            bench = source["arms"].get(BENCHMARK, {}).get("cells", {}).get(t)
            others = [a for a in arms if a != BENCHMARK and source["arms"][a]["cells"][t]["p"] is not None]
            classes = len(defs.TARGETS[t]["labels"]) if t in defs.TARGETS else None
            with ui.element("div").classes("tp-dotrow"):
                with ui.column().classes("gap-0"):
                    ui.label(_SHORT[t]).classes("text-sm font-semibold")
                    if classes:
                        ui.label(f"{classes} classes").classes("text-xs").style(_MUTED)
                with ui.element("div").classes("tp-track"):
                    ui.element("span").classes("tp-chance").style(f"left:{100 * source['chance'][t]:.1f}%")
                    if bench and bench["p"] is not None:
                        ui.element("span").classes("tp-bench").style(f"left:{100 * bench['p']:.1f}%").tooltip(
                            f"{BENCHMARK}, {ml.ARM_NAMES[BENCHMARK]}: {100 * bench['p']:.0f} %")
                        ui.label(BENCHMARK).classes("tp-bench-label").style(f"left:{100 * bench['p']:.1f}%")
                    for i, arm in enumerate(others):
                        c = source["arms"][arm]["cells"][t]
                        offset = (i - (len(others) - 1) / 2) * 9
                        miss = graded is not None and not c["hit"]
                        if graded:
                            what = defs.display(t, graded["realised"][t])
                            tip = f"{arm}, {ml.ARM_NAMES[arm]}: {100 * c['p']:.0f} % for {what}, what happened"
                        else:
                            name = (defs.display(t, c["cls"], "predicted") if c["cls"] else
                                    "a tie: " + " / ".join(defs.display(t, x, "predicted") for x in c["top"]))
                            tip = f"{arm}, {ml.ARM_NAMES[arm]}: {name} {100 * c['p']:.0f} %"
                        ui.label(arm).classes("tp-dot" + (" miss" if miss else "")).style(
                            f"left:{100 * c['p']:.1f}%;margin-top:{offset - 9:.0f}px;--c:{_ARM_COLOR[arm]}"
                        ).tooltip(tip)
        with ui.element("div").classes("tp-dotrow").style("border-top:0;min-height:18px"):
            ui.label("")
            with ui.element("div").classes("tp-axis"):
                for v in (0, 25, 50, 75, 100):
                    ui.label(f"{v} %").style(f"left:{v}%" + (";transform:translateX(-100%)" if v == 100 else
                                                             "" if v == 0 else ";transform:translateX(-50%)"))

    def _render_classes(self, graded: Optional[Dict[str, Any]]) -> None:
        """Every class of the arm shown, per target: a bar of its probability - the strongest, its prediction, in
        the arm's colour - with arm A's probability for the class as a tick, and what happened when shown."""
        arm = self.arm
        run = self.current.get(arm) if arm else None
        bench = self.current.get(BENCHMARK) if arm != BENCHMARK else None
        with ui.row().classes("items-center gap-2 no-wrap"):
            if arm:
                _mark(arm)
            ui.label(f"{_arm_name(arm)}, every class" if arm else "Every class").classes("tp-x text-lg font-bold")
        ui.label(f"Each bar is the probability arm {arm} gives the class; the strongest is its prediction."
                 + (f" The tick on each bar is arm {BENCHMARK}'s probability for the same class." if bench else "")
                 ).classes("text-xs").style(_MUTED)
        if run is None:
            ui.label("No issued run of this arm for the session.").classes("text-sm").style(_MUTED)
            return
        for _, t in fc.FORECAST_TARGETS:
            p = (run.get("predictions") or {}).get(t) or {}
            dist = p.get("distribution")
            if not dist:
                continue
            values = {c: float(Fraction(v)) for c, v in dist.items()}
            top = max(values.values())
            predicted = p.get("predicted_label") if p.get("status") == "predicted" else None
            bdist = ((bench or {}).get("predictions") or {}).get(t, {}) or {}
            bdist = bdist.get("distribution") or {}
            realised = graded["realised"].get(t) if graded else None
            spec = defs.TARGETS.get(t, {})
            with ui.column().classes("w-full gap-1 pt-2 mt-1").style("border-top:1px solid var(--tp-rule)"):
                with ui.row().classes("items-baseline gap-3"):
                    ui.label(_SHORT[t]).classes("text-sm font-semibold")
                    if spec.get("window_et"):
                        ui.label(f"{spec['window_et'][0]} to {spec['window_et'][1]}").classes("text-xs").style(_MUTED)
                for cls in spec.get("labels") or list(values):
                    v = values.get(cls, 0.0)
                    strongest = cls == predicted or (predicted is None and v == top)
                    with ui.element("div").classes("tp-crow text-xs"):
                        with ui.row().classes("items-center gap-1 no-wrap min-w-0"):
                            ui.label(defs.display(t, cls, "predicted")).classes(
                                "truncate" + (" font-semibold" if strongest else ""))
                            if realised == cls:
                                ui.label("✓ Happened").classes("tp-happened")
                        with ui.element("span").classes("tp-cbar"):
                            ui.element("span").style(
                                f"width:{100 * v:.1f}%;background:{_ARM_COLOR[arm] if strongest else theme.INK2};"
                                f"opacity:{1 if strongest else 0.38}")
                            if bdist:
                                ui.element("span").classes("tp-ctick").style(
                                    f"left:{100 * float(Fraction(bdist.get(cls, '0'))):.1f}%")
                        ui.label(f"{100 * v:.0f} %").classes("text-right")

    def _grading_table(self, g: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        """Per arm: hits, mean p(realised) and its difference to the benchmark."""
        ui.label("Grading for this session").classes("tp-x text-lg font-bold")
        ui.label(f"Against the realised outcome (revision {outcome['outcome_revision']}). One session says little on "
                 f"its own: the registered experiments on the Evaluation page are the evidence. A hit is a predicted "
                 f"class that happened.").classes("text-xs").style(_MUTED)
        rule = "border-top:1px solid var(--tp-rule)"
        with ui.grid(columns="minmax(0,1.6fr) repeat(3, minmax(0,1fr))").classes("w-full gap-x-3 gap-y-0"):
            for head in ("Arm", "Hits", "Mean p(realised)", f"Against {BENCHMARK}"):
                ui.label(head).classes("text-xs py-1").style(_MUTED)
            for arm in [a for a in ml.ARMS if a in g["arms"]]:
                a = g["arms"][arm]
                with ui.row().classes("items-center gap-2 no-wrap py-2").style(rule):
                    _mark(arm)
                    ui.label(_arm_name(arm))
                with ui.row().classes("items-baseline gap-1 py-2").style(rule):
                    ui.label(str(a["hits"])).classes("tp-x text-base font-bold")
                    ui.label(f"of {a['graded']}").style(_MUTED)
                ui.label("-" if a["mean_p"] is None else f"{100 * a['mean_p']:.1f} %").classes("py-2").style(rule)
                vs = a["vs_benchmark"]
                ui.label("Benchmark" if arm == BENCHMARK else "-" if vs is None else
                         f"{'+' if vs >= 0 else '−'}{abs(100 * vs):.1f} points").classes("py-2").style(
                    rule + (f";{_MUTED}" if arm == BENCHMARK or vs is None else ""))
        if g["ungraded"]:
            ui.label("Not graded: " + ", ".join(f"{_SHORT[t]} ({why})" for _, t, why in g["ungraded"])).classes(
                "text-xs").style(_MUTED)

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
