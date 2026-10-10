# tests/test_stored_data.py
"""
The session bar's stored data (dashboard/components/stored_data.py): a day's status from the collector's judgement
of its ledger row, "not collected yet" before an instrument's first stored day, the gaps as runs merged across
instruments, the ones ticked by default, their wording and the collector command that fetches one again. Pure - no
database.
"""

from datetime import date, timedelta

from dashboard.components import stored_data as sd
from dashboard.jobs import collector_window_command

# Mon 3 Aug to Fri 14 Aug 2026, weekdays only.
DAYS = [date(2026, 8, 3) + timedelta(days=i) for i in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11)]


def _row(status, score):
    return {"status": status, "score": score}


def test_a_days_status_follows_the_ledger_judgement():
    assert sd.status(None) == sd.NONE                                  # a scheduled day without a row
    assert sd.status(_row("EMPTY", 0.0)) == sd.NONE                    # empty at the source
    assert sd.status(_row("COMPLETE", 1.0)) == sd.FULL
    assert [sd.status(_row("PARTIAL", s)) for s in (0.95, 0.9, 0.7, 0.5, 0.2, 0.0)] == [
        sd.MOST, sd.MOST, sd.HALF, sd.HALF, sd.LOW, sd.NONE]


def test_before_its_first_stored_day_an_instrument_was_not_collected_yet():
    best = {("NQ", DAYS[0]): _row("COMPLETE", 1.0), ("NQ", DAYS[1]): _row("COMPLETE", 1.0),
            ("RTY", DAYS[2]): _row("COMPLETE", 1.0)}
    st = sd.statuses(best, ["NQ", "RTY", "QQQ"], DAYS[:5])
    assert st["RTY"] == [sd.NOT_COLLECTED, sd.NOT_COLLECTED, sd.FULL, sd.NONE, sd.NONE]
    assert st["NQ"] == [sd.FULL, sd.FULL, sd.NONE, sd.NONE, sd.NONE]  # missing once collected
    assert st["QQQ"] == [sd.NOT_COLLECTED] * 5                         # never stored
    assert sd.worst([sd.NOT_COLLECTED, sd.FULL, sd.MOST]) == sd.MOST
    assert sd.worst([sd.NOT_COLLECTED, sd.NOT_COLLECTED]) == sd.NOT_COLLECTED


def test_gaps_are_runs_merged_across_instruments_newest_first():
    F, M, H, N, X = sd.FULL, sd.MOST, sd.HALF, sd.NONE, sd.NOT_COLLECTED
    st = {"NQ": [F, F, N, N, F, M, F, F, F, F],
          "ES": [F, F, N, N, F, F, F, F, F, F],
          "RTY": [X, X, F, F, F, F, F, H, F, F]}
    gaps = sd.gap_runs(st, DAYS)
    assert [(g["first"], g["last"], g["worst"], g["symbols"]) for g in gaps] == [
        (7, 7, H, ["RTY"]), (5, 5, M, ["NQ"]), (2, 3, N, ["NQ", "ES"])]
    assert gaps[-1]["days"] == 2 and gaps[-1]["start"] == date(2026, 8, 5) and gaps[-1]["end"] == date(2026, 8, 6)
    assert sd.describe(gaps[-1], 3) == "NQ, ES, Wed 5 to Thu 6 Aug 2026"
    assert sd.describe(gaps[-1], 2) == "All 2 instruments, Wed 5 to Thu 6 Aug 2026"
    assert sd.describe(gaps[0], 3) == "RTY, Wed 12 Aug 2026"
    # Ticked by default: under 90 % stored, or in the last days; a 95 % day long ago is left to the user.
    assert sd.default_ticked(gaps, recent_from=8) == {"7-7-3", "2-3-1"}


def test_instruments_are_grouped_by_role():
    assert sd.groups(["NQ", "ES", "VIX", "SMH", "QQQ", "IWM"]) == [
        ("Target futures", ["NQ", "ES"]), ("Intermarket context", ["VIX", "SMH"]),
        ("Research datasets", ["QQQ", "IWM"])]


def test_a_gap_is_collected_for_its_instruments_and_days_only():
    cmd = collector_window_command(["NQ", "ES"], date(2026, 8, 5), date(2026, 8, 6), journal=False)
    assert cmd[cmd.index("--symbol") + 1] == "NQ,ES"
    assert cmd[cmd.index("--start") + 1] == "2026-08-05" and cmd[cmd.index("--end") + 1] == "2026-08-06"
    assert "--no-trailing-refresh" in cmd and "--no-journal" in cmd and "--days" not in cmd
    assert "--no-journal" not in collector_window_command(["NQ"], date(2026, 8, 5), date(2026, 8, 5))
