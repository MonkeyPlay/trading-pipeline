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
               then snapshots, structure annotations and outcomes of the final
               sessions, the analogue sets and the historical-replay forecasts
  live         python scripts/nq_journal.py live
               today's session captured at the 09:29 cutoff and its forecasts
               issued (forecaster/live_capture.py); only before the open

A job has no terminal (its stdin is empty), so nothing that asks a person - such as
the confirmation the Claude API requests need (they are manual only) - can run here.

One job runs at a time. It belongs to the dashboard process, not to a browser
tab: every page shows the same job and its output, a page opened while it runs
picks it up, and closing the tab does not stop it. Its output is appended to
logs/pipeline_run.log, beside the scheduled runs'.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Deque, List, Optional, TextIO, Tuple

from config import Config
from features import calendar as cal

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(_PROJECT_ROOT, "logs", "pipeline_run.log")
MAX_LINES = 5000          # output kept in memory for the pages; the log file keeps all of it
STOP_GRACE_S = 15         # after Stop, seconds before a job that has not exited is killed


def collector_command(days: int) -> List[str]:
    return [sys.executable, "-m", "collector.ib_collector", "--days", str(int(days)), "--host", Config.IB_HOST,
            "--port", str(Config.IB_PORT), "--client-id", str(Config.IB_CLIENT_ID)]


def forecaster_command() -> List[str]:
    return [sys.executable, "-m", "collector.ib_collector", "--journal-only"]


def live_command() -> List[str]:
    return [sys.executable, os.path.join("scripts", "nq_journal.py"), "live"]


def next_session(after: date) -> Optional[cal.Session]:
    """The first scheduled session after the date ``after``; None past the calendar's coverage."""
    d = after
    while True:
        d += timedelta(days=1)
        try:
            s = cal.session(d)
        except cal.CalendarCoverageError:
            return None
        if s.is_open:
            return s


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
    nxt = next_session(today)
    when = f"next: {nxt.session_date:%a %Y-%m-%d} before 09:30 ET" if nxt else "no later session in the calendar"
    return False, ("today's session has opened" if s.is_open else f"no session on {today:%a %Y-%m-%d}") + f"; {when}"


@dataclass
class Job:
    key: str                         # collector | forecaster | live
    title: str
    command: List[str]
    started_at: datetime
    finished_at: Optional[datetime] = None
    returncode: Optional[int] = None
    stopped: bool = False            # stopped from the dashboard
    lines: Deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    line_count: int = 0              # every line read, so a page can ask for the ones it has not shown

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def outcome(self) -> str:
        if self.running:
            return "running"
        if self.stopped:
            return f"stopped (exit {self.returncode})"
        return "finished" if self.returncode == 0 else f"failed (exit {self.returncode})"

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

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.running

    def start(self, key: str, title: str, command: List[str]) -> Job:
        """Starts ``command`` as the job, on the running event loop; raises RuntimeError while another runs."""
        if self.busy:
            raise RuntimeError(f"{self.job.title} is still running")
        self.job = Job(key, title, command, datetime.now(timezone.utc))
        self._task = asyncio.get_running_loop().create_task(self._run(self.job))
        return self.job

    def stop(self) -> bool:
        """Interrupts the running job as Ctrl-C would, so it closes its connections; killed if it has not exited
        after STOP_GRACE_S. False when nothing is running."""
        process = self._process
        if not self.busy or process is None or process.returncode is not None:
            return False
        self.job.stopped = True
        process.send_signal(signal.SIGINT)
        asyncio.get_running_loop().call_later(STOP_GRACE_S, self._kill, process)
        return True

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()

    async def _run(self, job: Job) -> None:
        self._open_log()
        self._write(f"[{job.started_at:%Y-%m-%dT%H:%M:%SZ}] Dashboard: {job.title} started: "
                    f"{' '.join(['python'] + job.command[1:])}")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *job.command, cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, limit=1 << 20)
            async for raw in self._process.stdout:
                self._add(job, raw.decode(errors="replace").rstrip())
            job.returncode = await self._process.wait()
        except Exception as e:                        # not started, or its output could not be read
            self._add(job, f"Dashboard: {type(e).__name__}: {e}")
            if self._process is not None and self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            job.returncode = self._process.returncode if self._process is not None else -1
        finally:
            self._process = None
            job.finished_at = datetime.now(timezone.utc)
            self._write(f"[{job.finished_at:%Y-%m-%dT%H:%M:%SZ}] Dashboard: {job.title} {job.outcome}.")
            self._close_log()

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


# The dashboard's one runner, shared by every page.
RUNNER = JobRunner()
