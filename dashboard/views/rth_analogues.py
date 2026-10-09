# dashboard/views/rth_analogues.py
"""
The RTH analogues of the Session Explorer (matcher nq_match_rth_v1,
forecaster/rth_analogues.py, docs/rth_analogues.md): the earlier NQ sessions whose
first n minutes from the 09:30 ET open most resemble the selected session's first n
- a stored set per window, separate from the saved pre-open set
(dashboard/views/analogues.py), which it never replaces.

Two views, switched explicitly:

  As issued       the record: of the sets issued live (by Auto or by hand, within 30
                  minutes of their cutoff), the newest stored by the time replayed - in
                  playback the candle played to, else the newest. A correction stored
                  later, or a backfill, never shows here
  Reconstructed   the latest calculation of the window replayed, live or not -
                  labelled as such; for a day nothing was issued live for, the only one

A day with live sets opens on As issued, any other on Reconstructed. A day without a
set of the current matcher shows a superseded version's reconstructions (v1's backfill),
labelled as such - for review, not the record. The window
follows the explorer (``Follow``) or any stored window is picked - the 15, 30 and 60
minute checkpoints (09:45, 10:00, 10:30 ET) are marked. A window is never read past
the minute replayed.

The header names the set - "RTH analogues — first 23 minutes — data through 09:53
ET" - and whether it was issued live, how long after its cutoff (the feed is
delayed), or reconstructed later; a window under ten minutes is provisional. The
table compares every feature of the session and of each analogue, coloured by the
feature's score. Similarity is agreement of the observed openings, never a
probability.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from contracts import nq_prompt_v2 as defs
from contracts import nq_rth as rth
from database import journal_store as store
from features import calendar as cal
from forecaster import rth_analogues as ra

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs whitespace-nowrap"
_GOOD, _BAD, _PARTIAL, _NONE = ("rgba(38,166,154,0.28)", "rgba(239,83,80,0.28)", "rgba(253,216,53,0.22)",
                                "rgba(120,123,134,0.12)")
_CHOSEN = "bg-primary text-white"
FOLLOW = "follow"
ISSUED, RECONSTRUCTED = "issued", "reconstructed"


def _value(feature: str, value: Optional[str]) -> str:
    """A stored feature value as shown: ATR units signed, the overnight-range fraction, volume as a ratio."""
    if value is None:
        return "—"
    x = float(value)
    unit = rth.UNITS[feature]
    if unit == "ratio":
        return f"×{math.exp(x):.2f}"
    if unit == "fraction":
        return f"{x:.2f}"
    return f"{x:+.3f}" if feature in ("net_move", "vs_vwap", "vs_prev_close", "gap") else f"{x:.3f}"


def _cell_style(component: Dict[str, Any]) -> str:
    if not component["comparable"]:
        return f"background:{_NONE};{_MUTED}"
    score = float(component["score"])
    return f"background:{_GOOD if score >= 0.75 else _BAD if score <= 0.25 else _PARTIAL}"


class RthAnaloguesPanel:
    """
    Built inside the Session Explorer: ``show(day, symbol, minute)`` reads the set a review at ``minute`` after the
    open sees (or the window picked) and returns its members, best first; ``on_pick(snapshot_id)`` is called with
    the analogue whose date is clicked; ``on_window`` when another window is picked.
    """

    def __init__(self, conn, on_pick: Optional[Callable[[str], Any]] = None,
                 on_window: Optional[Callable[[], Any]] = None) -> None:
        self.conn = conn
        self.on_pick, self.on_window = on_pick, on_window
        self.day: Optional[str] = None
        self.aset: Optional[Dict[str, Any]] = None
        self.reason: Optional[str] = None
        self.windows: List[Dict[str, Any]] = []
        self.version = rth.RTH_MATCHER_VERSION         # the matcher version shown (a superseded one for review)
        self.chosen: Optional[str] = None
        self.date_buttons: Dict[str, Any] = {}
        self.view_choice: Optional[str] = None          # the view picked; None: as issued when anything was
        self.view: str = RECONSTRUCTED                  # the view shown
        self._syncing = False

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        self.note = ui.label().classes("text-sm").style(_MUTED)
        with ui.column().classes("w-full gap-2") as self.body:
            self.title = ui.label().classes("text-sm font-medium")
            self.quality = ui.label().classes("text-sm").style("color:#ffa726")
            with ui.row().classes("w-full items-center gap-4"):
                self.view_toggle = ui.toggle({ISSUED: "As issued", RECONSTRUCTED: "Reconstructed"},
                                             value=RECONSTRUCTED, on_change=self._view_picked).props(
                    "dense no-caps").tooltip("As issued: what was issued live by the time replayed. Reconstructed: "
                                             "the latest calculation of that window, corrections and backfills "
                                             "included")
                self.window_select = ui.select({FOLLOW: "Follow"}, value=FOLLOW, label="Window",
                                               on_change=self._picked).props("dense options-dense").classes("w-72")
                self.summary = ui.label().classes("text-sm").style(_MUTED)
            ui.label(f"Earlier sessions whose opening most resembles this one's over the same minutes from the 09:30 "
                     f"ET open ({rth.RTH_MATCHER_VERSION}): the path from the open, its range, pullbacks and "
                     f"recoveries, where it stands against VWAP and the frozen pre-open levels, relative volume and "
                     f"the pre-open context - every earlier session scored afresh at each window. Prices are in each "
                     f"session's own daily ATR, frozen before its open. Similarity is how closely the observed "
                     f"openings agree, not a probability; the chart beside the session greys what an analogue did "
                     f"after the matched minutes - shown, never matched.").classes("text-sm").style(_MUTED)
            self.table = ui.column().classes("w-full gap-0 overflow-x-auto")
        self.body.set_visibility(False)

    # -- data -----------------------------------------------------------------

    @property
    def window(self) -> Optional[int]:
        """The window picked, or None to follow the explorer."""
        v = self.window_select.value
        return None if v in (None, FOLLOW) else int(v)

    def show(self, day: Optional[str], symbol: Optional[str], minute: int, at: Optional[datetime] = None,
             now: Optional[datetime] = None, live: bool = False) -> List[Dict[str, Any]]:
        """
        The set a review of ``day`` at ``minute`` after the open sees, in the view shown - as issued: what was
        issued live by ``at`` (the time replayed; None: everything); reconstructed: the latest calculation of the
        longest window not past ``minute`` - or of the window picked; and its members, best first. None
        (``reason`` says why) without such a set. ``live``: the session in progress, whose feed delay is shown
        against ``now``.
        """
        if day != self.day:
            self._set_window(FOLLOW)
            self.view_choice = None
        self.day, self.aset, self.reason = day, None, None
        if not day or symbol != defs.SYMBOL:
            self.reason = f"RTH analogues are kept for {defs.SYMBOL}, the journal symbol."
            return self._empty()
        self.version = rth.RTH_MATCHER_VERSION
        self.windows = store.rth_windows(self.conn, defs.SYMBOL, day, self.version)
        for older in () if self.windows else rth.SUPERSEDED:
            self.windows = store.rth_windows(self.conn, defs.SYMBOL, day, older)
            if self.windows:
                self.version = older
                break
        self._window_options()
        if not self.windows:
            self.reason = (f"No RTH analogue set of {day} is stored. The session in progress gets one a minute from "
                           f"its first completed RTH bar (Auto, Update data), any other day with python "
                           f"scripts/nq_journal.py rth-backfill --date {day}.")
            return self._empty()
        self.view = self.view_choice or (ISSUED if any(w["live"] for w in self.windows) else RECONSTRUCTED)
        self._set_view(self.view)
        if self.view == ISSUED:
            self.aset = store.rth_set_issued(self.conn, defs.SYMBOL, day, self.version,
                                             at=None if self.window is not None else at, minutes=self.window)
            if self.aset is None:
                when = (f"window {self.window}" if self.window is not None else
                        f"by {at.astimezone(cal.NY_TZ):%H:%M} ET" if at is not None else "for this session")
                self.reason = (f"As issued: nothing was issued live {when}. Reconstructed shows the later "
                               f"calculations.")
                return self._empty(keep_controls=True)
        else:
            upto = self.window if self.window is not None else minute
            self.aset = store.rth_set_at(self.conn, defs.SYMBOL, day, self.version, upto)
            if self.aset is None:
                first = self.windows[0]["elapsed_minutes"]
                self.reason = (f"No RTH set of {day} as of {upto} minute(s) after the open: the first stored window "
                               f"is {first} minute(s). The pre-open set stands until then.")
                return self._empty(keep_controls=True)
        self.note.set_visibility(False)
        self.body.set_visibility(True)
        self._header(now, live)
        self.render()
        if not self.aset["members"]:
            self.reason = f"No RTH analogue: {self.aset['pool_size']} earlier session(s) scored, none comparable."
        return [self._member(m) for m in self.aset["members"]]

    def _member(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """A member as the explorer reads it (the analogue select and the chart beside the session)."""
        return {**m, "elapsed_minutes": self.aset["elapsed_minutes"]}

    def _empty(self, keep_controls: bool = False) -> List[Dict[str, Any]]:
        """No set to show: the reason - with the view and window controls when the day has sets in another view or
        window."""
        self.note.text = self.reason or ""
        self.note.set_visibility(True)
        self.body.set_visibility(keep_controls)
        if keep_controls:
            self.title.text, self.summary.text = "", ""
            self.quality.set_visibility(False)
            self.table.clear()
        return []

    def _set_view(self, value: str) -> None:
        self._syncing = True
        try:
            self.view_toggle.value = value
        finally:
            self._syncing = False

    def _view_picked(self, event) -> None:
        if self._syncing or not event.value:
            return
        self.view_choice = event.value
        if self.on_window is not None:
            self.on_window()

    def _set_window(self, value) -> None:
        self._syncing = True
        try:
            self.window_select.value = value
        finally:
            self._syncing = False

    def _window_options(self) -> None:
        """Follow, then every stored window: its cutoff, length, checkpoint mark and how many sets it holds."""
        options: Dict[Any, str] = {FOLLOW: "Follow (newest, or the playback candle)"}
        try:
            open_at = cal.session(self.day).rth_open_at
        except cal.CalendarCoverageError:
            open_at = None
        for w in self.windows:
            n = int(w["elapsed_minutes"])
            at = f"{(open_at + n * ra.MINUTE).astimezone(cal.NY_TZ):%H:%M}" if open_at else "?"
            options[n] = (f"{at} · {n} min" + (" · checkpoint" if w["checkpoint"] else "")
                          + (" · issued live" if w["live"] else " · reconstructed only")
                          + (f" · {w['sets']} sets (revised input)" if w["sets"] > 1 else ""))
        keep = self.window_select.value if self.window_select.value in options else FOLLOW
        self._syncing = True
        try:
            self.window_select.set_options(options, value=keep)
        finally:
            self._syncing = False

    def _picked(self, event) -> None:
        if not self._syncing and self.on_window is not None:
            self.on_window()

    def _header(self, now: Optional[datetime], live: bool) -> None:
        aset = self.aset
        view = ("As issued: " if self.view == ISSUED else
                "Reconstructed (the latest calculation of this window): ")
        self.title.text = view + ra.describe(aset)
        notes = []
        if self.version != rth.RTH_MATCHER_VERSION:
            notes.append(f"Superseded matcher {self.version}: its reconstructions are shown for review only - "
                         f"{rth.RTH_MATCHER_VERSION} keeps the record.")
        if aset["quality"].get("provisional"):
            notes.append(f"Provisional: only {aset['elapsed_minutes']} minute(s) of the opening observed - early "
                         f"matches move a lot.")
        q = aset["quality"]
        if q.get("stopped") and q["stopped"] != "complete" and q.get("confirmed_minutes") == aset["elapsed_minutes"]:
            notes.append("When this set was made: " + ra.stop_text({"state": q["stopped"], "minute": q.get(
                "stopped_at")}) + ".")
        if live and now is not None and self.window is None:
            cutoff = ra.utc(aset["cutoff_at"])
            behind = (now - cutoff).total_seconds() / 60
            newest = ra.newest_bar_end(self.conn, self.day)
            if behind >= 2:
                notes.append(f"Now {now.astimezone(cal.NY_TZ):%H:%M} ET: these matches are {behind:.0f} min behind "
                             f"the clock" + (f" (newest bar stored ends {newest.astimezone(cal.NY_TZ):%H:%M} ET - "
                                             f"the feed is delayed)" if newest is not None else "") + ".")
        self.quality.text = " ".join(notes)
        self.quality.set_visibility(bool(notes))
        excluded = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in aset["excluded"].items()) or "none"
        mean = aset["mean_similarity"]
        self.summary.text = (f"{len(aset['members'])} analogue(s) from {aset['pool_size']} earlier session(s) scored "
                             f"afresh; excluded: {excluded}"
                             + (f"; mean similarity {float(mean):.1f}%" if mean is not None else ""))

    # -- rendering --------------------------------------------------------------

    def mark(self, snapshot_id: Optional[str]) -> None:
        self.chosen = snapshot_id
        for sid, button in self.date_buttons.items():
            button.classes(add=_CHOSEN) if sid == snapshot_id else button.classes(remove=_CHOSEN)

    def render(self) -> None:
        self.table.clear()
        self.date_buttons = {}
        aset = self.aset
        if aset is None:
            return
        members = aset["members"]
        with self.table:
            with ui.grid(columns=2 + len(members)).classes("gap-px").style("background:#2a2e39"):
                ui.label("").classes(_CELL).style("background:#1c212e")
                ui.label(f"target {self.day}").classes(_CELL + " font-medium").style("background:#1c212e")
                for m in members:
                    self.date_buttons[m["snapshot_id"]] = self._date_button(m)
                ui.label("similarity / coverage").classes(_CELL).style(f"background:#1c212e;{_MUTED}")
                ui.label("").classes(_CELL).style("background:#1c212e")
                for m in members:
                    ui.label(f"{float(m['similarity']):.1f}% / {float(m['comparable_weight']):.0f}%").classes(
                        _CELL).style("background:#1c212e")
                for group, names in rth.GROUPS.items():
                    ui.label(group).classes(_CELL + " font-medium").style("background:#262b38;color:#b2b5be")
                    for _ in range(1 + len(members)):
                        ui.label("").classes(_CELL).style("background:#262b38")
                    for f in names:
                        ui.label(f"{rth.LABELS[f]} ({float(rth.WEIGHTS[f]):g}%)").classes(_CELL).style(
                            f"background:#1c212e;{_MUTED}")
                        target = aset["target_features"].get(f)
                        ui.label("(the path)" if f == "path" and target is not None else _value(f, target)).classes(
                            _CELL).style("background:#1c212e")
                        for m in members:
                            c = m["components"][f]
                            if f == "path" and c["comparable"]:
                                text = f"Δ {float(c['difference']):.3f}"
                            else:
                                text = _value(f, c["analogue"])
                            if c["comparable"]:
                                text += f" ({float(c['score']):.2f})"
                            ui.label(text).classes(_CELL).style(_cell_style(c))
            ui.label("Prices in each session's own daily ATR (frozen before its open); path: the root mean square "
                     "gap between the two paths minute by minute; relative volume against the same minutes of the 20 "
                     "sessions before. In brackets the feature's score: green 0.75 or more, red 0.25 or less, yellow "
                     "between, grey not comparable. Click a date to chart that analogue beside the session.").classes(
                "text-xs mt-1").style(_MUTED)
        self.mark(self.chosen)

    def _date_button(self, member: Dict[str, Any]) -> Any:
        sid = member["snapshot_id"]
        return ui.button(f"#{member['rank']} {member['session_date']}",
                         on_click=lambda: self.on_pick and self.on_pick(sid)).props(
            "flat dense no-caps size=sm").classes("text-xs")
