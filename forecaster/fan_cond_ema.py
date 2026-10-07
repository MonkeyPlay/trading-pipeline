# forecaster/fan_cond_ema.py
"""
Conditional EMA Direction (docs/fan_cond_ema.md): do EMA slopes, alignment and price
extension, read jointly with timeframe, volatility, volume and session phase, add
information about NQ's next 5 and 15 minutes beyond recent returns and VWAP position?
The relationship may be continuation, reversal or nothing; every arm may end at zero.

Three arms on one shared fan per block - the width (lin_pois_ivx's multiplier of
fan_rw_v2's sigma) and v2's shape, every fit from sessions before the block:

  A  zero       no shift: the current centred fan
  B  context    shallow gradient boosting of the forward move on CONTEXT features
  C  ema        the same family and tuning budget on CONTEXT + EMA features

B and C predict the signed forward log return in the shared fan's sigmas (winsorised at
+-Z_WINSOR); the shift is mu = lambda x clip(prediction, +-Z_CAP) x sigma. Per horizon and
block, every GRID x SHRINK option and exactly zero are scored on the validation sessions
(the last VALID_SHARE of the training sessions, models and width fitted on the sessions
before them) by the shifted fan's CRPS; the best is activated only when its session-level
paired interval against zero (ACTIVATION_INTERVAL, moving blocks) lies wholly below zero -
otherwise the arm is zero. An activated option is refitted on every training session.

DEFINITION holds every choice, fixed before any result; definition_hash() is recorded
with every run. A changed choice is a new version.

  features(close, age, volume)       sessions x 1440 per feature (pure, point in time)
  emas(lp, new_bar)                  the four EMAs on the flat minute grid (pure)
  cond_table(panel, target, base)    the base feature table with these added
  select(...) / fit_block(...)       tuning, activation, refit
  run(frames, table, blocks, ...)    the rolling-origin evaluation
  decide(res)                        pass / inconclusive / fail by the declared rule
  write_report(res, path)
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from contracts import fan as F
from forecaster import fan_direction as fd
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

VERSION = "fan_cond_ema_v1"
GROUP = "cond_ema"
HORIZONS = (5, 15)
PRIMARY = 15
ARMS = ("zero", "context", "ema")
EMA_PERIODS = (14, 100)
BAR5 = 5
WARMUP_BARS = 3                 # an EMA is missing until it has seen 3 x its period of bars of its timeframe
SLOPE_MINUTES = 15              # every slope: the EMA's change over the last 15 elapsed minutes
VOL_MINUTES = 60                # trailing volatility: RMS of the last 60 one-minute log returns
UNIT_MINUTES = 15               # distances and gaps in sd60 x sqrt(15); returns over w minutes in sd60 x sqrt(w)
CLIP = 10.0
OWN_CONTEXT = ("ret1", "ret5", "ret15", "vwap_dist")
BASE_CONTEXT = ("rv15", "rv60", "vol60")          # forecaster/fan_features.py: volatility and volume over usual
EMA_FEATURES = tuple(f"{tf}_{k}" for tf in ("m1", "m5")
                     for k in ("slope14", "slope100", "dist14", "dist100", "gap")) + ("tf_agree",)
GBM = {"learning_rate": 0.05, "min_samples_leaf": 2000, "l2_regularization": 1.0, "max_bins": 64,
       "loss": "squared_error"}
GRID = tuple({"max_depth": d, "max_iter": n} for d in (2, 3) for n in (50, 150))
SHRINK = (0.25, 0.5, 1.0)
Z_WINSOR = 4.0
Z_CAP = 1.0
VALID_SHARE = 0.2
TRAIN_EVERY = 5
ACTIVATION_INTERVAL = 1 - 0.05 / (len(GRID) * len(SHRINK))   # 95 %, Bonferroni over the 12 options searched
DECISION_INTERVAL = 0.975       # Bonferroni: either horizon may qualify, so each is tested at 2.5 %
EXTENSION = "m1_dist100"        # the diagnostic's price extension: |distance from the 1-minute EMA 100|

DEFINITION: Dict[str, Any] = {
    "version": VERSION,
    "question": "Do EMA features add directional information beyond context, at NQ 5 and 15 minutes?",
    "width": "per block: lin_pois_ivx refitted on the sessions before the block (validation: before the validation "
             "sessions), times fan_rw_v2's walk-forward sigma; v2's shape. The frozen width model is not used.",
    "features": {
        "grid": "the target's 1440 one-minute slots from 18:00 ET, sessions laid end to end in date order",
        "price": "log of the panel's last close known at the end of each minute (carried, never filled backwards)",
        "ema": "Pine ta.ema, alpha = 2 / (period + 1), seeded with the first value, on log price; periods "
               f"{list(EMA_PERIODS)}",
        "m1": "updated only at minutes where a 1-minute bar closed (panel age 0), carried between",
        "m5": f"{BAR5}-minute bars aligned to 18:00 ET; a bar completes at the end of its last minute and exists when "
              "a 1-minute bar closed inside it; its close is the last close known then; updated at completion, "
              "carried until the next completed bar - never from a bar still forming",
        "warm_up": f"missing until the EMA has seen {WARMUP_BARS} x its period of its own bars",
        "sd60": f"sqrt(mean of the last {VOL_MINUTES} squared one-minute log returns of the grid); missing when zero",
        "slope": f"EMA_t - EMA_(t-{SLOPE_MINUTES} min), over sd60 x sqrt({SLOPE_MINUTES})",
        "dist": f"log price - EMA, over sd60 x sqrt({UNIT_MINUTES})",
        "gap": f"EMA 14 - EMA 100 of the timeframe, over sd60 x sqrt({UNIT_MINUTES})",
        "tf_agree": "sign of m1_gap where it equals the sign of m5_gap, else 0",
        "ret_w": "log return over the last w minutes over sd60 x sqrt(w), w = 1, 5, 15",
        "vwap_dist": f"log price - log session VWAP (closes x volume from 18:00 ET), over sd60 x sqrt({UNIT_MINUTES})",
        "context_base": "rv15, rv60 (realised variance over usual for the time of day), vol60 (relative volume)",
        "phase": "the origin's session phase, a categorical input",
        "clip": CLIP,
        "context": [*OWN_CONTEXT, *BASE_CONTEXT, "phase"],
        "ema_set": list(EMA_FEATURES),
    },
    "model": {"family": "HistGradientBoostingRegressor, one per horizon and arm", "fixed": GBM, "grid": list(GRID),
              "shrink": list(SHRINK), "zero_option": True, "target": f"y / sigma winsorised at +-{Z_WINSOR}",
              "shift": f"lambda x clip(prediction, +-{Z_CAP}) x sigma", "train_every": TRAIN_EVERY},
    "selection": {"validation": f"the last {VALID_SHARE:.0%} of the block's training sessions",
                  "loss": "mean over validation sessions of the session's mean CRPS of the shifted fan",
                  "activation": f"the best option is used only if its paired session-level {ACTIVATION_INTERVAL:.2%} "
                                "moving-block interval against zero shift (95 %, Bonferroni over the options searched) "
                                "lies wholly below zero; otherwise zero",
                  "refit": "an activated option is refitted on every training session (width likewise)"},
    "labels": "outcomes end inside their own session (t + h before the day's end); sessions are split whole, so no "
              "training label reaches into validation or evaluation",
    "evaluation": {"blocks": "the intermarket experiment's three checks, then its spent holdout in two blocks of 30",
                   "sessions": "all previously examined: development", "origins": "every minute",
                   "primary": f"CRPS at {PRIMARY} min", "secondary": "CRPS at 5 min",
                   "uncertainty": {**F.BOOTSTRAP, "decision_interval": DECISION_INTERVAL}},
    "decision": {
        "pass": f"at a horizon, C - A and C - B both have their {DECISION_INTERVAL:.1%} interval wholly below zero "
                "(Bonferroni over the two horizons); the experiment passes if either horizon does",
        "fail": "at both horizons C's mean CRPS is not below both A's and B's: no incremental EMA benefit",
        "inconclusive": "otherwise",
        "after_pass": "freeze the full specification and evaluate on fresh, timely forward data before any overlay",
    },
    "diagnostics": {"phase": "the origin's phase", "tf_agree": "-1 / 0 / +1 / missing",
                    "extension": f"terciles of |{EXTENSION}| with boundaries from the block's training rows",
                    "use": "explanation only - never a deployment rule"},
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Features (pure)
# --------------------------------------------------------------------------

def _ema_events(values: np.ndarray, events: np.ndarray, period: int) -> np.ndarray:
    """An EMA updated only where ``events``, carried between; NaN until WARMUP_BARS x period events."""
    idx = np.flatnonzero(events & np.isfinite(values))
    out = np.full(len(values), np.nan)
    if not len(idx):
        return out
    e = pd.Series(values[idx]).ewm(span=period, adjust=False).mean().to_numpy().copy()
    e[:WARMUP_BARS * period - 1] = np.nan
    out[idx] = e
    return pd.Series(out).ffill().to_numpy().copy() if np.isfinite(e).any() else out


def emas(lp: np.ndarray, new_bar: np.ndarray) -> Dict[str, np.ndarray]:
    """The four EMAs on the flat grid (log price ``lp`` carried, ``new_bar`` where a 1-minute bar closed). A 5-minute
    bar completes at slot 5k + 4 and exists when one of its minutes had a bar."""
    i = np.arange(len(lp))
    has5 = np.add.reduceat(new_bar.astype(int), np.arange(0, len(lp), BAR5)) > 0
    done5 = (i % BAR5 == BAR5 - 1) & np.repeat(has5, BAR5)[:len(lp)]
    out = {}
    for p in EMA_PERIODS:
        out[f"m1_{p}"] = _ema_events(lp, new_bar, p)
        out[f"m5_{p}"] = _ema_events(lp, done5, p)
    return out


def _lag(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    out[k:] = x[:-k]
    return out


def features(close: np.ndarray, age: np.ndarray, volume: np.ndarray) -> Dict[str, np.ndarray]:
    """OWN_CONTEXT and EMA_FEATURES, sessions x 1440 each, from one instrument's panel rows (sessions in date order)."""
    n = close.shape[0]
    with np.errstate(invalid="ignore", divide="ignore"):
        lp = pd.Series(np.log(close.reshape(-1))).ffill().to_numpy().copy()
    new_bar = (age.reshape(-1) == 0) & np.isfinite(close.reshape(-1))
    r = np.nan_to_num(np.diff(lp, prepend=np.nan))
    C = np.concatenate([[0.0], np.cumsum(r * r)])
    i = np.arange(len(lp))
    lo = np.maximum(0, i - VOL_MINUTES + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = np.sqrt((C[i + 1] - C[lo]) / (i + 1 - lo))
    sd = np.where((sd > 0) & (i >= VOL_MINUTES - 1), sd, np.nan)
    unit = sd * np.sqrt(UNIT_MINUTES)
    E = emas(lp, new_bar)
    v = np.nan_to_num(volume.astype(float))
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap_lp = np.log(np.cumsum(np.nan_to_num(close) * v, axis=1) / np.cumsum(v, axis=1)).reshape(-1)
        out = {"ret1": (lp - _lag(lp, 1)) / sd, "ret5": (lp - _lag(lp, 5)) / (sd * np.sqrt(5)),
               "ret15": (lp - _lag(lp, 15)) / (sd * np.sqrt(15)), "vwap_dist": (lp - vwap_lp) / unit}
        for tf in ("m1", "m5"):
            for p in EMA_PERIODS:
                e = E[f"{tf}_{p}"]
                out[f"{tf}_slope{p}"] = (e - _lag(e, SLOPE_MINUTES)) / (sd * np.sqrt(SLOPE_MINUTES))
                out[f"{tf}_dist{p}"] = (lp - e) / unit
            out[f"{tf}_gap"] = (E[f"{tf}_14"] - E[f"{tf}_100"]) / unit
    out = {k: np.clip(a, -CLIP, CLIP) for k, a in out.items()}
    s1, s5 = np.sign(out["m1_gap"]), np.sign(out["m5_gap"])
    out["tf_agree"] = np.where(np.isfinite(s1) & np.isfinite(s5), np.where(s1 == s5, s1, 0.0), np.nan)
    known = np.isfinite(close.reshape(-1))
    return {k: np.where(known, a, np.nan).reshape(n, DAY_SLOTS) for k, a in out.items()}


def cond_table(panel, target: str, base: FeatureTable) -> FeatureTable:
    """``base`` (forecaster/fan_features.build of the same panel) with the target's OWN_CONTEXT and EMA_FEATURES."""
    if list(panel.sessions) != list(base.sessions):
        raise ValueError("the panel and the feature table hold different sessions")
    j = panel.symbols.index(target)
    f = features(panel.close[:, j], panel.age[:, j], panel.volume[:, j])
    extra = [Feature(f"{target}.{k}", target, GROUP, k) for k in (*OWN_CONTEXT, *EMA_FEATURES)]
    X = np.concatenate([base.X, np.stack([f[x.family] for x in extra], axis=-1).astype(np.float32)], axis=-1)
    return FeatureTable(base.sessions, target, base.features + extra, X)


def inputs(arm: str, target: str) -> List[str]:
    ctx = [f"{target}.{k}" for k in (*OWN_CONTEXT, *BASE_CONTEXT)]
    return ctx + ([f"{target}.{k}" for k in EMA_FEATURES] if arm == "ema" else [])


def design(table: FeatureTable, rows: fh.Rows, arm: str) -> np.ndarray:
    """The arm's inputs for ``rows``, then the phase as its last (categorical) column."""
    names = inputs(arm, table.target)
    X, feats = table.matrix(rows, lambda f: f.name in set(names))
    got = [f.name for f in feats]
    cols = [X[:, got.index(n)].astype(float) for n in names]
    return np.column_stack(cols + [PHASE_OF_SLOT[rows.slot].astype(float)])


def column(table: FeatureTable, rows: fh.Rows, family: str) -> np.ndarray:
    """One feature of the target for ``rows``."""
    X, feats = table.matrix(rows, lambda f: f.family == family and f.instrument == table.target)
    return X[:, [g.name for g in feats].index(f"{table.target}.{family}")].astype(float)


# --------------------------------------------------------------------------
# Scoring helpers
# --------------------------------------------------------------------------

def session_crps(rows: fh.Rows, mu: np.ndarray, sigma: np.ndarray, shape_of: Callable[[str, int], np.ndarray]
                 ) -> Tuple[List[str], np.ndarray]:
    """Per session of ``rows`` (one horizon), the mean CRPS (bps) of the fan shifted by ``mu``."""
    days = rows.session.astype(str)
    order = np.unique(days)
    out = np.empty(len(order))
    for k, d in enumerate(order):
        s = days == d
        h = int(rows.horizon[s][0])
        out[k] = float(np.mean(sigma[s] * fh.crps((rows.y[s] - mu[s]) / sigma[s], shape_of(d, h)) * 1e4))
    return list(order), out


def _interval(diffs: Sequence[float], level: float) -> Optional[List[float]]:
    b = F.BOOTSTRAP
    iv = block_bootstrap(list(diffs), b["block_sessions"], b["resamples"], b["seed"], level)
    return list(iv) if iv else None


# --------------------------------------------------------------------------
# Tuning, activation, refit
# --------------------------------------------------------------------------

class Shift:
    """A fitted arm at one horizon: zero, or lambda x clip(model(x), +-Z_CAP) in the fan's sigmas."""

    def __init__(self, model=None, lam: float = 0.0) -> None:
        self.model, self.lam = model, lam

    @property
    def active(self) -> bool:
        return self.model is not None and self.lam > 0

    def zhat(self, X: np.ndarray) -> np.ndarray:
        if not self.active:
            return np.zeros(len(X))
        return self.lam * np.clip(self.model.predict(X), -Z_CAP, Z_CAP)


def _gbm(params: Dict[str, Any], n_cols: int) -> HistGradientBoostingRegressor:
    cat = np.zeros(n_cols, dtype=bool)
    cat[-1] = True
    return HistGradientBoostingRegressor(**GBM, **params, categorical_features=cat, early_stopping=False,
                                         random_state=0)


def select(X_fit: np.ndarray, z_fit: np.ndarray, X_val: np.ndarray, rows_val: fh.Rows, sigma_val: np.ndarray,
           shape_of: Callable[[str, int], np.ndarray]) -> Dict[str, Any]:
    """Every GRID x SHRINK option and zero, scored on the validation rows by session mean CRPS; the best, and whether
    its paired interval against zero lies wholly below zero (activation)."""
    days, zero = session_crps(rows_val, np.zeros(len(rows_val)), sigma_val, shape_of)
    options = [{"params": None, "lam": 0.0, "crps": float(zero.mean()), "diff": 0.0}]
    per = {0: zero}
    zc = np.clip(z_fit, -Z_WINSOR, Z_WINSOR)
    for g in GRID:
        m = _gbm(g, X_fit.shape[1]).fit(X_fit, zc)
        zh = np.clip(m.predict(X_val), -Z_CAP, Z_CAP)
        for lam in SHRINK:
            _, c = session_crps(rows_val, lam * zh * sigma_val, sigma_val, shape_of)
            per[len(options)] = c
            options.append({"params": dict(g), "lam": lam, "crps": float(c.mean()), "diff": float((c - zero).mean())})
    best = int(np.argmin([o["crps"] for o in options]))
    iv = _interval(per[best] - zero, ACTIVATION_INTERVAL) if best else None
    active = bool(best and iv is not None and iv[1] < 0)
    return {"options": options, "best": best, "best_interval": iv, "active": active,
            "best_diffs": [float(x) for x in (per[best] - zero)],
            "valid_sessions": len(days), "chosen": options[best] if active else options[0]}


def fit_block(frames: Dict[str, fh.Frame], table: FeatureTable, train_days: Sequence[str],
              shape_of: Callable[[str, int], np.ndarray], make_width: Optional[Callable[[], fh.Candidate]] = None,
              spec: Optional["Spec"] = None) -> Tuple[Any, Dict[Tuple[str, int], Shift], Dict]:
    """The block's width (``make_width``, lin_pois_ivx by default, fitted on every training session; for validation
    on the sessions before it), and per arm and horizon the selected Shift."""
    spec = spec or SPEC
    make_width = make_width or fm.candidate(fd.SCALE, table)
    train_days = sorted(train_days)
    cut = int(round(len(train_days) * (1 - VALID_SHARE)))
    fit_days, val_days = train_days[:cut], train_days[cut:]
    rows_fit = fd._rows(frames, fit_days, TRAIN_EVERY)
    rows_val = fd._rows(frames, val_days, TRAIN_EVERY)
    rows_all = fd._rows(frames, train_days, TRAIN_EVERY)
    w_fit = make_width()
    w_fit.fit(rows_fit)
    width = make_width()
    width.fit(rows_all)
    sig_fit = np.sqrt(rows_fit.var) * w_fit.predict(rows_fit)
    sig_val = np.sqrt(rows_val.var) * w_fit.predict(rows_val)
    sig_all = np.sqrt(rows_all.var) * width.predict(rows_all)
    shifts, info = {}, {}
    for arm in spec.fitted:
        for h in HORIZONS:
            f, v, a = rows_fit.horizon == h, rows_val.horizon == h, rows_all.horizon == h
            Xf, Xv = spec.design(table, rows_fit.take(f), arm), spec.design(table, rows_val.take(v), arm)
            sel = select(Xf, rows_fit.y[f] / sig_fit[f], Xv, rows_val.take(v), sig_val[v], shape_of)
            shift = Shift()
            if sel["active"]:
                ch = sel["chosen"]
                Xa = spec.design(table, rows_all.take(a), arm)
                za = np.clip(rows_all.y[a] / sig_all[a], -Z_WINSOR, Z_WINSOR)
                shift = Shift(_gbm(ch["params"], Xa.shape[1]).fit(Xa, za), ch["lam"])
            shifts[(arm, h)] = shift
            info[f"{arm}|h{h}"] = sel
    ext = np.abs(column(table, rows_all, spec.edge_family))
    edges = [float(x) for x in np.nanpercentile(ext, [100 / 3, 200 / 3])]
    return width, shifts, {"selection": info, "extension_edges": edges,
                           "fit_days": [fit_days[0], fit_days[-1], len(fit_days)],
                           "val_days": [val_days[0], val_days[-1], len(val_days)]}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _buckets(table: FeatureTable, rows: fh.Rows, edges: Sequence[float]) -> Dict[str, np.ndarray]:
    """Per diagnostic, each row's bucket label."""
    phase = np.array([PHASES[p][0] for p in PHASE_OF_SLOT[rows.slot]])
    ag = column(table, rows, "tf_agree")
    agree = np.where(np.isnan(ag), "missing", np.where(ag > 0, "up", np.where(ag < 0, "down", "disagree")))
    ext = np.abs(column(table, rows, EXTENSION))
    extension = np.where(np.isnan(ext), "missing",
                         np.where(ext <= edges[0], "low", np.where(ext <= edges[1], "mid", "high")))
    return {"phase": phase, "tf_agree": agree, "extension": extension}


def score_session(fr: fh.Frame, table: FeatureTable, rows: fh.Rows, sigma: np.ndarray, mus: Dict[str, np.ndarray],
                  edges: Sequence[float], acc: Dict, buckets: Optional[Callable] = None) -> Dict[str, Any]:
    """One session: per horizon and arm the mean CRPS and Brier, per diagnostic bucket the mean CRPS; adds coverage,
    reliability and shift counts to ``acc``."""
    out: Dict[str, Any] = {}
    bk = (buckets or _buckets)(table, rows, edges)
    for h in HORIZONS:
        s = rows.horizon == h
        if not s.any():
            continue
        Q = fr.shape[fh.FRAME_HORIZONS.index(h)]
        y, sg = rows.y[s], sigma[s]
        moved = y != 0
        up = (y > 0).astype(float)
        per: Dict[str, Any] = {}
        for arm, mu_all in mus.items():
            mu = mu_all[s]
            c = sg * fh.crps((y - mu) / sg, Q) * 1e4
            pu = 1 - fd.cdf(-mu / sg, Q)
            br = (pu - up)[moved] ** 2
            diag = {}
            for name, lab in bk.items():
                ls = lab[s]
                diag[name] = {b: [float(c[ls == b].mean()), int((ls == b).sum())] for b in np.unique(ls)}
            per[arm] = {"crps": float(c.mean()), "brier": float(br.mean()) if moved.any() else float("nan"),
                        "n": int(s.sum()), "diag": diag}
            pit = fd.cdf((y - mu) / sg, Q)
            t = acc.setdefault((arm, h), {"n": 0, "in50": 0, "in90": 0, "nonzero": 0, "abs_shift": 0.0,
                                          "abs_shift_bps": 0.0, "bins": np.zeros((10, 3))})
            t["n"] += len(y)
            t["in50"] += int(((pit >= 0.25) & (pit <= 0.75)).sum())
            t["in90"] += int(((pit >= 0.05) & (pit <= 0.95)).sum())
            nz = mu != 0
            t["nonzero"] += int(nz.sum())
            t["abs_shift"] += float(np.abs(mu[nz] / sg[nz]).sum())
            t["abs_shift_bps"] += float(np.abs(mu[nz]).sum() * 1e4)
            k = np.clip((pu[moved] * 10).astype(int), 0, 9)
            np.add.at(t["bins"], k, np.column_stack([np.ones(moved.sum()), pu[moved], up[moved]]))
        out[f"h{h}"] = per
    return out


def _merge(acc: Dict, part: Dict) -> None:
    for k, t in part.items():
        a = acc.setdefault(k, {kk: (v.copy() if isinstance(v, np.ndarray) else 0 * v) for kk, v in t.items()})
        for kk, v in t.items():
            a[kk] = a[kk] + v


def paired(per: Sequence[Dict], h: int, a: str, b: str, metric: str = "crps",
           level: float = 0.95) -> Optional[Dict[str, Any]]:
    """Arm ``a`` minus arm ``b`` over sessions (date order), with the moving-block interval at ``level``."""
    key = f"h{h}"
    pairs = [(r[key][a][metric], r[key][b][metric]) for r in per
             if key in r and np.isfinite(r[key][a][metric]) and np.isfinite(r[key][b][metric])]
    if not pairs:
        return None
    d = [x - y for x, y in pairs]
    base = float(np.mean([y for _, y in pairs]))
    iv = _interval(d, level)
    return {"sessions": len(d), "diff": float(np.mean(d)), "share": float(np.mean(d) / base) if base else None,
            "interval": iv, "level": level,
            "verdict": "no interval" if iv is None else "lower" if iv[1] < 0 else "higher" if iv[0] > 0 else "across zero"}


def paired_diag(per: Sequence[Dict], h: int, a: str, b: str, name: str, bucket: str) -> Optional[Dict[str, Any]]:
    key = f"h{h}"
    d, n = [], 0
    for r in per:
        if key not in r:
            continue
        x, y = r[key][a]["diag"][name].get(bucket), r[key][b]["diag"][name].get(bucket)
        if x and y:
            d.append(x[0] - y[0])
            n += x[1]
    if not d:
        return None
    iv = _interval(d, 0.95)
    return {"sessions": len(d), "rows": n, "diff": float(np.mean(d)), "interval": iv}


def summarise(per: Sequence[Dict], acc: Dict, spec: Optional["Spec"] = None) -> Dict[str, Any]:
    spec = spec or SPEC
    T, C = spec.treatment, spec.control
    out: Dict[str, Any] = {}
    for h in HORIZONS:
        r: Dict[str, Any] = {}
        for arm in spec.arms:
            t = acc[(arm, h)]
            r[arm] = {"crps": float(np.mean([p[f"h{h}"][arm]["crps"] for p in per])),
                      "brier": float(np.nanmean([p[f"h{h}"][arm]["brier"] for p in per])),
                      "cover50": t["in50"] / t["n"], "cover90": t["in90"] / t["n"],
                      "nonzero_share": t["nonzero"] / t["n"],
                      "mean_abs_shift_sigma": t["abs_shift"] / t["nonzero"] if t["nonzero"] else 0.0,
                      "mean_abs_shift_bps": t["abs_shift_bps"] / t["nonzero"] if t["nonzero"] else 0.0,
                      "reliability": [{"p": c[1] / c[0], "freq": c[2] / c[0], "n": int(c[0])}
                                      for c in t["bins"] if c[0] > 0]}
        r["comparisons"] = {
            f"{a}-{b}|{m}": paired(per, h, a, b, m)
            for a, b in ((T, "zero"), (T, C), (C, "zero")) for m in ("crps", "brier")}
        r["decision"] = {f"{a}-{b}": paired(per, h, a, b, "crps", DECISION_INTERVAL)
                         for a, b in ((T, "zero"), (T, C))}
        names = {}
        for p in per:
            for name, bks in p[f"h{h}"]["zero"]["diag"].items():
                names.setdefault(name, set()).update(bks)
        r["diagnostics"] = {name: {b: {f"{a}-{c}": paired_diag(per, h, a, c, name, b)
                                       for a, c in ((T, "zero"), (T, C))} for b in sorted(bks)}
                            for name, bks in names.items()}
        out[f"h{h}"] = r
    return out


@dataclass
class Spec:
    """One experiment on this machinery: its arms (zero, a control and a treatment fitted alike), their inputs, the
    diagnostics and the report's wording."""
    version: str
    kind: str
    definition: Dict[str, Any]
    control: str
    treatment: str
    design: Callable[[FeatureTable, fh.Rows, str], np.ndarray]
    buckets: Callable[[FeatureTable, fh.Rows, Sequence[float]], Dict[str, np.ndarray]]
    edge_family: str
    title: str
    doc: str
    arms_note: str
    diag_note: str

    @property
    def fitted(self) -> Tuple[str, str]:
        return (self.control, self.treatment)

    @property
    def arms(self) -> Tuple[str, str, str]:
        return ("zero", self.control, self.treatment)

    def definition_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.definition, sort_keys=True, default=str).encode()).hexdigest()[:16]


