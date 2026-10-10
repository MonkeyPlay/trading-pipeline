# dashboard/components/paths.py
"""
Small path charts for the analogue tables: a session's 1-minute closes over a window, as the change from the
window's first minute in percent, on one scale with the target session's - the analogue's minutes in ink, the
target's dashed, and what followed in grey.

The RTH set (views/rth_analogues.py) draws the matched minutes from the 09:30 open, then the 15 that followed; the
pre-open set (views/analogues.py) draws the premarket, 08:00 to 09:30, and the opening after it only once its
outcomes are shown - an analogue's opening is its outcome.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from html import escape
from typing import List, Optional, Sequence

from contracts import nq_prompt_v2 as defs
from dashboard import theme
from features import calendar as cal
from forecaster.rth_analogues import utc

OPEN = time(9, 30)
PREMARKET = time(8, 0)
PREMARKET_MINUTES = 90          # 08:00 to the 09:30 open
OPENING_MINUTES = 30            # what the pre-open set's outcome covers: the first half hour
FOLLOWING_MINUTES = 15          # what an RTH analogue did after its matched minutes
TARGET = theme.ARM_COLOR["B"]   # the target session's path, dashed
WIDTH, HEIGHT = 160, 40         # the SVG's own units; it is drawn at the cell's width

Path = List[Optional[float]]


def closes(conn, day, start: time, minutes: int, symbol: str = defs.SYMBOL) -> Path:
    """The 1-minute closes of ``day`` on its active contract from ``start`` ET for ``minutes`` minutes, one per
    minute - None where no bar is stored."""
    begin = cal.ny_instant(date.fromisoformat(str(day)), start)
    rows = conn.execute(
        "SELECT b.timestamp_utc, b.close FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id "
        "AND a.trading_day = b.trading_day WHERE a.symbol = %s AND b.trading_day = %s AND b.interval = '1m' "
        "AND b.price_type = 'TRADES' AND b.timestamp_utc >= %s AND b.timestamp_utc < %s ORDER BY b.timestamp_utc;",
        (symbol, str(day), begin, begin + timedelta(minutes=minutes))).fetchall()
    out: Path = [None] * minutes
    for row in rows:
        i = int((utc(row[0]) - begin).total_seconds() // 60)
        if 0 <= i < minutes and row[1] is not None:
            out[i] = float(row[1])
    return out


def change(values: Sequence[Optional[float]], base: Optional[float] = None) -> Path:
    """``values`` as the change in percent from ``base`` - by default the first stored one."""
    if base is None:
        base = next((v for v in values if v is not None), None)
    if not base:
        return [None] * len(values)
    return [None if v is None else (v / base - 1) * 100 for v in values]


def sparkline(target: Sequence[Optional[float]] = (), matched: Sequence[Optional[float]] = (),
              following: Sequence[Optional[float]] = (), label: str = "") -> str:
    """SVG markup: the analogue's ``matched`` minutes in ink, then ``following`` in grey, and the ``target``'s
    minutes dashed - each in percent from its window's first minute (None: no bar), on one scale."""
    n = max(len(target), len(matched) + len(following))
    values = [v for v in list(target) + list(matched) + list(following) if v is not None]
    if n < 2 or not values:
        return f'<span class="text-xs" style="color:{theme.INK2}">no bars</span>'
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1.0

    def x(i: int) -> float:
        return 2 + i * (WIDTH - 4) / (n - 1)

    def y(v: float) -> float:
        return HEIGHT - 3 - (v - lo) / span * (HEIGHT - 6)

    def path(series: Sequence[Optional[float]], offset: int = 0) -> str:
        out, pen = [], False
        for i, v in enumerate(series):
            if v is None:
                pen = False
                continue
            out.append(f"{'L' if pen else 'M'}{x(i + offset):.1f} {y(v):.1f}")
            pen = True
        return "".join(out)

    line = 'fill="none" vector-effect="non-scaling-stroke" stroke-linejoin="round"'
    parts = [f'<svg class="tp-path" viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none" role="img" '
             f'aria-label="{escape(label)}">',
             f'<path d="M0 {y(0.0):.1f}H{WIDTH}" stroke="{theme.RULE}" stroke-width="1" {line}/>']
    if following:
        joined = [matched[-1] if matched else None] + list(following)
        parts.append(f'<path d="{path(joined, len(matched) - 1)}" stroke="{theme.INK2}" stroke-opacity="0.6" '
                     f'stroke-width="1.4" {line}/>')
    if target:
        parts.append(f'<path d="{path(target)}" stroke="{TARGET}" stroke-width="1.4" stroke-dasharray="3 2" {line}/>')
    if matched:
        parts.append(f'<path d="{path(matched)}" stroke="{theme.INK}" stroke-width="1.6" {line}/>')
    parts.append("</svg>")
    return "".join(parts)


KEY = ("Path: the analogue in ink, the target dashed blue, what followed grey - the change from the window's first "
       "minute, each chart on its own scale.")
