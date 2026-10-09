# tests/test_collector_parallel.py
"""
The collector in parallel (no IB, no database): the pacer reserves each request's slot
under every rule - identical requests 15 s apart, the volume window, the spacing per
contract and the small gap between any two - so requests for different contracts go out
together while one contract's stay spaced, from any number of threads; a pacing violation
pushes the next slot back without stalling IB's reader thread; and the symbols are
collected by a pool of workers, each with its own database connection, each contract's
days still newest first.
"""

import threading
import time
from datetime import date

import pytest

import collector.ib_collector as collector
from collector.pacing import IBKRPacer
from config import INSTRUMENTS


def _pacer():
    return IBKRPacer(max_requests_per_window=5, window_seconds=100.0, default_delay=2.0, global_delay=0.25)


def test_different_contracts_go_out_together_and_one_contract_stays_spaced():
    p = _pacer()
    t0 = 1000.0
    assert p.reserve(1, "2 D", "1 min", "e1", now=t0) == t0
    assert p.reserve(2, "2 D", "1 min", "e2", now=t0) == t0 + 0.25            # another contract: the small gap only
    assert p.reserve(1, "2 D", "1 min", "e3", now=t0) == t0 + 2.0             # the same contract: its spacing
    assert p.reserve(3, "2 D", "1 min", "e4", now=t0, min_spacing=0) == t0 + 2.25   # after the last slot reserved
    assert p.reserve(1, "2 D", "1 min", "e3", now=t0 + 3) == t0 + 2.0 + 15.0  # identical: 15 s after the first


def test_the_volume_window_holds_for_reserved_slots():
    p = _pacer()
    slots = [p.reserve(c, "2 D", "1 min", f"e{c}", now=0.0) for c in range(1, 8)]
    assert slots[:5] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert slots[5] == 100.0 and slots[6] == 100.25                         # at most 5 in any 100 s


def test_a_pacing_violation_pushes_the_next_slot_back_without_sleeping(monkeypatch):
    p = _pacer()
    monkeypatch.setattr("collector.pacing.time.time", lambda: 50.0)
    started = time.perf_counter()
    p.handle_rate_limit_error(backoff_seconds=30.0)
    assert time.perf_counter() - started < 0.5                                # IB's reader thread is not held up
    assert p.reserve(9, "2 D", "1 min", "e", now=51.0) == 80.0


