# forecaster/fan_im_direction.py
"""
Intermarket Direction (docs/fan_im_direction.md): do recent moves of the related index
futures - ES and RTY - and NQ's performance relative to ES add information about NQ's next 5
and 15 minutes beyond NQ's own recent prices? The relationship may be catch-up, continued
divergence or nothing; every arm may end at zero.

The machinery is Conditional EMA Direction's (forecaster/fan_cond_ema.py) unchanged - the
shared fan per block, the model family, tuning budget, selection, activation (99.58 %), the
evaluation blocks and the decision rule - with other arms:

  A  zero          no shift
  B  own           NQ's own context: 1-, 5-, 15-minute returns, VWAP distance, rv15, rv60,
                   vol60, phase (Conditional EMA Direction's context arm)
  C  intermarket   B plus INTERMARKET below

Every intermarket input is read from the point-in-time panel at the origin: the last close
of each market known at the end of the minute. In the stored history the three markets' bars
are aligned (NQ's 1-minute return correlates 0.94 with ES's at lag 0 and at most 0.03 at one
minute's lead or lag); a live feed delayed differently per market would not be.

  im_features(close, age)          sessions x 1440 per feature (pure, point in time)
  im_table(panel, target, base)    the base table with NQ's own context and these added
  SPEC                             the experiment for fan_cond_ema.run
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np
import pandas as pd

from contracts import fan as F
from forecaster import fan_cond_ema as ce
from forecaster import fan_harness as fh
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

VERSION = "fan_im_dir_v1"
GROUP = "im_direction"
PEERS = ("ES", "RTY")
RET_WINDOWS = (1, 5, 15)
REL_WINDOWS = (5, 15)
BETA_SESSIONS = 20              # NQ's beta to ES: OLS over the 1-minute returns of the 20 sessions before
BETA_MIN_SESSIONS = 10
CORR_MINUTES = 60
PEER_BASE = ("vol60", "rv15")   # forecaster/fan_features.py: relative volume, volatility over usual
REL_EDGE = "rel_es15"           # the diagnostic's divergence: |NQ's 15-minute move beyond beta x ES's|
INTERMARKET = ([f"{p}.ret{w}" for p in PEERS for w in RET_WINDOWS]
               + [f"NQ.rel_es{w}" for w in REL_WINDOWS] + ["NQ.comove15", "NQ.corr_es60"]
               + [f"{p}.{k}" for p in PEERS for k in PEER_BASE])

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "question": "Do ES and RTY moves and NQ's move relative to ES add directional information beyond NQ's own "
                "recent prices, at NQ 5 and 15 minutes?",
    "machinery": f"forecaster/fan_cond_ema.py as defined by {ce.VERSION} ({ce.definition_hash()}): the shared fan, "
                 "model family, tuning budget, selection on validation CRPS with zero as an option, activation at "
                 f"{ce.ACTIVATION_INTERVAL:.2%} (95 % Bonferroni over the 12 options), evaluation blocks, decision "
                 f"rule at {ce.DECISION_INTERVAL:.1%}",
    "arms": {"A": "zero", "B": "own: NQ ret1/ret5/ret15, vwap_dist, rv15, rv60, vol60, phase",
             "C": "intermarket: B plus the intermarket set"},
    "features": {
        "grid": "the target's 1440 one-minute slots from 18:00 ET, sessions end to end; each market's last close "
                "known at the end of the minute (the panel), carried, never filled backwards",
        "sd60": "per market, the RMS of its last 60 one-minute log returns on the grid",
        "peer_ret_w": f"ES and RTY log return over the last w minutes over its own sd60 x sqrt(w), w = {list(RET_WINDOWS)}",
        "beta": f"per session, the OLS slope of NQ's on ES's 1-minute log returns over the {BETA_SESSIONS} sessions "
                f"before (minutes where both closed a bar); missing with fewer than {BETA_MIN_SESSIONS}",
        "rel_es_w": f"(NQ's w-minute return - beta x ES's) over NQ's sd60 x sqrt(w), w = {list(REL_WINDOWS)}",
        "comove15": "the sign of NQ's 15-minute return where it equals ES's sign, else 0 (diverging)",
        "corr_es60": f"the correlation of NQ's and ES's 1-minute returns over the last {CORR_MINUTES} minutes",
        "peer_base": f"ES and RTY {list(PEER_BASE)}: relative volume and volatility over usual",
        "clip": ce.CLIP,
        "set": INTERMARKET,
        "missing": "RTY before its first complete session (2025-09-18) and every warm-up is missing, read as missing",
    },
    "decision": {"pass": "at a horizon, C - A and C - B both have their 97.5 % interval wholly below zero; either horizon",
                 "fail": "at both horizons C's mean CRPS is not below both A's and B's: no incremental intermarket "
                         "benefit",
                 "inconclusive": "otherwise",
                 "after_pass": "freeze, then fresh timely forward data on synchronised feeds before any overlay"},
    "diagnostics": {"phase": "the origin's phase", "comove15": "together up / together down / diverging / missing",
                    "divergence": f"terciles of |{REL_EDGE}| with boundaries from the block's training rows",
                    "use": "explanation only - never a deployment rule"},
}


def definition_hash() -> str:
    return ce.Spec.definition_hash(SPEC)


# --------------------------------------------------------------------------
# Features (pure)
# --------------------------------------------------------------------------

def _flat(close: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return pd.Series(np.log(close.reshape(-1))).ffill().to_numpy().copy()


def _sd(lp: np.ndarray) -> np.ndarray:
    r = np.nan_to_num(np.diff(lp, prepend=np.nan))
    C = np.concatenate([[0.0], np.cumsum(r * r)])
    i = np.arange(len(lp))
    lo = np.maximum(0, i - ce.VOL_MINUTES + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = np.sqrt((C[i + 1] - C[lo]) / (i + 1 - lo))
    return np.where((sd > 0) & (i >= ce.VOL_MINUTES - 1), sd, np.nan)


def _beta(r_nq: np.ndarray, r_es: np.ndarray, both: np.ndarray) -> np.ndarray:
    """Per session, OLS slope of NQ's on ES's 1-minute returns over the BETA_SESSIONS sessions before."""
    n = r_nq.shape[0]
    sxy = np.where(both, r_nq * r_es, 0).sum(axis=1)
    sxx = np.where(both, r_es * r_es, 0).sum(axis=1)
    has = both.sum(axis=1) > 0
    out = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - BETA_SESSIONS)
        k = has[lo:i]
        if k.sum() >= BETA_MIN_SESSIONS and sxx[lo:i][k].sum() > 0:
            out[i] = sxy[lo:i][k].sum() / sxx[lo:i][k].sum()
    return out


def im_features(close: Dict[str, np.ndarray], age: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """``{'ES.ret1': ..., 'NQ.rel_es5': ...}``, sessions x 1440 each, from NQ's, ES's and RTY's panel rows."""
    n = close["NQ"].shape[0]
    lp = {s: _flat(close[s]) for s in ("NQ", *PEERS)}
    sd = {s: _sd(lp[s]) for s in lp}
    lag = lambda x, k: ce._lag(x, k)
    out: Dict[str, np.ndarray] = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        for p in PEERS:
            for w in RET_WINDOWS:
                out[f"{p}.ret{w}"] = (lp[p] - lag(lp[p], w)) / (sd[p] * np.sqrt(w))
        r = {s: np.diff(lp[s], prepend=np.nan) for s in ("NQ", "ES")}
        new = {s: (age[s].reshape(-1) == 0) & np.isfinite(close[s].reshape(-1)) for s in ("NQ", "ES")}
        both = (new["NQ"] & new["ES"] & np.isfinite(r["NQ"]) & np.isfinite(r["ES"])).reshape(n, DAY_SLOTS)
        beta = np.repeat(_beta(np.nan_to_num(r["NQ"]).reshape(n, DAY_SLOTS),
                               np.nan_to_num(r["ES"]).reshape(n, DAY_SLOTS), both), DAY_SLOTS)
        for w in REL_WINDOWS:
            m_nq, m_es = lp["NQ"] - lag(lp["NQ"], w), lp["ES"] - lag(lp["ES"], w)
            out[f"NQ.rel_es{w}"] = (m_nq - beta * m_es) / (sd["NQ"] * np.sqrt(w))
        s_nq = np.sign(lp["NQ"] - lag(lp["NQ"], 15))
        s_es = np.sign(lp["ES"] - lag(lp["ES"], 15))
        out["NQ.comove15"] = np.where(np.isfinite(s_nq) & np.isfinite(s_es), np.where(s_nq == s_es, s_nq, 0.0), np.nan)
        x, y = np.nan_to_num(r["NQ"]), np.nan_to_num(r["ES"])
        cs = lambda a: np.concatenate([[0.0], np.cumsum(a)])
        i = np.arange(len(x))
        lo = np.maximum(0, i - CORR_MINUTES + 1)
        win = lambda a: (cs(a)[i + 1] - cs(a)[lo]) / (i + 1 - lo)
        cov = win(x * y) - win(x) * win(y)
        vx, vy = win(x * x) - win(x) ** 2, win(y * y) - win(y) ** 2
        corr = cov / np.sqrt(vx * vy)
        out["NQ.corr_es60"] = np.where((i >= CORR_MINUTES - 1) & (vx > 0) & (vy > 0), corr, np.nan)
    known = {s: np.isfinite(close[s].reshape(-1)) for s in lp}
    res = {}
    for k, a in out.items():
        a = np.clip(a, -ce.CLIP, ce.CLIP)
        owner = k.split(".")[0]
        ok = known["NQ"] & (known["ES"] if owner == "NQ" else known[owner])    # NQ's relative features need ES
        res[k] = np.where(ok, a, np.nan).reshape(n, DAY_SLOTS)
    return res


