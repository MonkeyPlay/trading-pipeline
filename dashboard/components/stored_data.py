# dashboard/components/stored_data.py
"""
Stored data: what the database holds, in the session bar and in a panel beside the page.

In the session bar one low element (``StoredData.build``): every trading day since the first one stored, coloured
by the instrument that fared worst that day - the history before the last 20 days a cell per week, the last 20 a
cell each - with the last 20 days' state and the count of older gaps. **Details** opens the panel: the gaps to
fill, a checklist newest first whose ticked gaps the collector fetches again (only those instruments and days),
then every instrument by group - target futures, intermarket context, research datasets - over every trading day
since it was first collected.

A day's status is the collector's own judgement of its ledger row (coverage_map.stored_days): complete, partly
stored (90 % or more, 50 % or more, under 50 %), or nothing stored - a scheduled day without a row, or empty at the
source. Before an instrument's first stored day it was not collected yet, never "missing". Today's session counts
from 17:00 ET, once it has ended.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from collector.coverage import expected_trading_days
from config import ASSET_SOURCES, Config
from dashboard import theme
from dashboard.components.coverage_map import stored_days
from features.session_windows import NY_TZ

RECENT = 20                       # trading days drawn a cell each; the history before them a cell per week
SHOWN_GAPS = 6                    # gaps listed before "Show all"

# A day's status, worst first among the stored ones.
NOT_COLLECTED, NONE, LOW, HALF, MOST, FULL = range(6)
COLOR = {NOT_COLLECTED: "#E4E8E6", NONE: theme.SIGNAL, LOW: "#8A4B00", HALF: "#C27F22", MOST: "#E3B04B",
         FULL: "#7D8A91"}
WHAT = {NOT_COLLECTED: "not collected yet", NONE: "nothing stored", LOW: "under 50 % stored",
        HALF: "50 % or more stored", MOST: "90 % or more stored", FULL: "complete"}
_KEY_ORDER = (FULL, MOST, HALF, LOW, NONE)


# ---------------------------------------------------------------------------
# The ledger, day by day (pure: tests/test_stored_data.py)
# ---------------------------------------------------------------------------

def status(row: Optional[Dict[str, Any]]) -> int:
    """One stored day's status from its judged ledger row (None: no row)."""
    if row is None or row["status"] == "EMPTY" or not row["score"]:
        return NONE
    if row["status"] == "COMPLETE":
        return FULL
    return MOST if row["score"] >= 0.9 else HALF if row["score"] >= 0.5 else LOW


def first_days(best: Dict[tuple, Dict[str, Any]], symbols: Sequence[str]) -> Dict[str, Optional[date]]:
    """Per symbol its first stored day (None: nothing stored)."""
    first: Dict[str, Optional[date]] = {s: None for s in symbols}
    for sym, day in best:
        if sym in first and (first[sym] is None or day < first[sym]):
            first[sym] = day
    return first


def statuses(best: Dict[tuple, Dict[str, Any]], symbols: Sequence[str], days: Sequence[date]) -> Dict[str, List[int]]:
    """Per symbol a status per day of ``days``: not collected before its first stored day."""
    first = first_days(best, symbols)
    return {s: [NOT_COLLECTED if first[s] is None or d < first[s] else status(best.get((s, d))) for d in days]
            for s in symbols}


def worst(values: Sequence[int]) -> int:
    """The worst status among the instruments collected that day (NOT_COLLECTED when none was)."""
    stored = [v for v in values if v != NOT_COLLECTED]
    return min(stored) if stored else NOT_COLLECTED


def gap_runs(st: Dict[str, List[int]], days: Sequence[date]) -> List[Dict[str, Any]]:
    """
    The gaps: per symbol each run of consecutive trading days not complete, merged where several symbols share the
    same days and worst status - ``{'start', 'end', 'days', 'worst', 'symbols', 'first', 'last'}`` (``first`` and
    ``last`` index ``days``), newest first.
    """
    runs: Dict[tuple, Dict[str, Any]] = {}
    n = len(days)
    for sym, values in st.items():
        i = 0
        while i < n:
            if NONE <= values[i] < FULL:
                j, bad = i, values[i]
                while j + 1 < n and NONE <= values[j + 1] < FULL:
                    j += 1
                    bad = min(bad, values[j])
                run = runs.setdefault((i, j, bad), {"first": i, "last": j, "start": days[i], "end": days[j],
                                                    "days": j - i + 1, "worst": bad, "symbols": []})
                run["symbols"].append(sym)
                i = j + 1
            else:
                i += 1
    return sorted(runs.values(), key=lambda g: (-g["last"], g["worst"]))


