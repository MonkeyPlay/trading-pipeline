# tests/test_live_stream.py
"""Real-time bar finalisation and roll warm-up planning (no IB, no database)."""

from datetime import date, datetime, timedelta, timezone

from collector.live_stream import LiveBarAssembler
from collector.rolls import segments, upcoming_roll
from config import RollRule

KEY = (101, "TRADES")
T0 = datetime(2026, 6, 10, 13, 27, tzinfo=timezone.utc)      # 09:27 ET


def bar(minute_offset, close, volume=10):
    start = T0 + timedelta(minutes=minute_offset)
    return {"timestamp_utc": start.strftime("%Y-%m-%d %H:%M:%S"), "open": 1.0, "high": close + 1,
            "low": 0.5, "close": close, "volume": volume, "wap": None, "bar_count": 3,
            "session_scope": "ETH", "trading_day": "2026-06-10"}


def at(minute_offset, seconds):
    return T0 + timedelta(minutes=minute_offset, seconds=seconds)


def test_minute_is_final_when_the_next_one_starts():
    a = LiveBarAssembler()
    assert a.update(KEY, bar(0, 100.0), at(0, 5)) == []
    assert a.update(KEY, bar(0, 101.0), at(0, 55)) == []           # still forming
    out = a.update(KEY, bar(1, 102.0), at(1, 2))
    assert len(out) == 1
    assert out[0]["close"] == 101.0 and out[0]["finalised_by"] == "next_bar"
    assert out[0]["received_at"] == at(1, 2) and out[0]["key"] == KEY


def test_timer_finalises_a_quiet_minute_once():
    a = LiveBarAssembler(grace=timedelta(seconds=5))
    a.update(KEY, bar(0, 100.0), at(0, 30))
    assert a.tick(at(1, 4)) == []                                    # within grace
    out = a.tick(at(1, 5))
    assert [b["finalised_by"] for b in out] == ["timer"]
    assert a.tick(at(1, 30)) == []
    # The next minute starting does not emit the 09:27 bar again.
    assert a.update(KEY, bar(2, 103.0), at(2, 1)) == []


def test_changes_after_finalisation_are_revisions():
    a = LiveBarAssembler(grace=timedelta(seconds=5))
    a.update(KEY, bar(0, 100.0), at(0, 30))
    a.tick(at(1, 6))
    late = a.update(KEY, bar(0, 100.5), at(1, 8))                    # same minute, new value
    assert [(b["finalised_by"], b["close"]) for b in late] == [("late_update", 100.5)]
    assert a.update(KEY, bar(0, 100.5), at(1, 9)) == []              # unchanged: nothing
    a.update(KEY, bar(1, 101.0), at(1, 10))
    older = a.update(KEY, bar(0, 100.75), at(1, 12))                 # behind the current minute
    assert [b["finalised_by"] for b in older] == ["late_update"]


def test_initial_fill_and_confirm():
    a = LiveBarAssembler()
    filled = [b for m in range(3) for b in a.update(KEY, bar(m, 100.0 + m), at(3, 0), initial=True)]
    assert [b["finalised_by"] for b in filled] == ["initial_fill", "initial_fill"]   # the last is forming
    # IB's historical value of the 09:29 minute differs from the streamed one -> a revision.
    out = a.confirm(KEY, bar(2, 102.25), at(3, 1))
    assert [(b["finalised_by"], b["close"]) for b in out] == [("confirm_fetch", 102.25)]
    assert a.tick(at(3, 30)) == []                                   # confirmed: timer stays quiet
    assert a.confirm(KEY, bar(2, 102.25), at(3, 2)) == []            # identical: nothing new


def test_zero_volume_future_minute_is_not_a_bar():
    a = LiveBarAssembler()
    a.require_volume.add(KEY)
    a.update(KEY, bar(0, 100.0, volume=0), at(0, 10))
    assert a.update(KEY, bar(1, 100.0, volume=5), at(1, 1)) == []
    index_key = (103, "TRADES")                                      # a cash index has no volume
    a.update(index_key, bar(0, 16.0, volume=0), at(0, 10))
    assert len(a.update(index_key, bar(1, 16.1, volume=0), at(1, 1))) == 1


# --- roll warm-up ------------------------------------------------------------------

SEP = {"contract_id": 1, "expiry": "20260918", "contract_month": "202609"}
DEC = {"contract_id": 2, "expiry": "20261218", "contract_month": "202612"}
RULE = RollRule("HMUZ", 8)


def test_segments_carry_seven_warmup_days():
    days = [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
    segs = segments({d: (SEP if d < date(2026, 9, 10) else DEC) for d in days}, warmup=7)
    assert [s.contract["contract_id"] for s in segs] == [1, 2]
    dec = segs[1]
    # Labor Day (09-07) is skipped: seven trading days before the 09-10 roll.
    assert dec.warmup_days == [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3),
                               date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]
    assert dec.reference_day == date(2026, 9, 9)
    assert len(segs[0].warmup_days) == 7


def test_next_contract_warmup_is_collected_ahead_of_the_roll():
    chain = [SEP, DEC]
    nxt = upcoming_roll(chain, date(2026, 9, 3), RULE, warmup=7)
    assert nxt.contract["contract_id"] == 2 and nxt.first_day == date(2026, 9, 10)
    assert nxt.warmup_days == [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    assert upcoming_roll(chain, date(2026, 8, 20), RULE, warmup=7) is None     # not due yet
    assert upcoming_roll([SEP], date(2026, 9, 3), RULE, warmup=7) is None      # next not listed yet


def test_ib_callbacks_route_stream_bars(monkeypatch):
    """IB's keep-up-to-date callbacks (epoch-second dates, formatDate=2) reach the assembler."""
    import collector.live_stream as ls
    from types import SimpleNamespace

    app = ls.LiveStreamApp(LiveBarAssembler())
    app.streams[7] = KEY
    app.filling.add(7)

    def ib_bar(minute_offset, close):
        start = T0 + timedelta(minutes=minute_offset)
        return SimpleNamespace(date=str(int(start.timestamp())), open=1.0, high=close + 1, low=0.5,
                               close=close, volume=12, average=close, barCount=4)

    app.historicalData(7, ib_bar(0, 100.0))          # initial fill
    app.historicalData(7, ib_bar(1, 101.0))
    app.historicalDataEnd(7, "", "")
    app.historicalDataUpdate(7, ib_bar(1, 101.5))   # the forming minute updates
    app.historicalDataUpdate(7, ib_bar(2, 102.0))   # ... and a new one starts
    app.historicalDataUpdate(99, ib_bar(2, 1.0))    # not a stream: ignored

    got = []
    while not app.finalised.empty():
        got.append(app.finalised.get())
    assert [(b["timestamp_utc"][11:16], b["close"], b["finalised_by"]) for b in got] == \
        [("13:27", 100.0, "initial_fill"), ("13:28", 101.5, "next_bar")]
    assert got[1]["trading_day"] == "2026-06-10" and got[1]["wap"] == 101.5
    assert 7 not in app.filling
