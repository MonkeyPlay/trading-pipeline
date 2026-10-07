# forecaster/fan_first_hit.py
"""
First-hit probabilities (docs/fan_first_hit.md): within the next 15 minutes, does NQ reach an
upper barrier first, an equally distant lower barrier first, or neither? A different question
from the fan's endpoint: a market can finish near its start after a useful excursion. Kept
apart from the fan - a first-hit result would support probabilities beside the chart, not a
shift of the fan's centre.

  barrier     +-BARRIER_K x fan_rw_v2's walk-forward sigma to 15 minutes, around the log of the
              origin's last close - volatility known at issuance, the same in every block
  path        the 1-minute bars of the active contract starting in the 15 minutes after the
              origin (each bar's high and low); the first minute whose high reaches the upper
              barrier or whose low reaches the lower decides; both in that minute: ambiguous
              (minute bars cannot order them); none by the 15th minute: neither
  classes     DOWN (lower first), NEITHER, UP (upper first)

Predictors, per block fitted on the sessions before it (the direction experiments' blocks):

  freq        A: the training rows' class frequencies
  own         B: HistGradientBoostingClassifier on NQ's own context (fan_im_direction 'own')
  intermarket C: the same with the intermarket set (fan_im_direction 'intermarket')

B and C: GRID x SHRINK options blend the model with A's frequencies, p = lambda x model +
(1 - lambda) x freq, and lambda = 0 (A itself) is an option; the option with the lowest mean
session multiclass Brier score on the validation sessions (the last 20 % of training) is
refitted on every training session. Ambiguous rows are left out of training and selection; the
evaluation scores them under both assignments (UP first, DOWN first).

  labels(lp, hi, lo, end, b)       per origin the class (pure)
  run(frames, table, hl, blocks, sessions)   the rolling-origin evaluation
  decide(res), write_report(res, path)
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from contracts import fan as F
from forecaster import fan_cond_ema as ce
from forecaster import fan_direction as fd
from forecaster import fan_harness as fh
from forecaster import fan_im_direction as im
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

VERSION = "fan_first_hit_v1"
HORIZON = 15
BARRIER_K = 1.0
DOWN, NEITHER, UP, AMBIGUOUS = 0, 1, 2, 3
CLASSES = ("down", "neither", "up")
ARMS = ("freq", "own", "intermarket")
SYMMETRIC = ("own_sym", "intermarket_sym")     # the same with P(up) and P(down) averaged: no direction
GBM = {"learning_rate": 0.05, "min_samples_leaf": 2000, "l2_regularization": 1.0, "max_bins": 64}
GRID = ce.GRID
SHRINK = ce.SHRINK
VALID_SHARE = ce.VALID_SHARE
TRAIN_EVERY = ce.TRAIN_EVERY
DECISION_INTERVAL = 0.975       # Bonferroni over the two hypotheses
ASSIGNMENTS = ("up_first", "down_first")

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "question": "Within 15 minutes, does NQ reach an upper barrier first, an equal lower barrier first, or neither - "
                "and do NQ's own context or the intermarket inputs predict it better than training frequencies?",
    "barrier": f"+-{BARRIER_K} x fan_rw_v2's walk-forward sigma of the log price to {HORIZON} minutes (known at "
               "issuance), around the log of the origin's last close",
    "path": "the active contract's 1-minute TRADES bars starting in the 15 minutes after the origin; the first minute "
            "whose high >= the upper or whose low <= the lower barrier decides; both in that minute = ambiguous; "
            "none = neither. Origins as the fan scores them (a price at both ends inside the trading day)",
    "ambiguous": "left out of training and selection; evaluated under both assignments (up first, down first); "
                 "their share reported. No finer data is stored",
    "predictors": {"A": "freq: the block's training class frequencies",
                   "B": "own: HistGradientBoostingClassifier on NQ ret1/ret5/ret15, vwap_dist, rv15, rv60, vol60, "
                        "phase (categorical)",
                   "C": "intermarket: B's inputs plus fan_im_dir_v1's intermarket set (14)"},
    "model": {"fixed": {**GBM, "loss": "log_loss"}, "grid": list(GRID), "shrink_to_freq": list(SHRINK),
              "freq_option": True, "train_every": TRAIN_EVERY},
    "selection": f"lowest mean session multiclass Brier on the validation sessions (the last {VALID_SHARE:.0%} of "
                 "training, models fitted on the sessions before them); refitted on every training session. No "
                 "activation interval: A is itself a fitted forecast and the out-of-sample blocks decide",
    "evaluation": {"blocks": "fan_intermarket_v2's three checks and its spent holdout in two blocks of 30 (150 "
                             "sessions, all development)", "origins": "every minute",
                   "score": "multiclass Brier, sum over the three classes of (p - outcome)^2, session means",
                   "uncertainty": {**F.BOOTSTRAP, "decision_interval": DECISION_INTERVAL},
                   "calibration": "reliability per class in deciles, non-ambiguous origins"},
    "hypotheses": {"H1": "B beats A: NQ's own context predicts first hits",
                   "H2": "C beats B and A: the intermarket inputs add (beating B alone could reflect B overfitting)"},
    "decision": {"pass": f"the hypothesis's differences (H1: B - A; H2: C - B and C - A) have their "
                         f"{DECISION_INTERVAL:.1%} session-level intervals wholly below zero under both ambiguity "
                         "assignments (Bonferroni over H1 and H2)",
                 "fail": "a mean difference is not below zero under either assignment",
                 "inconclusive": "otherwise",
                 "directional": "a passing model counts as directional only if it also beats its symmetric version "
                                "(P(up) and P(down) averaged) with the 95 % interval wholly below zero under both "
                                "assignments; otherwise its information is about whether a barrier is reached, i.e. "
                                "size",
                 "after_pass": "freeze, then fresh forward data before any probabilities are shown"},
    "display": "none: kept apart from the fan",
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Labels (pure)
# --------------------------------------------------------------------------

def labels(lp: np.ndarray, hi: np.ndarray, lo: np.ndarray, slots: np.ndarray, b: np.ndarray,
           h: int = HORIZON) -> np.ndarray:
    """Per origin slot (``slots``) the class: DOWN, NEITHER, UP or AMBIGUOUS. ``lp``: the log last close per slot;
    ``hi``, ``lo``: the log high and low of the bar starting in each slot (NaN without one); ``b``: each origin's
    barrier distance in log price. Only bars starting in slots t + 1 to t + h are read."""
    k = slots[:, None] + np.arange(1, h + 1)[None, :]
    k = np.minimum(k, DAY_SLOTS - 1)
    up_lvl = (lp[slots] + b)[:, None]
    dn_lvl = (lp[slots] - b)[:, None]
    H, L = hi[k], lo[k]
    up = np.nan_to_num(H, nan=-np.inf) >= up_lvl
    dn = np.nan_to_num(L, nan=np.inf) <= dn_lvl
    beyond = (slots[:, None] + np.arange(1, h + 1)[None, :]) >= DAY_SLOTS
    up &= ~beyond
    dn &= ~beyond
    anyhit = up | dn
    first = np.where(anyhit.any(axis=1), anyhit.argmax(axis=1), -1)
    r = np.arange(len(slots))
    fu = np.where(first >= 0, up[r, np.maximum(first, 0)], False)
    fd_ = np.where(first >= 0, dn[r, np.maximum(first, 0)], False)
    return np.select([first < 0, fu & fd_, fu, fd_], [NEITHER, AMBIGUOUS, UP, DOWN], NEITHER)


def session_rows(fr: fh.Frame, hl: Tuple[np.ndarray, np.ndarray], every: int = 1) -> Tuple[fh.Rows, np.ndarray]:
    """The session's 15-minute origins (as the fan scores them) and their classes."""
    rows = fh.frame_rows(fr, horizons=(HORIZON,), every=every)
    rows = rows.take(rows.horizon == HORIZON)
    b = BARRIER_K * np.sqrt(rows.var)
    return rows, labels(fr.log_price, hl[0], hl[1], rows.slot, b)


