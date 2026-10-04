# dashboard/views/analogues.py
"""
The analogues of the Session Explorer (guideline stage 2D): the selected NQ
session's structural analogues - matcher nq_match_p1_v2 over the pre-open structure
annotations (matching/structural.py) - side by side with it: every rubric feature
of the session and of its up to five earlier analogues, each cell marked as a
match, a mismatch or not comparable, with the similarity and the comparable weight
of each analogue. The explorer's day drives it; only the journal symbol (NQ) has
analogues, and only for the days the journal holds a snapshot of.

The explorer charts one analogue beside the session - its regular hours on its own
contract and prices, never rebased - the most similar first; clicking an
analogue's date here charts that one. Its realised labels and the frequency table
stay hidden until "Show outcomes": raw counts over the analogues with a label, the
denominator, the smoothed baseline and the prior.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from dashboard.components.preopen import LEVEL_LABELS
from database import journal_store as store
from forecaster.preopen_display import p1_record
from matching.structural import features

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs whitespace-nowrap"
_WRAP = "px-2 py-1 text-xs whitespace-normal break-words"
_MATCH, _MISMATCH, _PARTIAL, _NONE = ("rgba(38,166,154,0.28)", "rgba(239,83,80,0.28)", "rgba(253,216,53,0.22)",
                                      "rgba(120,123,134,0.12)")
_FEATURE_LABEL = {f: f"vs {LEVEL_LABELS[f[len('price:'):]]}" if f.startswith("price:") else f
                  for f in pre.MATCH_WEIGHTS}
_CHOSEN = "bg-primary text-white"


def _cell_style(component: Optional[Dict[str, Any]]) -> str:
    if component is None or not component["comparable"]:
        return f"background:{_NONE};{_MUTED}"
    score = float(component["score"])
    return f"background:{_MATCH if score == 1 else _MISMATCH if score == 0 else _PARTIAL}"


class AnaloguesPanel:
    """
    Built inside the Session Explorer: ``show(day, symbol)`` follows its
    selection and returns the day's analogues; ``on_pick(snapshot_id)`` is
    called with the analogue whose date is clicked, and ``mark`` highlights the
    one charted. Snapshots are read per day, never all at once.
    """

    def __init__(self, conn, on_pick: Optional[Callable[[str], Any]] = None) -> None:
        self.conn = conn
        self.on_pick = on_pick
        self.version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
        self.snaps: Dict[str, Dict[str, Any]] = {}          # the snapshots read so far, by session date
        self.day: Optional[str] = None
        self.aset: Optional[Dict[str, Any]] = None
        self.reason: Optional[str] = None                   # why the day has no analogue to chart
        self.chosen: Optional[str] = None                   # the snapshot id of the analogue charted
        self.date_buttons: Dict[str, Any] = {}
        self.outcomes_shown = False

    def _snapshot(self, day: str) -> Optional[Dict[str, Any]]:
        if day not in self.snaps:
            found = store.list_snapshots(self.conn, day, day, self.version)
            if found:
                self.snaps[day] = found[0]
        return self.snaps.get(day)

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        """The panel's elements, in the caller's container: a note when the day has no analogues, else the body."""
        self.note = ui.label().classes("text-sm").style(_MUTED)
        with ui.column().classes("w-full gap-3") as self.body:
            ui.label(f"Earlier sessions most like the selected one by P1's rubric ({pre.MATCHER_VERSION}): price "
                     f"location against its own levels, structure, trends and moving averages, the final hour and "
                     f"event risk. The chart beside the session shows one of them; their realised labels and the "
                     f"outcome frequencies stay hidden until you show them.").classes("text-sm").style(_MUTED)
            with ui.row().classes("w-full items-center gap-4"):
                ui.switch("Show outcomes", value=False, on_change=self.toggle_outcomes)
                self.summary = ui.label().classes("text-sm").style(_MUTED)
            self.table = ui.column().classes("w-full gap-0 overflow-x-auto")
            with ui.expansion("P1 pre-open record (47 fields)", icon="list_alt", value=False).classes(
                    "w-full").style("background:#1c212e"):
                self.record = ui.column().classes("w-full gap-0")
            self.outcome_panel = ui.column().classes("w-full gap-1")

    # -- data -----------------------------------------------------------------

    def show(self, day: Optional[str], symbol: Optional[str]) -> List[Dict[str, Any]]:
        """
        Shows the analogues of ``day`` - when ``symbol`` is the journal symbol and
        the journal holds the day - and returns them, the most similar first;
        none, ``reason`` says why.
        """
        if not day:
            return []
        self.day, self.aset, self.reason = day, None, None
        snap = self._snapshot(day) if symbol == defs.SYMBOL else None
        self.note.set_visibility(snap is None)
        self.body.set_visibility(snap is not None)
        if snap is None:
            self.reason = (f"Analogues are kept for {defs.SYMBOL}, the journal symbol." if symbol != defs.SYMBOL
                           else f"No {self.version} snapshot of {day}: the journal holds the sessions it has "
                                f"caught up (python scripts/nq_journal.py catch-up).")
            self.note.text = self.reason
            return []
        self.aset = store.latest_analogue_set(self.conn, snap["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION,
                                              pre.RULES_PROTOCOL_VERSION)
        self.render()
        if self.aset is None:
            self.reason = f"No analogue set for {day} yet: python scripts/nq_journal.py match"
        elif not self.aset["members"]:
            self.reason = f"No analogue for {day}: {self.aset['pool_size']} earlier session(s) scored."
        return list(self.aset["members"]) if self.aset is not None else []

    def mark(self, snapshot_id: Optional[str]) -> None:
        """Highlights the date of the analogue charted beside the session."""
        self.chosen = snapshot_id
        for sid, button in self.date_buttons.items():
            button.classes(add=_CHOSEN) if sid == snapshot_id else button.classes(remove=_CHOSEN)

    def toggle_outcomes(self, event) -> None:
        self.outcomes_shown = bool(event.value)
        self.render()

    def _labels(self, snapshot_id: str, revision: Optional[int]) -> Optional[Dict[str, Any]]:
        if revision is not None:
            o = store.get_outcome(self.conn, snapshot_id, defs.LABEL_VERSION, revision)
        else:
            o = store.latest_outcome(self.conn, snapshot_id, defs.LABEL_VERSION)
        return None if o is None else o["labels"]

    # -- rendering --------------------------------------------------------------

    def render(self) -> None:
        self.table.clear()
        self.outcome_panel.clear()
        self.date_buttons = {}
        self._render_record()
        aset = self.aset
        if aset is None:
            self.summary.text = ""
            with self.table:
                ui.label(f"No analogue set for {self.day} yet: python scripts/nq_journal.py match").style(_MUTED)
            return
        members = aset["members"]
        excluded = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in aset["excluded"].items()) or "none"
        mean = aset["mean_similarity"]
        self.summary.text = (f"{len(members)} analogue(s) from {aset['pool_size']} earlier session(s); excluded: "
                             f"{excluded}; mean similarity {float(mean):.1f}%" if mean is not None
                             else f"no analogue: {aset['pool_size']} earlier session(s) scored; excluded: {excluded}")
        target_annotation = store.latest_annotation(self.conn, aset["target_snapshot_id"], pre.RULES_PROTOCOL_VERSION)
        target_values = features(target_annotation) if target_annotation else {}
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
                for feature, weight in pre.MATCH_WEIGHTS.items():
                    ui.label(f"{_FEATURE_LABEL[feature]} ({float(weight):g}%)").classes(_CELL).style(
                        f"background:#1c212e;{_MUTED}")
                    value = target_values.get(feature)
                    ui.label("—" if value is None else str(value)).classes(_CELL).style("background:#1c212e")
                    for m in members:
                        c = m["components"][feature]
                        shown = c["analogue"]
                        text = "—" if shown is None else str(shown)
                        if feature == "Chop Score" and c["comparable"]:
                            text += f" ({float(c['score']):.2f})"
                        ui.label(text).classes(_CELL).style(_cell_style(c))
                if self.outcomes_shown:
                    target_labels = self._labels(aset["target_snapshot_id"], None)
                    member_labels = [self._labels(m["snapshot_id"], m["outcome_revision"]) for m in members]
                    for t in pre.OUTCOME_TARGETS:
                        ui.label(defs.TARGETS[t]["realised_property"]).classes(_CELL).style(
                            "background:#262b38;color:#ffa726")
                        own = (target_labels or {}).get(t, {}).get("label")
                        ui.label(defs.display(t, own) if target_labels else "—").classes(_CELL).style(
                            "background:#262b38")
                        for labels in member_labels:
                            lab = (labels or {}).get(t, {}).get("label")
                            ui.label(defs.display(t, lab) if labels else "no outcome").classes(_CELL).style(
                                "background:#262b38")
            ui.label("Green: same value; red: different; yellow: partly similar (Chop Score); grey: not "
                     "comparable. Click an analogue's date to chart it beside the session.").classes(
                "text-xs mt-1").style(_MUTED)
        if self.outcomes_shown:
            self._render_frequencies(aset)
        self.mark(self.chosen)

    def _render_record(self) -> None:
        """P1's 47 fields of the selected session (forecaster/preopen_display.py): pre-open only, no outcome."""
        self.record.clear()
        snap = self._snapshot(self.day)
        annotation = store.latest_annotation(self.conn, snap["snapshot_id"], pre.RULES_PROTOCOL_VERSION)
        provenance, rows = p1_record(snap, annotation, self.aset)
        with self.record:
            ui.label(provenance).classes("text-xs mb-1").style(_MUTED)
            with ui.grid(columns="minmax(0,1fr) minmax(0,1.4fr) minmax(0,2fr)").classes("w-full gap-x-3 gap-y-0"):
                for i, (prop, value, basis) in enumerate(rows, 1):
                    ui.label(f"{i}. {prop}").classes("text-xs").style(_MUTED)
                    ui.label(value).classes("text-xs" + (" opacity-60" if value == "Unavailable" else ""))
                    ui.label(basis).classes("text-[11px]").style(_MUTED)

    def _date_button(self, member: Dict[str, Any]) -> Any:
        sid = member["snapshot_id"]
        return ui.button(f"#{member['rank']} {member['session_date']}",
                         on_click=lambda: self.on_pick and self.on_pick(sid)).props(
            "flat dense no-caps size=sm").classes("text-xs")

    def _render_frequencies(self, aset: Dict[str, Any]) -> None:
        summary = aset["outcome_summary"]
        with self.outcome_panel:
            ui.label(f"Analogue outcomes ({defs.LABEL_VERSION}): counts over the analogues with a label, the "
                     f"smoothed baseline (count + {pre.SMOOTHING_PSEUDO_COUNT} x prior) / (n + "
                     f"{pre.SMOOTHING_PSEUDO_COUNT}) and the prior from every earlier session. "
                     f"{aset['data_mode'].replace('_', ' ')}: outcomes computed after the fact.").classes(
                "text-xs").style(_MUTED)
            with ui.grid(columns="minmax(0,1.1fr) minmax(0,0.8fr) minmax(0,1.6fr) minmax(0,3fr)").classes(
                    "gap-px w-full").style("background:#2a2e39"):
                for head in ("Target", "Analogues with a label", "Counts", "Smoothed (prior)"):
                    ui.label(head).classes(_CELL).style(f"background:#1c212e;{_MUTED}")
                for t, s in summary["targets"].items():
                    ui.label(defs.TARGETS[t]["predicted_property"]).classes(_CELL).style("background:#1c212e")
                    ui.label(f"{s['eligible']} of {summary['analogues']} ({s['status'].replace('_', ' ')})").classes(
                        _CELL).style("background:#1c212e")
                    counts = ", ".join(f"{defs.display(t, c)} {n}" for c, n in s["counts"].items() if n) or "—"
                    ui.label(counts).classes(_WRAP).style("background:#1c212e")
                    smoothed = ", ".join(
                        f"{defs.display(t, c)} {float(v) * 100:.0f}% ({float(s['prior'][c]) * 100:.0f}%)"
                        for c, v in (s["smoothed"] or {}).items()) or "—"
                    ui.label(smoothed).classes(_WRAP).style("background:#1c212e")
