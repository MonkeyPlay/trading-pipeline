# dashboard/components/pipeline.py
"""
The header's "Update data" control: starts the pipeline's jobs (dashboard/jobs.py)
- the collector, the forecaster, forecast now (a preview) and the live pre-open
capture - and shows the one running, or the last, with its output.

Each page has its own copy, which polls the dashboard's one job every second. When
a job ends, every page that saw it running reloads what it shows from the database
(``on_update``), so the newest bars, sessions and forecasts appear in place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from nicegui import ui

from config import Config
from dashboard import theme
from dashboard.jobs import (RUNNER, Job, collector_command, forecaster_command, live_command, live_window,
                            preview_command, rth_command)
from features import calendar as cal
from forecaster.preview import PreviewUnavailable, preview_target
from forecaster.rth_analogues import due_window as rth_due

_MUTED = theme.MUTED
_PANEL = theme.PANEL
_LOG_LINES = 1000         # lines the page's log holds; logs/pipeline_run.log has every line
_JOB = "w-full items-start no-wrap gap-4 py-4 border-t"   # one job's row in the panel
_OUTCOME_COLOR = {"running": theme.SIGNAL, "finished": theme.INK}

COLLECTOR, FORECASTER, PREVIEW, LIVE = ("Collector", "Forecaster", "Forecast now", "Live pre-open forecast")


def preview_note(now: datetime) -> tuple:
    """``(possible, text)``: what Forecast now would forecast at ``now``, or why it cannot."""
    try:
        session, as_of = preview_target(now)
    except PreviewUnavailable as e:
        return False, str(e)
    complete = as_of == session.cutoff_at
    return True, (f"now: {session.session_date:%a %Y-%m-%d} as of {as_of.astimezone(cal.NY_TZ):%H:%M} ET"
                  + (" - the whole pre-open" if complete else " - the pre-open so far"))


def _elapsed(job: Job) -> str:
    seconds = int(((job.finished_at or datetime.now(timezone.utc)) - job.started_at).total_seconds())
    return f"{seconds // 60}:{seconds % 60:02d}"


def _et(at: datetime) -> str:
    return f"{at.astimezone(cal.NY_TZ):%a %H:%M:%S} ET"


class PipelinePanel:
    def __init__(self, conn=None) -> None:
        self.conn = conn
        self.on_update: List[Callable[[], Any]] = []     # called when a job this page saw running has ended
        self.job: Optional[Job] = None                   # the job the log shows
        self.seen = 0                                    # its lines already pushed to the log
        self.running: set = set()                        # ids of the jobs seen running on this page
        self.preview_possible = False                    # Forecast now has a session to forecast
        self.preview_why = ""                            # which, or why not
        # Shows the preview (the Session Explorer's Forecast now tab): set by the page.
        self.show_preview: Callable[[], Any] = lambda: ui.navigate.to("/?view=preview")

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        """The header button (with the running job's name and time), the dialog, and the poll."""
        with ui.row().classes("items-center gap-2 no-wrap"):
            self.spinner = ui.spinner(size="sm")
            self.button = ui.button("Update data", icon="sync", on_click=lambda: self.dialog.open()).props(
                "outline no-caps").tooltip("Run the collector or the forecaster, and see their output")
        with ui.dialog().props("position=right full-height") as self.dialog, ui.card().classes(
                "w-[36rem] max-w-full h-full gap-4 p-6").style(f"background:{_PANEL};border-radius:10px 0 0 10px"):
            with ui.row().classes("w-full items-start no-wrap gap-4"):
                with ui.column().classes("grow gap-1"):
                    ui.label("Update data").classes("tp-x text-xl font-bold")
                    ui.label("One job runs at a time. It belongs to the dashboard, not this tab: closing the tab does "
                             "not stop it.").classes("text-xs").style(_MUTED)
                ui.button(icon="close", on_click=self.dialog.close).props('flat dense round aria-label="Close"')
            # The job shown: how it stands, Stop, and its output.
            with ui.column().classes("w-full gap-2 p-3 rounded-lg").style("background:var(--tp-tint)"):
                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    self.status = ui.html().classes("text-sm grow")
                    self.stop_button = ui.button("Stop", icon="stop", on_click=self.stop).props(
                        "outline no-caps color=negative")
                self.log = ui.log(max_lines=_LOG_LINES).classes("w-full h-48 text-xs").style(
                    "background:var(--tp-paper);color:var(--tp-ink);border-radius:6px;"
                    "font-family:ui-monospace,monospace")
                ui.label("Every run's output is also appended to logs/pipeline_run.log.").classes("text-xs").style(
                    _MUTED)
            # The jobs: what each does on the left, its button on the right.
            with ui.column().classes("w-full gap-0"):
                with ui.row().classes(_JOB):
                    with ui.column().classes("grow gap-1 min-w-0"):
                        ui.label("Collect bars from IB").classes("text-sm font-semibold")
                        ui.label(f"Fetches the missing 1-minute bars of every instrument from IB "
                                 f"({Config.IB_HOST}:{Config.IB_PORT}) over the days back, and again the recent days "
                                 f"IB may still revise - then runs the forecaster.").classes("text-xs").style(_MUTED)
                        self.days = ui.number("Days back", value=5, min=1, max=460, step=1, format="%d").props(
                            "outlined dense stack-label").classes("w-28 mt-1")
                    self.collect_button = ui.button("Run collector", icon="download", on_click=self.collect).props(
                        "outline no-caps").classes("shrink-0")
                with ui.row().classes(_JOB):
                    with ui.column().classes("grow gap-1 min-w-0"):
                        ui.label("Bring the journal up to date").classes("text-sm font-semibold")
                        ui.label("Without IB: the event calendar and earnings, then a snapshot, structure annotation "
                                 "and analogue set for every session past its 09:29 ET cutoff - today's once its bars "
                                 "were fetched after the cutoff - and the baseline, prior and ML forecasts (historical "
                                 "replay), each stored once; outcomes once a session is final, two hours after its "
                                 "close.").classes("text-xs").style(_MUTED)
                    self.forecast_button = ui.button("Run forecaster", icon="insights", on_click=self.forecast).props(
                        "outline no-caps").classes("shrink-0")
                with ui.row().classes(_JOB):
                    with ui.column().classes("grow gap-1 min-w-0"):
                        ui.label("Forecast now").classes("text-sm font-semibold")
                        ui.label("The next session's forecast from the data so far, at any time from its Globex open "
                                 "(18:00 ET the evening before): collects the latest bars, then forecasts in memory - "
                                 "a preview, never stored; the official forecast comes from the 09:31 ET snapshot."
                                 ).classes("text-xs").style(_MUTED)
                        self.preview_note = ui.label().classes("text-xs")
                        ui.button("Show the preview", on_click=self._show_preview).props(
                            "flat dense no-caps size=sm").classes("self-start")
                    self.preview_button = ui.button("Forecast now", icon="bolt", on_click=self.forecast_now).props(
                        "outline no-caps").classes("shrink-0")
                with ui.row().classes(_JOB):
                    with ui.column().classes("grow gap-1 min-w-0"):
                        ui.label("Live forecast").classes("text-sm font-semibold")
                        ui.label("Captures today's NQ session live at the 09:29 ET cutoff and issues its forecasts, "
                                 "due by 09:29:50 ET (the database marks a later one late). Start it before the open, "
                                 "after collecting.").classes("text-xs").style(_MUTED)
                        self.live_note = ui.label().classes("text-xs")
                    self.live_button = ui.button("Live forecast", icon="schedule", on_click=self.live).props(
                        "outline no-caps").classes("shrink-0")
        self._follow(self._latest())
        self._render()
        ui.timer(1.0, self.poll)

    # -- actions ----------------------------------------------------------------

    def _show_preview(self) -> None:
        self.dialog.close()
        self.show_preview()

    def _start(self, key: str, title: str, steps: List[tuple]) -> None:
        try:
            self._follow(RUNNER.start(key, title, steps))
        except RuntimeError as e:
            ui.notify(str(e), type="warning")
        self._render()

    def _days(self) -> int:
        return int(self.days.value or 5)

    def collect(self) -> None:
        """The collector - then, during a session (to 30 minutes after the RTH close), its RTH analogue sets."""
        steps = [("collector", collector_command(self._days()))]
        if rth_due(datetime.now(timezone.utc)):
            steps.append(("rth", rth_command("manual")))
        self._start("collector", COLLECTOR, steps)

    def forecast(self) -> None:
        self._start("forecaster", FORECASTER, [("forecaster", forecaster_command())])

    def forecast_now(self) -> None:
        """The dedicated forecast-now job: the latest bars, then the preview (also the forecast's own button)."""
        possible, why = preview_note(datetime.now(timezone.utc))
        if not possible:
            ui.notify(f"Nothing to forecast now: {why}", type="warning", multi_line=True)
            return
        self._start("preview", PREVIEW, [("collector", collector_command(self._days())), ("preview", preview_command())])

    def live(self) -> None:
        allowed, why = live_window(datetime.now(timezone.utc))
        if not allowed:
            ui.notify(f"The live forecast cannot start now: {why}.", type="warning")
            return
        self._start("live", LIVE, [("live", live_command())])

    def stop(self) -> None:
        if RUNNER.stop():
            ui.notify(f"Stopping {RUNNER.job.title.lower()}...")

    @staticmethod
    def _latest() -> Optional[Job]:
        """The job to show: the running one, else the last."""
        return RUNNER.job

    # -- polling ----------------------------------------------------------------

    def _follow(self, job: Optional[Job]) -> None:
        """Shows ``job`` in the log from its first kept line."""
        self.job, self.seen = job, 0
        if job is not None and job.running:
            self.running.add(id(job))
        self.log.clear()
        self._push()

    def _push(self) -> None:
        if self.job is not None:
            for line in self.job.since(self.seen)[-_LOG_LINES:]:
                self.log.push(line)
            self.seen = self.job.line_count

    def poll(self) -> None:
        latest = self._latest()
        if latest is not self.job:
            self._follow(latest)                # started on another page
        self._push()
        self._render()
        # Any job this page saw running reloads the page's data when it ends.
        for job in [j for j in (RUNNER.job,) if j is not None]:
            if job.running:
                self.running.add(id(job))
            elif id(job) in self.running:
                self.running.discard(id(job))
                if job.key != "auto" or job.returncode != 0:      # auto mode runs once a minute: its failures only
                    ui.notify(f"{job.title} {job.outcome} - the page shows the stored data now.",
                              type="positive" if job.returncode == 0 else "negative", multi_line=True)
                for callback in self.on_update:
                    try:
                        callback()
                    except Exception as e:          # one view failing to reload leaves the others
                        ui.notify(f"Could not reload the page's data: {e}", type="negative")

    def _render(self) -> None:
        job, busy = self.job, RUNNER.busy
        now = datetime.now(timezone.utc)
        allowed, why = live_window(now)
        self.preview_possible, preview_why = preview_note(now)
        self.preview_why = preview_why
        self.collect_button.set_enabled(not busy)
        self.forecast_button.set_enabled(not busy)
        self.preview_button.set_enabled(not busy and self.preview_possible)
        self.live_button.set_enabled(not busy and allowed)
        note = ("Possible " if self.preview_possible else "Not possible now: ") + preview_why
        if note != self.preview_note.text:
            self.preview_note.set_text(note)
            self.preview_note.style(f"color:{theme.INK if self.preview_possible else theme.INK2}")
        note = ("Available now - " if allowed else "Not available: ") + why
        if note != self.live_note.text:
            self.live_note.set_text(note)
            self.live_note.style(f"color:{theme.INK if allowed else theme.INK2}")
        shown_running = job is not None and job.running
        self.stop_button.set_visibility(shown_running)
        self.spinner.set_visibility(busy)
        self.button.set_text(f"{job.title} · {_elapsed(job)}" if shown_running else "Update data")
        if job is None:
            self.status.set_content(f"<span style='{_MUTED}'>Nothing run from the dashboard since it started.</span>")
            return
        color = _OUTCOME_COLOR.get(job.outcome, theme.SIGNAL)
        end = "" if job.running else f", ended {_et(job.finished_at)}"
        self.status.set_content(f"<b>{job.title}</b> - <span style='color:{color}'>{job.outcome}</span> "
                                f"<span style='{_MUTED}'>· started {_et(job.started_at)}{end} · {_elapsed(job)}</span>")
