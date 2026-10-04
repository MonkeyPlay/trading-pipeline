# dashboard/views/review.py
"""
Review: a person checks the realised NQ-v2 labels of a review set's sessions
against the prompts P1 / P2 (guideline stage 1E). ``scripts/nq_journal.py
review-set`` chooses the sessions (forecaster/review_set.py).

For the selected session the chart shows the regular session (09:15-16:15, VWAP,
opening range) with the frozen references the labels used - previous RTH close /
high / low, ON high / low, the cutoff VWAP - and O +/- T, the opening threshold.
Beside it is P2's 40-field record (forecaster/outcome_display.py). Every field
counts as agreed unless marked disagree or unsure, with an optional note; Save
stores one verdict per field (journal.review_verdicts, append-only: a re-review
adds rows and the latest counts).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd
from nicegui import ui

from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import VWAP_COLOR, build_chart_spec
from dashboard.views.candles import opening_range, session_window, window_bars
from database import journal_store as store
from database.queries import get_day_bars
from features.calculations import calculate_vwap, enrich_candle_timezones
from forecaster.outcome_display import p2_record

_MUTED = "color:#787b86"
_VERDICTS = {"agree": "✓", "disagree": "✗", "unsure": "?"}
_LEVEL_COLOURS = {"prev_rth_high": "#29b6f6", "prev_rth_low": "#29b6f6", "vwap": VWAP_COLOR, "o_plus_t": "#ffa726",
                  "o_minus_t": "#ffa726"}


def _num(value) -> Optional[float]:
    return None if value is None else float(Decimal(str(value)))


def chart_levels(snapshot: Dict[str, Any], measurements: Dict[str, Any]):
    """(levels, extra_levels) for the chart: the snapshot's frozen references and O +/- T."""
    refs = snapshot["payload"]["references"]

    def ref(name):
        return _num(refs.get(name, {}).get("value"))

    levels = {"previous_rth_close": ref("prev_rth_close"), "overnight_high": ref("on_high"),
              "overnight_low": ref("on_low")}
    o, t = _num(measurements.get("O")), _num(measurements.get("T"))
    extra = [
        {"key": "prev_rth_high", "label": "Prev RTH High", "value": ref("prev_rth_high"), "dash": 2},
        {"key": "prev_rth_low", "label": "Prev RTH Low", "value": ref("prev_rth_low"), "dash": 2},
        {"key": "vwap", "label": "Cutoff VWAP", "value": _num(measurements.get("vwap")), "dash": 1},
        {"key": "o_plus_t", "label": "O + T", "value": None if o is None or t is None else o + t, "dash": 3},
        {"key": "o_minus_t", "label": "O - T", "value": None if o is None or t is None else o - t, "dash": 3},
    ]
    for e in extra:
        e["color"] = _LEVEL_COLOURS[e["key"]]
    return levels, extra


