# dashboard/views/evaluation.py
"""
Evaluation (guideline revision 2, 3F and stage 4): a registered experiment, its
stored scorings and its frozen cases.

  manifest     what was fixed before any score: purpose (a historical range is
               development data), sessions, versions, arms, the official-run rule,
               the primary target and metric, the zero-probability policy
  scoring      one stored result (journal.experiment_results), chosen by its id: the
               primary paired difference with its bootstrap interval, every target
               per arm with its denominators and exclusions, and where the
               differences sit - by realised class, month and volatility
  session day  the session bar's day in the experiment: inside its sessions or not,
               and per arm its frozen case - the official run, a link to that run in
               the Session Explorer's forecast, and the outcome revision it is scored on
  cases        per session and arm the official run - each a link as above - and the
               outcome revision it is scored on

Nothing is computed here that is not stored: scoring is
``python scripts/nq_journal.py experiment-score``.
"""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import urlencode

from nicegui import ui

from contracts import nq_prompt_v2 as defs
from dashboard.components.session_bar import SessionBar
from database import journal_store as store

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs"


def _f(x, nd: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    return f"{x:.{nd}f}"


def _ci(interval) -> str:
    return "-" if not interval else f"[{_f(interval[0])}, {_f(interval[1])}]"


def _run_link(session_date: str, run_id: str) -> None:
    """A run's id, linking to it in the Session Explorer's forecast, on its day."""
    ui.link(run_id[:8], "/?" + urlencode({"day": session_date, "symbol": defs.SYMBOL, "run": run_id})).classes(_CELL)


def _grid(header: List[str], rows: List[List[Any]]) -> None:
    with ui.grid(columns=len(header)).classes("w-full gap-x-2 gap-y-0"):
        for h in header:
            ui.label(h).classes(_CELL).style(_MUTED)
        for row in rows:
            for cell in row:
                ui.label(str(cell)).classes(_CELL + " break-words")


class EvaluationPage:
    def __init__(self, conn, bar: SessionBar) -> None:
        self.conn = conn
        self.bar = bar
        self.experiments = {r["version"]: r for r in reversed(store.list_versions(conn, "experiment"))}
        self.name: Optional[str] = None
        self.cases: List[dict] = []

    def build(self) -> None:
        with ui.column().classes("w-full px-4 pb-4 gap-3"):
            ui.label("Evaluation").classes("text-2xl font-medium")
            if not self.experiments:
                ui.label("No registered experiment yet. Register one before looking at any score: python "
                         "scripts/nq_journal.py experiment-register --name hist_dev_v1 --start 2025-09-02 --end "
                         "2026-10-02, then experiment-score --name hist_dev_v1").style(_MUTED)
                return
            names = list(self.experiments)
            with ui.row().classes("w-full items-center gap-4"):
                ui.select(names, value=names[0], label="Experiment", on_change=lambda e: self.show(e.value)).classes(
                    "w-72")
                self.result_select = ui.select({}, label="Scoring", on_change=lambda e: self.show_result(e.value)
                                               ).classes("w-[28rem]")
            self.manifest = ui.column().classes("w-full gap-0")
            self.day_box = ui.column().classes("w-full gap-0")
            self.body = ui.column().classes("w-full gap-3")
        self.show(names[0])
        self.bar.on_change.append(self.on_selection)

    def show(self, name: Optional[str]) -> None:
        if not name:
            return
        self.name = name
        m = self.experiments[name]["definition"]
        self.manifest.clear()
        with self.manifest:
            ui.label(f"{name}: {m['purpose']} - {m['purpose_note']}").classes("text-sm").style(
                "color:#ffa726" if m["purpose"] == "development" else "")
            ui.label(f"sessions {m['sessions']['from']} to {m['sessions']['to']}, {m['profile']}, "
                     f"{m['mode'].replace('_', ' ')}; arms " + ", ".join(f"{k} {v['algorithm']}"
                                                                        for k, v in m["arms"].items())
                     + f"; official run: {m['official_run']['text']}").classes("text-xs").style(_MUTED)
            ui.label(f"primary {m['primary']['target']} - {m['primary']['metric']}; {m['zero_probability']}; "
                     f"{m['uncertainty']['method']}, block {m['uncertainty']['block_sessions']} sessions, "
                     f"{m['uncertainty']['resamples']} resamples").classes("text-xs").style(_MUTED)
            ui.label(f"labels {m['label_version']}, snapshots {m['snapshot_version']}, annotation "
                     f"{m['annotation_protocol']}, matcher {m['matcher_version']}; profitability "
                     f"{m['profitability']}").classes("text-xs").style(_MUTED)
        self.cases = store.experiment_cases(self.conn, name)
        self._render_day()
        self.results = {r["result_id"]: r for r in store.experiment_results(self.conn, name)}
        options = {rid: f"{str(r['computed_at'])[:16]} UTC · code {r['code_revision'][:10]}"
                   for rid, r in self.results.items()}
        self.result_select.set_options(options, value=next(iter(options), None))
        if not options:
            self.body.clear()
            with self.body:
                ui.label(f"Not scored yet: python scripts/nq_journal.py experiment-score --name {name}").style(_MUTED)
            self._render_cases()

    def show_result(self, result_id: Optional[str]) -> None:
        if not result_id:
            return
        r = self.results[result_id]["results"]
        m = self.experiments[self.name]["definition"]
        arms = list(m["arms"])
        self.body.clear()
        with self.body:
            ui.label("Coverage").classes("text-sm font-medium")
            _grid(["arm", "case", "no run", "no outcome"],
                  [[a, r["cases"][a].get("case", 0), r["cases"][a].get("no_run", 0), r["cases"][a].get("no_outcome", 0)]
                   for a in arms])
            ui.label(f"{r['sessions']} scheduled sessions; arms on the same analogue set in "
                     f"{r['pairs_on_the_same_analogue_set']}").classes("text-xs").style(_MUTED)
            primary = r["primary"]
            for key, p in primary["paired"].items():
                ll, br, acc = p["log_loss"], p["brier"], p["accuracy"]
                ui.label(f"Primary: {primary['target']}, {key} on {p['common']} common sessions (negative favours "
                         f"{key.split('-')[0]})").classes("text-sm font-medium")
                _grid(["metric", arms[0], key.split("-")[0], "difference", "interval", "n"], [
                    ["log loss (both finite)", _f(ll["base"]), _f(ll["other"]), _f(ll["diff"]), _ci(ll["interval"]),
                     ll["both_finite"]],
                    ["Brier sum", _f(br["base"]), _f(br["other"]), _f(br["diff"]), _ci(br["interval"]), p["common"]],
                    ["accuracy (both classed)", _f(acc["base"]), _f(acc["other"]), "-", "-", acc["both_classed"]]])
                ui.label(f"infinite log loss: {ll['infinite_base_only']} only in {arms[0]}, {ll['infinite_other_only']} "
                         f"only in {key.split('-')[0]}, {ll['infinite_both']} in both").classes("text-xs").style(_MUTED)
            ui.label("Every target").classes("text-sm font-medium")
            rows = []
            for target, entry in r["targets"].items():
                for arm in arms:
                    s = entry["arms"][arm]
                    rows.append([target, arm, s["scored"], _f(s["log_loss_mean_finite"]), s["log_loss_infinite"],
                                 _f(s["brier_mean"]), _f(s["accuracy"]), s["ambiguous"],
                                 ", ".join(f"{k} {v}" for k, v in sorted(s["no_label"].items())) or "-",
                                 ", ".join(f"{k} {v}" for k, v in sorted(s["no_distribution"].items())) or "-"])
            _grid(["target", "arm", "scored", "log loss", "infinite", "Brier", "accuracy", "ambiguous",
                   "no realised label", "no distribution"], rows)
            ui.label(f"Where the differences sit ({primary['target']})").classes("text-sm font-medium")
            _grid(["class", "n"] + [f"{a} mean p(realised)" for a in arms] + [f"{a} accuracy" for a in arms],
                  [[c["class"], c["n"]] + [_f(c[a]["mean_p_realised"]) for a in arms]
                   + [_f(c[a]["accuracy"]) for a in arms] for c in r["targets"][primary["target"]]["classes"]])
            for title, key in (("By month", "monthly"), ("By daily-ATR tercile", "volatility")):
                ui.label(title).classes("text-xs").style(_MUTED)
                _grid(["group", "common"] + [f"{a} Brier" for a in arms] + [f"{a} log loss" for a in arms],
                      [[g["group"], g["common"]] + [_f(g[a]["brier"]) for a in arms]
                       + [_f(g[a]["log_loss_finite"]) for a in arms] for g in r[key]])
            with ui.expansion("Reliability", value=False).classes("w-full"):
                for arm, classes in r["reliability"].items():
                    for cls, bins in classes.items():
                        ui.label(f"{arm}, {cls}: " + "; ".join(
                            f"{b['bin']} n={b['n']} p={_f(b['mean_p'], 2)} obs={_f(b['observed'], 2)}" for b in bins)
                        ).classes("text-xs")
        self._render_cases()

    def on_selection(self, what: str) -> None:
        """The bar's day changed, or the database was read again after a job: the day's cases."""
        if what in ("day", "data") and self.name:
            self.cases = store.experiment_cases(self.conn, self.name)
            self._render_day()

    def _render_day(self) -> None:
        """The bar's day in the experiment: inside its sessions or not, and per arm its frozen case."""
        self.day_box.clear()
        day = self.bar.date
        if not day:
            return
        sessions = self.experiments[self.name]["definition"]["sessions"]
        inside = sessions["from"] <= day <= sessions["to"]
        mine = [c for c in self.cases if c["session_date"] == day]
        with self.day_box:
            ui.label(f"Session day {day}: " + ("inside" if inside else "outside")
                     + f" this experiment's sessions ({sessions['from']} to {sessions['to']})").classes(
                "text-sm font-medium")
            if not mine:
                ui.label("No frozen case for this day." if self.cases else
                         "No frozen case yet (experiment-score freezes them).").classes("text-xs").style(_MUTED)
                return
            with ui.grid(columns=4).classes("w-full max-w-[48rem] gap-x-2 gap-y-0"):
                for h in ("arm", "run", "outcome revision", "status"):
                    ui.label(h).classes(_CELL).style(_MUTED)
                for c in mine:
                    self._case_cells(c)

    @staticmethod
    def _case_cells(c: dict) -> None:
        """A frozen case's arm, run (a link), outcome revision and status."""
        ui.label(c["arm"]).classes(_CELL)
        if c["run_id"]:
            _run_link(c["session_date"], c["run_id"])
        else:
            ui.label("-").classes(_CELL)
        ui.label(str(c["outcome_revision"] or "-")).classes(_CELL)
        ui.label(c["status"] if c["status"] == "case" else f"{c['status']}: {c['detail']}").classes(_CELL)

    def _render_cases(self) -> None:
        cases = self.cases
        with self.body:
            with ui.expansion(f"Frozen cases ({len(cases)})", value=False).classes("w-full"):
                if not cases:
                    ui.label("Not frozen yet (experiment-score freezes them).").style(_MUTED)
                    return
                with ui.grid(columns=5).classes("w-full gap-x-2 gap-y-0"):
                    for h in ("session", "arm", "run", "outcome revision", "status"):
                        ui.label(h).classes(_CELL).style(_MUTED)
                    for c in cases:
                        ui.label(c["session_date"]).classes(_CELL)
                        self._case_cells(c)


def show_evaluation_page(conn, bar: SessionBar) -> EvaluationPage:
    page = EvaluationPage(conn, bar)
    page.build()
    return page