def outcome_matrix(y: np.ndarray, assignment: str) -> np.ndarray:
    """One-hot outcomes; AMBIGUOUS read as UP or DOWN by ``assignment``."""
    yy = np.where(y == AMBIGUOUS, UP if assignment == "up_first" else DOWN, y)
    return np.eye(3)[yy]


def brier(p: np.ndarray, O: np.ndarray) -> np.ndarray:
    return ((p - O) ** 2).sum(axis=1)


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

class Predictor:
    """freq, or lambda x a classifier + (1 - lambda) x freq."""

    def __init__(self, freq: np.ndarray, model=None, lam: float = 0.0) -> None:
        self.freq, self.model, self.lam = freq, model, lam

    def predict(self, X: Optional[np.ndarray], n: int) -> np.ndarray:
        base = np.tile(self.freq, (n, 1))
        if self.model is None or self.lam == 0:
            return base
        return self.lam * self.model.predict_proba(X) + (1 - self.lam) * base


def _clf(params: Dict[str, Any], n_cols: int) -> HistGradientBoostingClassifier:
    cat = np.zeros(n_cols, dtype=bool)
    cat[-1] = True
    return HistGradientBoostingClassifier(**GBM, **params, categorical_features=cat, early_stopping=False,
                                          random_state=0)