class ReviewPage:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.sets = store.review_sets(conn)
        self.set: Optional[Dict[str, Any]] = self.sets[0] if self.sets else None
        self.members: List[Dict[str, Any]] = []
        self.member: Optional[Dict[str, Any]] = None
        self.outcome: Optional[Dict[str, Any]] = None
        self.record: List[tuple] = []
        self.inputs: Dict[str, Any] = {}
        self.chart: Optional[LightweightChart] = None

    # -- data ---------------------------------------------------------------

    def _reviewed(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """{snapshot_id: {field: latest verdict}} of the current set."""
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for v in store.latest_verdicts(self.conn, self.set["review_set"]):
            out.setdefault(v["snapshot_id"], {})[v["field"]] = v
        return out

    def _member_options(self) -> Dict[str, str]:
        done = self._reviewed()
        return {m["snapshot_id"]: f"#{m['position']}  {m['session_date']}  {'✓ reviewed' if m['snapshot_id'] in done else ''}"
                for m in self.members}

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        with ui.column().classes("w-full p-4 gap-3"):
            ui.label("Label review").classes("text-2xl font-medium")
            if self.set is None:
                ui.label("No review set yet. Create one with: python scripts/nq_journal.py review-set "
                         "--name stage1_review_v1").style(_MUTED)
                return
            self.members = store.review_members(self.conn, self.set["review_set"])
            ui.label(f"Check each session's realised labels against P2. Every field counts as agreed unless you "
                     f"mark it ✗ (disagree) or ? (unsure); Save records the verdicts. Set "
                     f"{self.set['review_set']}: {len(self.members)} sessions, labels "
                     f"{self.set['label_version']}.").classes("text-sm").style(_MUTED)
            with ui.row().classes("w-full items-center gap-4"):
                self.member_select = ui.select(self._member_options(), label="Session",
                                               on_change=lambda e: self.show(e.value)).classes("w-72")
                self.progress = ui.label().classes("text-sm").style(_MUTED)
                self.reasons = ui.label().classes("text-xs").style(_MUTED)
            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=560)
                with ui.card().classes("w-[560px] shrink-0").style("background:#1c212e"):
                    self.record_panel = ui.column().classes("w-full gap-0")
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
        self.outcome = store.latest_outcome(self.conn, snapshot_id, self.set["label_version"])
        reasons = self.member["reasons"]
        self.reasons.text = ("chosen for: " + ", ".join(reasons[:8]) + (" ..." if len(reasons) > 8 else "")
                             if reasons else "chosen to spread the set over the year (every label class was "
                                             "already covered)")
        self._push_chart(snapshot)
        self._render_record(snapshot)

    def _push_chart(self, snapshot: Dict[str, Any]) -> None:
        day = snapshot["session_date"]
        rows = get_day_bars(self.conn, snapshot["contract_id"], day, interval="1m")
        if not rows or self.outcome is None:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []})
            return
        df = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        levels, extra = chart_levels(snapshot, self.outcome["measurements"])
        w = session_window(day)
        self.chart.apply(build_chart_spec(
            window_bars(calculate_vwap(df), day), levels=levels, extra_levels=extra,
            opening_range=opening_range(df, day), visible_range=(w["start"], w["open"] + pd.Timedelta(minutes=75))))

    def _render_record(self, snapshot: Dict[str, Any]) -> None:
        self.record_panel.clear()
        self.inputs = {}
        with self.record_panel:
            if self.outcome is None:
                ui.label(f"No {self.set['label_version']} outcome for this session.").style(_MUTED)
                return
            self.record = p2_record(snapshot, self.outcome, self.set["label_version"])
            previous = self._reviewed().get(self.member["snapshot_id"], {})
            with ui.grid(columns="minmax(0,1.3fr) minmax(0,1fr) 92px minmax(0,1fr)").classes(
                    "w-full items-center gap-x-2 gap-y-0"):
                for prop, value in self.record:
                    if prop == "Outcome Data Notes":
                        continue
                    old = previous.get(prop, {})
                    ui.label(prop).classes("text-xs").style(_MUTED)
                    ui.label(value or "—").classes("text-xs")
                    toggle = ui.toggle(_VERDICTS, value=old.get("verdict", "agree")).props("dense size=sm")
                    note = ui.input(placeholder="note", value=old.get("note") or "").props("dense").classes("text-xs")
                    self.inputs[prop] = (toggle, note, value)
            notes = dict(self.record)["Outcome Data Notes"]
            ui.label(notes).classes("text-xs mt-2").style(_MUTED)
            old_notes = previous.get("Outcome Data Notes", {})
            self.session_note = ui.textarea(label="Session note (optional)", value=old_notes.get("note") or "").props(
                "dense autogrow").classes("w-full text-xs")
            with ui.row().classes("items-center gap-3 mt-1"):
                ui.button("Save review", icon="save", on_click=self.save).props("color=primary")
                if previous:
                    ui.label(f"last saved {str(max(v['recorded_at'] for v in previous.values()))[:16]} UTC").classes(
                        "text-xs").style(_MUTED)

    def save(self) -> None:
        verdicts = [{"field": prop, "shown_value": shown, "verdict": toggle.value, "note": note.value.strip()}
                    for prop, (toggle, note, shown) in self.inputs.items()]
        verdicts.append({"field": "Outcome Data Notes", "shown_value": dict(self.record)["Outcome Data Notes"],
                         "verdict": "agree", "note": self.session_note.value.strip()})
        n = store.save_verdicts(self.conn, self.set["review_set"], self.member["snapshot_id"],
                                int(self.outcome["outcome_revision"]), verdicts)
        flagged = sum(1 for v in verdicts if v["verdict"] != "agree")
        ui.notify(f"Saved {n} verdicts ({flagged} flagged).", type="positive")
        current = self.member["snapshot_id"]
        self.member_select.set_options(self._member_options(), value=current)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        done = self._reviewed()
        self.progress.text = f"{len(done)} of {len(self.members)} reviewed"
        counts: Dict[str, Dict[str, int]] = {}
        flagged: List[str] = []
        for snapshot_id, fields in done.items():
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
                if field == "Outcome Data Notes":
                    continue
                ui.label(f"{field}: ✓ {c['agree']}  ✗ {c['disagree']}  ? {c['unsure']}").classes("text-xs")
            for line in flagged:
                ui.label(line).classes("text-xs").style("color:#ffa726")


def show_review_page(conn) -> None:
    ReviewPage(conn).build()