def groups(symbols: Sequence[str]) -> List[Tuple[str, List[str]]]:
    """The instruments by role: the target futures, the intermarket context an evidence snapshot reads
    (config.ASSET_SOURCES), and the research datasets collected for the studies alone."""
    sourced = {a.symbol for a in ASSET_SOURCES.values() if a.symbol}
    targets = [s for s in symbols if s in Config.SYMBOLS]
    context = [s for s in symbols if s not in Config.SYMBOLS and s in sourced]
    research = [s for s in symbols if s not in Config.SYMBOLS and s not in sourced]
    return [(name, members) for name, members in (("Target futures", targets), ("Intermarket context", context),
                                                   ("Research datasets", research)) if members]


def stored_share(best: Dict[tuple, Dict[str, Any]], symbol: str, days: Sequence[date],
                 values: Sequence[int]) -> Optional[float]:
    """The mean score of ``symbol``'s days since it was first collected (1 complete, the share stored, 0 none)."""
    scored = [(best.get((symbol, d)) or {}).get("score") or 0.0 for d, v in zip(days, values) if v != NOT_COLLECTED]
    return sum(scored) / len(scored) if scored else None


def default_ticked(gaps: Sequence[Dict[str, Any]], recent_from: int) -> set:
    """The gaps ticked when the panel opens: anything under 90 % stored, anything that hit five or more instruments,
    and anything in the last ``RECENT`` days."""
    return {_key(g) for g in gaps if g["worst"] <= HALF or len(g["symbols"]) >= 5 or g["last"] >= recent_from}


def _key(g: Dict[str, Any]) -> str:
    return f"{g['first']}-{g['last']}-{g['worst']}"


# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------

def _day(d: date, with_month: bool = True) -> str:
    return f"{d:%a} {d.day}" + (f" {d:%b %Y}" if with_month else "")


def describe(g: Dict[str, Any], collected: int) -> str:
    """'RTY, Mon 10 to Fri 14 Aug 2026' - 'All 9 instruments' when every instrument collected then had it."""
    who = f"All {collected} instruments" if len(g["symbols"]) == collected and collected > 1 else ", ".join(g["symbols"])
    a, b = g["start"], g["end"]
    when = _day(a) if a == b else f"{_day(a, a.month != b.month or a.year != b.year)} to {_day(b)}"
    return f"{who}, {when}"


# ---------------------------------------------------------------------------
# Drawing (SVG strings for ui.html)
# ---------------------------------------------------------------------------

def _strip_svg(values: Sequence[int], height: int, label: str) -> str:
    """A day-per-column strip: every trading day a 2.2-unit bar in its status colour, before the first collected
    day a hairline."""
    paths = {k: [] for k in COLOR}
    for i, v in enumerate(values):
        paths[v].append(f"M{3 * i} {height / 2 - 1:g}h3v2h-3Z" if v == NOT_COLLECTED else f"M{3 * i} 0h2.2v{height}h-2.2Z")
    body = "".join(f'<path d="{"".join(p)}" fill="{COLOR[k]}"/>' for k, p in paths.items() if p)
    return (f'<svg viewBox="0 0 {max(3, 3 * len(values))} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="{label}" style="display:block;width:100%;height:{height}px">{body}</svg>')


def _weeks_svg(weeks: Sequence[Tuple[date, int]], width: int, height: int) -> str:
    """The history a cell per week, each with its tooltip."""
    if not weeks:
        return ""
    pitch = width / len(weeks)
    rects = "".join(f'<rect x="{i * pitch:.2f}" y="0" width="{max(1.0, pitch - 1.2):.2f}" height="{height}" '
                    f'fill="{COLOR[v]}"><title>Week of {_day(monday)}: the worst day {WHAT[v]}</title></rect>'
                    for i, (monday, v) in enumerate(weeks))
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Every week before the last {RECENT} trading days, coloured by its worst day">{rects}</svg>')


