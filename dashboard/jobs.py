# dashboard/jobs.py
"""
The pipeline's jobs, started from the dashboard. Each runs as its own process -
the same command as on the command line - so a long download never holds the
dashboard's database connection or its event loop:

  collector    python -m collector.ib_collector --days N
               the missing bars from IB, then the journal step, as after every
               full collection
  forecaster   python -m collector.ib_collector --journal-only
               that journal step alone, no IB: the event calendar and earnings,
               then the snapshot, annotation, analogue set and historical-replay
               forecasts of every session past its cutoff, outcomes once final
  preview      the collector, then python scripts/nq_journal.py preview
               forecast now: the next session's forecast from the data stored so
               far, any time from its Globex open - never stored in the journal
               (forecaster/preview.py); the preview step runs even when the
               collection failed, from the bars already stored
  llm          python scripts/nq_journal.py llm-forecast --date D ... --approval T
               arms C and D (Claude) on the chosen sessions, after the
               dashboard's confirmation issued the one-time approval T for them
  live         python scripts/nq_journal.py live
               today's session captured at the 09:29 cutoff and its forecasts
               issued (forecaster/live_capture.py); only before the open
  auto         the collector for the session in progress only (--days 0
               --no-trailing-refresh: that session and any missing or
               incomplete day, not the refetch of the last complete sessions,
               which the daily run does; --workers 4: four symbols at a time;
               with the journal step), then the preview while one is possible -
               one run a minute while auto mode is on (AUTO, the Session
               Explorer's Auto button), so the chart, the fan and the analogue
               preview follow the session; each run also issues any pending mark
               of the intermarket fan's forward record (python scripts/fan.py
               forward issue, forecaster/fan_forward.py: every 15 minutes and
               09:29 ET, once the delayed feed has stored the mark's origin bar).
               A step that prints nothing for AUTO_STEP_IDLE_S is killed as hung;
               one that keeps reporting may run to AUTO_STEP_LIMIT_S

A job is one or more steps, each a process run in turn; Stop ends the running step
and skips the rest.

A job has no terminal (its stdin is empty), so nothing that asks a person - such as
the confirmation the Claude API requests need (they are manual only) - can run here.

One job runs at a time. It belongs to the dashboard process, not to a browser
tab: every page shows the same job and its output, a page opened while it runs
picks it up, and closing the tab does not stop it. Its output is appended to
logs/pipeline_run.log, beside the scheduled runs'. Auto mode belongs to the process
too: it starts its next run when no job is running, a few seconds into each minute,
and ends when switched off or when one of its runs is stopped.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional, TextIO, Tuple

from config import Config
from dashboard.components.fan import current_session
from features import calendar as cal
from forecaster.preview import PreviewUnavailable, preview_target

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(_PROJECT_ROOT, "logs", "pipeline_run.log")
MAX_LINES = 5000          # output kept in memory for the pages; the log file keeps all of it
STOP_GRACE_S = 15         # after Stop, seconds before a job that has not exited is killed
AUTO_SECOND = 5           # an auto run starts this many seconds into a minute, once the minute's bar has closed
AUTO_TITLE = "Auto update"
# An auto step that prints nothing for AUTO_STEP_IDLE_S is hung: killed, so the next minute can run. A slow one that
# keeps reporting - the collector logs every request, and every pacing wait before it waits - may run to
# AUTO_STEP_LIMIT_S (a normal auto run takes under a minute in all).
AUTO_STEP_IDLE_S = 120
AUTO_STEP_LIMIT_S = 900
AUTO_COLLECT_WORKERS = 4   # symbols an auto run collects at once (collector --workers)


def collector_command(days: int, trailing_refresh: bool = True, workers: int = 1) -> List[str]:
    """The collector over ``days`` back; without ``trailing_refresh`` (Auto mode, every minute) only the session in
    progress and missing or incomplete days - the last complete sessions' vendor revisions are left to the daily
    run; ``workers`` symbols collected at once (threads sharing the IB connection and its pacer)."""
    return ([sys.executable, "-m", "collector.ib_collector", "--days", str(int(days)), "--host", Config.IB_HOST,
             "--port", str(Config.IB_PORT), "--client-id", str(Config.IB_CLIENT_ID)]
            + ([] if trailing_refresh else ["--no-trailing-refresh"])
            + (["--workers", str(int(workers))] if workers > 1 else []))


def forecaster_command() -> List[str]:
    return [sys.executable, "-m", "collector.ib_collector", "--journal-only"]


def live_command() -> List[str]:
    return [sys.executable, os.path.join("scripts", "nq_journal.py"), "live"]


def preview_command() -> List[str]:
    return [sys.executable, os.path.join("scripts", "nq_journal.py"), "preview"]


def llm_command(days: List[str], arms: str, approval: Optional[str] = None, batch: bool = False) -> List[str]:
    """Arms C and D on the given session ``days`` (forecaster/llm_arms.py); Claude requests need the dashboard's
    one-time ``approval`` for exactly those days (forecaster/approvals.py) - without one only what needs no request
    runs."""
    dates = [x for d in days for x in ("--date", d)]
    return ([sys.executable, os.path.join("scripts", "nq_journal.py"), "llm-forecast", *dates, "--arms", arms]
            + (["--batch"] if batch else []) + (["--approval", approval] if approval else []))


def live_window(now: datetime) -> Tuple[bool, str]:
    """
    Whether the live capture can start at ``now`` (aware), and what it will do or
    why not: on a scheduled session before its 09:30 open - it then waits for
    the cutoff. After the open no live snapshot can be frozen, so it is not offered.
    """
    today = now.astimezone(cal.NY_TZ).date()
    try:
        s = cal.session(today)
    except cal.CalendarCoverageError as e:
        return False, str(e)
    if s.is_open and now < s.rth_open_at:
        return True, f"{today:%a %Y-%m-%d}: waits for the 09:29 ET cutoff, then issues by 09:29:50 ET"
    nxt = cal.next_session(today)
    when = f"next: {nxt.session_date:%a %Y-%m-%d} before 09:30 ET" if nxt else "no later session in the calendar"
    return False, ("today's session has opened" if s.is_open else f"no session on {today:%a %Y-%m-%d}") + f"; {when}"


@dataclass
class Step:
    label: str                       # collector, preview, ...
    command: List[str]
    returncode: Optional[int] = None


@dataclass
class Job:
    key: str                         # collector | forecaster | preview | live
    title: str
    steps: List[Step]
    started_at: datetime
    finished_at: Optional[datetime] = None
    stopped: bool = False            # stopped from the dashboard
    lines: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    line_count: int = 0              # every line read, so a page can ask for the ones it has not shown

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def returncode(self) -> Optional[int]:
        """The first step's that failed, else 0; None while running."""
        if self.running:
            return None
        return next((s.returncode for s in self.steps if s.returncode), 0)

    @property
    def outcome(self) -> str:
        if self.running:
            return "running"
        if self.stopped:
            return f"stopped (exit {self.returncode})"
        failed = [s for s in self.steps if s.returncode]
        if not failed:
            return "finished"
        if len(self.steps) == 1:
            return f"failed (exit {failed[0].returncode})"
        return "; ".join(f"{s.label} failed (exit {s.returncode})" for s in failed)

    def since(self, seen: int) -> List[str]:
        """The lines after the first ``seen``, as many of them as are still kept."""
        new = self.line_count - seen
        return list(self.lines)[-new:] if new > 0 else []


