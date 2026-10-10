# dashboard/views/evaluation.py
"""
Evaluation (guideline revision 2, 3F and stage 4), laid out as the redesign's Evaluation
page: a heading that says what every result here is, with links to its two sections, and
the two sections as sheets.

  Intermarket fan experiment   its question, where it stands, the holdout as a forest plot and
                               the forward record (dashboard/components/fan_experiment_panel.py)
  P1 experiments               a registered experiment and one of its stored scorings
                               (journal.experiment_results), chosen by its id:
    heading      the experiment, its definition and its purpose (a historical range is
                 development data: a candidate at most, never a result)
    decision     the primary paired difference, on the manifest's primary metric, read
                 against its bootstrap interval
    forest       every target's paired Brier difference with its interval, the primary
                 tinted, and what it reads (dashboard/components/forest.py)
    session day  the session bar's day in the experiment: inside its sessions or not, and
                 per arm its frozen case - the official run, a link to that run in the
                 Session Explorer's forecast, and the outcome revision it is scored on
    expansions   the manifest - purpose, sessions, versions, arms, the official-run rule,
                 the primary target and metric, the zero-probability policy -; coverage and
                 every target per arm with its denominators and exclusions; where the
                 differences sit - by realised class, month and volatility -; reliability;
                 and the frozen cases, each linked as above

Nothing is computed here that is not stored: scoring is
``python scripts/nq_journal.py experiment-score`` (``python scripts/fan.py`` for the fan).
"""

from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from nicegui import ui

from contracts import nq_prompt_v2 as defs
from dashboard import theme
from dashboard.components import fan_experiment_panel, forest
from dashboard.components.session_bar import SessionBar
from dashboard.views.forecast import TARGET_SHORT
from database import journal_store as store

_MUTED = theme.MUTED
_CELL = "px-2 py-1 text-xs"
# The targets in the order the session resolves them.
_ORDER = ("first_move_5m", "opening_type_15m", "direction_15m", "opening_bias_30m", "close_direction_rth",
          "session_type_rth", "first_level_tested")
_HEADER = (
    '<h1 class="tp-x" style="margin:0;font-size:30px;font-weight:720;letter-spacing:-0.02em;line-height:1.1">'
    'Evaluation</h1><p style="margin:8px 0 0;max-width:72ch">Every result here was fixed in advance: a registered '
    "definition first, then one scoring. Development numbers are candidates; only a holdout or the forward record is "
    'evidence.</p><nav aria-label="On this page" class="tp-small tp-onpage"><a href="#fan">Intermarket fan '
    'experiment</a><a href="#p1">P1 experiments</a></nav>')


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


def _short(target: str) -> str:
    return TARGET_SHORT.get(target, target)


def primary_metric(manifest: Dict[str, Any]) -> str:
    """The paired result's key of the manifest's primary metric: 'log_loss' or 'brier'."""
    return "log_loss" if "log loss" in manifest["primary"]["metric"] else "brier"


def decision(primary: Dict[str, Any], metric: str) -> List[str]:
    """Per paired comparison of the primary target, what its interval on ``metric`` establishes - and on what."""
    name = "log loss" if metric == "log_loss" else "Brier"
    out = []
    for key, p in primary["paired"].items():
        other, base = key.split("-")
        iv = (p.get(metric) or {}).get("interval")
        if iv is None:
            said = f"Not decided: no interval on {p.get('common', 0)} common sessions."
        elif iv[1] < 0:
            said = f"Arm {other} forecast better than arm {base}: the whole 95 % interval lies below zero."
        elif iv[0] > 0:
            said = f"Arm {base} forecast better than arm {other}: the whole 95 % interval lies above zero."
        else:
            said = f"No reliable difference between arm {other} and arm {base} was established."
        out.append(f"{said} Primary: {_short(primary['target'])}, {name}, arm {other} minus arm {base}.")
    return out


def target_rows(r: Dict[str, Any], key: str, primary_target: str) -> List[Dict[str, Any]]:
    """Every target's paired Brier difference for the comparison ``key`` ('B-A': B minus A) as forest rows, in the
    order the session resolves the targets."""
    other, base = key.split("-")
    order = {t: i for i, t in enumerate(_ORDER)}
    rows = []
    for target in sorted(r["targets"], key=lambda t: (order.get(t, len(order)), t)):
        p = (r["targets"][target].get("paired") or {}).get(key)
        if p is None:
            continue
        b = p["brier"]
        iv = b.get("interval")
        lo, hi = iv if iv else (None, None)
        primary = target == primary_target
        rows.append({"label": _short(target), "role": "primary" if primary else "", "n": p.get("common", 0),
                     "point": b.get("diff"), "lo": lo, "hi": hi, "value": forest.signed(b.get("diff"), 4),
                     "ci": f"[{forest.signed(lo, 4)}, {forest.signed(hi, 4)}]" if iv else "no interval",
                     "reading": forest.reading(lo, hi, f"{other} better", f"{base} better")
                     if b.get("diff") is not None else "Not scored", "primary": primary})
    return rows