class StoredData:
    """The session bar's stored-data element and its panel; ``refresh`` reads the ledger again (after a job)."""

    def __init__(self, conn, collect: Optional[Callable[[List[Dict[str, Any]]], Any]] = None) -> None:
        self.conn = conn
        self.collect = collect             # starts the collector for the ticked gaps (the header's Update data)
        self.symbols = Config.collect_symbols()
        self.ticked: Optional[set] = None  # gap keys ticked; None until changed: the defaults
        self.show_all = False
        self.gaps: List[Dict[str, Any]] = []

    # -- data -----------------------------------------------------------------

    def _read(self) -> None:
        now = datetime.now(NY_TZ)
        last = now.date() if now.hour >= 17 else now.date() - timedelta(days=1)    # today's session once ended
        best = stored_days(self.conn, self.symbols)
        firsts = [d for d in first_days(best, self.symbols).values() if d is not None]
        self.days: List[date] = expected_trading_days(min(firsts), last) if firsts else []
        self.best = best
        self.st = statuses(best, self.symbols, self.days)
        n = len(self.days)
        self.recent_from = max(0, n - RECENT)
        self.daily = [worst([self.st[s][i] for s in self.symbols]) for i in range(n)]
        self.gaps = gap_runs(self.st, self.days)
        self.collected = [sum(1 for s in self.symbols if self.st[s][i] != NOT_COLLECTED) for i in range(n)]
        if self.ticked is None:
            self.ticked = default_ticked(self.gaps, self.recent_from)
        else:                                  # keep the user's choice for the gaps that are still there
            self.ticked &= {_key(g) for g in self.gaps}

    # -- the session bar --------------------------------------------------------

    def build(self) -> None:
        from nicegui import ui

        with ui.column().classes("gap-1").style("width:560px;max-width:100%") as self.element:
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                self.headline = ui.html("").classes("text-xs grow min-w-0")
                self.details = ui.button("Details", on_click=self.open).props("outline no-caps dense").classes(
                    "px-3")
            self.strip = ui.html("").classes("w-full")
        with ui.dialog().props("position=right full-height") as self.dialog, ui.card().classes(
                "w-[40rem] max-w-full h-full gap-4 p-6").style("border-radius:10px 0 0 10px"):
            with ui.row().classes("w-full items-start no-wrap gap-4"):
                with ui.column().classes("grow gap-1"):
                    ui.label("Stored data").classes("tp-x text-xl font-bold")
                    self.summary = ui.label("").classes("text-xs").style(theme.MUTED)
                ui.button(icon="close", on_click=self.dialog.close).props('flat dense round aria-label="Close"')
            self.checklist = ui.column().classes("w-full gap-0")
            self.instruments = ui.column().classes("w-full gap-3")
        self.refresh()

    def refresh(self) -> None:
        self._read()
        self._render_bar()
        if self.dialog.value:
            self._render_panel()

    def open(self) -> None:
        self._render_panel()
        self.dialog.open()

    def _render_bar(self) -> None:
        n, days = len(self.days), self.days
        if not n:
            self.headline.set_content(f'<b style="color:{theme.INK}">Stored data</b> nothing stored yet')
            self.strip.set_content("")
            return
        first = min(d for d in first_days(self.best, self.symbols).values() if d is not None)
        recent = self.daily[self.recent_from:]
        short = sum(1 for v in recent if v < FULL)
        older = [g for g in self.gaps if g["last"] < self.recent_from]
        recent_text = (f"Last {len(recent)} days complete" if not short else f"Last {len(recent)} days: {short} short")
        self.headline.set_content(
            f'<span style="display:flex;gap:12px;align-items:baseline;white-space:nowrap;color:{theme.INK2}">'
            f'<b style="color:{theme.INK};font-size:12.5px">Stored data</b>'
            f'<span>{len(self.symbols)} instruments since {_day(first)}</span>'
            f'<span style="color:{theme.INK if not short else "#8A4B00"};font-weight:600">{recent_text}</span>'
            f'<span style="color:{theme.SIGNAL if older else theme.INK2};font-weight:{650 if older else 400}">'
            f'{len(older) or "No"} older gap{"" if len(older) == 1 else "s"}</span></span>')
        # The history before the last days: a cell per week, its worst day.
        weeks: Dict[date, List[int]] = {}
        for d, v in zip(days[:self.recent_from], self.daily[:self.recent_from]):
            weeks.setdefault(d - timedelta(days=d.weekday()), []).append(v)
        week_cells = sorted((monday, worst(values)) for monday, values in weeks.items())
        cells = "".join(
            f'<span title="{_day(d)}: {WHAT[v]}" style="height:14px;border-radius:2px;background:{COLOR[v]}"></span>'
            for d, v in zip(days[self.recent_from:], recent))
        last_week = week_cells[-1][0] if week_cells else None
        self.strip.set_content(
            '<div style="display:flex;gap:12px;align-items:flex-end">'
            + (f'<div style="flex:0 0 320px;display:flex;flex-direction:column;gap:3px">'
               f'{_weeks_svg(week_cells, 320, 14)}'
               f'<span style="display:flex;justify-content:space-between;font-size:10.5px;color:{theme.INK2}">'
               f'<span>By week, {week_cells[0][0]:%b %Y}</span><span>{last_week:%b %Y}</span></span></div>'
               if week_cells else "")
            + f'<div style="flex:1 1 0;display:flex;flex-direction:column;gap:3px">'
              f'<div style="display:grid;grid-template-columns:repeat({len(recent)},minmax(0,1fr));gap:2px">{cells}</div>'
              f'<span style="display:flex;justify-content:space-between;font-size:10.5px;color:{theme.INK2}">'
              f'<span>Last {len(recent)} days, {days[self.recent_from].day} {days[self.recent_from]:%b}</span>'
              f'<span>{_day(days[-1], False)} {days[-1]:%b}</span></span></div></div>')

    # -- the panel ----------------------------------------------------------------

    def _render_panel(self) -> None:
        from nicegui import ui

        n = len(self.days)
        total = [stored_share(self.best, s, self.days, self.st[s]) for s in self.symbols]
        mean = [v for v in total if v is not None]
        self.summary.set_text(
            (f"{100 * sum(mean) / len(mean):.2f} % stored over {n} trading days. " if mean else "")
            + (f"The newest, {_day(self.days[-1])}, is {WHAT[self.daily[-1]]}." if n else "Nothing stored yet."))
        self.checklist.clear()
        with self.checklist:
            with ui.row().classes("w-full items-center gap-3 pb-1").style(f"border-bottom:1px solid {theme.RULE}"):
                ui.label("Gaps to fill").classes("text-sm font-bold")
                ui.label(f"{len(self.gaps)} in all, newest first" if self.gaps else "None").classes("text-xs").style(
                    theme.MUTED)
                ui.space()
                if self.gaps:
                    ui.button("Select all", on_click=lambda: self._tick({_key(g) for g in self.gaps})).props(
                        "flat dense no-caps size=sm")
                    ui.button("Clear", on_click=lambda: self._tick(set())).props("flat dense no-caps size=sm")
            if not self.gaps:
                ui.label("Every instrument is complete on every trading day since it was first collected.").classes(
                    "text-sm py-2").style(theme.MUTED)
            shown = self.gaps if self.show_all else self.gaps[:SHOWN_GAPS]
            for g in shown:
                key = _key(g)
                with ui.row().classes("w-full items-center gap-3 no-wrap py-1").style(
                        "border-bottom:1px solid #E4E8E6"):
                    ui.checkbox(value=key in self.ticked, on_change=lambda e, k=key: self._tick_one(k, e.value)).props(
                        "dense")
                    ui.element("span").style(f"width:10px;height:10px;border-radius:2px;flex:none;"
                                             f"background:{COLOR[g['worst']]}")
                    ui.html(f'<b>{describe(g, self.collected[g["first"]])}</b> '
                            f'<span style="color:{theme.INK2}">{WHAT[g["worst"]]}</span>').classes("text-sm grow")
                    ui.label("1 day" if g["days"] == 1 else f"{g['days']} days").classes("text-xs").style(theme.MUTED)
            with ui.row().classes("w-full items-center gap-3 pt-2"):
                if len(self.gaps) > SHOWN_GAPS:
                    ui.button(f"Show the newest {SHOWN_GAPS}" if self.show_all else f"Show all {len(self.gaps)} gaps",
                              on_click=self._toggle_all).props("flat dense no-caps size=sm")
                ui.space()
                chosen = [g for g in self.gaps if _key(g) in self.ticked]
                instrument_days = sum(g["days"] * len(g["symbols"]) for g in chosen)
                ui.button(f"Collect {len(chosen)} gap{'' if len(chosen) == 1 else 's'} again: {instrument_days} "
                          f"instrument-day{'' if instrument_days == 1 else 's'}" if chosen else "Tick a gap to collect it",
                          on_click=self._collect).props("unelevated no-caps").set_enabled(bool(chosen and self.collect))
            ui.label("Runs the collector for the ticked instruments and days only; it fetches their missing bars from "
                     "IB, then brings the journal up to date.").classes("text-xs self-end").style(theme.MUTED)
        self.instruments.clear()
        with self.instruments:
            with ui.row().classes("w-full items-baseline justify-between"):
                ui.label("By instrument").classes("text-sm font-bold")
                ui.label(f"Every trading day since {_day(self.days[0])}" if n else "").classes("text-xs").style(
                    theme.MUTED)
            for name, members in groups(self.symbols):
                shares = [stored_share(self.best, s, self.days, self.st[s]) for s in members]
                known = [v for v in shares if v is not None]
                gap_count = sum(1 for g in self.gaps if set(g["symbols"]) & set(members))
                rows = "".join(self._instrument_row(s, v) for s, v in zip(members, shares))
                head = 100 * sum(known) / len(known) if known else None
                ui.html(
                    f'<div style="display:flex;flex-direction:column;gap:2px">'
                    f'<div class="tp-sd-row" style="border-bottom:1px solid {theme.RULE};padding-bottom:3px">'
                    f'<b style="font-size:13px">{name}</b><span style="font-size:12px;color:{theme.INK2}">'
                    f'{len(members)} instruments, {gap_count or "no"} gap{"" if gap_count == 1 else "s"}</span>'
                    f'<span style="text-align:right;font-size:12.5px;font-weight:650;color:{_share_colour(head)}">'
                    f'{"-" if head is None else f"{head:.1f} %"}</span></div>{rows}</div>').classes("w-full")

    def _instrument_row(self, symbol: str, share: Optional[float]) -> str:
        inst = Config.instrument(symbol)
        pct = None if share is None else 100 * share
        first = next((d for d, v in zip(self.days, self.st[symbol]) if v != NOT_COLLECTED), None)
        return (f'<div class="tp-sd-row"><span style="display:flex;gap:8px;align-items:baseline;white-space:nowrap;'
                f'overflow:hidden"><b style="font-size:12.5px;width:34px;flex:none">{symbol}</b>'
                f'<span style="font-size:11.5px;color:{theme.INK2};overflow:hidden;text-overflow:ellipsis">'
                f'{inst.name if inst else ""}</span></span>'
                + _strip_svg(self.st[symbol], 10, f"{symbol}" + (f" since {_day(first)}" if first else ""))
                + f'<span style="text-align:right;font-size:12.5px;color:{_share_colour(pct)}">'
                  f'{"-" if pct is None else f"{pct:.1f} %"}</span></div>')

    # -- actions --------------------------------------------------------------------

    def _tick(self, keys: set) -> None:
        self.ticked = set(keys)
        self._render_panel()

    def _tick_one(self, key: str, on: bool) -> None:
        (self.ticked.add if on else self.ticked.discard)(key)
        self._render_panel()

    def _toggle_all(self) -> None:
        self.show_all = not self.show_all
        self._render_panel()

    def _collect(self) -> None:
        chosen = [g for g in self.gaps if _key(g) in self.ticked]
        if chosen and self.collect:
            self.dialog.close()
            self.collect(chosen)


def _share_colour(pct: Optional[float]) -> str:
    if pct is None or pct >= 99.995:
        return theme.INK2
    return "#8A4B00" if pct >= 99 else theme.SIGNAL
