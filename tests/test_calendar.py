# tests/test_calendar.py
from datetime import date

import pytest

from features import calendar as cal


# --- calendar ---------------------------------------------------------------

def test_cutoff_is_0929_et_across_dst():
    assert cal.session("2026-01-12").cutoff_at.strftime("%H:%M") == "14:29"   # EST
    assert cal.session("2026-07-13").cutoff_at.strftime("%H:%M") == "13:29"   # EDT


def test_monday_overnight_starts_sunday_evening():
    s = cal.session("2026-03-09")          # the day after the DST switch
    assert s.overnight_start_at == cal.ny_instant(date(2026, 3, 8), cal.OVERNIGHT_START)
    assert (s.cutoff_at - s.overnight_start_at).total_seconds() / 60 == 929


def test_schedules():
    assert cal.session("2026-09-07").schedule == "closed"            # Labor Day
    assert cal.session("2026-09-12").schedule == "closed"            # Saturday
    early = cal.session("2025-11-28")
    assert early.schedule == "early_close"
    assert early.scheduled_close_at == cal.ny_instant(date(2025, 11, 28), cal.EARLY_CLOSE)
    assert cal.previous_session("2025-12-01").session_date == date(2025, 11, 28)
    assert cal.previous_session("2026-09-08").session_date == date(2026, 9, 4)


def test_outside_coverage_raises():
    with pytest.raises(cal.CalendarCoverageError):
        cal.session("2023-06-01")


def test_opex_week_moves_off_good_friday():
    assert cal.monthly_opex_date(2025, 4) == date(2025, 4, 17)
    assert cal.monthly_opex_week("2025-04-14")
    assert not cal.monthly_opex_week("2025-04-21")


def test_roll_transition_window_skips_holidays():
    # Sep 2026 expiry 09-18, roll 8 days earlier on Thu 09-10; Labor Day 09-07 is closed.
    flagged = [d for d in ("2026-09-04", "2026-09-08", "2026-09-10", "2026-09-14", "2026-09-15")
               if cal.roll_transition(d, "HMUZ", 8)]
    assert flagged == ["2026-09-08", "2026-09-10", "2026-09-14"]
