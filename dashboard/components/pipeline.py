# dashboard/components/pipeline.py
"""
The header's "Update data" control: starts the pipeline's jobs (dashboard/jobs.py)
- the collector, the forecaster and the live pre-open capture - and shows the one
running, or the last, with its output.

Each page has its own copy, which polls the dashboard's one job every second. When
a job ends, every page that saw it running reloads what it shows from the database
(``on_update``), so the newest bars, sessions and forecasts appear in place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from nicegui import ui

from config import Config
from dashboard.jobs import RUNNER, Job, collector_command, forecaster_command, live_command, live_window
from features import calendar as cal

_MUTED = "color:#787b86"
_PANEL = "#1c212e"
_LOG_LINES = 1000         # lines the page's log holds; logs/pipeline_run.log has every line
_OUTCOME_COLOR = {"running": "#2962ff", "finished": "#26a69a"}

COLLECTOR, FORECASTER, LIVE = "Collector", "Forecaster", "Live pre-open forecast"


def _elapsed(job: Job) -> str:
    seconds = int(((job.finished_at or datetime.now(timezone.utc)) - job.started_at).total_seconds())
    return f"{seconds // 60}:{seconds % 60:02d}"


def _et(at: datetime) -> str:
    return f"{at.astimezone(cal.NY_TZ):%a %H:%M:%S} ET"


class PipelinePanel:
    def __init__(self) -> None:
        self.on_update: List[Callable[[], Any]] = []     # called when a job this page saw running has ended
        self.job: Optional[Job] = None                   # the job the log shows
        self.seen = 0                                    # its lines already pushed to the log
        self.watching = False                            # it was running while this page was open

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        """The header button (with the running job's name and time), the dialog, and the poll."""
        with ui.row().classes("items-center gap-2 no-wrap"):
            self.spinner = ui.spinner(size="sm")
            self.button = ui.button("Update data", icon="sync", on_click=lambda: self.dialog.open()).props(
                "flat no-caps").tooltip("Run the collector or the forecaster, and see their output")
        with ui.dialog() as self.dialog, ui.card().classes("w-[64rem] max-w-full gap-3").style(f"background:{_PANEL}"):
            with ui.row().classes("w-full items-center"):
                ui.label("Update data").classes("text-lg font-medium")
                ui.space()
                ui.button(icon="close", on_click=self.dialog.close).props('flat dense round aria-label="Close"')
            with ui.grid(columns="12rem minmax(0,1fr)").classes("w-full items-center gap-x-4 gap-y-3"):
                self.collect_button = ui.button("Run collector", icon="download", on_click=self.collect).props(
                    "no-caps")
                with ui.row().classes("w-full items-center gap-4 no-wrap"):
                    ui.label(f"Fetches the missing 1-minute bars of every instrument from IB "
                             f"({Config.IB_HOST}:{Config.IB_PORT}) over the days back, and again the recent days IB "
                             f"may still revise - then runs the forecaster.").classes("text-sm grow").style(_MUTED)
                    self.days = ui.number("Days back", value=5, min=1, max=460, step=1, format="%d").props(
                        "dense").classes("w-20 shrink-0")
                self.forecast_button = ui.button("Run forecaster", icon="insights", on_click=self.forecast).props(
                    "no-caps")
                ui.label("Brings the NQ journal up to date without IB: the event calendar and earnings, then a "
                         "snapshot, structure annotation and analogue set for every session past its 09:29 ET "
                         "cutoff - today's once its bars were fetched after the cutoff - and the baseline and prior "
                         "forecasts (historical replay), each stored once; outcomes once a session is final, two "
                         "hours after its close. No Claude requests: those are started by hand only."
                         ).classes("text-sm").style(_MUTED)
                self.live_button = ui.button("Live forecast", icon="schedule", on_click=self.live).props("no-caps")
                with ui.column().classes("gap-0"):
                    ui.label("Captures today's NQ session live at the 09:29 ET cutoff and issues its forecasts, due "
                             "by 09:29:50 ET (the database marks a later one late). Start it before the open, after "
                             "collecting.").classes("text-sm").style(_MUTED)
                    self.live_note = ui.label().classes("text-xs")
            with ui.row().classes("w-full items-center gap-3"):
                self.status = ui.html().classes("text-sm")
                ui.space()
                self.stop_button = ui.button("Stop", icon="stop", on_click=self.stop).props(
                    "flat no-caps color=negative")
            self.log = ui.log(max_lines=_LOG_LINES).classes("w-full h-96 text-xs").style(
                "background:#131722;font-family:ui-monospace,monospace")
            ui.label("Every run's output is also appended to logs/pipeline_run.log.").classes("text-xs").style(_MUTED)
        self._follow(RUNNER.job)
        self._render()
        ui.timer(1.0, self.poll)

    # -- actions ----------------------------------------------------------------

    def _start(self, key: str, title: str, command: List[str]) -> None:
        try:
            self._follow(RUNNER.start(key, title, command))
        except RuntimeError as e:
            ui.notify(str(e), type="warning")
        self._render()

    def collect(self) -> None:
        days = int(self.days.value or 5)
        self._start("collector", COLLECTOR, collector_command(days))

    def forecast(self) -> None:
        self._start("forecaster", FORECASTER, forecaster_command())

    def live(self) -> None:
        allowed, why = live_window(datetime.now(timezone.utc))
        if not allowed:
            ui.notify(f"The live forecast cannot start now: {why}.", type="warning")
            return
        self._start("live", LIVE, live_command())

    def stop(self) -> None:
        if RUNNER.stop():
            ui.notify(f"Stopping {RUNNER.job.title.lower()}...")

    # -- polling ----------------------------------------------------------------

    def _follow(self, job: Optional[Job]) -> None:
        """Shows ``job`` in the log from its first kept line; its end reloads the page when it is running now."""
        self.job, self.seen, self.watching = job, 0, job is not None and job.running
        self.log.clear()
        self._push()

    def _push(self) -> None:
        if self.job is not None:
            for line in self.job.since(self.seen)[-_LOG_LINES:]:
                self.log.push(line)
            self.seen = self.job.line_count

    def poll(self) -> None:
        if RUNNER.job is not self.job:          # started on another page
            self._follow(RUNNER.job)
        self._push()
        self._render()
        job = self.job
        if self.watching and job is not None and not job.running:
            self.watching = False
            ui.notify(f"{job.title} {job.outcome} - the page shows the stored data now.",
                      type="positive" if job.returncode == 0 else "negative", multi_line=True)
            for callback in self.on_update:
                try:
                    callback()
                except Exception as e:          # one view failing to reload leaves the others
                    ui.notify(f"Could not reload the page's data: {e}", type="negative")

    def _render(self) -> None:
        job, busy = self.job, RUNNER.busy
        allowed, why = live_window(datetime.now(timezone.utc))
        self.collect_button.set_enabled(not busy)
        self.forecast_button.set_enabled(not busy)
        self.live_button.set_enabled(not busy and allowed)
        note = ("Available now - " if allowed else "Not available: ") + why
        if note != self.live_note.text:
            self.live_note.set_text(note)
            self.live_note.style(f"color:{'#26a69a' if allowed else '#787b86'}")
        self.stop_button.set_visibility(busy)
        self.spinner.set_visibility(busy)
        self.button.set_text(f"{job.title} · {_elapsed(job)}" if busy else "Update data")
        if job is None:
            self.status.set_content(f"<span style='{_MUTED}'>Nothing run from the dashboard since it started.</span>")
            return
        color = _OUTCOME_COLOR.get(job.outcome, "#ef5350")
        end = "" if job.running else f", ended {_et(job.finished_at)}"
        self.status.set_content(f"<b>{job.title}</b> - <span style='color:{color}'>{job.outcome}</span> "
                                f"<span style='{_MUTED}'>· started {_et(job.started_at)}{end} · {_elapsed(job)}</span>")