def im_table(panel, target: str, base: FeatureTable) -> FeatureTable:
    """``base`` with NQ's own context (forecaster/fan_cond_ema.py) and the intermarket features added."""
    if target != "NQ":
        raise ValueError("the intermarket direction experiment is defined for NQ")
    if list(panel.sessions) != list(base.sessions):
        raise ValueError("the panel and the feature table hold different sessions")
    idx = {s: panel.symbols.index(s) for s in ("NQ", *PEERS)}
    own = ce.features(panel.close[:, idx["NQ"]], panel.age[:, idx["NQ"]], panel.volume[:, idx["NQ"]])
    im = im_features({s: panel.close[:, j] for s, j in idx.items()}, {s: panel.age[:, j] for s, j in idx.items()})
    extra = [Feature(f"NQ.{k}", "NQ", GROUP, k) for k in ce.OWN_CONTEXT]
    extra += [Feature(k, k.split(".")[0], GROUP, k.split(".")[1]) for k in im]
    cols = [own[f.family] for f in extra[:len(ce.OWN_CONTEXT)]] + list(im.values())
    X = np.concatenate([base.X, np.stack(cols, axis=-1).astype(np.float32)], axis=-1)
    return FeatureTable(base.sessions, target, base.features + extra, X)


def inputs(arm: str) -> List[str]:
    own = [f"NQ.{k}" for k in (*ce.OWN_CONTEXT, *ce.BASE_CONTEXT)]
    return own + (INTERMARKET if arm == "intermarket" else [])