def test_reservations_from_many_threads_keep_every_rule():
    p = IBKRPacer(max_requests_per_window=1000, window_seconds=600.0, default_delay=2.0, global_delay=0.25)
    out, lock = [], threading.Lock()

    def worker(c):
        for i in range(20):
            at = p.reserve(c % 4, "2 D", "1 min", f"{c}-{i}", now=0.0)
            with lock:
                out.append((at, c % 4))

    threads = [threading.Thread(target=worker, args=(c,)) for c in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    slots = sorted(out)
    assert all(b[0] - a[0] >= 0.25 - 1e-9 for a, b in zip(slots, slots[1:]))       # any two: the small gap
    for c in range(4):
        mine = [s for s, k in slots if k == c]
        assert all(b - a >= 2.0 - 1e-9 for a, b in zip(mine, mine[1:]))             # one contract: its spacing


# ---------------------------------------------------------------------------
# The workers
# ---------------------------------------------------------------------------

def _bar(day):
    return {"timestamp_utc": f"{day} 14:30:00", "open": 3.9, "high": 3.9, "low": 3.9, "close": 3.9, "volume": 1,
            "session_scope": "RTH", "trading_day": day}


class SlowIB:
    """Answers each day's window after a moment, recording which requests were in flight together."""

    def __init__(self, fail_symbol=None):
        self.lock, self.in_flight, self.most, self.asked = threading.Lock(), 0, 0, []
        self.fail_symbol = fail_symbol

    def fetch_historical_bars(self, contract, end_dt, duration_str, what_to_show="TRADES", **_):
        with self.lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        time.sleep(0.05)
        with self.lock:
            self.in_flight -= 1
            day = (end_dt.date() - collector.timedelta(days=1)).isoformat()
            self.asked.append((contract["con_id"], day))
        return [_bar(day)]


@pytest.fixture
def stored(monkeypatch):
    saved, lock = [], threading.Lock()

    def save_trading_day(conn, contract_id, trading_day, bars, **_):
        with lock:
            saved.append((conn, contract_id, trading_day))
        return {"bar_count": len(bars)}

    monkeypatch.setattr(collector, "create_collection_run", lambda conn, **_: 1)
    monkeypatch.setattr(collector, "update_collection_run", lambda *a, **kw: None)
    monkeypatch.setattr(collector, "save_trading_day", save_trading_day)
    return saved


class Conn:
    opened, closed = [], []

    def __init__(self):
        Conn.opened.append(self)

    def close(self):
        Conn.closed.append(self)


def _works(n):
    """``n`` single-contract symbols, three old days each (their windows end in the past: one request a day)."""
    days = ["2026-09-28", "2026-09-29", "2026-09-30"]
    out = []
    for k in range(n):
        w = collector._Work(f"S{k}", INSTRUMENTS["10Y"], "20261030", rolling=False)
        w.jobs = [({"con_id": 500 + k, "symbol": f"S{k}", "expiry": "20261030", "sec_type": "FUT",
                    "exchange": "CBOT", "currency": "USD"}, list(days))]
        out.append(w)
    return out


def test_symbols_are_collected_by_workers_each_with_its_own_connection(stored):
    Conn.opened, Conn.closed = [], []
    ib, works = SlowIB(), _works(6)
    total = collector._collect_all(ib, Conn, None, works, date(2026, 9, 28), date(2026, 9, 30), True, workers=3)
    assert total == 18 and len(stored) == 18
    assert ib.most > 1                                                         # in flight together
    assert len(Conn.opened) == 6 and sorted(map(id, Conn.closed)) == sorted(map(id, Conn.opened))
    for k in range(6):                                                         # each contract newest first
        assert [d for c, d in ib.asked if c == 500 + k] == ["2026-09-30", "2026-09-29", "2026-09-28"]
    assert {c for c, _, _ in stored} == set(Conn.opened)                      # stored on the workers' connections


def test_one_worker_is_the_serial_collection_on_the_given_connection(stored):
    ib, works, main = SlowIB(), _works(3), object()
    assert collector._collect_all(ib, Conn, main, works, date(2026, 9, 28), date(2026, 9, 30), True, workers=1) == 9
    assert ib.most == 1 and {c for c, _, _ in stored} == {main}


def test_a_failing_symbol_does_not_stop_the_others(stored, monkeypatch):
    real = collector._collect_work

    def collect(app, conn, work):
        if work.symbol == "S1":
            raise RuntimeError("S1 broke")
        return real(app, conn, work)

    monkeypatch.setattr(collector, "_collect_work", collect)
    ib, works = SlowIB(), _works(4)
    with pytest.raises(RuntimeError, match="S1 broke"):
        collector._collect_all(ib, Conn, None, works, date(2026, 9, 28), date(2026, 9, 30), True, workers=4)
    assert len(stored) == 9                                                    # the other three, whole


# ---------------------------------------------------------------------------
# The calendar and earnings reload beside the collection
# ---------------------------------------------------------------------------

def test_the_journal_uses_the_reload_made_beside_the_collection_and_redoes_a_failed_one(monkeypatch):
    import forecaster.journal as journal
    calls = []
    monkeypatch.setattr(collector, "refresh_events", lambda dsn: calls.append("reload"))
    monkeypatch.setattr(collector, "get_db_connection", lambda dsn: Conn())
    monkeypatch.setattr(journal, "catch_up", lambda conn, profile=None: calls.append(
        "catch_up" if profile is None else f"catch_up {profile}"))
    started = set()                                      # candidate profiles whose pool exists
    monkeypatch.setattr(journal, "pool_started", lambda conn, profile: profile in started)

    class Reload:
        def __init__(self, ok):
            self.ok = ok

        def finish(self):
            return self.ok

    assert collector.update_journal("dsn", Reload(True)) and calls == ["catch_up"]
    calls.clear()
    assert collector.update_journal("dsn", Reload(False)) and calls == ["reload", "catch_up"]   # redone in line
    calls.clear()
    assert collector.update_journal("dsn") and calls == ["reload", "catch_up"]                   # as before
    calls.clear()
    started.add("candidate_0915")                        # once its pool is started, Auto keeps it current too
    assert collector.update_journal("dsn", Reload(True)) and calls == ["catch_up", "catch_up candidate_0915"]


def test_the_reload_thread_reports_a_failure_instead_of_raising(monkeypatch):
    def broken(dsn):
        raise RuntimeError("calendar file unreadable")

    monkeypatch.setattr(collector, "refresh_events", broken)
    failed = collector.EventsRefresh("dsn")
    failed.start()
    assert failed.finish() is False and "unreadable" in str(failed.error)
    monkeypatch.setattr(collector, "refresh_events", lambda dsn: None)
    fine = collector.EventsRefresh("dsn")
    fine.start()
    assert fine.finish() is True
