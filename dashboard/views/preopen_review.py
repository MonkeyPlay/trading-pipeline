# dashboard/views/preopen_review.py
"""
Pre-open review: a person checks the pre-open structure annotation of a review
set's sessions against P1 - without the outcome (guideline 1E / 2A: pre-open
classifications are reviewed without future charts). ``scripts/nq_journal.py
annotation-review-set`` chooses the sessions.

For the selected session the chart shows its overnight to the cutoff only, on
2-minute bars with the frozen references and the three moving averages as the
annotation computed them. Beside it is every annotated field with its value and
the numbers behind it. Every field counts as agreed unless marked disagree or
unsure, with an optional note; Save stores one verdict per field
(journal.annotation_review_verdicts, append-only: a re-review adds rows and the
latest counts).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from nicegui import ui

from contracts import nq_preopen as pre
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.preopen import LEVEL_LABELS, preopen_spec
from database import journal_store as store

_MUTED = "color:#787b86"
_VERDICTS = {"agree": "✓", "disagree": "✗", "unsure": "?"}


def review_rows(annotation: Dict[str, Any]) -> List[tuple]:
    """``(field, shown value, basis)`` of an annotation, in P1's order, then the price locations."""
    rows = []
    for name in pre.FIELDS:
        f = annotation["fields"].get(name)
        if f is None:
            continue
        shown = str(f["value"]) if f["value"] is not None else f"Unavailable ({f['reason']})"
        rows.append((name, shown, f["basis"]))
    for level in pre.PRICE_LOCATION["levels"]:
        value = annotation["price_location"].get(level)
        rows.append((f"Price vs {LEVEL_LABELS[level]}", value or "Unavailable",
                     "the cutoff price against the level, At within 1 point"))
    return rows