def design(table: FeatureTable, rows: fh.Rows, arm: str) -> np.ndarray:
    """The arm's inputs for ``rows``, then the phase as its last (categorical) column."""
    names = inputs(arm)
    X, feats = table.matrix(rows, lambda f: f.name in set(names))
    got = [f.name for f in feats]
    return np.column_stack([X[:, got.index(n)].astype(float) for n in names]
                           + [PHASE_OF_SLOT[rows.slot].astype(float)])


def _named(table: FeatureTable, rows: fh.Rows, name: str) -> np.ndarray:
    X, feats = table.matrix(rows, lambda f: f.name == name)
    return X[:, [f.name for f in feats].index(name)].astype(float)


def buckets(table: FeatureTable, rows: fh.Rows, edges: Sequence[float]) -> Dict[str, np.ndarray]:
    phase = np.array([PHASES[p][0] for p in PHASE_OF_SLOT[rows.slot]])
    cm = _named(table, rows, "NQ.comove15")
    comove = np.where(np.isnan(cm), "missing", np.where(cm > 0, "together up",
                                                        np.where(cm < 0, "together down", "diverging")))
    rel = np.abs(_named(table, rows, f"NQ.{REL_EDGE}"))
    divergence = np.where(np.isnan(rel), "missing",
                          np.where(rel <= edges[0], "low", np.where(rel <= edges[1], "mid", "high")))
    return {"phase": phase, "comove15": comove, "divergence": divergence}


SPEC = ce.Spec(VERSION, "im_direction", DEFINITION, "own", "intermarket", design, buckets, REL_EDGE,
               "Intermarket Direction", "docs/fan_im_direction.md",
               "Arms: A `zero`, B `own` (NQ's returns, VWAP distance, volatility, relative volume, phase), C "
               "`intermarket` (B plus ES and RTY returns, NQ relative to ES, co-movement, ES and RTY volume and "
               "volatility).", "divergence terciles of |NQ.rel_es15| from each block's training rows.")
