# dashboard/components/session_timeline.py
"""
The session timeline under the session bar: the selected session from its 18:00 ET Globex open to the close,
on a scale that gives the first 90 minutes most of the width.

It shows how far the stored bars reach (the ink bar) and, while the session is in progress, the wall clock
(the red line): the IB feed on this account is delayed, so the two differ, and the gap between them is hatched.
On it sit the times the targets resolve - the 09:29 cutoff, the 09:30 open, the first move at 09:35, the
15-minute direction and opening type at 09:45, the 30-minute bias at 10:00, the last RTH analogue window at
10:30, and the close. A time the stored bars have passed is observed (solid); one only the clock has passed is
waiting for its bars (red outline); the rest are ahead (grey outline).

``refresh`` reads the newest bar of the selected contract again; a timer calls it every 30 seconds while the
session is in progress.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pytz
from nicegui import ui

from features import calendar as cal
from forecaster.rth_analogues import utc

NY = pytz.timezone("America/New_York")

# (minute of the ET session day, share of the width): the scale between them is linear. The overnight and the
# premarket are compressed, the cutoff-to-open minute is widened, and the first 90 minutes take 62 % of the width.
_SCALE = ((-360, 0.0), (480, 8.0), (569, 20.0), (570, 22.0), (660, 84.0), (None, 97.0), (1020, 100.0))
_SEGMENTS = ((0.0, "Overnight"), (8.0, "Premarket"), (22.0, "The first 90 minutes"), (84.0, "To the close"))

# (minute, label, what resolves, row, right-aligned, (observed, waiting, ahead) wording). Row "low" hangs its
# label lower, so neighbours a few minutes apart do not collide. The close's minute comes from the calendar.
_GATES = (
    (569, "09:29", "Cutoff", "", True, ("Snapshot frozen", "Waiting for its bars", "Snapshot at the cutoff")),
    (570, "09:30", "Open", "", False, ("", "", "")),
    (575, "09:35", "First move", "low", False, ("Observed", "Window over, waiting for its bars", "Forecast")),
    (585, "09:45", "15-min direction, opening type", "", False,
     ("Observed", "Window over, waiting for its bars", "Forecast")),
    (600, "10:00", "30-min bias", "low", False, ("Observed", "Window over, waiting for its bars", "Forecast")),
    (630, "10:30", "Last RTH analogue window", "", False, ("Stored", "Waiting for its bars", "Ahead")),
    (None, None, "RTH close direction, session type", "low", True,
     ("Observed", "Window over, waiting for its bars", "Forecast")),
)


def _close_minute(day: date) -> int:
    """The RTH close as minutes after midnight ET: 16:00, or 13:00 on an early close."""
    try:
        close = cal.session(day).scheduled_close_at
    except Exception:                                     # outside the calendar's coverage: a full session
        close = None
    if close is None:
        return 960
    local = close.astimezone(NY)
    return local.hour * 60 + local.minute


def _position(minute: float, close: int) -> float:
    """A minute of the session day (negative: the evening before) as a share of the width, 0 to 100."""
    points = [(close if m is None else m, x) for m, x in _SCALE]
    if minute <= points[0][0]:
        return points[0][1]
    for (m0, x0), (m1, x1) in zip(points, points[1:]):
        if minute <= m1:
            return x0 + (x1 - x0) * (minute - m0) / (m1 - m0)
    return points[-1][1]


def _minute(day: date, instant: datetime) -> float:
    """``instant`` as minutes after midnight ET on ``day`` (negative before it)."""
    midnight = NY.localize(datetime.combine(day, datetime.min.time()))
    return (instant.astimezone(NY) - midnight).total_seconds() / 60


def _hhmm(minute: float) -> str:
    m = int(round(minute)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


class SessionTimeline:
    """Draws the selected session's timeline where ``build`` is called; ``refresh`` redraws it."""

    def __init__(self, conn, bar) -> None:
        self.conn = conn
        self.bar = bar
        self.html = None
        self.timer = None

    def build(self) -> None:
        with ui.element("div").classes("w-full px-6 pt-2").style("overflow-x:auto"):
            self.html = ui.html("").classes("w-full")
        self.timer = ui.timer(30, self.refresh)
        self.refresh()

    def refresh(self) -> None:
        if self.html is None or self.bar.date is None:
            return
        day = date.fromisoformat(self.bar.date)
        close = _close_minute(day)
        now = _minute(day, datetime.now(pytz.utc))
        end = self._newest_bar_end()
        through = None if end is None else _minute(day, end)
        live = -360 <= now <= 1020                          # the session is in progress
        self.timer.active = live
        self.html.set_content(self._render(close, now if live else None, through))

    def _newest_bar_end(self) -> Optional[datetime]:
        """The end of the selected contract's newest stored 1-minute bar of the day (the one still forming
        included), or None."""
        contract = self.bar.contract
        if contract is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT max(timestamp_utc) FROM bars WHERE contract_id = %s AND trading_day = %s "
                "AND interval = '1m' AND price_type = 'TRADES';", (int(contract["contract_id"]), self.bar.date),
            ).fetchone()
        except Exception:                                 # the timeline never breaks the page
            return None
        if row is None or row[0] is None:
            return None
        return utc(row[0]) + timedelta(minutes=1)

    def _render(self, close: int, now: Optional[float], through: Optional[float]) -> str:
        x = lambda minute: f"{_position(minute, close):.2f}%"   # noqa: E731
        parts: List[str] = ['<div class="tp-tl" role="group" aria-label="The session from the Globex open to the '
                            'close">']
        for left, text in _SEGMENTS:
            parts.append(f'<span class="seg" style="left:{left}%">{text}</span>')
        parts.append('<span class="track" aria-hidden="true"></span>')
        if through is not None:
            parts.append(f'<span class="bars" aria-hidden="true" style="width:{x(through)}"></span>')
        if now is not None:
            behind = None if through is None else now - through
            if behind is not None and behind >= 1:
                parts.append(f'<span class="wait" aria-hidden="true" style="left:{x(through)};'
                             f'width:calc({x(now)} - {x(through)})"></span>')
            local = datetime.now().astimezone()
            local_text = ""
            if local.utcoffset() != datetime.now(NY).utcoffset():     # the viewer's own clock, when it differs
                local_text = f'<span class="tp-muted">{local:%H:%M} local</span>'
            lag = ""
            if through is not None and behind is not None and behind >= 1:
                lag = f'<span class="lag">Newest bar {_hhmm(through - 1)}, {int(behind)} min behind</span>'
            side = " right" if _position(now, close) > 70 else ""
            parts.append(f'<span class="now" aria-hidden="true" style="left:{x(now)}"></span>')
            parts.append(f'<span class="nowlabel{side}" style="left:{x(now)}"><b>Now {_hhmm(now)} ET</b>'
                         f'{local_text}{lag}</span>')
        for minute, label, what, row, right, wording in _GATES:
            minute = close if minute is None else minute
            label = label or _hhmm(minute)
            if through is not None and through >= minute:
                state, text = "observed", wording[0]
            elif now is not None and now >= minute:
                state, text = "waiting", wording[1]
            else:
                state, text = "ahead", wording[2]
            parts.append(f'<span class="gate {state}" aria-hidden="true" style="left:{x(minute)}"></span>')
            parts.append(f'<span class="tick {row}" aria-hidden="true" style="left:{x(minute)}"></span>')
            align = " right" if right else ""
            state_html = f'<span class="state {state}">{text}</span>' if text else ""
            parts.append(f'<span class="label {row}{align}" style="left:{x(minute)}"><b>{label}</b>{what}'
                         f'{state_html}</span>')
        parts.append("</div>")
        return "".join(parts)