class PreopenReviewPage:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.sets = store.annotation_review_sets(conn)
        self.set: Optional[Dict[str, Any]] = self.sets[0] if self.sets else None
        self.members: List[Dict[str, Any]] = []
        self.member: Optional[Dict[str, Any]] = None
        self.annotation: Optional[Dict[str, Any]] = None
        self.inputs: Dict[str, Any] = {}
        self.chart: Optional[LightweightChart] = None

    def _reviewed(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for v in store.latest_annotation_verdicts(self.conn, self.set["review_set"]):
            out.setdefault(v["snapshot_id"], {})[v["field"]] = v
        return out

    def _member_options(self) -> Dict[str, str]:
        done = self._reviewed()
        return {m["snapshot_id"]: f"#{m['position']}  {m['session_date']}  {'✓ reviewed' if m['snapshot_id'] in done else ''}"
                for m in self.members}

    def build(self) -> None:
        with ui.column().classes("w-full p-4 gap-3"):
            ui.label("Pre-open review").classes("text-2xl font-medium")
            if self.set is None:
                ui.label("No annotation review set yet. Create one with: python scripts/nq_journal.py "
                         "annotation-review-set --name preopen_review_v1").style(_MUTED)
                return
            self.members = store.annotation_review_members(self.conn, self.set["review_set"])
            ui.label(f"Check each session's pre-open structure annotation against P1, from the overnight chart "
                     f"alone - the session's outcome is not shown. Every field counts as agreed unless you mark it ✗ "
                     f"or ?; Save records the verdicts. Set {self.set['review_set']}: {len(self.members)} sessions, "
                     f"{self.set['protocol_version']}.").classes("text-sm").style(_MUTED)
            with ui.row().classes("w-full items-center gap-4"):
                self.member_select = ui.select(self._member_options(), label="Session",
                                               on_change=lambda e: self.show(e.value)).classes("w-72")
                self.progress = ui.label().classes("text-sm").style(_MUTED)
                self.reasons = ui.label().classes("text-xs").style(_MUTED)
            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=560)
                with ui.card().classes("w-[640px] shrink-0").style("background:#1c212e"):
                    self.panel = ui.column().classes("w-full gap-0")
            with ui.expansion("Verdicts so far, per field", icon="fact_check", value=False).classes("w-full").style(
                    "background:#1c212e"):
                self.summary = ui.column().classes("w-full gap-0")
        self._refresh_summary()
        if self.members:
            self.member_select.value = self.members[0]["snapshot_id"]

    def show(self, snapshot_id: Optional[str]) -> None:
        if not snapshot_id:
            return
        self.member = next(m for m in self.members if m["snapshot_id"] == snapshot_id)
        snapshot = store.get_snapshot(self.conn, snapshot_id)
        self.annotation = store.latest_annotation(self.conn, snapshot_id, self.set["protocol_version"])
        reasons = self.member["reasons"]
        self.reasons.text = ("chosen for: " + ", ".join(reasons[:8]) + (" ..." if len(reasons) > 8 else "")
                             if reasons else "chosen to spread the set over the year")
        self.chart.apply(preopen_spec(self.conn, snapshot, through_close=False))
        self._render_panel()

    def _render_panel(self) -> None:
        self.panel.clear()
        self.inputs = {}
        previous = self._reviewed().get(self.member["snapshot_id"], {})
        with self.panel:
            if self.annotation is None:
                ui.label("No annotation for this session.").style(_MUTED)
                return
            with ui.grid(columns="minmax(0,1fr) minmax(0,1.1fr) 92px minmax(0,0.9fr)").classes(
                    "w-full items-center gap-x-2 gap-y-1"):
                for field, shown, basis in review_rows(self.annotation):
                    old = previous.get(field, {})
                    ui.label(field).classes("text-xs").style(_MUTED)
                    with ui.column().classes("gap-0"):
                        ui.label(shown).classes("text-xs")
                        if basis:
                            ui.label(basis).classes("text-[10px] leading-tight").style(_MUTED)
                    toggle = ui.toggle(_VERDICTS, value=old.get("verdict", "agree")).props("dense size=sm")
                    note = ui.input(placeholder="note", value=old.get("note") or "").props("dense").classes("text-xs")
                    self.inputs[field] = (toggle, note, shown)
            with ui.row().classes("items-center gap-3 mt-2"):
                ui.button("Save review", icon="save", on_click=self.save).props("color=primary")
                if previous:
                    ui.label(f"last saved {str(max(v['recorded_at'] for v in previous.values()))[:16]} UTC").classes(
                        "text-xs").style(_MUTED)

    def save(self) -> None:
        verdicts = [{"field": field, "shown_value": shown, "verdict": toggle.value, "note": note.value.strip()}
                    for field, (toggle, note, shown) in self.inputs.items()]
        n = store.save_annotation_verdicts(self.conn, self.set["review_set"], self.member["snapshot_id"],
                                           self.annotation["annotation_id"], verdicts)
        flagged = sum(1 for v in verdicts if v["verdict"] != "agree")
        ui.notify(f"Saved {n} verdicts ({flagged} flagged).", type="positive")
        self.member_select.set_options(self._member_options(), value=self.member["snapshot_id"])
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        done = self._reviewed()
        self.progress.text = f"{len(done)} of {len(self.members)} reviewed"
        counts: Dict[str, Dict[str, int]] = {}
        flagged: List[str] = []
        for fields in done.values():
            for field, v in fields.items():
                counts.setdefault(field, {"agree": 0, "disagree": 0, "unsure": 0})[v["verdict"]] += 1
                if v["verdict"] != "agree":
                    flagged.append(f"{v['session_date']} · {field}: {v['verdict']} (shown {v['shown_value']})"
                                   + (f" - {v['note']}" if v["note"] else ""))
        self.summary.clear()
        with self.summary:
            if not counts:
                ui.label("No verdicts yet.").classes("text-sm").style(_MUTED)
                return
            for field, c in sorted(counts.items(), key=lambda kv: (-kv[1]["disagree"] - kv[1]["unsure"], kv[0])):
                ui.label(f"{field}: ✓ {c['agree']}  ✗ {c['disagree']}  ? {c['unsure']}").classes("text-xs")
            for line in flagged:
                ui.label(line).classes("text-xs").style("color:#ffa726")


def show_preopen_review_page(conn) -> None:
    PreopenReviewPage(conn).build()
