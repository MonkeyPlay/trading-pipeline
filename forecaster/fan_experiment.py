# forecaster/fan_experiment.py
"""
The intermarket fan experiment - step 3 of the fan (docs/fan_experiment.md): a learned
fan that reads every collected instrument, accepted only where it forecasts NQ better
than the benchmark fan, and a measure of how much each instrument contributes. Every
choice is fixed here, in a registered manifest (kind 'fan_experiment'), before any
model exists.

  availability(conn, symbols, last)   per instrument, its complete sessions as stored:
                                      the active contract's where the symbol rolls (the
                                      series the fan reads), else its only contract's
  experiment_manifest(...)            the manifest from that availability and the
                                      exchange calendar (pure; see below)
  register_experiment / load_experiment
  guard(conn, first, last)            refuses a session range that reaches into a
                                      sealed holdout (HoldoutSealed)
  holdout_sessions(conn, name, model) the holdout's sessions - for a frozen model only

The session split, resolved from the stored data at registration:

  holdout       the last HOLDOUT_SESSIONS full sessions to HOLDOUT_END, fixed by date:
                no backfill ever moves it
  window        starts at the primary target's (NQ's) first complete session. Every
                other instrument is read from its own first complete session - its
                values are missing before it - and is included when it is complete from
                at least MIN_COVERAGE of the development sessions on; one that starts
                later is deferred until its history is backfilled, which a new version
                takes up
  development   the full sessions from WARM_UP_SESSIONS into the window to the last one
                before the holdout
  checks        the last CHECKS x CHECK_SESSIONS development sessions in consecutive
                blocks, each trained on every development session before it

The holdout stays sealed until a model frozen against the manifest - a registered
'fan_model' naming this experiment and its hash, trained on development sessions only -
opens it; one model per experiment version is scored on it. A version names the one it
supersedes; the seal follows the newest, so a superseded version never holds it shut.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from config import Config
from contracts import fan as F
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal

EXPERIMENT_NAME = "fan_intermarket_v2"
SUPERSEDES = "fan_intermarket_v1"
SUPERSEDES_WHY = ("registered 2026-10-06 before any result; its window waited for the instrument with the latest "
                  "start (RTY's active series, 2025-09-18). This version starts the window with NQ's history and reads "
                  "every other instrument from its own start (the user's decision of 2026-10-06)")
PRIMARY_TARGET = "NQ"
PRIMARY_HORIZON = 15                     # minutes
SECONDARY_TARGETS = ("ES", "RTY")
HORIZONS = {"primary": [PRIMARY_HORIZON], "secondary": [5, 30, 60], "exploratory": [1, 20, 120, 240, "close"]}
PRE_OPEN = {"origin_et": "09:29", "endpoints_et": ["09:45", "10:00", "10:30"], "minutes": [16, 31, 61]}
HOLDOUT_END = "2026-10-05"
HOLDOUT_SESSIONS = 60
CHECKS = 3
CHECK_SESSIONS = 30
WARM_UP_SESSIONS = 20
MIN_DEVELOPMENT_SESSIONS = 150
MIN_COVERAGE = 0.75                      # an instrument complete from less of development than this is deferred
CRPS_LEVELS = 200
# Every collected instrument belongs to exactly one group; the target itself is the 'own' group.
GROUPS = {
    "index_futures": ("NQ", "ES", "RTY"),
    "volatility": ("VIX", "VXN"),
    "rates": ("TNX", "10Y"),
    "dollar": ("DX",),
    "equity_etfs": ("QQQ", "SPY", "IWM", "SMH"),
}
REMOVED = {"2YY": "Micro 2-Year Yield futures: dropped entirely on 2026-10-06 with 26 complete sessions, its stored "
                  "data removed (migration 0018)"}


class HoldoutSealed(RuntimeError):
    """A session range reaches into a holdout no frozen model has opened yet."""


# --------------------------------------------------------------------------
# Availability (read from the store)
# --------------------------------------------------------------------------

_DAYS_ACTIVE = """
    SELECT DISTINCT d.trading_day FROM session_days d
      JOIN active_contracts a ON a.contract_id = d.contract_id AND a.trading_day = d.trading_day
     WHERE a.symbol = %s AND d.interval = '1m' AND d.price_type = %s AND d.status = 'COMPLETE'
       AND d.trading_day <= %s;"""
_DAYS_ANY = """
    SELECT DISTINCT d.trading_day FROM session_days d JOIN contracts c ON c.contract_id = d.contract_id
     WHERE c.symbol = %s AND d.interval = '1m' AND d.price_type = %s AND d.status = 'COMPLETE'
       AND d.trading_day <= %s;"""


def availability(conn, symbols: Sequence[str], last: str) -> Dict[str, List[str]]:
    """Per symbol, its complete sessions to ``last`` in date order: the active contract's where the symbol rolls,
    else those of its contracts."""
    out: Dict[str, List[str]] = {}
    for symbol in symbols:
        inst = Config.instrument(symbol)
        if inst is None:
            raise ValueError(f"unknown instrument {symbol!r}")
        rolls = conn.execute("SELECT 1 FROM active_contracts WHERE symbol = %s LIMIT 1;", (symbol,)).fetchone()
        rows = conn.execute(_DAYS_ACTIVE if rolls else _DAYS_ANY, (symbol, inst.what_to_show, last)).fetchall()
        out[symbol] = sorted(str(r[0])[:10] for r in rows)
    return out


# --------------------------------------------------------------------------
# The manifest (pure)
# --------------------------------------------------------------------------

def _full_sessions(first: str, last: str) -> List[str]:
    return [s.session_date.isoformat() for s in cal.sessions_between(date.fromisoformat(first),
                                                                         date.fromisoformat(last))
            if s.schedule == "full"]


def _group_of(symbol: str) -> str:
    found = [g for g, members in GROUPS.items() if symbol in members]
    if len(found) != 1:
        raise ValueError(f"{symbol} must belong to exactly one instrument group (forecaster/fan_experiment.GROUPS)")
    return found[0]


def resolve_split(days: Dict[str, List[str]], primary: str, targets: Sequence[str], holdout_end: str = HOLDOUT_END,
                  holdout_sessions: int = HOLDOUT_SESSIONS, warm_up: int = WARM_UP_SESSIONS,
                  min_development: int = MIN_DEVELOPMENT_SESSIONS, min_coverage: float = MIN_COVERAGE,
                  checks: int = CHECKS, check_sessions: int = CHECK_SESSIONS) -> Dict[str, Any]:
    """
    The window, the instruments it includes or defers, development, the checks and the holdout (see the module
    docstring) from each instrument's complete sessions ``days``. Raises ValueError when the primary target's
    history gives fewer than ``min_development`` development sessions or a target covers too little of them.
    """
    s = cal.session(date.fromisoformat(holdout_end))
    if s.schedule != "full":
        raise ValueError(f"the holdout must end on a full session; {holdout_end} is {s.schedule}")
    if min_development <= checks * check_sessions:
        raise ValueError("the development sessions must be more than the checks' sessions")
    missing = [t for t in targets if not days.get(t)]
    if missing:
        raise ValueError(f"no complete session stored for the target(s) {', '.join(missing)}")
    start = days[primary][0]
    full = _full_sessions(max(start, cal.COVERAGE_START.isoformat()), holdout_end)
    if len(full) < holdout_sessions:
        raise ValueError(f"only {len(full)} full sessions to {holdout_end}")
    holdout = full[-holdout_sessions:]
    development = [d for d in full[warm_up:] if d < holdout[0]]
    if len(development) < min_development:
        raise ValueError(f"{primary}'s history from {start} leaves {len(development)} development sessions, fewer "
                         f"than {min_development}")
    included, deferred, coverage = [], {}, {}
    for sym, held in days.items():
        if not held:
            deferred[sym] = "no complete session stored"
            continue
        coverage[sym] = sum(1 for d in development if d >= held[0]) / len(development)
        if coverage[sym] >= min_coverage:
            included.append(sym)
        else:
            deferred[sym] = (f"complete from {held[0]}: {coverage[sym]:.0%} of the development sessions, fewer than "
                             f"{min_coverage:.0%}. Included once its history is backfilled - a new experiment version")
    late = [t for t in targets if t in deferred]
    if late:
        raise ValueError(f"the target(s) {', '.join(late)} cover too little of development, and a target cannot be "
                         f"deferred")
    blocks = development[-checks * check_sessions:]
    check_list = []
    for k in range(checks):
        block = blocks[k * check_sessions:(k + 1) * check_sessions]
        train = [d for d in development if d < block[0]]
        check_list.append({"check": k + 1, "train": {"first": train[0], "last": train[-1], "sessions": len(train)},
                           "sessions": {"first": block[0], "last": block[-1], "count": len(block)}})
    return {"start": start, "set_by": [primary], "included": sorted(included), "deferred": deferred,
            "coverage": coverage, "development": development, "checks": check_list, "holdout": holdout}


def experiment_manifest(name: str, days: Dict[str, List[str]], holdout_end: str = HOLDOUT_END,
                        holdout_sessions: int = HOLDOUT_SESSIONS, warm_up: int = WARM_UP_SESSIONS,
                        min_development: int = MIN_DEVELOPMENT_SESSIONS, min_coverage: float = MIN_COVERAGE,
                        supersedes: Optional[str] = None) -> Dict[str, Any]:
    """The manifest (see the module docstring) from every collected instrument's complete sessions ``days``;
    ``supersedes``: the version it replaces (with SUPERSEDES_WHY when that is SUPERSEDES)."""
    targets = [PRIMARY_TARGET, *SECONDARY_TARGETS]
    for sym in days:
        _group_of(sym)
    split = resolve_split(days, PRIMARY_TARGET, targets, holdout_end, holdout_sessions, warm_up, min_development,
                          min_coverage)
    scored = split["development"] + split["holdout"]
    excluded = {t: [d for d in scored if d not in set(days[t])] for t in targets}
    dev_first = split["development"][0]
    instruments = {
        sym: {"group": _group_of(sym), "role": "target" if sym in targets else "context",
              "status": "included" if sym in split["included"] else "deferred",
              "first_complete": days[sym][0] if days[sym] else None, "complete_sessions": len(days[sym]),
              "development_coverage": round(split["coverage"].get(sym, 0.0), 4),
              "missing_before": days[sym][0] if days[sym] and days[sym][0] > dev_first else None,
              **({"deferred_because": split["deferred"][sym]} if sym in split["deferred"] else {})}
        for sym in sorted(days)}
    groups = {g: [m for m in members if m in days] for g, members in GROUPS.items()}
    boot = dict(F.BOOTSTRAP)
    return {
        "name": name,
        **({"supersedes": {"version": supersedes,
                           "why": SUPERSEDES_WHY if supersedes == SUPERSEDES else "a revised manifest"}}
           if supersedes else {}),
        "question": "Does a fan that reads every collected instrument forecast NQ's price distribution better than "
                    "the benchmark fan, horizon by horizon - and how much does each instrument contribute?",
        "plan": "docs/fan_experiment.md (step 3 of the fan, docs/fan.md)",
        "purpose": "historical test",
        "purpose_note": "the holdout is historical: the benchmark fan_rw_v1 was scored over every session to "
                        "2026-10-05, the holdout's included, on 2026-10-06 before this registration "
                        "(docs/reports/fan_rw_v1_*). Nothing was fitted to those scores, but the lessons drawn - too "
                        "narrow around releases, too thin in the tails, NQ's pre-open hour slightly narrow - came "
                        "partly from the holdout's period",
        "forecast": {
            "kind": "anytime fan: from any minute, the distribution of the price at every later minute of the "
                    "trading day",
            "origin": "the close of the target's last completed 1-minute bar (its active contract); every minute "
                      "of the trading day - 18:00 ET the evening before to the day's end",
            "outcome": "the log of the last traded price h minutes after the origin (a minute without a trade "
                       "keeps the price), inside the same trading day",
            "point_in_time": "a forecast from minute t reads only what is known at t: bars closed by t, the "
                             "trading day's scheduled releases, the sessions before it",
        },
        "targets": {"primary": PRIMARY_TARGET, "secondary": list(SECONDARY_TARGETS),
                    "secondary_note": "reported with intervals, never deciding"},
        "horizons": {
            "primary": {"target": PRIMARY_TARGET, "minutes": PRIMARY_HORIZON, "origins": "every minute"},
            "secondary": {"minutes": HORIZONS["secondary"],
                          "targets_at_primary_horizon": list(SECONDARY_TARGETS),
                          "pre_open": {"origin": "the last bar completed by 09:29 ET (the P1 cutoff)",
                                       "endpoints_et": PRE_OPEN["endpoints_et"], "minutes": PRE_OPEN["minutes"],
                                       "target": PRIMARY_TARGET}},
            "exploratory": {"minutes": [h for h in HORIZONS["exploratory"] if h != "close"],
                            "close": "the last price at the regular session's close, from every origin before "
                                     "it",
                            "breakdowns": ["origin phase (overnight, pre-open, opening hour, midday, afternoon, "
                                           "after the close)", "origins with a scheduled release ahead"]},
            "rule": "fixed here, before any result; the primary is never switched to whichever horizon scores best",
        },
        "score": {
            "metric": "CRPS of the log price, in basis points; lower is better",
            "computation": f"2/K x sum over k of the pinball loss at tau_k = (k - 1/2)/K, K = {CRPS_LEVELS}, of "
                           "the issued distribution's quantile at tau_k: the same for every version, on both sides "
                           "of a comparison",
            "origins_scored": "every origin with a last price at the origin and at the endpoint, the endpoint "
                              "inside the trading day; an origin where the baseline's spread is zero (the market "
                              "closed) is not scored, for either side",
            "session": "the mean over the session's scored origins (the pre-open slice: its one origin)",
            "comparison": "model minus baseline per session, paired on identical sessions and origins; the mean "
                          "over sessions",
            "uncertainty": {"method": "moving-block bootstrap of the paired session differences in date order",
                            **boot},
        },
        "baselines": {
            "v1": F.FAN_VERSION,
            "v2_rule": "a rule-based fan_rw_v2 (development chunk 2) replaces fan_rw_v1 as the baseline when its "
                       f"paired difference against fan_rw_v1, {PRIMARY_TARGET} at {PRIMARY_HORIZON} minutes over the "
                       "three checks' sessions, has its whole 95 % interval below zero - decided before any model "
                       "is frozen; otherwise fan_rw_v1 stays the baseline",
        },
        "gate": {
            "pass": f"the whole 95 % interval of model minus baseline, {PRIMARY_TARGET} at {PRIMARY_HORIZON} "
                    "minutes over the holdout's sessions, lies below zero",
            "inconclusive": "the interval includes zero: the model is not promoted on this evidence",
            "fail": "the whole interval lies above zero",
            "drawing": "on the chart the model draws a target's horizon only where its own holdout interval for "
                       "that target and horizon lies below zero; the baseline draws the rest",
            "scope": "a pass says the fan forecasts the size of moves better; nothing about direction or profit",
        },
        "split": {
            "calendar_version": cal.CALENDAR_VERSION,
            "schedule": "full sessions only: early closes are not scored, as in the benchmark's scoring",
            "window": {"rule": f"starts at the primary target's ({PRIMARY_TARGET}) first complete session; every "
                               "other instrument is read from its own first complete session - missing before it - "
                               f"and included when it is complete from at least {min_coverage:.0%} of the "
                               "development sessions on; one that starts later is deferred until backfilled",
                       "start": split["start"], "set_by": split["set_by"], "warm_up_sessions": warm_up,
                       "min_development_sessions": min_development, "min_coverage": min_coverage},
            "development": {"first": split["development"][0], "last": split["development"][-1],
                            "count": len(split["development"]), "sessions": split["development"]},
            "checks": {"rule": f"the last {CHECKS} x {CHECK_SESSIONS} development sessions in consecutive blocks, "
                               "each trained on every development session before it",
                       "blocks": split["checks"]},
            "holdout": {"rule": f"the last {holdout_sessions} full sessions to {holdout_end}, fixed by date",
                        "first": split["holdout"][0], "last": split["holdout"][-1],
                        "count": len(split["holdout"]), "sessions": split["holdout"]},
        },
        "instruments": {
            "rule": "every instrument the collector collects (config.Config.collect_symbols) at registration, in "
                    "one group each; the target is its own 'own' group",
            "groups": groups, "availability": instruments, "removed": dict(REMOVED),
        },
        "data": {
            "target_sessions": "a session is scored for a target when it is full in the calendar and the target's "
                               "active-contract day was COMPLETE in the store at registration",
            "excluded": excluded,
            "context": "a context instrument's value at a minute is the close of its last bar at or before that "
                       "minute, with that bar's age in minutes; missing before the instrument's first complete "
                       "session (its 'missing_before') and before its first bar of the trading day's window; a "
                       "feature that needs an instrument's history is missing until it has enough; a context gap "
                       "never excludes a session",
        },
        "development": {
            "rule": "features, settings and model choices are made on the three checks only, with any number of "
                    "attempts; a check's result is development, never the verdict",
            "attribution": {
                "target": PRIMARY_TARGET,
                "units": "the groups and, within them, single instruments",
                "measures": {"drop_one": "retrained without the unit: what is lost",
                             "add_one": "the own-instrument model plus the unit only: what it adds",
                             "shapley": "each group's average contribution over every order of adding groups",
                             "precision": "band width and band coverage, with and without the unit"},
                "data": "the three checks only; intervals as the gate's; an interval that includes zero is "
                        "reported as no detectable contribution",
            },
        },
        "freeze": {
            "rule": "one model per experiment version is frozen, then scored once on the holdout: registered as kind "
                    "fan_model naming this experiment and its definition hash, with its training sessions "
                    "(development only), features, settings and code revision",
            "repeat": "a second holdout evaluation needs a new experiment version and is reported as having seen "
                      "the first result",
        },
        "holdout_access": {
            "sealed_until": "a frozen fan_model opens it; until then no candidate (fan_rw_v2, a model) is scored or "
                            "drawn on the holdout's sessions",
            "exempt": "the Session Explorer keeps drawing fan_rw_v1 with its recent accuracy - an operational "
                      "display of the registered benchmark; nothing is developed from it",
        },
    }


# --------------------------------------------------------------------------
# Registration and the sealed holdout (the store)
# --------------------------------------------------------------------------

def build_manifest(conn, name: str = EXPERIMENT_NAME, holdout_end: str = HOLDOUT_END,
                   holdout_sessions: int = HOLDOUT_SESSIONS, warm_up: int = WARM_UP_SESSIONS,
                   min_development: int = MIN_DEVELOPMENT_SESSIONS, min_coverage: float = MIN_COVERAGE,
                   supersedes: Optional[str] = SUPERSEDES) -> Dict[str, Any]:
    """The manifest from the store as it is now: every collected instrument's complete sessions to the holdout's
    end."""
    days = availability(conn, Config.collect_symbols(), holdout_end)
    return experiment_manifest(name, days, holdout_end, holdout_sessions, warm_up, min_development, min_coverage,
                               supersedes)


def register_experiment(conn, manifest: Dict[str, Any]) -> bool:
    """Registers the manifest (kind 'fan_experiment'); True when new, False when the identical manifest is already
    registered; a different manifest under the same name raises journal_store.VersionConflict. The version it
    supersedes must be a registered experiment whose holdout no frozen model has opened."""
    if store.get_version(conn, F.FAN_VERSION) is None:
        store.register_version(conn, F.fan_record())
    old = (manifest.get("supersedes") or {}).get("version")
    if old:
        rec = store.get_version(conn, old)
        if rec is None or rec["kind"] != "fan_experiment":
            raise ValueError(f"{manifest['name']} supersedes {old}, which is not a registered fan experiment")
        if frozen_model(conn, rec) is not None:
            raise ValueError(f"{old}'s holdout was opened by a frozen model: a version replacing it must be reported "
                             f"as having seen that result - register it under a manifest that says so")
    return store.register_version(conn, defs._record(manifest["name"], "fan_experiment", manifest))


def current_experiments(conn) -> List[Dict[str, Any]]:
    """The registered fan experiments no other names as superseded."""
    exps = store.list_versions(conn, "fan_experiment")
    gone = {(e["definition"].get("supersedes") or {}).get("version") for e in exps}
    return [e for e in exps if e["version"] not in gone]


def superseded_by(conn, name: str) -> Optional[str]:
    """The registered experiment that supersedes ``name``, if any."""
    return next((e["version"] for e in store.list_versions(conn, "fan_experiment")
                 if (e["definition"].get("supersedes") or {}).get("version") == name), None)


def load_experiment(conn, name: str) -> Dict[str, Any]:
    """The registered experiment ({'version', 'kind', 'definition', 'definition_hash', 'registered_at'})."""
    rec = store.get_version(conn, name)
    if rec is None or rec["kind"] != "fan_experiment":
        raise ValueError(f"no registered fan experiment {name!r}")
    return rec


def why_sealed(experiment: Dict[str, Any], model: Dict[str, Any]) -> Optional[str]:
    """Why ``model`` (a registered definition) cannot open ``experiment``'s holdout - None when it can."""
    if model.get("kind") != "fan_model":
        return f"{model.get('version')} is a {model.get('kind')}, not a fan_model"
    bound = model["definition"].get("experiment") or {}
    if bound.get("name") != experiment["version"] or bound.get("definition_hash") != experiment["definition_hash"]:
        return f"{model['version']} was frozen against another experiment"
    trained = set(model["definition"].get("training_sessions") or [])
    if not trained:
        return f"{model['version']} records no training sessions"
    outside = sorted(trained - set(experiment["definition"]["split"]["development"]["sessions"]))
    if outside:
        return f"{model['version']} was trained on {len(outside)} session(s) outside development, from {outside[0]}"
    return None


