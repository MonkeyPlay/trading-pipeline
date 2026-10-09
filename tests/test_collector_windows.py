# tests/test_collector_windows.py
"""
The collector's requests around the 18:00 ET open (no IB, no database): a day's window
ends 06:00 UTC the next day or now, so the session in progress and the one before it
share one window - requested once, both days stored from its answer, never an identical
request (which IB forbids within 15 s, and the pacer would wait out). And a run every
minute (--no-trailing-refresh) plans only missing and incomplete days.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import collector.ib_collector as collector
from collector import coverage
from config import INSTRUMENTS

TEN_Y = 887699006


def _info():
    return {"con_id": TEN_Y, "symbol": "10Y", "expiry": "20261030", "sec_type": "FUT", "exchange": "CBOT",
            "currency": "USD"}


def _bar(day, hhmm="14:30"):
    return {"timestamp_utc": f"{day} {hhmm}:00", "open": 3.9, "high": 3.9, "low": 3.9, "close": 3.9, "volume": 1,
            "session_scope": "RTH", "trading_day": day}


class Window:
    """IB as the collector sees it at 18:06 ET on 2026-10-06: a window ending now holds the whole 2026-10-06
    session and nothing of 2026-10-07 yet (the delayed feed); the 2026-10-05 window holds that session."""

    def __init__(self, fail=0):
        self.asked, self.fail = [], fail

    def fetch_historical_bars(self, contract, end_dt, duration_str, what_to_show="TRADES", **_):
        self.asked.append(end_dt)
        if self.fail:
            self.fail -= 1
            return None                                  # a timeout or an IB error
        if end_dt == datetime(2026, 10, 6, 6, tzinfo=timezone.utc):
            return [_bar("2026-10-05"), _bar("2026-10-06", "02:00")]
        return [_bar("2026-10-06"), _bar("2026-10-06", "20:59")]


@pytest.fixture
def stored(monkeypatch):
    saved = []

    def save_trading_day(conn, contract_id, trading_day, bars, **_):
        saved.append((trading_day, len(bars)))
        return {"bar_count": len(bars)}

    monkeypatch.setattr(collector, "create_collection_run", lambda conn, **_: 1)
    monkeypatch.setattr(collector, "update_collection_run", lambda *a, **kw: None)
    monkeypatch.setattr(collector, "save_trading_day", save_trading_day)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)
    return saved


NOW = datetime(2026, 10, 6, 22, 6, tzinfo=timezone.utc)        # 18:06 ET: the 2026-10-07 session has opened


def test_the_session_in_progress_and_the_one_before_share_one_request(stored):
    ib, windows = Window(), {}
    for day in ("2026-10-07", "2026-10-06", "2026-10-05"):     # newest first, as a run asks
        collector._fetch_and_store_day(ib, None, INSTRUMENTS["10Y"], _info(), day, NOW, windows)
    assert ib.asked == [NOW, datetime(2026, 10, 6, 6, tzinfo=timezone.utc)]   # two requests for three days
    assert stored == [("2026-10-07", 0), ("2026-10-06", 2), ("2026-10-05", 1)]  # each day from its window's answer


def test_a_failed_answer_is_asked_again_and_without_the_memory_every_day_asks(stored):
    ib, windows = Window(fail=1), {}
    assert collector._fetch_and_store_day(ib, None, INSTRUMENTS["10Y"], _info(), "2026-10-07", NOW, windows) is None
    assert collector._fetch_and_store_day(ib, None, INSTRUMENTS["10Y"], _info(), "2026-10-06", NOW, windows) == 2
    assert ib.asked == [NOW, NOW]                                # the failure was not remembered
    plain = Window()
    for day in ("2026-10-07", "2026-10-06"):
        collector._fetch_and_store_day(plain, None, INSTRUMENTS["10Y"], _info(), day, NOW)
    assert plain.asked == [NOW, NOW]                             # no memory given: as before


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_a_run_keeps_one_memory_per_contract(stored, monkeypatch):
    monkeypatch.setattr(collector, "datetime", Frozen)
    work = collector._Work("10Y", INSTRUMENTS["10Y"], None, rolling=True)
    other = {**_info(), "con_id": TEN_Y + 1, "expiry": "20261130"}
    work.jobs = [(_info(), ["2026-10-06", "2026-10-07"]), (other, ["2026-10-07"])]
    ib = Window()
    collector._collect_work(ib, None, work)
    assert len(ib.asked) == 2                                    # one per contract: the next contract asks its own


def test_without_the_trailing_refresh_only_missing_incomplete_and_in_progress_days_are_planned(monkeypatch):
    """At 18:06 ET the 2026-10-07 session has opened, and a run a minute ago stored it EMPTY (the delayed feed had
    nothing of it yet): it is still filling, so it is fetched again - with or without the trailing refresh."""
    row = lambda bars, status, open_=0: {"bar_count": bars, "status": status, "fetched_at": "x",
                                         "rth_bar_count": min(bars, 390), "open_bar_count": open_}
    ledger = {"2026-10-05": row(1380, "COMPLETE"), "2026-10-06": row(1380, "PARTIAL", 53)}
    monkeypatch.setattr(coverage, "get_stored_trading_days", lambda conn, contract_id, **_: ledger)
    monkeypatch.setattr(coverage, "datetime", Frozen)                    # "now" decides the day in progress
    plan = lambda **kw: {p.trading_day: (p.action, p.reason) for p in coverage.plan_trading_days(
        None, TEN_Y, date(2026, 10, 5), date(2026, 10, 7), **kw)}
    actions = lambda **kw: {d: a for d, (a, _) in plan(**kw).items()}
    assert actions() == {"2026-10-05": "refetch", "2026-10-06": "refetch", "2026-10-07": "fetch"}   # the daily run
    assert actions(trailing_days=None) == {"2026-10-05": "ok", "2026-10-06": "refetch", "2026-10-07": "fetch"}
    ledger["2026-10-07"] = row(0, "EMPTY")
    assert plan(trailing_days=None)["2026-10-07"] == ("refetch", "session in progress")
    assert plan()["2026-10-07"] == ("refetch", "session in progress")


class _LastAssigned:
    """A connection whose only answer is a symbol's last recorded active-contract day."""

    def __init__(self, last):
        self.last = last

    def execute(self, sql, params):
        assert "active_contracts" in sql
        last = self.last

        class Row:
            def fetchone(self):
                return (last,)
        return Row()


def test_a_missed_session_is_planned_and_assigned_again():
    """2026-10-08 was missed while nothing collected; the next run's window (today only, Auto) reaches back to it so
    its active contract is recorded - without it no snapshot, ATR or matcher reads the day. At most CATCH_UP_DAYS."""
    today = date(2026, 10, 9)
    assert collector._catch_up_start(_LastAssigned(date(2026, 10, 7)), "NQ", today) == date(2026, 10, 8)
    assert collector._catch_up_start(_LastAssigned("2026-10-08"), "NQ", today) == today       # nothing missed
    assert collector._catch_up_start(_LastAssigned(None), "NQ", today) == today               # a new symbol
    assert collector._catch_up_start(_LastAssigned(date(2026, 9, 1)), "NQ", today) == \
        today - timedelta(days=collector.CATCH_UP_DAYS)

