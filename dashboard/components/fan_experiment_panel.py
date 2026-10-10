# dashboard/components/fan_experiment_panel.py
"""
The intermarket fan experiment on the Evaluation page (docs/fan_experiment.md, chunk 9) -
what is stored, nothing computed here but its presentation:

  the question       the manifest's, the primary target and horizon, the frozen model and the
                     horizons it draws in the Session Explorer (the baseline draws the rest)
  where it stands    a rail: registered, development checks, frozen, the holdout scored once,
                     the forward record
  the holdout        its one scoring as a forest plot: per horizon model minus the baseline as a
                     share of the baseline's CRPS with the 95 % interval, the primary row tinted,
                     and beside it what else the stored result shows - where in the day the gain
                     sits, how concentrated it is, how the 90 % bands covered
                     (journal.experiment_results)
  the forward record under the current rules: the marks the calendar expected and what became of
                     them, the issues by class (live, delayed origin - the delayed-feed evaluation
                     -, late, expired) and how long an issue took against the live deadline, the
                     feed's part hatched (forecaster/fan_forward.py); earlier rules in a line
  the numbers        the holdout per horizon, per rule version the issues by horizon and class
                     and the timing by trigger, and the latest issues - in an expansion
"""

from __future__ import annotations

import json
from datetime import date
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nicegui import ui

from dashboard import theme
from dashboard.components import forest
from database import journal_store as store
from forecaster import fan_experiment as fx
from forecaster import fan_forward as fwd
from forecaster import fan_harness as fh
from forecaster import fan_live
from forecaster.fan_scoring import PHASES

_MUTED = theme.MUTED
_CELL = "px-2 py-1 text-xs"
_VERDICT = {"better": theme.INK, "worse": theme.SIGNAL}
_CLASS = {"live": "Live", "delayed_origin": "Delayed origin", "late": "Late", "expired": "Expired",
          "on_time": "v1 'on time'", "delayed": "v1 delayed", "legacy": "No rules"}
_MAIN_CLASSES = ("live", "delayed_origin", "late", "expired")      # shown even when none was issued
_TRIGGER = {"mark": "mark trigger", "catchup": "2-minute catch-up", "auto": "Auto mode", "manual": "by hand",
            "unrecorded": "trigger not recorded"}
_PHASE = {"overnight": "overnight", "pre_open": "before the open", "opening_hour": "in the opening hour",
          "midday": "at midday", "afternoon": "in the afternoon", "after_close": "after the close"}
_KEYS = [f"h{h}" for h in fh.REPORT_HORIZONS] + [f"pre_open_{m}" for m in fh.PRE_OPEN_MINUTES]
_NOTE_HORIZONS = [h for h in fh.REPORT_HORIZONS if 5 <= h <= 60]     # where the phases are read
_BAND_HORIZONS = [h for h in fh.REPORT_HORIZONS if h <= 60]          # where the bands' coverage is read


def _grid(header: List[str], rows: List[List[Any]], colors: Optional[List[Optional[str]]] = None) -> None:
    with ui.grid(columns=len(header)).classes("w-full gap-x-2 gap-y-0"):
        for h in header:
            ui.label(h).classes(_CELL).style(_MUTED)
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                style = f"color:{colors[i]}" if colors and colors[i] and j == len(row) - 1 else ""
                ui.label(str(cell)).classes(_CELL + " break-words").style(style)


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{100 * x:+.2f} %"


def _day(x: Any, year: bool = True) -> str:
    """A stored date or timestamp as '6 Oct 2026' ('6 Oct' without the year)."""
    d = x if isinstance(x, date) else date.fromisoformat(str(x)[:10])
    return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")


def _span(first: str, last: str) -> str:
    """'13 Jul to 5 Oct 2026' ('7 Jul 2025 to 5 Oct 2026' across a year)."""
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return f"{_day(a, a.year != b.year)} to {_day(b)}"


