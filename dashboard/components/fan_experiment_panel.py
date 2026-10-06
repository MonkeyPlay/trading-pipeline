# dashboard/components/fan_experiment_panel.py
"""
The intermarket fan experiment on the Evaluation page (docs/fan_experiment.md, chunk 9) -
what is stored, nothing computed here:

  the frozen model   its version and definition, the code it was frozen from, the sessions
                     it was trained on, and the horizons it draws in the Session Explorer
  the holdout        its one scoring: per horizon the paired CRPS against fan_rw_v2, the
                     95 % interval and the verdict (journal.experiment_results)
  the forward record per rule version the marks the calendar expected and what became of
                     them, the issues by horizon and class (live, delayed origin - the
                     delayed-feed evaluation -, late, expired), per trigger how fast the
                     forecasts got out against the live deadline and when their origin bar
                     arrived, and the latest issues with their latency and trigger
                     (forecaster/fan_forward.py)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from nicegui import ui

from database import journal_store as store
from forecaster import fan_experiment as fx
from forecaster import fan_forward as fwd
from forecaster import fan_live

_MUTED = "color:#787b86"
_CELL = "px-2 py-1 text-xs"
_VERDICT = {"better": "#26a69a", "worse": "#ef5350"}
_CLASS = {"live": "live", "delayed_origin": "delayed origin", "late": "late", "expired": "expired",
          "on_time": "v1 'on time'", "delayed": "v1 delayed", "legacy": "no rules"}


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


def holdout_rows(res: Dict[str, Any]) -> List[List[Any]]:
    """The stored holdout result as table rows: horizon, role, sessions, v2 and model CRPS, share, interval, verdict."""
    out = []
    for k in [f"h{h}" for h in fan_live.fh.REPORT_HORIZONS] + [f"pre_open_{m}" for m in fan_live.fh.PRE_OPEN_MINUTES]:
        p = res["results"].get(k)
        if p is None:
            continue
        label = f"{k[1:]} min" if k.startswith("h") else f"pre-open +{k.split('_')[-1]} min"
        iv = p.get("interval")
        out.append([label, res["roles"].get(k, ""), p["sessions"], f"{p['base_crps_bps']:.4f}",
                    f"{p['other_crps_bps']:.4f}", _pct(p.get("diff_share")),
                    "-" if not iv else f"[{iv[0]:+.5f}, {iv[1]:+.5f}]", res["verdicts"].get(k, "")])
    return out


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
    """The panel (see the module docstring)."""
    try:
        exp = fx.load_experiment(conn, name)
    except ValueError:
        ui.label(f"No registered fan experiment {name}.").style(_MUTED)
        return
    model, horizons, why = fan_live.drawn(conn, name)
    if model is None:
        ui.label(f"{why}.").style(_MUTED)
        return
    d = model["definition"]
    src = (d.get("provenance") or {}).get("source") or {}
    ui.label(f"Frozen model {model['version']} (definition {model['definition_hash'][:16]}): {d['candidate']}, "
             f"trained once on {len(d['training_sessions'])} development sessions, from commit "
             f"{src.get('commit', '?')[:10]}{' +dirty' if src.get('dirty') else ''}. In the Session Explorer it draws "
             + (f"{exp['definition']['targets']['primary']} at {', '.join(f'{h} min' for h in horizons)} - the horizons "
                "whose holdout interval lay below zero; fan_rw_v2 draws the rest." if horizons else f"nothing: {why}."
                )).classes("text-sm")
    done = [r for r in store.experiment_results(conn, name) if r["results"].get("kind") == "holdout"]
    if done:
        res = done[0]["results"]
        ui.label(f"Holdout ({res['sessions']['wanted']} sessions, scored once {str(done[0]['computed_at'])[:16]} UTC): "
                 f"{res['verdict']} at the primary horizon").classes("text-sm font-medium mt-2")
        rows = holdout_rows(res)
        _grid(["Horizon", "Role", "Sessions", "v2 CRPS", "Model CRPS", "Model - v2", "95 % interval", "Verdict"], rows,
              [_VERDICT.get(r[-1]) for r in rows])
    s = fwd.summary(conn, model)
    ui.label("Forward record").classes("text-sm font-medium mt-2")
    ui.label("Issued from what the store held at each mark, scored once each session is final. On this account the "
             "IB feed is delayed (no real-time subscription), so no issue is live: delayed-origin horizons are the "
             "delayed-feed evaluation - a different experiment from live 5- and 15-minute forecasts. The mark "
             "trigger tries each mark within its 60 s deadline (the live attempt); on this feed it finds the origin "
             "bar missing and records the mark stale - the data, not the schedule, was late - and the 2-minute "
             "catch-up issues it once the bar arrives.").classes("text-xs").style(_MUTED)
    if not s["versions"]:
        ui.label("Nothing recorded yet: scripts/fan_forward.sh, or the dashboard's Auto mode, issues it.").style(_MUTED)
    for g in s["versions"]:
        title = f"Rules {g['version']}" if g["version"] else "Issues made under no rules (legacy research)"
        ui.label(title).classes("text-xs font-medium mt-1")
        o = g["operations"]
        if o:
            ui.label(f"marks the calendar expected since these rules were registered: {o['expected']} "
                     f"({o['sessions_expected']} session(s)) · attempted {o['attempted']} · issued {o['issued_expected']} "
                     f"· stale {o['stale']} · failed {o['failed']} · missed {o['missed']} - and {o['issued']} issue(s) "
                     "in all under these rules").classes("text-xs")
        _grid(["Horizon", "Class", "Issued", "Scored", "Sessions", "Model - v2", "90 % covers v2 / model"],
              [[f"{r['horizon']} min", _CLASS[r["class"]], r["issued"], r["scored"], r["sessions"],
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
    latest = latest_issues(conn, model["version"])
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
