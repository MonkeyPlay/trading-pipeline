# tests/test_preview.py
"""Forecast now (forecaster/preview.py): which session a preview made at a given moment is for - no database."""

from datetime import datetime

import pytest

from features import calendar as cal
from forecaster.preview import PreviewUnavailable, preview_target


def _ny(day: str, hhmm: str) -> datetime:
    return cal.NY_TZ.localize(datetime.fromisoformat(f"{day}T{hhmm}"))


def _target(day, hhmm):
    session, as_of = preview_target(_ny(day, hhmm))
    return session.session_date.isoformat(), f"{as_of.astimezone(cal.NY_TZ):%a %H:%M}"


def test_a_preview_is_for_the_session_whose_pre_open_is_under_way():
    assert _target("2026-10-05", "08:12") == ("2026-10-05", "Mon 08:12")      # Monday's pre-open so far
    assert _target("2026-10-05", "09:30") == ("2026-10-05", "Mon 09:29")      # the cutoff has passed: all of it
    assert _target("2026-10-05", "18:01") == ("2026-10-06", "Mon 18:01")      # Tuesday's overnight has a bar
    assert _target("2026-10-05", "23:59") == ("2026-10-06", "Mon 23:59")
    assert _target("2026-10-11", "19:30") == ("2026-10-12", "Sun 19:30")      # Sunday evening: Monday's


@pytest.mark.parametrize("day, hhmm, says", [
    ("2026-10-05", "09:31", "today's pre-open is over"),                       # the official snapshot is due
    ("2026-10-05", "18:00", "possible from Mon 2026-10-05 18:01 ET"),         # no bar of it complete yet
    ("2026-10-10", "12:00", "possible from Sun 2026-10-11 18:01 ET, once Mon 2026-10-12's overnight"),
])
def test_between_the_official_snapshot_and_the_next_globex_open_there_is_nothing_to_preview(day, hhmm, says):
    with pytest.raises(PreviewUnavailable, match=says):
        preview_target(_ny(day, hhmm))