def _and(xs: Sequence[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def holdout_rows(res: Dict[str, Any]) -> List[List[Any]]:
    """The stored holdout result as table rows: horizon, role, sessions, v2 and model CRPS, share, interval, verdict."""
    out = []
    for k in _KEYS:
        p = res["results"].get(k)
        if p is None:
            continue
        label = f"{k[1:]} min" if k.startswith("h") else f"pre-open +{k.split('_')[-1]} min"
        iv = p.get("interval")
        out.append([label, res["roles"].get(k, ""), p["sessions"], f"{p['base_crps_bps']:.4f}",
                    f"{p['other_crps_bps']:.4f}", _pct(p.get("diff_share")),
                    "-" if not iv else f"[{iv[0]:+.5f}, {iv[1]:+.5f}]", res["verdicts"].get(k, "")])
    return out


def holdout_forest(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The holdout as forest rows (components/forest.py): per horizon model minus the baseline as a share of the
    baseline's CRPS, in %, its 95 % interval on the same scale (the interval in bps over the baseline's CRPS) and the
    verdict - the primary's in the rule's words (pass, inconclusive, fail)."""
    rows = []
    for k in _KEYS:
        p = res["results"].get(k)
        if p is None:
            continue
        base, iv = p.get("base_crps_bps"), p.get("interval")
        share = lambda x: None if x is None or not base else 100 * x / base
        point = 100 * p["diff_share"] if p.get("diff_share") is not None else share(p.get("diff_bps"))
        lo, hi = (share(iv[0]), share(iv[1])) if iv else (None, None)
        primary = res["roles"].get(k) == "primary"
        verdict = res["verdicts"].get(k, "")
        rows.append({"label": f"{k[1:]} min" if k.startswith("h") else f"09:29 + {k.split('_')[-1]}",
                     "role": res["roles"].get(k, "") if k.startswith("h") else "pre-open",
                     "point": point, "lo": lo, "hi": hi, "value": forest.signed(point, 2, " %"),
                     "ci": f"[{forest.signed(lo, 2)}, {forest.signed(hi, 2)}]" if iv else "no interval",
                     "reading": (fh.HOLDOUT_VERDICT.get(verdict, verdict) if primary else verdict).capitalize(),
                     "primary": primary})
    return rows


def holdout_notes(res: Dict[str, Any], baseline: str) -> List[str]:
    """What else the stored holdout shows, in sentences: where in the day the gain sits (the origin phases at 5 to 60
    minutes), how concentrated the primary's gain is, and how the 90 % bands covered against the baseline's."""
    notes = []
    rng = lambda xs: f"{forest.signed(100 * min(xs), 2)} to {forest.signed(100 * max(xs), 2)} %"
    phases = {}
    for name, _, _ in PHASES:
        xs = [p["diff_share"] for h in _NOTE_HORIZONS
              if (p := res["results"].get(f"h{h}:{name}")) and p.get("diff_share") is not None]
        if xs:
            phases[name] = xs
    if phases:
        mean = lambda n: sum(phases[n]) / len(phases[n])
        best, worst = min(phases, key=mean), max(phases, key=mean)
        if mean(best) < 0:
            text = (f"The gain sits {_PHASE[best]}: {rng(phases[best])} at {_NOTE_HORIZONS[0]} to "
                    f"{_NOTE_HORIZONS[-1]} minutes")
            notes.append(text + (f"; {_PHASE[worst]} it loses, {rng(phases[worst])}." if mean(worst) > 0 else "."))
    c = res.get("concentration")
    if c:
        text = f"Better than {baseline} in {100 * c['improved']:.0f} % of sessions"
        if c.get("top5_share") is not None:
            text += f", and its five best sessions carry {100 * c['top5_share']:.0f} % of the gain"
        flips = (c.get("leave_week_out") or {}).get("weeks_flipping_sign")
        if flips is not None:
            text += (f"; with any one of its {c['weeks']} weeks left out it stays a gain" if flips == 0 else
                     f"; leaving out {flips} of its {c['weeks']} weeks, one at a time, turns it to no gain")
        notes.append(text + ".")
    cal = res.get("calibration") or {}
    pairs = [(b["coverage"]["0.90"], m["coverage"]["0.90"]) for h in _BAND_HORIZONS
             if (b := cal.get(f"base|h{h}|all")) and (m := cal.get(f"cand|h{h}|all"))]
    if pairs:
        bs, ms = [b for b, _ in pairs], [m for _, m in pairs]
        mb, mm = sum(bs) / len(bs), sum(ms) / len(ms)
        lead = "Too narrow: its" if mm < 0.9 and mm < mb else "Too wide: its" if mm > 0.9 and mm > mb else "Its"
        f = lambda xs: f"{100 * min(xs):.1f} to {100 * max(xs):.1f} %"
        notes.append(f"{lead} 90 % bands covered {f(ms)} at {_BAND_HORIZONS[0]} to {_BAND_HORIZONS[-1]} minutes, "
                     f"against {baseline}'s {f(bs)}.")
    return notes


def stages(exp: Dict[str, Any], model: Optional[Dict[str, Any]], holdout: Optional[Dict[str, Any]],
           versions: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """Where the experiment stands: (step, what it says, 'done' | 'now' | 'todo'), in order - the first step not
    done is 'now'. The forward record is never done: once begun it stays 'now'."""
    m = exp["definition"]
    dev, held = m["split"]["development"], m["split"]["holdout"]
    rules = [g for g in versions if g["version"]]
    steps = [
        ("Registered", f"{_day(exp['registered_at'])}: manifest fixed, holdout sealed", True),
        ("Development checks", f"{dev['count']} sessions, {_span(dev['first'], dev['last'])}", model is not None),
        ("Frozen", f"{model['definition']['candidate']}, {_day(model['registered_at'], False)}, definition "
                   f"{model['definition_hash'][:8]}" if model else "One candidate, trained once", model is not None),
        ("Holdout, scored once",
         f"{_day(holdout['computed_at'], False)}: {holdout['results']['verdict']} at "
         f"{m['horizons']['primary']['minutes']} minutes" if holdout
         else f"{held['count']} sessions, {_span(held['first'], held['last'])}, sealed", holdout is not None),
        ("Forward record", f"Gathering untouched evidence since {_day(rules[0]['active_from'], False)}" if rules
         else "Not started", False),
    ]
    out, now = [], False
    for title, text, done in steps:
        state = "done" if done else "todo" if now else "now"
        now = now or state == "now"
        out.append((title, text, state))
    return out


def class_counts(group: Dict[str, Any]) -> List[Tuple[str, int]]:
    """A rule version's issues per class, every horizon counted: the four classes of the current rules always, the
    others when they hold issues."""
    counts: Dict[str, int] = {}
    for r in group["horizons"]:
        counts[r["class"]] = counts.get(r["class"], 0) + r["issued"]
    return [(_CLASS.get(c, c), counts.get(c, 0)) for c in fwd.CLASS_ORDER if c in counts or c in _MAIN_CLASSES]


def timing_block(group: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The time from the mark to the issue under the trigger that issued most: the median, the feed's part of it
    (when the origin bar reached the store), the pipeline's, the computation's, the live deadline, and the axis's
    end - a round number past both."""
    rows = [t for t in group.get("timing") or [] if t.get("latency_s")]
    if not rows:
        return None
    t = max(rows, key=lambda t: t["issued"])
    median = t["latency_s"]["median"]
    feed = min(t["arrival_s"]["median"], median) if t.get("arrival_s") else None
    _, top, _ = forest.scale([median, t["deadline_s"] or 0.0], ticks=5)
    return {"trigger": _TRIGGER.get(t["trigger"], t["trigger"]), "median": median, "feed": feed,
            "own": None if feed is None else median - feed,
            "compute": t["compute_s"]["median"] if t.get("compute_s") else None,
            "deadline": t["deadline_s"], "top": top}


def latest_issues(conn, model: str, limit: int = 10) -> List[Dict[str, Any]]:
    """The model's latest forward issues: mark, latency, origin price, rule version, the trigger that issued it and
    per horizon its numbers."""
    rows = conn.execute(
        "SELECT to_char(i.mark_at AT TIME ZONE 'America/New_York', 'YYYY-MM-DD HH24:MI'), "
        "extract(epoch FROM i.recorded_at - i.mark_at), i.origin_price, i.record, i.forecast, "
        "(SELECT r.detail->'trigger'->>'kind' FROM journal.fan_forward_runs r WHERE r.issue_id = i.issue_id AND "
        "r.status = 'issued' LIMIT 1) FROM journal.fan_forward_issues i WHERE i.model = %s "
        "ORDER BY i.mark_at DESC LIMIT %s;", (model, limit)).fetchall()
    return [{"mark": m, "latency_s": float(lat), "price": float(p), "record": r,
             "forecast": json.loads(f) if isinstance(f, str) else f, "trigger": t or "unrecorded"}
            for m, lat, p, r, f, t in rows]


def render(conn, name: str = fx.EXPERIMENT_NAME) -> None:
    """The panel (see the module docstring), read from the store."""
    try:
        exp = fx.load_experiment(conn, name)
    except ValueError:
        ui.label(f"No registered fan experiment {name}.").style(_MUTED)
        return
    model, horizons, why = fan_live.drawn(conn, name)
    done = [r for r in store.experiment_results(conn, name) if r["results"].get("kind") == "holdout"]
    summary = fwd.summary(conn, model) if model else {"versions": []}
    latest = latest_issues(conn, model["version"]) if model else []
    draw(exp, model, horizons, why, done[0] if done else None, summary, latest)


def draw(exp: Dict[str, Any], model: Optional[Dict[str, Any]], horizons: Sequence[int], why: str,
         holdout: Optional[Dict[str, Any]], summary: Dict[str, Any], latest: Sequence[Dict[str, Any]]) -> None:
    """The panel from what render() read: the question, the rail, the holdout, the forward record, the numbers."""
    m = exp["definition"]
    target, minutes = m["targets"]["primary"], m["horizons"]["primary"]["minutes"]
    baseline = model["definition"]["baseline"]["version"] if model else "the baseline"
    if model and horizons:
        drawn = (f"The frozen model, {model['definition']['candidate']}, now draws {target}'s fan at "
                 f"{_and([str(h) for h in horizons])} minutes; {baseline} draws every other horizon.")
    elif model:
        drawn = f"The frozen model, {model['definition']['candidate']}, draws nothing yet: {why}."
    else:
        drawn = f"{why[:1].upper()}{why[1:]}."
    ui.html(f'<p style="max-width:78ch">{escape(m["question"])} {escape(target)} at {minutes} minutes is the '
            f"primary target. {escape(drawn)}</p>").classes("w-full")
    versions = summary.get("versions") or []
    ui.html(_rail(stages(exp, model, holdout, versions))).classes("w-full")
    ui.html(_holdout(exp, model, holdout, baseline)).classes("w-full")
    if model:
        ui.html(_forward([g for g in versions if g["version"]], [g for g in versions if not g["version"]],
                         model["version"])).classes("w-full")
    with ui.expansion("The numbers: the holdout per horizon, the forward record per rule version",
                      value=False).classes("w-full tp-inner"):
        _numbers(holdout, versions, latest)


def _rail(steps: Sequence[Tuple[str, str, str]]) -> str:
    items = "".join(f'<li class="tp-step {state}"><span class="tp-stepdot"></span><strong>{escape(title)}</strong>'
                    f'<span class="tp-small tp-muted">{escape(text)}</span></li>' for title, text, state in steps)
    return f'<ol class="tp-rail" aria-label="Where the experiment stands">{items}</ol>'


def _holdout(exp: Dict[str, Any], model: Optional[Dict[str, Any]], holdout: Optional[Dict[str, Any]],
             baseline: str) -> str:
    m = exp["definition"]
    held = m["split"]["holdout"]
    rule = model["definition"]["acceptance"]["pass"] if model else m["gate"]["pass"]
    head = f'<h3>The holdout: {held["count"]} sessions, {escape(_span(held["first"], held["last"]))}</h3>'
    if holdout is None:
        return (f'<div>{head}<p class="tp-small tp-muted" style="margin-top:4px;max-width:76ch">Sealed until a model '
                f"is frozen, then scored once. The rule, fixed before any result: pass when {escape(rule)}.</p></div>")
    res = holdout["results"]
    plot = forest.html(holdout_forest(res), ("Horizon", "Difference", "Verdict"), "Model better",
                       f"{baseline} better", unit=" %")
    notes = "".join(f"<li>{escape(n)}</li>" for n in holdout_notes(res, baseline))
    aside = ('<aside class="tp-well" style="flex:1 1 260px;min-width:0"><h3 style="font-size:15px;margin-bottom:8px">'
             f'What else the holdout shows</h3><ul class="tp-small tp-notes">{notes}</ul></aside>' if notes else "")
    return ('<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start">'
            f'<div style="flex:3 1 640px;min-width:0">{head}'
            '<p class="tp-small tp-muted" style="margin:4px 0 12px;max-width:76ch">'
            f"Model minus {escape(baseline)} as a share of {escape(baseline)}'s CRPS, with the 95 % interval; "
            f"scored once, {_day(holdout['computed_at'])}. The rule, fixed before any result: pass when "
            f"{escape(rule)}. The holdout is spent; a new evaluation needs a new experiment version.</p>"
            f'<div style="overflow-x:auto">{plot}</div></div>{aside}</div>')


def _rules(version: str, model: str) -> str:
    """A rule version's name without its model's: 'forward_v2'."""
    return version[len(model) + 1:] if version.startswith(f"{model}_") else version


def _forward(rules: Sequence[Dict[str, Any]], legacy: Sequence[Dict[str, Any]], model: str) -> str:
    if not rules:
        return ('<div class="tp-ruled"><h3>Forward record</h3><p class="tp-small tp-muted" style="margin-top:4px">'
                "Nothing recorded yet: scripts/fan_forward.sh, or the dashboard's Auto mode, issues it.</p></div>")
    g = rules[-1]
    since = g["active_from"]
    head = ('<div style="display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 16px">'
            f'<h3>Forward record under rules {escape(_rules(g["version"], model))}</h3>'
            f'<span class="tp-small tp-muted">Active since {since.day} {since:%B %Y, %H:%M} UTC</span></div>')
    o = g["operations"] or {}
    dl = "".join(f'<dt{cls}>{label}</dt><dd{cls}>{o.get(k, "-")}</dd>'
                 for label, k, cls in (("Expected", "expected", ""), ("Attempted", "attempted", ""),
                                       ("Issued", "issued_expected", ""), ("Stale", "stale", ""),
                                       ("Failed", "failed", ""), ("Missed", "missed", ' class="strong"')))
    ops = ('<div class="tp-well"><div class="tp-small tp-muted" style="margin-bottom:8px">Marks the calendar expected, '
           f'{o.get("sessions_expected", 0)} session(s)</div><dl class="tp-dl">{dl}</dl></div>')
    counts = class_counts(g)
    most = max([n for _, n in counts] + [1])
    bars = "".join(f'<div class="tp-kbar"><span>{escape(name)}</span><span class="track"><span style="width:'
                   f'{100 * n / most:.1f}%"></span></span><span>{n}</span></div>' for name, n in counts)
    by_name = dict(counts)
    note = ('<p class="tp-small" style="margin-top:10px">No live issues yet: on a delayed IB feed the origin bar '
            "reaches the store after the live deadline, so these are the delayed-feed evaluation - never evidence "
            "about live forecasts.</p>" if not by_name.get(_CLASS["live"]) and by_name.get(_CLASS["delayed_origin"])
            else "")
    classes = ('<div class="tp-well"><div class="tp-small tp-muted" style="margin-bottom:8px">Issues by class, every '
               f'horizon</div><div style="display:flex;flex-direction:column;gap:6px">{bars}</div>{note}</div>')
    tail = _earlier(rules[:-1], legacy, model)
    return f'<div class="tp-ruled">{head}<div class="tp-fwd">{ops}{classes}{_timing(g)}</div>{tail}</div>'


def _timing(g: Dict[str, Any]) -> str:
    t = timing_block(g)
    if not t:
        return ('<div class="tp-well"><div class="tp-small tp-muted">Median time from the mark to the issue</div>'
                '<p class="tp-small" style="margin-top:8px">No issue timed yet.</p></div>')
    pos = lambda v: f"{100 * v / t['top']:.2f}%"
    deadline = (f"against a {t['deadline']:.0f} s deadline" if t["deadline"] is not None else
                "no live deadline under these rules")
    said = (f"{t['median']:.0f} seconds from the mark to the issue"
            + (f", {t['feed']:.0f} of them waiting for the origin bar" if t["feed"] is not None else "")
            + (f"; the deadline is at {t['deadline']:.0f} seconds" if t["deadline"] is not None else ""))
    if t["feed"] is not None:
        parts = (f'<span class="feed" style="width:{pos(t["feed"])}"></span>'
                 f'<span class="own" style="left:{pos(t["feed"])};width:{pos(t["own"])}"></span>')
        words = (f"{t['feed']:,.0f} s of it is the feed: the origin bar reached the store that late. The pipeline's "
                 f"own part is {t['own']:,.0f} s")
    else:
        parts = f'<span class="own" style="left:0;width:{pos(t["median"])}"></span>'
        words = "When the origin bar reached the store was not recorded"
    if t["deadline"] is not None:
        parts += f'<span class="dl" style="left:{pos(t["deadline"])}"></span>'
    words += f", computation {t['compute']:,.0f} s." if t["compute"] is not None else "."
    return ('<div class="tp-well"><div class="tp-small tp-muted" style="margin-bottom:8px">Median time from the mark '
            f'to the issue, {escape(t["trigger"])}</div><div class="tp-x" style="font-weight:700;font-size:22px;'
            f'line-height:1.1">{t["median"]:,.0f} s <span class="tp-small tp-muted" style="font-stretch:100%;'
            f'font-weight:400">{deadline}</span></div>'
            f'<div class="tp-lat" role="img" aria-label="{escape(said)}">{parts}</div>'
            '<div class="tp-small" style="display:flex;justify-content:space-between;gap:8px"><span>'
            + (f"Deadline {t['deadline']:.0f} s" if t["deadline"] is not None else "")
            + f'</span><span class="tp-muted">0 to {t["top"]:,.0f} s</span></div>'
            f'<p class="tp-small" style="margin-top:10px">{escape(words)}</p></div>')


def _earlier(rules: Sequence[Dict[str, Any]], legacy: Sequence[Dict[str, Any]], model: str) -> str:
    """The earlier rule versions and the issues made under none, in a line."""
    said = []
    for e in rules:
        o = e["operations"] or {}
        a, b = e["active_from"], e["active_to"]
        when = (f"from {a:%H:%M} to {b:%H:%M} UTC on {a.day} {a:%B}" if a.date() == b.date() else
                f"from {a.day} {a:%B %H:%M} to {b.day} {b:%B %H:%M} UTC")
        said.append(f"Rules {_rules(e['version'], model)} ran {when}: {o.get('expected', 0)} marks expected, "
                    f"{o.get('issued_expected', 0)} issued, {o.get('missed') or 'none'} missed.")
    for e in legacy:
        said.append(f"{sum(r['issued'] for r in e['horizons'])} horizon issues were made under no rules (legacy "
                    "research).")
    return f'<p class="tp-small tp-muted" style="margin-top:12px">{escape(" ".join(said))}</p>' if said else ""


def _numbers(holdout: Optional[Dict[str, Any]], versions: Sequence[Dict[str, Any]],
             latest: Sequence[Dict[str, Any]]) -> None:
    """The stored numbers behind the card, as tables."""
    if holdout:
        res = holdout["results"]
        ui.label(f"Holdout ({res['sessions']['wanted']} sessions, scored once {str(holdout['computed_at'])[:16]} UTC): "
                 f"{res['verdict']} at the primary horizon").classes("text-sm font-medium mt-2")
        rows = holdout_rows(res)
        _grid(["Horizon", "Role", "Sessions", "v2 CRPS", "Model CRPS", "Model - v2", "95 % interval", "Verdict"], rows,
              [_VERDICT.get(r[-1]) for r in rows])
    ui.label("Forward record").classes("text-sm font-medium mt-3")
    ui.label("Issued from what the store held at each mark, scored once each session is final. On this account the "
             "IB feed is delayed (no real-time subscription), so no issue is live: delayed-origin horizons are the "
             "delayed-feed evaluation - a different experiment from live 5- and 15-minute forecasts. The mark "
             "trigger tries each mark within its 60 s deadline (the live attempt); on this feed it finds the origin "
             "bar missing and records the mark stale - the data, not the schedule, was late - and the 2-minute "
             "catch-up issues it once the bar arrives.").classes("text-xs").style(_MUTED)
    if not versions:
        ui.label("Nothing recorded yet: scripts/fan_forward.sh, or the dashboard's Auto mode, issues it.").style(_MUTED)
    for g in versions:
        title = f"Rules {g['version']}" if g["version"] else "Issues made under no rules (legacy research)"
        ui.label(title).classes("text-xs font-medium mt-1")
        _grid(["Horizon", "Class", "Issued", "Scored", "Sessions", "Model - v2", "90 % covers v2 / model"],
              [[f"{r['horizon']} min", _CLASS.get(r["class"], r["class"]), r["issued"], r["scored"], r["sessions"],
                _pct((r["paired"] or {}).get("diff_share")) if r["paired"] else "-",
                "-" if r["cover90_base"] is None else
                f"{100 * r['cover90_base']:.0f} / {100 * r['cover90_model']:.0f} %"] for r in g["horizons"]])
        if g.get("timing"):
            dl = g["timing"][0]["deadline_s"]
            ui.label("Timing, seconds after the mark (median / 90th percentile / largest)"
                     + (f"; the live deadline {dl:.0f} s" if dl is not None else "; no live deadline under these rules")
                     + ". When the origin bar was stored is the feed's part; collection and computation the "
                       "pipeline's.").classes("text-xs mt-1").style(_MUTED)
            _grid(["Trigger", "Marks tried", "Issued", "Found the bar missing", "Within deadline", "Issued after",
                   "Origin bar stored after", "Collection", "Computation"],
                  [[fwd.TRIGGER_TEXT.get(t["trigger"], t["trigger"]), t["attempted"], t["issued"], t["stale"],
                    "-" if t["within_deadline"] is None else t["within_deadline"], fwd.spread(t["latency_s"]),
                    fwd.spread(t["arrival_s"]), fwd.spread(t["collect_s"]), fwd.spread(t["compute_s"])]
                   for t in g["timing"]])
    if latest:
        ui.label("Latest issues").classes("text-xs font-medium mt-1")
        lo, hi = fwd.CHART_LEVELS.index(0.05), fwd.CHART_LEVELS.index(0.95)
        rows = []
        for i in latest:
            f15 = i["forecast"].get("15")
            rows.append([i["mark"], f"{i['latency_s'] / 60:.1f} min", fwd.TRIGGER_TEXT.get(i["trigger"], i["trigger"]),
                         f"{i['price']:,.2f}", (i["record"] or "no rules")[-10:],
                         "-" if not f15 else f"{f15['base'][lo]:,.2f}–{f15['base'][hi]:,.2f}",
                         "-" if not f15 else f"{f15['model'][lo]:,.2f}–{f15['model'][hi]:,.2f} (x{f15['multiplier']:.2f})"])
        _grid(["Mark (ET)", "Latency", "Trigger", "Origin price", "Rules", "v2 90 % at 15 min", "Model 90 % at 15 min"],
              rows)
