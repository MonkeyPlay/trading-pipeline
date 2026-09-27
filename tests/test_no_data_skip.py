# tests/test_no_data_skip.py
"""
A contract IB has no data for is given up on after a few tries (no IB, no
database): IB's "no data" answer is told apart from other failures, and the
collector skips a contract's remaining days after NO_DATA_SKIP_AFTER in a row.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import collector.ib_collector as collector
from collector.coverage import expected_trading_days
from collector.ib_collector import NO_DATA, NO_DATA_SKIP_AFTER, _Work, is_no_data
from config import INSTRUMENTS

NO_DATA_MSG = "Historical Market Data Service error message:HMDS query returned no data: 2YYN6@CBOT Trades"
N6, Q6 = 887699004, 887699005        # 2YY July and August 2026


def _contract(cid, expiry):
    return {"con_id": cid, "symbol": "2YY", "expiry": expiry, "sec_type": "FUT",
            "exchange": "CBOT", "currency": "USD"}


def _bar(day):
    return {"timestamp_utc": f"{day} 14:30:00", "open": 3.9, "high": 3.9, "low": 3.9, "close": 3.9,
            "volume": 1, "session_scope": "RTH", "trading_day": day}


def _days(start, end):
    return [d.isoformat() for d in expected_trading_days(date.fromisoformat(start), date.fromisoformat(end))]


def _work(*jobs):
    """2YY with one (contract id, expiry, days) job per contract."""
    work = _Work("2YY", INSTRUMENTS["2YY"], None, rolling=True)
    work.jobs = [(_contract(cid, expiry), days) for cid, expiry, days in jobs]
    return work


class FakeIB:
    """Answers each day's window from ``has_data(con_id, day)`` and records what was asked."""

    def __init__(self, has_data):
        self.has_data = has_data
        self.asked = []

    def fetch_historical_bars(self, contract, end_dt, duration_str, what_to_show="TRADES", **_):
        day = (end_dt - timedelta(days=1, hours=6)).date().isoformat()   # the window ends 06:00 UTC the next day
        self.asked.append((contract["con_id"], day))
        return [_bar(day)] if self.has_data(contract["con_id"], day) else []


@pytest.fixture
def stored(monkeypatch):
    """The database writes replaced by a list of the (contract id, day) pairs saved."""
    saved = []

    def save_trading_day(conn, contract_id, trading_day, bars, **_):
        saved.append((contract_id, trading_day))
        return {"bar_count": len(bars)}

    monkeypatch.setattr(collector, "create_collection_run", lambda conn, **_: 1)
    monkeypatch.setattr(collector, "update_collection_run", lambda *a, **kw: None)
    monkeypatch.setattr(collector, "save_trading_day", save_trading_day)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)
    return saved


def test_no_data_is_told_apart_from_other_errors():
    assert is_no_data(162, NO_DATA_MSG)
    assert not is_no_data(162, "Historical Market Data Service error message:Historical data request "
                               "pacing violation")
    assert not is_no_data(162, "Historical Market Data Service error message:API historical data "
                               "query cancelled: 5")
    assert not is_no_data(200, "No security definition has been found for the request")


def test_fetch_answers_empty_for_no_data_and_none_for_a_failure(monkeypatch):
    pytest.importorskip("ibapi")
    app = collector.IBCollectorApp()
    answers = iter([(162, NO_DATA_MSG), (162, "Historical Market Data Service error message:API historical "
                                              "data query cancelled: 5")])
    monkeypatch.setattr(app, "reqHistoricalData", lambda reqId, **_: app.error(reqId, *next(answers)))
    end = datetime(2026, 7, 10, 6, tzinfo=timezone.utc)
    assert app.fetch_historical_bars(_contract(N6, "20260731"), end, "2 D", min_spacing=0, timeout=1) == []
    assert app.fetch_historical_bars(_contract(N6, "20260731"), end + timedelta(days=1), "2 D",
                                     min_spacing=0, timeout=1) is None


def test_a_contract_without_data_is_skipped_and_the_next_one_still_collected(stored):
    n6_days, q6_days = _days("2026-06-18", "2026-07-27"), _days("2026-07-17", "2026-08-27")
    ib = FakeIB(lambda cid, day: cid == Q6)
    written = collector._collect_work(ib, None, _work((N6, "20260731", n6_days), (Q6, "20260831", q6_days)))

    # Its newest days are tried, then the other ones are left for the next run.
    assert [d for cid, d in ib.asked if cid == N6] == sorted(n6_days, reverse=True)[:NO_DATA_SKIP_AFTER]
    # IB's "no data" is about one contract: the next one of the same future gets its own tries.
    assert [d for cid, d in ib.asked if cid == Q6] == sorted(q6_days, reverse=True)
    assert stored == [(Q6, d) for d in sorted(q6_days, reverse=True)]
    assert written == len(q6_days)


def test_thin_warm_up_days_do_not_cost_a_contract_its_active_days(stored):
    days = _days("2026-07-17", "2026-08-27")
    warm_up = set(days[:7])                          # no trades at all while it was the deferred month
    ib = FakeIB(lambda cid, day: day not in warm_up)
    collector._collect_work(ib, None, _work((Q6, "20260831", days)))

    active = sorted(set(days) - warm_up, reverse=True)
    assert [d for _, d in stored] == active
    assert [d for _, d in ib.asked] == active + sorted(warm_up, reverse=True)[:NO_DATA_SKIP_AFTER]


def test_a_day_with_data_restarts_the_count(stored):
    days = sorted(_days("2026-06-01", "2026-07-27"), reverse=True)
    # Newest first: one day short of the limit without data, then a day with data, and again.
    ib = FakeIB(lambda cid, day: days.index(day) % NO_DATA_SKIP_AFTER == NO_DATA_SKIP_AFTER - 1)
    collector._collect_work(ib, None, _work((N6, "20260731", days)))
    assert [d for _, d in ib.asked] == days


def test_bars_around_a_day_but_none_on_it_still_store_it_empty(stored):
    """The day itself had no trading (the neighbouring session did): that is recorded, as before."""
    class Neighbour:
        def fetch_historical_bars(self, *a, **kw):
            return [_bar("2026-07-02")]

    now = datetime.now(timezone.utc)
    info = _contract(N6, "20260731")
    assert collector._fetch_and_store_day(Neighbour(), None, INSTRUMENTS["2YY"], info, "2026-07-03", now) == 0
    assert stored == [(N6, "2026-07-03")]
    assert collector._fetch_and_store_day(FakeIB(lambda c, d: False), None, INSTRUMENTS["2YY"], info,
                                          "2026-07-06", now) == NO_DATA
    assert stored == [(N6, "2026-07-03")]            # a window without any data stores nothing
