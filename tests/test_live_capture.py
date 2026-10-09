# tests/test_live_capture.py
"""The live capture's freshness rule (forecaster/live_capture.fresh_bars, issue policy nq_issue_live_v3) with a fake
IB connection and a fake clock - no database, no network. The stored capture is tested in tests/test_nq_journal.py."""

from datetime import date, time, timedelta

from contracts import nq_forecast as fc
from features import calendar as cal
from forecaster.live_capture import fresh_bars

DAY = "2026-06-12"
SESSION = cal.session(DAY)
CUTOFF = cal.ny_instant(date(2026, 6, 12), time(9, 29))


class Clock:
    def __init__(self, t):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def bar(minutes_before_cutoff):
    start = CUTOFF - timedelta(minutes=minutes_before_cutoff)
    return {"timestamp_utc": start.strftime("%Y-%m-%d %H:%M:%S"), "trading_day": DAY, "open": 1.0, "high": 1.0,
            "low": 1.0, "close": 1.0, "volume": 1, "wap": None, "bar_count": 1, "session_scope": "ETH"}


class FakeIB:
    """Answers with bars up to ``last`` minutes before the cutoff; ``arrives`` calls later the 09:28 bar is there."""
    def __init__(self, arrives):
        self.calls, self.arrives = [], arrives

    def fetch_historical_bars(self, contract, end, duration, what_to_show=None):
        self.calls.append((end, duration))
        newest = 1 if self.arrives is not None and len(self.calls) >= self.arrives else 2
        return [bar(m) for m in range(30, newest - 1, -1)]


def run(arrives, start_offset=-30):
    clock = Clock(CUTOFF + timedelta(seconds=start_offset))
    ib, events = FakeIB(arrives), []
    out = fresh_bars(ib, {}, SESSION, CUTOFF, "TRADES", clock, clock.sleep, lambda e, **d: events.append((e, d)))
    return out, ib, clock, events


def test_it_waits_for_the_cutoff_and_retries_until_the_last_bar_arrives():
    bars, ib, clock, events = run(arrives=3)
    assert bars[-1]["timestamp_utc"] == "2026-06-12 13:28:00"                 # the bar ending at the cutoff
    assert ib.calls[0][0] == CUTOFF + timedelta(seconds=fc.LIVE_FIRST_REQUEST_S)   # not before the cutoff
    assert len(ib.calls) == 3 and clock.slept[1:] == [fc.LIVE_RETRY_S, fc.LIVE_RETRY_S]
    assert [e for e, _ in events] == ["bars_requested"] * 3
    seconds = int((ib.calls[0][0] - SESSION.overnight_start_at).total_seconds()) + 60
    assert ib.calls[0][1] == f"{seconds} S"                                  # from the overnight start


def test_without_the_last_bar_within_the_wait_the_capture_is_stale():
    bars, ib, clock, _ = run(arrives=None)
    assert bars is None
    limit = timedelta(seconds=fc.LIVE_DEFAULT_WAIT_S)
    assert all(end - CUTOFF <= limit for end, _ in ib.calls) and ib.calls[-1][0] - CUTOFF == limit  # last at it
    assert len(ib.calls) == 2 + (fc.LIVE_DEFAULT_WAIT_S - fc.LIVE_FIRST_REQUEST_S) // fc.LIVE_RETRY_S


def test_a_long_wait_retries_slowly_and_reports_the_newest_bar():
    """A 13-minute wait on a feed about 11 minutes late: every 2 s for the first 30 s, then every 15 s, until the
    bar ending at the cutoff arrives; each attempt records the newest bar it returned."""
    clock = Clock(CUTOFF - timedelta(minutes=5))
    events = []

    class LateIB(FakeIB):
        def fetch_historical_bars(self, contract, end, duration, what_to_show=None):
            self.calls.append((end, duration))
            newest = end - timedelta(minutes=11, seconds=30)           # bars appear 11.5 minutes after they start
            return [bar(m) for m in range(30, -5, -1) if CUTOFF - timedelta(minutes=m) <= newest]

    ib = LateIB(None)
    out = fresh_bars(ib, {}, SESSION, CUTOFF, "TRADES", clock, clock.sleep, lambda e, **d: events.append((e, d)),
                     wait_s=13 * 60)
    assert out is not None and out[-1]["timestamp_utc"] == "2026-06-12 13:28:00"
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(ib.calls, ib.calls[1:])]
    fast = 1 + (fc.LIVE_FAST_S - fc.LIVE_FIRST_REQUEST_S) // fc.LIVE_RETRY_S
    assert set(gaps[:fast - 1]) == {fc.LIVE_RETRY_S} and set(gaps[fast:]) == {fc.LIVE_SLOW_RETRY_S}
    assert ib.calls[-1][0] - CUTOFF < timedelta(minutes=11, seconds=45)    # found within one slow retry
    assert events[0][1]["newest"] < "2026-06-12 13:28:00" and events[-1][1]["newest"] == "2026-06-12 13:28:00"
    assert len(ib.calls) < 60                                            # well inside IB's request pacing


def test_a_late_start_requests_at_once():
    bars, ib, clock, _ = run(arrives=1, start_offset=5)
    assert bars is not None and len(ib.calls) == 1 and clock.slept == []