SPEC = Spec(VERSION, "cond_ema", DEFINITION, "context", "ema", design, _buckets, EXTENSION,
            "Conditional EMA Direction", "docs/fan_cond_ema.md",
            "Arms: A `zero`, B `context` (returns, VWAP distance, volatility, relative volume, phase), C `ema` (B plus "
            "the EMA set).", "extension terciles from each block's training rows.")


def run(frames: Dict[str, fh.Frame], table: FeatureTable, blks: Sequence[Dict[str, Any]], sessions: Sequence[str],
        log: Callable[[str], None] = lambda s: None, make_width: Optional[Callable[[], fh.Candidate]] = None,
        spec: Optional["Spec"] = None) -> Dict[str, Any]:
    """Every block fitted on the sessions before it, scored on every origin of its own; pooled and per block."""
    spec = spec or SPEC
    shape_of = lambda d, h: frames[d].shape[fh.FRAME_HORIZONS.index(h)]
    pooled, days_all, acc_all, out_blocks = [], [], {}, []
    for b in blks:
        train = [d for d in sessions if d < b["first"] and d in frames]
        test = [d for d in sessions if b["first"] <= d <= b["last"] and d in frames]
        width, shifts, info = fit_block(frames, table, train, shape_of, make_width, spec)
        per, acc = [], {}
        for d in test:
            rows = fd._rows(frames, [d], 1)
            sigma = np.sqrt(rows.var) * width.predict(rows)
            mus = {"zero": np.zeros(len(rows))}
            for arm in spec.fitted:
                mu = np.zeros(len(rows))
                for h in HORIZONS:
                    s = rows.horizon == h
                    mu[s] = shifts[(arm, h)].zhat(spec.design(table, rows.take(s), arm)) * sigma[s]
                mus[arm] = mu
            per.append(score_session(frames[d], table, rows, sigma, mus, info["extension_edges"], acc, spec.buckets))
        _merge(acc_all, acc)
        pooled += per
        days_all += test
        out_blocks.append({**b, "train_sessions": len(train), "scored": len(test), **info,
                           "active": {f"{a}|h{h}": shifts[(a, h)].active for a in spec.fitted for h in HORIZONS},
                           "results": summarise(per, acc, spec)})
        on = ", ".join(k for k, v in out_blocks[-1]["active"].items() if v) or "none"
        log(f"{b['block']}: trained on {len(train)} sessions, scored {len(test)}; active: {on}")
    res = {"kind": spec.kind, "version": spec.version, "definition": spec.definition,
           "definition_hash": spec.definition_hash(), "arms": list(spec.arms),
           "blocks": out_blocks, "sessions": days_all, "results": summarise(pooled, acc_all, spec),
           "per_session": {d: {h: {a: {"crps": v[a]["crps"], "brier": v[a]["brier"]} for a in spec.arms}
                               for h, v in r.items()} for d, r in zip(days_all, pooled)}}
    res["conclusion"] = decide(res, spec)
    return res