def _freq(y: np.ndarray) -> np.ndarray:
    y = y[y != AMBIGUOUS]
    return np.bincount(y, minlength=3) / len(y)


def _session_means(days: np.ndarray, x: np.ndarray) -> np.ndarray:
    order, inv = np.unique(days, return_inverse=True)
    return np.bincount(inv, weights=x) / np.bincount(inv)


def _gather(frames, table, hl, days, every, arm):
    parts = [session_rows(frames[d], hl[d], every) for d in days]
    rows = fh.Rows.concat([p[0] for p in parts])
    y = np.concatenate([p[1] for p in parts])
    return rows, y, im.design(table, rows, arm)


def fit_arm(frames, table, hl, train_days: Sequence[str], arm: str) -> Tuple[Predictor, Dict[str, Any]]:
    """Selection on the validation sessions, then the refit on every training session."""
    train_days = sorted(train_days)
    cut = int(round(len(train_days) * (1 - VALID_SHARE)))
    rf, yf, Xf = _gather(frames, table, hl, train_days[:cut], TRAIN_EVERY, arm)
    rv, yv, Xv = _gather(frames, table, hl, train_days[cut:], TRAIN_EVERY, arm)
    kf, kv = yf != AMBIGUOUS, yv != AMBIGUOUS
    freq_f = _freq(yf)
    Ov = np.eye(3)[yv[kv]]
    dv = rv.session.astype(str)[kv]
    score = lambda p: float(_session_means(dv, brier(p, Ov)).mean())
    options = [{"params": None, "lam": 0.0, "brier": score(np.tile(freq_f, (kv.sum(), 1)))}]
    for g in GRID:
        m = _clf(g, Xf.shape[1]).fit(Xf[kf], yf[kf])
        pm = m.predict_proba(Xv[kv])
        for lam in SHRINK:
            options.append({"params": dict(g), "lam": lam,
                            "brier": score(lam * pm + (1 - lam) * freq_f)})
    best = int(np.argmin([o["brier"] for o in options]))
    ra, ya, Xa = _gather(frames, table, hl, train_days, TRAIN_EVERY, arm)
    ka = ya != AMBIGUOUS
    freq = _freq(ya)
    o = options[best]
    model = _clf(o["params"], Xa.shape[1]).fit(Xa[ka], ya[ka]) if o["params"] else None
    return Predictor(freq, model, o["lam"]), {"options": options, "best": best, "chosen": o,
                                              "valid_sessions": len(train_days) - cut,
                                              "freq": freq.tolist()}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _sym(p: np.ndarray) -> np.ndarray:
    q = p.copy()
    q[:, DOWN] = q[:, UP] = (p[:, DOWN] + p[:, UP]) / 2
    return q


