# dashboard/components/forest.py
"""
A forest plot as HTML, for the Evaluation page: a row per comparison - its label and role, its 95 % interval as a
bar with the point on it, the value and the interval in words, and a reading - over one axis computed from the
data, zero a hairline, and under the axis what each side of zero means. A solid bar clears zero; an open bar
includes it. The primary comparison's row is tinted. The styles are dashboard/theme.py's .tp-forest.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

MINUS = "−"
_STEPS = (1.0, 2.0, 5.0, 10.0)
# Label, the bar, the value, the reading - and with a sessions column after the label.
_COLUMNS = "150px minmax(260px,1fr) 170px 120px"
_COLUMNS_N = "170px 70px minmax(260px,1fr) 200px 150px"


def signed(x: Optional[float], nd: int, unit: str = "") -> str:
    """``x`` with a typographic sign - '−0.14 %', '+0.0060' - and '-' for nothing."""
    if x is None:
        return "-"
    if x == 0:
        return f"{0:.{nd}f}{unit}"
    return (MINUS if x < 0 else "+") + f"{abs(x):.{nd}f}{unit}"


def scale(values: Iterable[Optional[float]], ticks: int = 6) -> Tuple[float, float, float]:
    """``(low, high, step)``: an axis over ``values`` and zero, its ends on a 1-2-5 step that gives at most about
    ``ticks`` intervals - few enough for the labels to stand apart."""
    vs = [v for v in values if v is not None and math.isfinite(v)] + [0.0]
    lo, hi = min(vs), max(vs)
    if hi <= lo:
        lo, hi = lo - 1.0, hi + 1.0
    raw = (hi - lo) / ticks
    mag = 10.0 ** math.floor(math.log10(raw))
    step = next(s * mag for s in _STEPS if s * mag >= raw - 1e-12)
    return math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step, step


def decimals(step: float) -> int:
    """The decimals a tick on ``step`` needs: 0.5 -> 1, 0.02 -> 2, 1 -> 0."""
    nd = 0
    while nd < 8 and abs(step * 10 ** nd - round(step * 10 ** nd)) > 1e-6:
        nd += 1
    return nd


def reading(lo: Optional[float], hi: Optional[float], below: str, above: str, between: str = "No difference shown",
            none: str = "No interval") -> str:
    """What an interval of 'other minus base' says: ``below`` when it lies wholly below zero, ``above`` wholly above,
    ``between`` when it includes zero, ``none`` without one."""
    if lo is None or hi is None:
        return none
    return below if hi < 0 else above if lo > 0 else between


def html(rows: Sequence[Dict[str, Any]], heads: Sequence[str], left: str, right: str, unit: str = "",
         with_n: bool = False, min_width: int = 720) -> str:
    """
    The plot. ``rows``: {label, role, n (with ``with_n``), point, lo, hi, value, ci, reading, primary}, the numbers
    on the axis's own scale (``unit`` is written after the ticks); ``heads``: the label's, (the sessions',) the
    value's and the reading's column titles; ``left`` and ``right``: what lies below and above zero.
    """
    lo, hi, step = scale(v for r in rows for v in (r.get("point"), r.get("lo"), r.get("hi")))
    nd = decimals(step)
    pos = lambda v: f"{100 * (v - lo) / (hi - lo):.2f}%"
    cols = _COLUMNS_N if with_n else _COLUMNS
    n_cell = lambda text: f"<span>{escape(str(text))}</span>" if with_n else ""

    out = [f'<div class="tp-forest" style="--cols:{cols};min-width:{min_width}px">',
           f'<div class="tp-frow head tp-small tp-muted"><span>{escape(heads[0])}</span>'
           + (n_cell(heads[1]) if with_n else "")
           + f"<span></span><span>{escape(heads[-2])}</span><span>{escape(heads[-1])}</span></div>"]
    for r in rows:
        weight = 700 if r.get("primary") else 400
        bar = [f'<span class="zero" style="left:{pos(0)}"></span>']
        if r.get("lo") is not None and r.get("hi") is not None:
            clear = r["hi"] < 0 or r["lo"] > 0
            bar.append(f'<span class="tp-ivl {"solid" if clear else "open"}" style="left:{pos(r["lo"])};'
                       f'width:{100 * (r["hi"] - r["lo"]) / (hi - lo):.2f}%"></span>')
        if r.get("point") is not None:
            bar.append(f'<span class="tp-pt" style="left:{pos(r["point"])}"></span>')
        said = f"{r['label']}: {r.get('value', '-')} {r.get('ci', '')}, {r.get('reading', '')}".strip()
        out.append(
            f'<div class="tp-frow{" primary" if r.get("primary") else ""}" title="{escape(said)}">'
            f'<span><span style="font-weight:{weight}">{escape(r["label"])}</span> '
            f'<span class="tp-small tp-muted">{escape(r.get("role") or "")}</span></span>'
            + (n_cell(r.get("n", "-")) if with_n else "")
            + f'<span class="tp-fbar" role="img" aria-label="{escape(said)}">{"".join(bar)}</span>'
            f'<span style="white-space:nowrap"><span style="font-weight:600">{escape(r.get("value", "-"))}</span> '
            f'<span class="tp-small tp-muted">{escape(r.get("ci", ""))}</span></span>'
            f'<span style="font-weight:{weight}">{escape(r.get("reading", ""))}</span></div>')
    blanks = "<span></span><span></span>" if with_n else "<span></span>"
    ticks = []
    for i in range(int(round((hi - lo) / step)) + 1):
        v = lo + i * step
        text = "0" if abs(v) < step / 1e6 else signed(v, nd, unit)
        ticks.append(f'<span style="left:{pos(v)}">{escape(text)}</span>')
    out.append(f'<div class="tp-frow foot tp-small tp-muted">{blanks}<span class="tp-faxis">{"".join(ticks)}</span>'
               "<span></span><span></span></div>")
    out.append(f'<div class="tp-frow foot tp-small">{blanks}<span class="tp-fsides"><span>{escape(left)}</span>'
               f"<span>{escape(right)}</span></span><span></span><span></span></div>")
    out.append("</div>")
    return "".join(out)