def decide(res: Dict[str, Any], spec: Optional["Spec"] = None) -> Dict[str, Any]:
    """The definition's decision applied to the pooled results."""
    spec = spec or SPEC
    T, C = spec.treatment, spec.control
    per_h = {}
    for h in HORIZONS:
        r = res["results"][f"h{h}"]
        d = r["decision"]
        passes = all(d[k] and d[k]["interval"] and d[k]["interval"][1] < 0 for k in (f"{T}-zero", f"{T}-{C}"))
        below_both = r[T]["crps"] < r["zero"]["crps"] and r[T]["crps"] < r[C]["crps"]
        per_h[f"h{h}"] = "pass" if passes else "below both, not significant" if below_both else "not below both"
    if any(v == "pass" for v in per_h.values()):
        verdict = "pass"
    elif all(v == "not below both" for v in per_h.values()):
        verdict = "fail"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "horizons": per_h}


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def _p(c: Optional[Dict[str, Any]], pct: bool = True) -> str:
    if not c:
        return "-"
    iv = c["interval"]
    s = f"{100 * c['share']:+.3f} %" if pct and c.get("share") is not None else f"{c['diff']:+.5f}"
    return s + (f" [{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "")


def write_report(res: Dict[str, Any], path: str, spec: Optional["Spec"] = None) -> str:
    spec = spec or SPEC
    T, C = spec.treatment, spec.control
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = res["conclusion"]
    L = [f"# {spec.title}: {res['version']}, {res.get('target', 'NQ')}", "",
         f"Definition `{res['definition_hash']}` ({spec.doc}), fixed before this run; code "
         f"{res.get('code_revision', '?')[:12]}. {len(res['sessions'])} sessions in {len(res['blocks'])} chronological "
         "blocks, every origin; all previously examined - development.", "",
         f"**Conclusion: {con['verdict'].upper()}** - " + "; ".join(f"{k[1:]} min: {v}" for k, v in con["horizons"].items())
         + ".", "",
         spec.arms_note + " Differences per session in CRPS basis points (share of the second arm's CRPS), 95 % moving-"
         "block interval; the decision uses 97.5 % (Bonferroni over two horizons).", "",
         "## Pooled", "",
         "| Horizon | Arm | CRPS (bps) | Brier | 50 % / 90 % coverage | Shifted origins | Mean abs shift (sigma / bps) |",
         "|---|---|---|---|---|---|---|"]
    for key, r in res["results"].items():
        for arm in spec.arms:
            a = r[arm]
            L.append(f"| {key[1:]} min | {arm} | {a['crps']:.4f} | {a['brier']:.5f} | {100 * a['cover50']:.1f} / "
                     f"{100 * a['cover90']:.1f} % | {100 * a['nonzero_share']:.1f} % | "
                     f"{a['mean_abs_shift_sigma']:.3f} / {a['mean_abs_shift_bps']:.2f} |")
    L += ["", "| Horizon | Comparison | CRPS | Brier | Decision (97.5 %) |", "|---|---|---|---|---|"]
    for key, r in res["results"].items():
        for a, b in ((T, "zero"), (T, C), (C, "zero")):
            c = r["comparisons"]
            dec = r["decision"].get(f"{a}-{b}")
            L.append(f"| {key[1:]} min | {a} - {b} | {_p(c[f'{a}-{b}|crps'])} | {_p(c[f'{a}-{b}|brier'], False)} | "
                     + (_p(dec) if dec else "-") + " |")
    L += ["", "## Per block", "",
          "| Block | Trained on | Validation | Activated | C - A 5 / 15 min | C - B 5 / 15 min |", "|---|---|---|---|---|---|"]
    for b in res["blocks"]:
        rr = b["results"]
        act = ", ".join(k for k, v in b["active"].items() if v) or "none"
        ca = " / ".join(_p(rr[f"h{h}"]["comparisons"][f"{T}-zero|crps"]).split(" [")[0] for h in HORIZONS)
        cb = " / ".join(_p(rr[f"h{h}"]["comparisons"][f"{T}-{C}|crps"]).split(" [")[0] for h in HORIZONS)
        L.append(f"| {b['block']} ({b['first']} to {b['last']}) | {b['train_sessions']} | {b['val_days'][2]} "
                 f"({b['val_days'][0]} to {b['val_days'][1]}) | {act} | {ca} | {cb} |")
    L += ["", "## Selection on validation", "",
          "Best option per block, arm and horizon: its mean CRPS change against zero on the validation sessions and "
          f"the 95 % interval (activation reads {100 * ACTIVATION_INTERVAL:.2f} %).", "",
          "| Block | Arm, horizon | Best option | Validation change (bps) | Interval | Activated |", "|---|---|---|---|---|---|"]
    for b in res["blocks"]:
        for k, s in b["selection"].items():
            o = s["options"][s["best"]]
            desc = "zero" if o["params"] is None else f"depth {o['params']['max_depth']}, {o['params']['max_iter']} trees, lambda {o['lam']}"
            iv = s["best_interval"]
            L.append(f"| {b['block']} | {k} | {desc} | {o['diff']:+.5f} | "
                     + (f"[{iv[0]:+.5f}, {iv[1]:+.5f}]" if iv else "-") + f" | {'yes' if s['active'] else 'no'} |")
    L += ["", "## Reliability of P(up), pooled", ""]
    for key, r in res["results"].items():
        for arm in spec.fitted:
            cells = "; ".join(f"{x['p']:.2f} -> {x['freq']:.3f} ({x['n']:,})" for x in r[arm]["reliability"])
            L.append(f"- {key[1:]} min, {arm}: {cells}")
    L += ["", "## Diagnostics (explanation only, never a deployment rule)", "",
          "CRPS difference in bps per session, bucket by bucket; " + spec.diag_note, "",
          "| Horizon | Breakdown | Bucket | Rows | C - A | C - B |", "|---|---|---|---|---|---|"]
    for key, r in res["results"].items():
        for name, bks in r["diagnostics"].items():
            for bucket, cmp in bks.items():
                ca, cb = cmp[f"{T}-zero"], cmp[f"{T}-{C}"]
                rows = ca["rows"] if ca else 0
                L.append(f"| {key[1:]} min | {name} | {bucket} | {rows:,} | {_p(ca, False)} | {_p(cb, False)} |")
    L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path