def frozen_model(conn, experiment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first registered model that opens ``experiment``'s holdout, or None while it is sealed."""
    return next((m for m in store.list_versions(conn, "fan_model") if why_sealed(experiment, m) is None), None)


def guard(conn, first, last) -> None:
    """Raises HoldoutSealed when the sessions ``first`` to ``last`` reach into the holdout of a current (not
    superseded) experiment that no frozen model has opened."""
    first, last = str(first)[:10], str(last)[:10]
    for exp in current_experiments(conn):
        split = exp["definition"]["split"]
        hold = split["holdout"]
        if first <= hold["last"] and last >= hold["first"] and frozen_model(conn, exp) is None:
            raise HoldoutSealed(
                f"{first} to {last} reaches into the sealed holdout of {exp['version']} ({hold['first']} to "
                f"{hold['last']}): score sessions to {split['development']['last']} (development) until a model "
                f"frozen against it opens the holdout")


def holdout_sessions(conn, name: str, model_version: str) -> List[str]:
    """The holdout's sessions, for the frozen model ``model_version`` only (raises HoldoutSealed otherwise)."""
    exp = load_experiment(conn, name)
    model = store.get_version(conn, model_version)
    if model is None:
        raise HoldoutSealed(f"no registered model {model_version!r}")
    why = why_sealed(exp, model)
    if why:
        raise HoldoutSealed(f"{name}'s holdout stays sealed: {why}")
    return list(exp["definition"]["split"]["holdout"]["sessions"])
