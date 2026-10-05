# tests/test_dashboard_jobs.py
"""
The dashboard's job runner (dashboard/jobs.py): a child process's output and exit
status, one job at a time, Stop, and when the live pre-open capture is offered.
"""

import asyncio
import sys
from datetime import datetime

import pytest

from dashboard.jobs import MAX_LINES, Job, JobRunner, Step, live_window
from features import calendar as cal


def _ny(day: str, hhmm: str) -> datetime:
    return cal.NY_TZ.localize(datetime.fromisoformat(f"{day}T{hhmm}"))


def _python(code: str):
    return [sys.executable, "-c", code]


async def _finished(runner: JobRunner, timeout: float = 30.0) -> Job:
    async def wait():
        while runner.busy:
            await asyncio.sleep(0.05)
    await asyncio.wait_for(wait(), timeout)
    return runner.job


def test_a_job_keeps_its_output_and_exit_status(tmp_path):
    log = tmp_path / "logs" / "pipeline_run.log"

    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=str(log))
        runner.start("collector", "Collector", [("collector", _python("import sys; print('one'); "
                                                                      "print('two', file=sys.stderr); sys.exit(3)"))])
        assert runner.busy
        return await _finished(runner)

    job = asyncio.run(run())
    assert list(job.lines) == ["one", "two"] and job.line_count == 2
    assert job.returncode == 3 and job.outcome == "failed (exit 3)"
    text = log.read_text().splitlines()
    assert "Dashboard: Collector started" in text[0] and "Dashboard: python -c" in text[1]
    assert text[2:4] == ["one", "two"]
    assert text[-1].endswith("Dashboard: Collector failed (exit 3).")


def test_one_job_at_a_time_and_stop(tmp_path):
    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("live", "Live", [("live", _python("import time; print('waiting', flush=True); time.sleep(60)"))])
        with pytest.raises(RuntimeError, match="still running"):
            runner.start("forecaster", "Forecaster", [("forecaster", _python("pass"))])
        while not runner.job.line_count:                 # the child is up
            await asyncio.sleep(0.05)
        assert runner.stop()
        job = await _finished(runner)
        assert not runner.stop()                         # nothing left to stop
        runner.start("forecaster", "Forecaster", [("forecaster", _python("print('next')"))])
        return job, await _finished(runner)

    stopped, after = asyncio.run(run())
    assert stopped.stopped and stopped.returncode != 0 and stopped.outcome.startswith("stopped")
    assert after.outcome == "finished" and list(after.lines) == ["next"]


def test_a_command_that_cannot_start_fails(tmp_path):
    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("collector", "Collector", [("collector", [str(tmp_path / "no-such-program")])])
        return await _finished(runner)

    job = asyncio.run(run())
    assert job.returncode == -1 and "FileNotFoundError" in job.lines[0]


def test_a_page_asks_for_the_lines_it_has_not_shown():
    job = Job("collector", "Collector", [Step("collector", [])], datetime.now())
    for i in range(MAX_LINES + 10):
        job.lines.append(str(i))
        job.line_count += 1
    assert job.since(job.line_count) == []
    assert job.since(job.line_count - 2) == [str(MAX_LINES + 8), str(MAX_LINES + 9)]
    assert len(job.since(0)) == MAX_LINES and job.since(0)[0] == "10"     # the oldest are no longer kept


def test_the_live_capture_is_offered_before_the_open_only():
    ok, why = live_window(_ny("2026-10-05", "08:45"))                     # a Monday
    assert ok and "waits for the 09:29 ET cutoff" in why
    ok, why = live_window(_ny("2026-10-05", "09:30"))
    assert not ok and "opened" in why and "Tue 2026-10-06" in why
    ok, why = live_window(_ny("2026-10-10", "08:00"))                     # Saturday
    assert not ok and "no session on Sat 2026-10-10" in why and "Mon 2026-10-12" in why


def test_a_job_has_no_terminal(tmp_path):
    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("forecaster", "Forecaster",
                     [("forecaster", _python("import sys; print(repr(sys.stdin.read()), sys.stdin.isatty())"))])
        return await _finished(runner)

    assert list(asyncio.run(run()).lines) == ["'' False"]     # nothing can ask a person, or wait for one


def test_a_job_runs_its_steps_in_turn_even_after_a_failed_one(tmp_path):
    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("preview", "Forecast now", [("collector", _python("import sys; print('no IB'); sys.exit(2)")),
                                                 ("preview", _python("print('preview from the stored bars')"))])
        return await _finished(runner)

    job = asyncio.run(run())
    assert list(job.lines) == ["Dashboard: step 1/2, collector", "no IB", "Dashboard: step 2/2, preview",
                               "preview from the stored bars"]
    assert [s.returncode for s in job.steps] == [2, 0]
    assert job.returncode == 2 and job.outcome == "collector failed (exit 2)"


def test_stop_skips_the_steps_after_the_running_one(tmp_path):
    async def run():
        runner = JobRunner(cwd=str(tmp_path), log_file=None)
        runner.start("preview", "Forecast now", [("collector", _python("import time; print('up', flush=True); "
                                                                       "time.sleep(60)")),
                                                 ("preview", _python("print('should not run')"))])
        while not runner.job.line_count >= 2:
            await asyncio.sleep(0.05)
        assert runner.stop()
        return await _finished(runner)

    job = asyncio.run(run())
    assert job.stopped and job.steps[1].returncode is None and "should not run" not in job.lines
    assert job.outcome.startswith("stopped")