class EvaluationPage:
    def __init__(self, conn, bar: SessionBar) -> None:
        self.conn = conn
        self.bar = bar
        self.experiments = {r["version"]: r for r in reversed(store.list_versions(conn, "experiment"))}
        self.name: Optional[str] = None
        self.cases: List[dict] = []
        self.results: Dict[str, Any] = {}

    def build(self) -> None:
        with ui.column().classes("w-full px-6 pt-6 pb-12 gap-5").style("max-width:1240px"):
            ui.html(_HEADER).classes("w-full")
            with ui.element("section").classes("tp-sec").props('id=fan aria-labelledby=fan-h'):
                ui.html('<h2 id="fan-h">Intermarket fan experiment</h2>').classes("w-full")
                try:
                    fan_experiment_panel.render(self.conn)
                except Exception as e:                    # the P1 evaluation below stays usable
                    ui.label(f"Could not read the fan experiment: {type(e).__name__}: {e}").style(_MUTED)
            with ui.element("section").classes("tp-sec").props('id=p1 aria-labelledby=p1-h'):
                ui.html('<h2 id="p1-h">P1 experiments</h2><p style="margin-top:6px;max-width:78ch">Each registered '
                        "before it was scored (guideline stage 4). Pick one to see its manifest, its scorings and its "
                        "frozen cases, each linked to its run in the Session Explorer.</p>").classes("w-full")
                if not self.experiments:
                    ui.label("No registered experiment yet. Register one before looking at any score: python "
                             "scripts/nq_journal.py experiment-register --name hist_dev_v1 --start 2025-09-02 --end "
                             "2026-10-02, then experiment-score --name hist_dev_v1").style(_MUTED)
                    return
                names = list(self.experiments)
                with ui.row().classes("w-full items-center gap-4"):
                    ui.select(names, value=names[0], label="Experiment", on_change=lambda e: self.show(e.value)
                              ).classes("w-72")
                    self.result_select = ui.select({}, label="Scoring", on_change=lambda e: self.show_result(e.value)
                                                   ).classes("w-[28rem]")
                self.head = ui.column().classes("w-full gap-3")
                self.day_box = ui.column().classes("w-full gap-0")
                self.body = ui.column().classes("w-full gap-0")
        self.show(names[0])
        self.bar.on_change.append(self.on_selection)

    def show(self, name: Optional[str]) -> None:
        if not name:
            return
        self.name = name
        self.cases = store.experiment_cases(self.conn, name)
        self._render_day()
        self.results = {r["result_id"]: r for r in store.experiment_results(self.conn, name)}
        options = {rid: f"{str(r['computed_at'])[:16]} UTC · code {r['code_revision'][:10]}"
                   for rid, r in self.results.items()}
        self.result_select.set_options(options, value=next(iter(options), None))
        if not options:
            self._render_head(None)
            self.body.clear()
            with self.body:
                self._render_manifest()
            self._render_cases()

    def _render_head(self, r: Optional[Dict[str, Any]]) -> None:
        """The experiment's heading; with a scoring, its decision and every target's forest."""
        rec = self.experiments[self.name]
        m = rec["definition"]
        purpose = ("development data: a candidate at most, never a result" if m["purpose"] == "development"
                   else m["purpose"])
        sessions = f"{r['sessions']} sessions" if r else f"sessions {m['sessions']['from']} to {m['sessions']['to']}"
        self.head.clear()
        with self.head:
            ui.html('<div class="tp-ruled" style="display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 16px">'
                    f'<h3>{escape(self.name)}</h3><span class="tp-small tp-muted">Definition '
                    f'{escape(rec.get("definition_hash", "")[:8])}, {escape(sessions)}, {escape(purpose)}</span>'
                    "</div>").classes("w-full")
            if r is None:
                ui.html('<p class="tp-well" style="max-width:80ch"><strong>Not scored yet.</strong> python '
                        f"scripts/nq_journal.py experiment-score --name {escape(self.name)}</p>").classes("w-full")
                return
            said = " ".join(decision(r["primary"], primary_metric(m)))
            ui.html(f'<p class="tp-well" style="max-width:80ch"><strong>Decision.</strong> {escape(said)}</p>'
                    ).classes("w-full")
            for key in r["primary"]["paired"]:
                other, base = key.split("-")
                plot = forest.html(target_rows(r, key, r["primary"]["target"]),
                                   ("Target", "Sessions", f"{other} minus {base}", "Reading"),
                                   f"{other} better", f"{base} better", with_n=True, min_width=820)
                ui.html(f'<div style="overflow-x:auto">{plot}</div>').classes("w-full")
            arms = ", ".join(f"{k} {v['algorithm']}" for k, v in m["arms"].items())
            ui.html('<p class="tp-small tp-muted" style="max-width:80ch">Mean multiclass Brier, lower is better, '
                    "paired per session with a 95 % block-bootstrap interval. Solid: the interval clears zero. Arms: "
                    f"{escape(arms)}.</p>").classes("w-full")

    def _render_manifest(self) -> None:
        m = self.experiments[self.name]["definition"]
        with ui.expansion("Manifest", value=False).classes("w-full tp-inner"):
            ui.label(f"{self.name}: {m['purpose']} - {m['purpose_note']}").classes("text-sm").style(
                "color:var(--tp-amber)" if m["purpose"] == "development" else "")
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

    def show_result(self, result_id: Optional[str]) -> None:
        if not result_id:
            return
        r = self.results[result_id]["results"]
        m = self.experiments[self.name]["definition"]
        arms = list(m["arms"])
        self._render_head(r)
        self.body.clear()
        with self.body:
            self._render_manifest()
            primary = r["primary"]
            with ui.expansion("Coverage, the primary in full and every target", value=False).classes("w-full tp-inner"):
                _grid(["arm", "case", "no run", "no outcome"],
                      [[a, r["cases"][a].get("case", 0), r["cases"][a].get("no_run", 0),
                        r["cases"][a].get("no_outcome", 0)] for a in arms])
                ui.label(f"{r['sessions']} scheduled sessions; arms on the same analogue set in "
                         f"{r['pairs_on_the_same_analogue_set']}").classes("text-xs").style(_MUTED)
                for key, p in primary["paired"].items():
                    ll, br, acc = p["log_loss"], p["brier"], p["accuracy"]
                    ui.label(f"Primary: {primary['target']}, {key} on {p['common']} common sessions (negative favours "
                             f"{key.split('-')[0]})").classes("text-sm font-medium mt-2")
                    _grid(["metric", arms[0], key.split("-")[0], "difference", "interval", "n"], [
                        ["log loss (both finite)", _f(ll["base"]), _f(ll["other"]), _f(ll["diff"]),
                         _ci(ll["interval"]), ll["both_finite"]],
                        ["Brier sum", _f(br["base"]), _f(br["other"]), _f(br["diff"]), _ci(br["interval"]),
                         p["common"]],
                        ["accuracy (both classed)", _f(acc["base"]), _f(acc["other"]), "-", "-", acc["both_classed"]]])
                    ui.label(f"infinite log loss: {ll['infinite_base_only']} only in {arms[0]}, "
                             f"{ll['infinite_other_only']} only in {key.split('-')[0]}, {ll['infinite_both']} in both"
                             ).classes("text-xs").style(_MUTED)
                ui.label("Every target").classes("text-sm font-medium mt-2")
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
            with ui.expansion(f"Where the differences sit ({_short(primary['target'])})", value=False).classes(
                    "w-full tp-inner"):
                _grid(["class", "n"] + [f"{a} mean p(realised)" for a in arms] + [f"{a} accuracy" for a in arms],
                      [[c["class"], c["n"]] + [_f(c[a]["mean_p_realised"]) for a in arms]
                       + [_f(c[a]["accuracy"]) for a in arms] for c in r["targets"][primary["target"]]["classes"]])
                for title, key in (("By month", "monthly"), ("By daily-ATR tercile", "volatility")):
                    ui.label(title).classes("text-xs mt-2").style(_MUTED)
                    _grid(["group", "common"] + [f"{a} Brier" for a in arms] + [f"{a} log loss" for a in arms],
                          [[g["group"], g["common"]] + [_f(g[a]["brier"]) for a in arms]
                           + [_f(g[a]["log_loss_finite"]) for a in arms] for g in r[key]])
            with ui.expansion("Reliability", value=False).classes("w-full tp-inner"):
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
            with ui.expansion(f"Frozen cases ({len(cases)})", value=False).classes("w-full tp-inner"):
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
