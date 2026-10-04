# dashboard/views/analogues.py
"""
Analogues (guideline stage 2D): a session's structural analogues - matcher
nq_match_p1_v1 over the pre-open structure annotations (matching/structural.py) -
side by side with it: every rubric feature of the session and of its up to five
earlier analogues, each cell marked as a match, a mismatch or not comparable, with
the similarity and the comparable weight of each analogue.

Outcomes are hidden at first, so the page serves the outcome-blind check the
guideline asks for: can a person see why each analogue qualifies? "Show outcomes"
adds each analogue's realised labels and the frequency table - raw counts over the
analogues with a label, the denominator, the smoothed baseline and the prior.

Clicking a session's date charts that session's own pre-open (its own contract and
price basis, never rebased); with outcomes shown, through its close.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from nicegui import ui

from contracts import nq_preopen as pre
from contracts import nq_prompt_v2 as defs
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.preopen import LEVEL_LABELS, preopen_spec
from database import journal_store as store
from matching.structural import features

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs whitespace-nowrap"
_WRAP = "px-2 py-1 text-xs whitespace-normal break-words"
_MATCH, _MISMATCH, _PARTIAL, _NONE = ("rgba(38,166,154,0.28)", "rgba(239,83,80,0.28)", "rgba(253,216,53,0.22)",
                                      "rgba(120,123,134,0.12)")
_FEATURE_LABEL = {f: f"vs {LEVEL_LABELS[f[len('price:'):]]}" if f.startswith("price:") else f
                  for f in pre.MATCH_WEIGHTS}


def _cell_style(component: Optional[Dict[str, Any]]) -> str:
    if component is None or not component["comparable"]:
        return f"background:{_NONE};{_MUTED}"
    score = float(component["score"])
    return f"background:{_MATCH if score == 1 else _MISMATCH if score == 0 else _PARTIAL}"


class AnaloguesPage:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.version = defs.PROFILES[defs.DEFAULT_PROFILE].snapshot_version
        self.snaps = {str(s["session_date"]): s
                      for s in store.list_snapshots(conn, "2000-01-01", "2100-01-01", self.version)}
        self.days = sorted(self.snaps, reverse=True)
        self.day: Optional[str] = None
        self.chart_day: Optional[str] = None
        self.aset: Optional[Dict[str, Any]] = None
        self.outcomes_shown = False
        self.chart: Optional[LightweightChart] = None

    # -- layout ---------------------------------------------------------------

    def build(self) -> None:
        with ui.column().classes("w-full p-4 gap-3"):
            ui.label("Analogues").classes("text-2xl font-medium")
            if not self.days:
                ui.label(f"No {self.version} snapshots yet. Run the collector, or: python scripts/nq_journal.py "
                         f"catch-up").style(_MUTED)
                return
            ui.label(f"Earlier sessions most like the selected one by P1's rubric ({pre.MATCHER_VERSION}): price "
                     f"location against its own levels, structure, trends and moving averages, the final hour and "
                     f"event risk. Outcomes stay hidden until you show them, so you can judge first why each "
                     f"analogue qualifies.").classes("text-sm").style(_MUTED)
            with ui.row().classes("w-full items-center gap-4"):
                self.day_select = ui.select(self.days, value=self.days[0], label="Session", with_input=True,
                                            on_change=lambda e: self.show(e.value)).classes("w-52")
                ui.switch("Show outcomes", value=False, on_change=self.toggle_outcomes)
                self.summary = ui.label().classes("text-sm").style(_MUTED)
            self.table = ui.column().classes("w-full gap-0 overflow-x-auto")
            self.outcome_panel = ui.column().classes("w-full gap-1")
            self.chart_title = ui.label().classes("text-sm")
            self.chart = LightweightChart(height=480)
        self.show(self.days[0])

    # -- data -----------------------------------------------------------------

    def show(self, day: Optional[str]) -> None:
        if not day:
            return
        self.day, self.chart_day = day, day
        snap = self.snaps[day]
        self.aset = store.latest_analogue_set(self.conn, snap["snapshot_id"], pre.MATCHER_VERSION, defs.LABEL_VERSION)
        self.render()

    def toggle_outcomes(self, event) -> None:
        self.outcomes_shown = bool(event.value)
        self.render()

    def select_chart(self, day: str) -> None:
        self.chart_day = day
        self._push_chart()

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
        aset = self.aset
        if aset is None:
            self.summary.text = ""
            with self.table:
                ui.label(f"No analogue set for {self.day} yet: python scripts/nq_journal.py match").style(_MUTED)
            self._push_chart()
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
                self._date_button(self.day, "target")
                for m in members:
                    self._date_button(m["session_date"], f"#{m['rank']}")
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
                     "comparable. Click a date to chart that session.").classes("text-xs mt-1").style(_MUTED)
        if self.outcomes_shown:
            self._render_frequencies(aset)
        self._push_chart()

    def _date_button(self, day: str, caption: str) -> None:
        ui.button(f"{caption} {day}", on_click=lambda d=day: self.select_chart(d)).props(
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

    def _push_chart(self) -> None:
        if self.chart is None or self.chart_day is None:
            return
        snap = self.snaps.get(self.chart_day)
        if snap is None:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []})
            return
        self.chart_title.text = (f"{self.chart_day} ({snap['payload']['identity'].get('local_symbol') or ''}): "
                                 + ("overnight and the session" if self.outcomes_shown else
                                    "overnight to the cutoff, 2-minute bars"))
        self.chart.apply(preopen_spec(self.conn, snap, through_close=self.outcomes_shown))


def show_analogues_page(conn) -> None:
    AnaloguesPage(conn).build()
