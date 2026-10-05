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

from nicegui import run, ui

from config import Config
from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from dashboard.jobs import (RUNNER, Job, collector_command, forecaster_command, live_command, live_window,
                            llm_command, preview_command)
from features import calendar as cal
from forecaster.preview import PreviewUnavailable, preview_target

_MUTED = "color:#787b86"
_PANEL = "#1c212e"
_LOG_LINES = 1000         # lines the page's log holds; logs/pipeline_run.log has every line
_OUTCOME_COLOR = {"running": "#2962ff", "finished": "#26a69a"}

COLLECTOR, FORECASTER, PREVIEW, LIVE, LLM = ("Collector", "Forecaster", "Forecast now", "Live pre-open forecast",
                                             "LLM forecast")


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
        self.conn = conn                                 # for the LLM forecast's plan, before anything is sent
        self.on_update: List[Callable[[], Any]] = []     # called when a job this page saw running has ended
        self.job: Optional[Job] = None                   # the job the log shows
        self.seen = 0                                    # its lines already pushed to the log
        self.watching = False                            # it was running while this page was open
        self.preview_possible = False                    # Forecast now has a session to forecast
        self.preview_why = ""                            # which, or why not

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
                self.preview_button = ui.button("Forecast now", icon="bolt", on_click=self.forecast_now).props(
                    "no-caps")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-baseline gap-2"):
                        ui.label("The next session's forecast from the data so far, at any time from its Globex open "
                                 "(18:00 ET the evening before): collects the latest bars, then forecasts in memory - "
                                 "a preview, never stored; the official forecast comes from the 09:31 ET snapshot."
                                 ).classes("text-sm").style(_MUTED)
                        ui.link("Show the preview", "/forecast?view=preview").classes("text-sm")
                    self.preview_note = ui.label().classes("text-xs")
                self.llm_button = ui.button("Run LLM forecast", icon="psychology", on_click=self.llm_forecast).props(
                    "no-caps")
                with ui.row().classes("w-full items-center gap-4 no-wrap"):
                    ui.label("Arms C (restricted LLM: Claude reads the overnight and premarket structure, matched "
                             "and smoothed like B) and D (synthesis: Claude forecasts from arm B's evidence) for the "
                             "last sessions up to today, date-blinded. Claude requests: you see what would be sent "
                             "and its rough cost, and confirm, before anything is sent.").classes(
                        "text-sm grow").style(_MUTED)
                    with ui.column().classes("gap-0 shrink-0"):
                        self.llm_sessions = ui.number("Sessions", value=1, min=1, max=300, step=1, format="%d").props(
                            "dense").classes("w-20")
                        with ui.row().classes("gap-0 no-wrap"):
                            self.llm_c = ui.checkbox("C", value=True).props("dense")
                            self.llm_d = ui.checkbox("D", value=True).props("dense")
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

    def _start(self, key: str, title: str, steps: List[tuple]) -> None:
        try:
            self._follow(RUNNER.start(key, title, steps))
        except RuntimeError as e:
            ui.notify(str(e), type="warning")
        self._render()

    def _days(self) -> int:
        return int(self.days.value or 5)

    def collect(self) -> None:
        self._start("collector", COLLECTOR, [("collector", collector_command(self._days()))])

    def forecast(self) -> None:
        self._start("forecaster", FORECASTER, [("forecaster", forecaster_command())])

    def forecast_now(self) -> None:
        """The dedicated forecast-now job: the latest bars, then the preview (also the Forecast page's button)."""
        possible, why = preview_note(datetime.now(timezone.utc))
        if not possible:
            ui.notify(f"Nothing to forecast now: {why}", type="warning", multi_line=True)
            return
        self._start("preview", PREVIEW, [("collector", collector_command(self._days())), ("preview", preview_command())])

    async def llm_forecast(self) -> None:
        """The plan of arms C and D over the chosen sessions, then the confirmation; only its Send button issues
        the one-time approval the job needs to send Claude requests (forecaster/approvals.py)."""
        from forecaster import approvals
        from forecaster import llm_arms as la
        arms = "".join(a for a, box in (("C", self.llm_c), ("D", self.llm_d)) if box.value)
        if not arms:
            ui.notify("Choose arm C, arm D or both.", type="warning")
            return
        if self.conn is None:
            ui.notify("No database connection on this page.", type="warning")
            return
        sessions = max(1, int(self.llm_sessions.value or 1))
        self.llm_button.props("loading")
        try:
            plan = await run.io_bound(la.plan, self.conn, sessions, tuple(arms))
        except Exception as e:
            ui.notify(f"Could not plan the LLM forecast: {e}", type="negative")
            return
        finally:
            self.llm_button.props(remove="loading")

        def start(approval=None):
            confirm.close()
            self._start("llm", LLM, [("llm-forecast", llm_command(sessions, arms, approval))])

        def send():
            token = approvals.issue({"command": "llm-forecast", "sessions": sessions, "arms": arms,
                                     "profile": defs.DEFAULT_PROFILE, "max_requests": plan["requests"]})
            start(token)

        with ui.dialog() as confirm, ui.card().classes("w-[44rem] max-w-full gap-2").style(f"background:{_PANEL}"):
            n = plan["requests"]
            ui.label("Send Claude requests?" if n else "Nothing to send").classes("text-lg font-medium")
            with ui.grid(columns="8rem" + " minmax(0,1fr)" * len(arms)).classes("w-full gap-x-3 gap-y-0"):
                ui.label("session").classes("text-xs").style(_MUTED)
                for a in arms:
                    ui.label(f"arm {a}").classes("text-xs").style(_MUTED)
                for row in plan["sessions"]:
                    ui.label(row["session_date"]).classes("text-sm")
                    for a in arms:
                        text = row.get(a) or f"skipped - {row.get('skip')}"
                        ui.label(text).classes("text-sm break-words").style(
                            "color:#ffa726" if text == "request" else _MUTED)
            ui.label(f"{n} request(s) to {plan['model']} (effort {plan['effort']}): {plan['requests_c']} restricted "
                     f"annotation(s), {plan['requests_d']} synthesis(es) - roughly ${plan['usd']} at list prices, "
                     f"at most ${plan['usd_max']} if every request used its whole token cap (effort xhigh thinks "
                     f"at length, and the thinking is billed).").classes("text-sm")
            if "C" in arms:
                ui.label(f"Arm C's pool: {plan['restricted_pool']} session(s) annotated by "
                         f"{pre.RESTRICTED_PROTOCOL_VERSION}. Its analogues come only from those; with few, arm C "
                         f"is close to the prior. The pool is cheapest at half price through the Batch API, from a "
                         f"terminal: python scripts/nq_journal.py annotate-llm --restricted --start ... --end ... "
                         f"--batch").classes("text-xs").style(_MUTED)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=confirm.close).props("flat no-caps")
                if n:
                    ui.button(f"Send {n} request(s)", icon="send", on_click=send).props("no-caps color=warning")
                elif "C" in arms:
                    ui.button("Issue arm C runs from stored annotations", on_click=lambda: start()).props("no-caps")
        confirm.open()

    def live(self) -> None:
        allowed, why = live_window(datetime.now(timezone.utc))
        if not allowed:
            ui.notify(f"The live forecast cannot start now: {why}.", type="warning")
            return
        self._start("live", LIVE, [("live", live_command())])

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
        now = datetime.now(timezone.utc)
        allowed, why = live_window(now)
        self.preview_possible, preview_why = preview_note(now)
        self.preview_why = preview_why
        self.collect_button.set_enabled(not busy)
        self.forecast_button.set_enabled(not busy)
        self.llm_button.set_enabled(not busy)
        self.preview_button.set_enabled(not busy and self.preview_possible)
        self.live_button.set_enabled(not busy and allowed)
        note = ("Possible " if self.preview_possible else "Not possible now: ") + preview_why
        if note != self.preview_note.text:
            self.preview_note.set_text(note)
            self.preview_note.style(f"color:{'#26a69a' if self.preview_possible else '#787b86'}")
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