class JobRunner:
    """Runs one job at a time as a child process and keeps its output (see the module docstring)."""

    def __init__(self, cwd: str = _PROJECT_ROOT, log_file: Optional[str] = LOG_FILE) -> None:
        self.cwd, self.log_file = cwd, log_file
        self.job: Optional[Job] = None               # the running job, else the last one
        self._process: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._log: Optional[TextIO] = None
        self.step_timeout: Optional[float] = None
        self.step_idle: Optional[float] = None

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.running

    def start(self, key: str, title: str, steps: List[Tuple[str, List[str]]],
              step_timeout: Optional[float] = None, step_idle: Optional[float] = None) -> Job:
        """Starts the job of ``steps`` - ``(label, command)`` pairs, run in turn - on the running event loop; raises
        RuntimeError while another runs. A step still running after ``step_timeout`` seconds, or silent - no line of
        output - for ``step_idle`` seconds, is killed and fails."""
        if self.busy:
            raise RuntimeError(f"{self.job.title} is still running")
        self.step_timeout, self.step_idle = step_timeout, step_idle
        self.job = Job(key, title, [Step(label, command) for label, command in steps], datetime.now(timezone.utc))
        self._task = asyncio.get_running_loop().create_task(self._run(self.job))
        return self.job

    def stop(self) -> bool:
        """Interrupts the running step as Ctrl-C would, so it closes its connections (killed if it has not exited
        after STOP_GRACE_S), and skips the steps after it. False when nothing is running."""
        if not self.busy:
            return False
        self.job.stopped = True
        process = self._process
        if process is not None and process.returncode is None:
            process.send_signal(signal.SIGINT)
            asyncio.get_running_loop().call_later(STOP_GRACE_S, self._kill, process)
        return True

    def _timed_out(self, job: Job, process: asyncio.subprocess.Process, why: str) -> None:
        if process.returncode is None:
            self._add(job, f"Dashboard: step {why} - killed")
            process.kill()

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()

    async def _run(self, job: Job) -> None:
        self._open_log()
        self._write(f"[{job.started_at:%Y-%m-%dT%H:%M:%SZ}] Dashboard: {job.title} started.")
        try:
            for i, step in enumerate(job.steps, 1):
                if job.stopped:
                    break
                if len(job.steps) > 1:
                    self._add(job, f"Dashboard: step {i}/{len(job.steps)}, {step.label}")
                self._write(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] Dashboard: "
                            f"{' '.join(['python'] + step.command[1:])}")
                step.returncode = await self._run_step(job, step)
        finally:
            job.finished_at = datetime.now(timezone.utc)
            self._write(f"[{job.finished_at:%Y-%m-%dT%H:%M:%SZ}] Dashboard: {job.title} {job.outcome}.")
            self._close_log()

    async def _run_step(self, job: Job, step: Step) -> int:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *step.command, cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, limit=1 << 20)
            process = self._process
            loop = asyncio.get_running_loop()
            limit = (loop.call_later(self.step_timeout, self._timed_out, job, process,
                                     f"ran {self.step_timeout:.0f} s") if self.step_timeout else None)
            idle = None

            def listen() -> None:                     # (re)armed by every line: silence is what kills
                nonlocal idle
                if idle is not None:
                    idle.cancel()
                if self.step_idle:
                    idle = loop.call_later(self.step_idle, self._timed_out, job, process,
                                           f"printed nothing for {self.step_idle:.0f} s")
            listen()
            try:
                async for raw in process.stdout:
                    listen()
                    self._add(job, raw.decode(errors="replace").rstrip())
                return await process.wait()
            finally:
                for watchdog in (limit, idle):
                    if watchdog is not None:
                        watchdog.cancel()
        except Exception as e:                        # not started, or its output could not be read
            self._add(job, f"Dashboard: {type(e).__name__}: {e}")
            if self._process is not None and self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            return self._process.returncode if self._process is not None else -1
        finally:
            self._process = None

    def _add(self, job: Job, line: str) -> None:
        job.lines.append(line)
        job.line_count += 1
        self._write(line)

    # -- the log file: a failure to write it never stops a job --------------------

    def _open_log(self) -> None:
        if self.log_file:
            try:
                os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
                self._log = open(self.log_file, "a", encoding="utf-8")
            except OSError:
                self._log = None

    def _write(self, line: str) -> None:
        if self._log is not None:
            try:
                self._log.write(line + "\n")
                self._log.flush()
            except OSError:
                pass

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None