def score_session(rows: fh.Rows, y: np.ndarray, probs: Dict[str, np.ndarray], rel: Dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {"n": int(len(y)), "ambiguous": int((y == AMBIGUOUS).sum()),
                           "counts": np.bincount(y, minlength=4).tolist()}
    for a in ASSIGNMENTS:
        O = outcome_matrix(y, a)
        out[a] = {arm: float(brier(p, O).mean()) for arm, p in probs.items()}
    phase = PHASE_OF_SLOT[rows.slot]
    out["phase"] = {}
    O = outcome_matrix(y, "up_first")
    for p_i, (name, _, _) in enumerate(PHASES):
        s = phase == p_i
        if s.any():
            out["phase"][name] = {arm: [float(brier(p[s], O[s]).mean()), int(s.sum())] for arm, p in probs.items()}
    k = y != AMBIGUOUS
    for arm, p in probs.items():
        for c in range(3):
            t = rel.setdefault((arm, c), np.zeros((10, 3)))
            b = np.clip((p[k, c] * 10).astype(int), 0, 9)
            np.add.at(t, b, np.column_stack([np.ones(k.sum()), p[k, c], (y[k] == c).astype(float)]))
    return out


def _paired(per: Sequence[Dict], a: str, b: str, assignment: str, level: float = 0.95) -> Dict[str, Any]:
    d = [r[assignment][a] - r[assignment][b] for r in per]
    base = float(np.mean([r[assignment][b] for r in per]))
    bs = F.BOOTSTRAP
    iv = block_bootstrap(d, bs["block_sessions"], bs["resamples"], bs["seed"], level)
    return {"sessions": len(d), "diff": float(np.mean(d)), "share": float(np.mean(d) / base) if base else None,
            "interval": list(iv) if iv else None, "level": level}


PAIRS = (("own", "freq"), ("intermarket", "own"), ("intermarket", "freq"), ("own", "own_sym"),
         ("intermarket", "intermarket_sym"))


def summarise(per: Sequence[Dict], rel: Dict) -> Dict[str, Any]:
    n = sum(r["n"] for r in per)
    counts = np.sum([r["counts"] for r in per], axis=0)
    out: Dict[str, Any] = {"origins": n, "ambiguous_share": float(counts[AMBIGUOUS] / n) if n else 0.0,
                           "class_share": {c: float(counts[i] / n) for i, c in enumerate([*CLASSES, "ambiguous"])}}
    for a in ASSIGNMENTS:
        out[a] = {"brier": {arm: float(np.mean([r[a][arm] for r in per])) for arm in per[0][a]},
                  "comparisons": {f"{x}-{y}": _paired(per, x, y, a) for x, y in PAIRS},
                  "decision": {f"{x}-{y}": _paired(per, x, y, a, DECISION_INTERVAL) for x, y in PAIRS[:3]}}
    out["reliability"] = {f"{arm}|{CLASSES[c]}": [{"p": t[1] / t[0], "freq": t[2] / t[0], "n": int(t[0])}
                                                  for t in tab if t[0] > 0] for (arm, c), tab in rel.items()}
    phases = sorted({p for r in per for p in r["phase"]}, key=lambda x: [q[0] for q in PHASES].index(x))
    out["phase"] = {}
    for ph in phases:
        rs = [r["phase"][ph] for r in per if ph in r["phase"]]
        d = [x["own"][0] - x["freq"][0] for x in rs]
        e = [x["intermarket"][0] - x["own"][0] for x in rs]
        bs = F.BOOTSTRAP
        out["phase"][ph] = {"origins": int(sum(x["freq"][1] for x in rs)),
                            "own-freq": [float(np.mean(d)), block_bootstrap(d, bs["block_sessions"], bs["resamples"],
                                                                            bs["seed"], 0.95)],
                            "intermarket-own": [float(np.mean(e)), block_bootstrap(e, bs["block_sessions"],
                                                                                   bs["resamples"], bs["seed"], 0.95)]}
    return out


def run(frames: Dict[str, fh.Frame], table: FeatureTable, hl: Dict[str, Tuple[np.ndarray, np.ndarray]],
        blks: Sequence[Dict[str, Any]], sessions: Sequence[str], log: Callable[[str], None] = lambda s: None
        ) -> Dict[str, Any]:
    pooled, days_all, rel_all, out_blocks = [], [], {}, []
    for b in blks:
        train = [d for d in sessions if d < b["first"] and d in frames and d in hl]
        test = [d for d in sessions if b["first"] <= d <= b["last"] and d in frames and d in hl]
        fitted = {arm: fit_arm(frames, table, hl, train, arm) for arm in ("own", "intermarket")}
        freq = fitted["own"][0].freq
        per, rel = [], {}
        for d in test:
            rows, y = session_rows(frames[d], hl[d], 1)
            probs = {"freq": np.tile(freq, (len(y), 1))}
            for arm, (pred, _) in fitted.items():
                probs[arm] = pred.predict(im.design(table, rows, arm), len(y))
                probs[f"{arm}_sym"] = _sym(probs[arm])
            per.append(score_session(rows, y, probs, rel))
        for k, t in rel.items():
            rel_all[k] = rel_all.get(k, 0) + t
        pooled += per
        days_all += test
        out_blocks.append({**b, "train_sessions": len(train), "scored": len(test),
                           "selection": {arm: info for arm, (_, info) in fitted.items()},
                           "results": summarise(per, rel)})
        ch = {arm: ("freq" if not info["chosen"]["params"] else
                    f"depth {info['chosen']['params']['max_depth']}/{info['chosen']['params']['max_iter']} "
                    f"lambda {info['chosen']['lam']}") for arm, (_, info) in fitted.items()}
        log(f"{b['block']}: trained on {len(train)} sessions, scored {len(test)}; chosen {ch}")
    res = {"kind": "first_hit", "version": VERSION, "definition": DEFINITION, "definition_hash": definition_hash(),
           "blocks": out_blocks, "sessions": days_all, "results": summarise(pooled, rel_all),
           "per_session": {d: {a: r[a] for a in ASSIGNMENTS} for d, r in zip(days_all, pooled)}}
    res["conclusion"] = decide(res)
    return res


def decide(res: Dict[str, Any]) -> Dict[str, Any]:
    r = res["results"]
    out = {}
    for h, keys, sym in (("H1", ("own-freq",), "own-own_sym"),
                         ("H2", ("intermarket-own", "intermarket-freq"), "intermarket-intermarket_sym")):
        ivs = [r[a]["decision"][k]["interval"] for a in ASSIGNMENTS for k in keys]
        means = [r[a]["decision"][k]["diff"] for a in ASSIGNMENTS for k in keys]
        if all(iv and iv[1] < 0 for iv in ivs):
            v = "pass"
        elif any(m >= 0 for m in means):
            v = "fail"
        else:
            v = "inconclusive"
        sy = [r[a]["comparisons"][sym]["interval"] for a in ASSIGNMENTS]
        directional = all(iv and iv[1] < 0 for iv in sy)
        out[h] = {"verdict": v, "directional": directional if v == "pass" else None}
    return out


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def _c(c: Optional[Dict[str, Any]]) -> str:
    if not c:
        return "-"
    iv = c["interval"]
    return f"{c['diff']:+.6f} ({100 * c['share']:+.3f} %)" + (f" [{iv[0]:+.6f}, {iv[1]:+.6f}]" if iv else "")


def write_report(res: Dict[str, Any], path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = res["results"]
    con = res["conclusion"]
    L = [f"# First-hit probabilities: {res['version']}, {res.get('target', 'NQ')}", "",
         f"Definition `{res['definition_hash']}` (docs/fan_first_hit.md), fixed before this run; code "
         f"{res.get('code_revision', '?')[:12]}. {len(res['sessions'])} sessions in {len(res['blocks'])} chronological "
         f"blocks, every origin ({r['origins']:,}); all development.", "",
         f"**H1** (NQ's own context beats training frequencies): **{con['H1']['verdict'].upper()}**"
         + (f", directional: {con['H1']['directional']}" if con['H1']['directional'] is not None else "") + ". "
         f"**H2** (intermarket adds to own): **{con['H2']['verdict'].upper()}**"
         + (f", directional: {con['H2']['directional']}" if con['H2']['directional'] is not None else "") + ".", "",
         f"Barrier +-{BARRIER_K} x fan_rw_v2's 15-minute sigma. Outcomes: "
         + ", ".join(f"{k} {100 * v:.1f} %" for k, v in r["class_share"].items())
         + ". Multiclass Brier (lower is better), session means; differences first minus second with the 95 % "
         "moving-block interval (decision 97.5 %).", "",
         "| Ambiguous read as | freq | own | intermarket | own, symmetric | intermarket, symmetric |",
         "|---|---|---|---|---|---|"]
    for a in ASSIGNMENTS:
        bb = r[a]["brier"]
        L.append(f"| {a.replace('_', ' ')} | {bb['freq']:.5f} | {bb['own']:.5f} | {bb['intermarket']:.5f} | "
                 f"{bb['own_sym']:.5f} | {bb['intermarket_sym']:.5f} |")
    L += ["", "| Comparison | Ambiguous up first | Ambiguous down first | Decision (97.5 %), up / down first |",
          "|---|---|---|---|"]
    for x, y in PAIRS:
        k = f"{x}-{y}"
        dec = " / ".join(_c(r[a]["decision"].get(k)).split(" (")[0] + (
            f" [{r[a]['decision'][k]['interval'][0]:+.6f}, {r[a]['decision'][k]['interval'][1]:+.6f}]"
            if r[a]["decision"].get(k) and r[a]["decision"][k]["interval"] else "") for a in ASSIGNMENTS) \
            if k in r["up_first"]["decision"] else "-"
        L.append(f"| {x} - {y} | {_c(r['up_first']['comparisons'][k])} | {_c(r['down_first']['comparisons'][k])} | {dec} |")
    L += ["", "## Per block (ambiguous read as up first)", "",
          "| Block | Trained on | own chosen | intermarket chosen | own - freq | intermarket - own | own - own symmetric |",
          "|---|---|---|---|---|---|---|"]
    for b in res["blocks"]:
        cs = b["results"]["up_first"]["comparisons"]
        ch = {arm: ("freq" if not s["chosen"]["params"] else
                    f"d{s['chosen']['params']['max_depth']}/{s['chosen']['params']['max_iter']}, lambda {s['chosen']['lam']}")
              for arm, s in b["selection"].items()}
        L.append(f"| {b['block']} ({b['first']} to {b['last']}) | {b['train_sessions']} | {ch['own']} | "
                 f"{ch['intermarket']} | {_c(cs['own-freq']).split(' [')[0]} | {_c(cs['intermarket-own']).split(' [')[0]} | "
                 f"{_c(cs['own-own_sym']).split(' [')[0]} |")
    L += ["", "## By phase (ambiguous read as up first; explanation only)", "",
          "| Phase | Origins | own - freq | intermarket - own |", "|---|---|---|---|"]
    fmt = lambda v: f"{v[0]:+.6f}" + (f" [{v[1][0]:+.6f}, {v[1][1]:+.6f}]" if v[1] else "")
    for ph, v in r["phase"].items():
        L.append(f"| {ph} | {v['origins']:,} | {fmt(v['own-freq'])} | {fmt(v['intermarket-own'])} |")
    L += ["", "## Reliability (non-ambiguous origins)", ""]
    for k, rows in r["reliability"].items():
        if k.startswith("freq"):
            continue
        L.append(f"- {k}: " + "; ".join(f"{x['p']:.2f} -> {x['freq']:.3f} ({x['n']:,})" for x in rows))
    L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path


# --------------------------------------------------------------------------
# The store: each minute's high and low
# --------------------------------------------------------------------------

_HL = """
    SELECT b.trading_day, b.timestamp_utc, b.high, b.low, b.close
      FROM bars b
      JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
     WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES' AND b.trading_day BETWEEN %s AND %s
     ORDER BY b.timestamp_utc;"""


def load_high_low(conn, symbol: str, sessions: Sequence[str], frames: Dict[str, fh.Frame]
                  ) -> Tuple[Dict[str, Tuple[np.ndarray, np.ndarray]], Dict[str, int]]:
    """Per session the log high and log low of the active contract's bar starting in each slot (NaN without one), and
    a check: bars whose close differs from the frame's last price at their slot (should be none)."""
    from datetime import date
    from forecaster.fan_benchmark import slot_at
    from forecaster.fan_data import _utc
    out, mismatched, bars = {}, 0, 0
    rows = conn.execute(_HL, (symbol, sessions[0], sessions[-1])).fetchall()
    by: Dict[str, List] = {}
    for d, ts, h, l, c in rows:
        by.setdefault(str(d)[:10], []).append((ts, float(h), float(l), float(c)))
    for d in sessions:
        if d not in by or d not in frames:
            continue
        hi, lo = np.full(DAY_SLOTS, np.nan), np.full(DAY_SLOTS, np.nan)
        lp = frames[d].log_price
        for ts, h, l, c in by[d]:
            s = slot_at(date.fromisoformat(d), _utc(ts))
            if 0 <= s < DAY_SLOTS:
                hi[s], lo[s] = np.log(h), np.log(l)
                bars += 1
                mismatched += int(not np.isclose(np.log(c), lp[s], rtol=0, atol=1e-12))
        out[d] = (hi, lo)
    return out, {"bars": bars, "close_mismatch": mismatched}
