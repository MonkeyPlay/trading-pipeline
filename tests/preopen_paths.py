# tests/preopen_paths.py
"""Synthetic pre-open snapshots for the rule-based structure annotation tests."""

import math
from datetime import date, time, timedelta

from features import calendar as cal
from features.nq_evidence import aggregate, iso

DAY = "2026-06-12"                                     # a Friday
SESSION = cal.session(DAY)
ON_START = SESSION.overnight_start_at
CUTOFF = cal.ny_instant(date(2026, 6, 12), time(9, 29))
MINUTES = int((CUTOFF - ON_START).total_seconds() // 60)    # 929
ALL_SOURCES = ["bea", "bls", "census", "fed", "ism_rule", "sec_earnings"]


def minute_of(et: str) -> int:
    """Minutes from 18:00 ET to ``et`` (HH:MM) on the session morning."""
    h, m = map(int, et.split(":"))
    return int((cal.ny_instant(date(2026, 6, 12), time(h, m)) - ON_START).total_seconds() // 60)


def piecewise(points, wiggle=0.0, period=40):
    """A price path through (minute, price) points, plus a sine wiggle."""
    def price(m):
        for (m0, p0), (m1, p1) in zip(points, points[1:]):
            if m0 <= m <= m1:
                base = p0 + (p1 - p0) * (m - m0) / (m1 - m0)
                break
        else:
            base = points[-1][1]
        return round((base + wiggle * math.sin(2 * math.pi * m / period)) * 4) / 4
    return price


def snapshot(price, T=8, events=(), covered=ALL_SOURCES, coverage="1.0000", refs=None, extra_1m=(), prior=None,
             A=None):
    bars = []
    for m in range(MINUTES):
        o, c = price(m), price(m + 1)
        bars.append((ON_START + timedelta(minutes=m), o, max(o, c) + 0.25, min(o, c) - 0.25, c, 10))
    rows = lambda n: [[iso(s), o, h, low, c, v, k] for s, o, h, low, c, v, k in aggregate(bars, n, CUTOFF)]
    close = bars[-1][4]
    references = {"cutoff_price": {"value": str(close), "status": "valid"}}
    for name, value in (refs or {}).items():
        references[name] = {"value": str(value), "status": "valid"}
    payload = {
        "identity": {"session_date": DAY, "weekday": "Friday", "contract_id": 1, "local_symbol": "NQM6",
                     "active_contract_rule": "test"},
        "schedule": {"overnight_start_at": iso(ON_START), "scheduled_close_at": iso(SESSION.scheduled_close_at)},
        "cutoff": {"input_cutoff_at": iso(CUTOFF), "cutoff_et": "09:29", "data_mode": "historical_reconstruction",
                   "pit_availability_status": "unverified_historical"},
        "atr": {"daily": {"status": "valid", "value": "300.500000"},
                "two_minute": {"status": "valid", "value": "12.345678", "last_bucket_end": "2026-06-12T13:28:00Z"}},
        "references": references,
        "thresholds": {"T": T, "A": A},
        "bars": {"1m": [[iso(s), o, h, low, c, v] for s, o, h, low, c, v in bars] + list(extra_1m),
                 "2m": rows(2), "5m": rows(5), "15m": rows(15), "coverage": {"ratio": coverage}},
        "events": {"covered_sources": list(covered), "events": list(events)},
        "prior_sessions": {"contract_id": 1, "sessions": list(prior or []), "bars_digest": "-"},
    }
    return {"snapshot_id": "test", "session_date": DAY, "snapshot_version": "test_snapshot", "payload": payload}


def event(source, key, name, tier, et, day=DAY):
    at = cal.ny_instant(date.fromisoformat(day), time(*map(int, et.split(":"))))
    return {"source": source, "event_key": key, "name": name, "tier": tier, "scheduled_at": iso(at),
            "time_et": et, "before_cutoff": at < CUTOFF}


def prior_sessions(first_open, incomplete=False):
    """Five prior sessions with H5 = 120 and L5 = 90; the first one opens at ``first_open``."""
    rows = []
    for i, (day, high, low) in enumerate((("2026-06-05", 115, 95), ("2026-06-08", 120, 92), ("2026-06-09", 112, 90),
                                          ("2026-06-10", 110, 96), ("2026-06-11", 111, 94))):
        bad = incomplete and i == 2
        rows.append({"session_date": day, "schedule": "full", "minutes": 389 if bad else 390, "expected_minutes": 390,
                     "status": "incomplete" if bad else "valid", "open": None if bad else str(first_open if i == 0 else 100),
                     "high": None if bad else str(high), "low": None if bad else str(low),
                     "close": None if bad else "100"})
    return rows