def forward_command(started: Optional[datetime] = None) -> List[str]:
    """The forward record's pending marks, recorded as issued by Auto mode (fan_forward.TRIGGERS) - with the time
    its run ``started``, before the collection, so the record times the whole run."""
    cmd = [sys.executable, os.path.join("scripts", "fan.py"), "forward", "issue", "--trigger", "auto"]
    return cmd + (["--triggered-at", started.astimezone(timezone.utc).isoformat()] if started else [])


def forward_due(now: datetime) -> bool:
    """Whether an auto run tries the fan's forward record (forecaster/fan_forward.py): every run while a session is
    in progress - the feed is delayed, so a mark is issued by the first run that finds its origin bar stored; the
    issue itself skips what is issued and says once when a mark's bar is still missing."""
    return current_session(now) is not None


def auto_steps(now: datetime) -> List[Tuple[str, List[str]]]:
    """An auto run's steps at ``now``: the session in progress collected - its missing and incomplete days only, not
    the trailing refresh, so a run fits in its minute - (and the journal step), then the preview while one is
    possible (forecaster/preview.py), and the fan's forward record's pending marks."""
    steps = [("collector", collector_command(0, trailing_refresh=False, workers=AUTO_COLLECT_WORKERS))]
    try:
        preview_target(now)
        steps.append(("preview", preview_command()))
    except PreviewUnavailable:
        pass
    if forward_due(now):
        steps.append(("forward", forward_command(now)))
    return steps


class AutoMode:
    """
    Collecting and forecasting once a minute while on (see the module docstring): a run starts when no job is
    running, AUTO_SECOND seconds or more into a minute it has not run in, while a session is in progress. Stopping
    one of its runs (Update data, Stop) switches it off.
    """

    def __init__(self, runner: JobRunner) -> None:
        self.runner = runner
        self.on = False
        self._task: Optional[asyncio.Task] = None
        self._last_minute: Optional[datetime] = None
        self._started: Optional[Job] = None              # its latest run

    def switch(self, on: bool) -> None:
        if on and not self.on:
            self.on, self._started = True, None
            self._task = asyncio.get_running_loop().create_task(self._loop())
        elif not on and self.on:
            self.on = False
            if self._task is not None:
                self._task.cancel()
                self._task = None

    def due(self, now: datetime) -> Optional[str]:
        """The session to collect at ``now``, or None: a job is running, this minute had its run, it is too early in
        the minute, or no session is in progress."""
        if self.runner.busy or now.second < AUTO_SECOND:
            return None
        if self._last_minute == now.replace(second=0, microsecond=0):
            return None
        return current_session(now)

    def status(self, now: datetime) -> str:
        if not self.on:
            return "Off: collect and forecast by hand (Update data)"
        if current_session(now) is None:
            return "On - waiting: no session in progress"
        return "On - collecting and forecasting once a minute"

    async def _loop(self) -> None:
        try:
            while self.on:
                if self._started is not None and self._started.stopped:
                    self.on = False
                    break
                now = datetime.now(timezone.utc)
                if self.due(now):
                    self._started = self.runner.start("auto", AUTO_TITLE, auto_steps(now),
                                                      step_timeout=AUTO_STEP_LIMIT_S, step_idle=AUTO_STEP_IDLE_S)
                    self._last_minute = now.replace(second=0, microsecond=0)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass


# The dashboard's one runner, shared by every page, and its auto mode.
RUNNER = JobRunner()
AUTO = AutoMode(RUNNER)
