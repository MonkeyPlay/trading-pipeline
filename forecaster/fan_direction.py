# forecaster/fan_direction.py
"""
The direction experiment (docs/fan_direction.md): can a learned shift of the fan's
centre beat the zero-drift fan at 5 and 15 minutes? One bounded experiment, three arms
on one shared scale - the learned fan's sigma (lin_pois_ivx, refitted per block) and
fan_rw_v2's shape:

  forecast price quantile = P_t exp(mu_h(x_t) + s_h(x_t) z_q)

  zero          mu = 0: the fan as it is
  ema_damped    mu = DAMP x h x the EMA 14's slope per minute over the last 5 minutes -
                trend continuation, damped by a factor fixed in advance (nothing fitted)
  ridge         mu = s_h x clip(zhat, +-Z_CAP): per horizon a ridge regression, without
                intercept on standardised inputs, of the realised move in the scale's
                sigmas (winsorised at +-Z_WINSOR); its penalty chosen by the training
                sessions' own chronological validation (the last VALID_SHARE of them),
                and no shift at all when no penalty beats predicting zero there

Inputs (point in time, from the panel's last closes and volumes, on 1-minute bars; the
EMAs run across sessions): DIRECTION_FAMILIES below, every price distance and slope over
the trailing 60-minute volatility of 1-minute returns, clipped to +-CLIP; beside them the
target's own vol60, rv15 and rv60 (forecaster/fan_features.py) and the origin's phase.

Evaluation (rolling origin): the intermarket experiment's three checks and its spent
holdout cut into two blocks of 30, each block trained on every session before it only.
Per arm against zero: CRPS (as the experiment scores it), the central bands' coverage,
the up/down probability's Brier score (moves of exactly zero left out) and reliability,
and the ranked probability score of below / within / above a neutral band of
+-NEUTRAL scale sigmas around the origin. Every result here is development: the holdout
was examined before this experiment existed, so confirmation needs sessions after it.

  direction_features(close, volume)        sessions x 1440 per family (pure)
  direction_table(panel, target, base)     the base feature table with those added
  Ridge / EMA arms                         fit(rows, X, sigma), shift(rows, X, sigma)
  score_session(...)                       one session, every arm, per horizon
  run(frames, table, blocks, ...)          the rolling-origin evaluation
  write_report(res, path)                  docs/reports/fan_direction_<experiment>_<target>.md
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from contracts import fan as F
from forecaster import fan_harness as fh
from forecaster import fan_model as fm
from forecaster.experiments import block_bootstrap
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

DIRECTION_VERSION = "fan_direction_v1"
HORIZONS = (5, 15)
EMA_FAST, EMA_SLOW = 14, 100
VOL_WINDOW = 60                 # the trailing volatility every distance and slope is measured in
SLOPE_FAST, SLOPE_SLOW = 5, 15  # minutes over which each EMA's slope is read
CLIP = 10.0
DAMP = 0.5                      # ema_damped: half the slope carried forward - fixed before any result
NEUTRAL = 0.25                  # the neutral band: +-0.25 of the scale's sigma around the origin
Z_WINSOR = 4.0
Z_CAP = 1.0                     # a learned shift never exceeds one sigma of the fan
VALID_SHARE = 0.2
ALPHAS = tuple(float(a) for a in np.logspace(0, 7, 15))
SCALE = "lin_pois_ivx"          # the shared scale, refitted per block as the checks fitted it
ARMS = ("zero", "ema_damped", "ridge")
DIRECTION_FAMILIES = ("ema14_slope", "ema100_slope", "dist_ema14", "dist_ema100", "ema_gap",
                      "ret1", "ret5", "ret15", "vwap_dist")
CONTEXT_FAMILIES = ("vol60", "rv15", "rv60")
EMA_RAW = "ema14_step"          # the EMA 14's raw slope in log price per minute: ema_damped's input only
BANDS = (0.50, 0.80, 0.90)
GROUP = "direction"


# --------------------------------------------------------------------------
# Features (pure)
# --------------------------------------------------------------------------

def _ema(x: np.ndarray, span: int) -> np.ndarray:
    """Pine's ta.ema over a 1-D series (alpha = 2 / (span + 1), seeded with its first value); NaN before it."""
    s = pd.Series(x)
    first = s.first_valid_index()
    out = np.full(len(x), np.nan)
    if first is None:
        return out
    out[first:] = s.iloc[first:].ewm(span=span, adjust=False).mean().to_numpy()
    return out


def _lag(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    out[k:] = x[:-k]
    return out


def direction_features(close: np.ndarray, volume: np.ndarray) -> Dict[str, np.ndarray]:
    """
    Per family (DIRECTION_FAMILIES and EMA_RAW), sessions x 1440, from one instrument's panel ``close`` (the last
    close known at the end of each minute) and ``volume`` (the bar starting in the minute), sessions in date order.
    The EMAs and the trailing volatility run over the sessions laid end to end, so nothing after a minute is read.
    The VWAP is the session's own from 18:00 ET on closes (the panel has no high or low), missing before its first
    volume.
    """
    n = close.shape[0]
    with np.errstate(invalid="ignore", divide="ignore"):
        lp = pd.Series(np.log(close.reshape(-1))).ffill().to_numpy()
    r = np.nan_to_num(np.diff(lp, prepend=np.nan))
    C = np.concatenate([[0.0], np.cumsum(r * r)])
    i = np.arange(len(lp))
    lo = np.maximum(0, i - VOL_WINDOW + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sd = np.sqrt((C[i + 1] - C[lo]) / (i + 1 - lo))
    sd = np.where((sd > 0) & (i >= VOL_WINDOW - 1), sd, np.nan)
    e14, e100 = _ema(lp, EMA_FAST), _ema(lp, EMA_SLOW)
    v = np.nan_to_num(volume.astype(float))
    pv = np.nan_to_num(close) * v
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.cumsum(pv, axis=1) / np.cumsum(v, axis=1)
        vwap_lp = np.log(vwap).reshape(-1)
        out = {
            "ema14_slope": (e14 - _lag(e14, SLOPE_FAST)) / SLOPE_FAST / sd,
            "ema100_slope": (e100 - _lag(e100, SLOPE_SLOW)) / SLOPE_SLOW / sd,
            "dist_ema14": (lp - e14) / sd,
            "dist_ema100": (lp - e100) / sd,
            "ema_gap": (e14 - e100) / sd,
            "ret1": (lp - _lag(lp, 1)) / sd,
            "ret5": (lp - _lag(lp, 5)) / (sd * np.sqrt(5)),
            "ret15": (lp - _lag(lp, 15)) / (sd * np.sqrt(15)),
            "vwap_dist": (lp - vwap_lp) / sd,
        }
    out = {k: np.clip(a, -CLIP, CLIP) for k, a in out.items()}
    out[EMA_RAW] = (e14 - _lag(e14, SLOPE_FAST)) / SLOPE_FAST
    known = np.isfinite(close.reshape(-1))
    return {k: np.where(known, a, np.nan).reshape(n, DAY_SLOTS) for k, a in out.items()}


def direction_table(panel, target: str, base: FeatureTable) -> FeatureTable:
    """``base`` (forecaster/fan_features.build of the same panel) with the target's direction features added, group
    'direction'."""
    if list(panel.sessions) != list(base.sessions):
        raise ValueError("the panel and the feature table hold different sessions")
    j = panel.symbols.index(target)
    feats = direction_features(panel.close[:, j], panel.volume[:, j])
    extra = [Feature(f"{target}.{k}", target, GROUP, k) for k in (*DIRECTION_FAMILIES, EMA_RAW)]
    X = np.concatenate([base.X, np.stack([feats[f.family] for f in extra], axis=-1).astype(np.float32)], axis=-1)
    return FeatureTable(base.sessions, target, base.features + extra, X)


def ridge_inputs(target: str) -> List[str]:
    return [f"{target}.{k}" for k in DIRECTION_FAMILIES] + [f"{target}.{k}" for k in CONTEXT_FAMILIES]


def _columns(table: FeatureTable, rows: fh.Rows, names: Sequence[str]) -> np.ndarray:
    """``rows`` x ``names`` from the table (float64, NaN where missing), and the origin's phase as dummies."""
    X, feats = table.matrix(rows, lambda f: f.name in set(names))
    got = [f.name for f in feats]
    cols = np.column_stack([X[:, got.index(n)] for n in names]).astype(float)
    phase = PHASE_OF_SLOT[rows.slot]
    return np.column_stack([cols] + [(phase == p).astype(float) for p in range(1, len(PHASES))])


def column_names(target: str) -> List[str]:
    return ridge_inputs(target) + [f"phase:{PHASES[p][0]}" for p in range(1, len(PHASES))]


# --------------------------------------------------------------------------
# The arms
# --------------------------------------------------------------------------

class Zero:
    name = "zero"

    def fit(self, rows, table, sigma) -> None:
        pass

    def shift(self, rows, table, sigma) -> np.ndarray:
        return np.zeros(len(rows))


class EMADamped(Zero):
    name = "ema_damped"

    def shift(self, rows, table, sigma) -> np.ndarray:
        X, feats = table.matrix(rows, lambda f: f.family == EMA_RAW)
        step = X[:, [f.name for f in feats].index(f"{table.target}.{EMA_RAW}")].astype(float)
        return np.nan_to_num(DAMP * rows.horizon * step)


class RidgeShift(Zero):
    """See the module docstring. ``state`` per horizon holds everything shift() reads (describe/from_state)."""
    name = "ridge"

    def __init__(self, target: str) -> None:
        self.target = target
        self.state: Dict[int, Dict[str, Any]] = {}

    def _design(self, rows, table) -> np.ndarray:
        return _columns(table, rows, ridge_inputs(self.target))

    def fit(self, rows, table, sigma) -> None:
        for h in HORIZONS:
            s = rows.horizon == h
            X = self._design(rows.take(s), table)
            z = np.clip(rows.y[s] / sigma[s], -Z_WINSOR, Z_WINSOR)
            ok = np.isfinite(X).all(axis=1) & np.isfinite(z)
            X, z, days = X[ok], z[ok], rows.session[s][ok]
            ordered = np.unique(days)
            cut = ordered[int(len(ordered) * (1 - VALID_SHARE))]
            tr, va = days < cut, days >= cut
            mu, sd = X[tr].mean(axis=0), X[tr].std(axis=0)
            sd = np.where(sd > 0, sd, 1.0)
            zero_mse = float(np.mean(z[va] ** 2))
            scores = []
            for a in ALPHAS:
                m = Ridge(alpha=a, fit_intercept=False).fit((X[tr] - mu) / sd, z[tr])
                pred = np.clip(m.predict((X[va] - mu) / sd), -Z_CAP, Z_CAP)
                scores.append(float(np.mean((z[va] - pred) ** 2)))
            best = int(np.argmin(scores))
            use = scores[best] < zero_mse
            mu, sd = X.mean(axis=0), X.std(axis=0)
            sd = np.where(sd > 0, sd, 1.0)
            coef = (Ridge(alpha=ALPHAS[best], fit_intercept=False).fit((X - mu) / sd, z).coef_
                    if use else np.zeros(X.shape[1]))
            self.state[h] = {"columns": column_names(self.target), "alpha": ALPHAS[best], "used": bool(use),
                             "valid_mse": scores[best], "valid_zero_mse": zero_mse,
                             "valid_gain": 1 - scores[best] / zero_mse, "rows": int(len(z)),
                             "mu": mu.tolist(), "sd": sd.tolist(), "coef": [float(c) for c in coef]}

    def zhat(self, rows, table) -> np.ndarray:
        out = np.zeros(len(rows))
        for h in np.unique(rows.horizon):
            st = self.state.get(int(h))
            if st is None:
                continue
            s = rows.horizon == h
            X = self._design(rows.take(s), table)
            ok = np.isfinite(X).all(axis=1)
            p = np.zeros(int(s.sum()))
            p[ok] = ((X[ok] - np.array(st["mu"])) / np.array(st["sd"])) @ np.array(st["coef"])
            out[s] = np.clip(p, -Z_CAP, Z_CAP)
        return out

    def shift(self, rows, table, sigma) -> np.ndarray:
        return sigma * self.zhat(rows, table)

    def describe(self) -> Dict[str, Any]:
        return {str(h): dict(st) for h, st in self.state.items()}

    @classmethod
    def from_state(cls, target: str, fitted: Dict[str, Any]) -> "RidgeShift":
        obj = cls(target)
        obj.state = {int(h): dict(st) for h, st in fitted.items()}
        return obj


MAKERS: Dict[str, Callable[[str], Any]] = {"zero": lambda t: Zero(), "ema_damped": lambda t: EMADamped(),
                                           "ridge": RidgeShift}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def cdf(x: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """The issued standardised distribution's CDF at ``x`` (its quantiles Q at TAU, as the PIT reads it)."""
    return np.interp(x, Q, fh.TAU, left=0.0, right=1.0)


def probabilities(mu: np.ndarray, sigma: np.ndarray, Q: np.ndarray) -> Dict[str, np.ndarray]:
    """P(up), and below / within / above the neutral band +-NEUTRAL x sigma around the origin."""
    below = cdf((-NEUTRAL * sigma - mu) / sigma, Q)
    above = 1 - cdf((NEUTRAL * sigma - mu) / sigma, Q)
    return {"up": 1 - cdf(-mu / sigma, Q), "below": below, "within": 1 - below - above, "above": above}


def score_session(fr: fh.Frame, rows: fh.Rows, sigma: np.ndarray, shifts: Dict[str, np.ndarray],
                  rel: Dict) -> Dict[str, Dict[str, Dict[str, float]]]:
    """``{'h5': {arm: {crps, brier, rps, n, n_dir, hits, signed}}}`` for one session; adds the bands' misses and the
    reliability bins of P(up) to ``rel``."""
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for h in HORIZONS:
        s = rows.horizon == h
        if not s.any():
            continue
        Q = fr.shape[fh.FRAME_HORIZONS.index(h)]
        y, sg = rows.y[s], sigma[s]
        moved = y != 0
        cat = np.select([y < -NEUTRAL * sg, y > NEUTRAL * sg], [0, 2], 1)
        per = {}
        for arm, mu_all in shifts.items():
            mu = mu_all[s]
            crps = sg * fh.crps((y - mu) / sg, Q) * 1e4
            p = probabilities(mu, sg, Q)
            up = (y > 0).astype(float)
            brier = (p["up"] - up)[moved] ** 2
            c1 = p["below"] - (cat == 0)
            c2 = p["below"] + p["within"] - (cat <= 1)
            rps = (c1 ** 2 + c2 ** 2) / 2
            dirn = moved & (mu != 0)
            per[arm] = {"crps": float(crps.mean()), "brier": float(brier.mean()) if moved.any() else np.nan,
                        "rps": float(rps.mean()), "n": int(s.sum()), "n_dir": int(dirn.sum()),
                        "hits": int((np.sign(mu[dirn]) == np.sign(y[dirn])).sum()),
                        "mean_abs_shift_sigma": float(np.mean(np.abs(mu) / sg))}
            pit = cdf((y - mu) / sg, Q)
            t = rel.setdefault((arm, h), {"n": 0, "miss": np.zeros(len(BANDS)), "bins": np.zeros((10, 3))})
            t["n"] += len(y)
            t["miss"] += np.array([((pit < (1 - b) / 2) | (pit > (1 + b) / 2)).sum() for b in BANDS])
            k = np.clip((p["up"][moved] * 10).astype(int), 0, 9)
            np.add.at(t["bins"], k, np.column_stack([np.ones(moved.sum()), p["up"][moved], up[moved]]))
        out[f"h{h}"] = per
    return out


def _paired(per: List[Dict], key: str, arm: str, metric: str) -> Optional[Dict[str, Any]]:
    pairs = [(r[key]["zero"][metric], r[key][arm][metric]) for r in per
             if key in r and np.isfinite(r[key]["zero"][metric]) and np.isfinite(r[key][arm][metric])]
    if not pairs:
        return None
    base = np.array([p[0] for p in pairs])
    other = np.array([p[1] for p in pairs])
    d = list(other - base)
    b = F.BOOTSTRAP
    iv = block_bootstrap(d, b["block_sessions"], b["resamples"], b["seed"], b["interval"])
    verdict = "no interval" if iv is None else "better" if iv[1] < 0 else "worse" if iv[0] > 0 else "inconclusive"
    return {"sessions": len(pairs), "zero": float(base.mean()), "arm": float(other.mean()),
            "diff": float(np.mean(d)), "share": float(np.mean(d) / base.mean()) if base.mean() else None,
            "interval": list(iv) if iv else None, "verdict": verdict}


def summarise(per: List[Dict], rel: Dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for h in HORIZONS:
        key = f"h{h}"
        res: Dict[str, Any] = {}
        for arm in ARMS:
            rows = [r[key][arm] for r in per if key in r]
            n_dir = sum(r["n_dir"] for r in rows)
            t = rel.get((arm, h))
            res[arm] = {
                "crps": float(np.mean([r["crps"] for r in rows])),
                "brier": float(np.nanmean([r["brier"] for r in rows])),
                "rps": float(np.mean([r["rps"] for r in rows])),
                "hit_rate": (sum(r["hits"] for r in rows) / n_dir) if n_dir else None, "n_dir": n_dir,
                "mean_abs_shift_sigma": float(np.mean([r["mean_abs_shift_sigma"] for r in rows])),
                "coverage": ({f"{int(100 * b)}": 1 - m / t["n"] for b, m in zip(BANDS, t["miss"])} if t else None),
                "reliability": ([{"bin": i / 10, "n": int(c[0]), "p": c[1] / c[0], "freq": c[2] / c[0]}
                                 for i, c in enumerate(t["bins"]) if c[0] > 0] if t else None),
            }
            if arm != "zero":
                res[arm]["vs_zero"] = {m: _paired(per, key, arm, m) for m in ("crps", "brier", "rps")}
        out[key] = res
    return out


# --------------------------------------------------------------------------
# The rolling-origin evaluation
# --------------------------------------------------------------------------

def blocks(manifest: Dict[str, Any], holdout_block: int = 30) -> List[Dict[str, Any]]:
    """The experiment's three checks, then its holdout in consecutive blocks of ``holdout_block`` sessions."""
    out = [{"block": f"check {b['check']}", "first": b["sessions"]["first"], "last": b["sessions"]["last"]}
           for b in manifest["split"]["checks"]["blocks"]]
    hold = manifest["split"]["holdout"]["sessions"]
    for k in range(0, len(hold), holdout_block):
        part = hold[k:k + holdout_block]
        out.append({"block": f"holdout {k // holdout_block + 1}", "first": part[0], "last": part[-1]})
    return out


def _rows(frames: Dict[str, fh.Frame], days: Sequence[str], every: int) -> fh.Rows:
    r = fh.Rows.concat([fh.frame_rows(frames[d], horizons=HORIZONS, every=every) for d in days])
    return r.take(np.isin(r.horizon, HORIZONS))


def fit_block(frames: Dict[str, fh.Frame], table: FeatureTable, train_days: Sequence[str]
              ) -> Tuple[fh.Candidate, Dict[str, Any]]:
    """The shared scale and every arm, fitted on ``train_days`` (origins every TRAIN_EVERY minutes)."""
    train = _rows(frames, train_days, fh.TRAIN_EVERY)
    scale = fm.candidate(SCALE, table)()
    scale.fit(train)
    sigma = np.sqrt(train.var) * scale.predict(train)
    arms = {a: MAKERS[a](table.target) for a in ARMS}
    for a in arms.values():
        a.fit(train, table, sigma)
    return scale, arms


def run(frames: Dict[str, fh.Frame], table: FeatureTable, blks: Sequence[Dict[str, Any]], sessions: Sequence[str],
        log: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    """Every block: the scale and the arms fitted on ``sessions`` before it, then scored on every origin of its
    sessions. Pooled and per-block results."""
    pooled, pooled_days, rel_all, out_blocks = [], [], {}, []
    for b in blks:
        train_days = [d for d in sessions if d < b["first"] and d in frames]
        test_days = [d for d in sessions if b["first"] <= d <= b["last"] and d in frames]
        scale, arms = fit_block(frames, table, train_days)
        per, rel = [], {}
        zs, zh = {h: [] for h in HORIZONS}, {h: [] for h in HORIZONS}
        for d in test_days:
            rows = _rows(frames, [d], 1)
            sigma = np.sqrt(rows.var) * scale.predict(rows)
            shifts = {a: arm.shift(rows, table, sigma) for a, arm in arms.items()}
            per.append(score_session(frames[d], rows, sigma, shifts, rel))
            zr = arms["ridge"].zhat(rows, table)
            for h in HORIZONS:
                s = rows.horizon == h
                zs[h].append(rows.y[s] / sigma[s])
                zh[h].append(zr[s])
        corr = {}
        for h in HORIZONS:
            a, c = np.concatenate(zs[h]), np.concatenate(zh[h])
            corr[f"h{h}"] = float(np.corrcoef(a, c)[0, 1]) if np.std(c) > 0 else None
        for k, t in rel.items():
            acc = rel_all.setdefault(k, {"n": 0, "miss": np.zeros(len(BANDS)), "bins": np.zeros((10, 3))})
            acc["n"] += t["n"]
            acc["miss"] += t["miss"]
            acc["bins"] += t["bins"]
        pooled += per
        pooled_days += test_days
        out_blocks.append({**b, "train": {"first": train_days[0], "last": train_days[-1], "sessions": len(train_days)},
                           "scored": len(test_days), "ridge": arms["ridge"].describe(), "ridge_corr": corr,
                           "results": summarise(per, rel)})
        log(f"{b['block']}: trained on {len(train_days)} sessions, scored {len(test_days)}")
    return {"kind": "direction", "version": DIRECTION_VERSION, "scale": SCALE, "horizons": list(HORIZONS),
            "settings": settings(), "blocks": out_blocks, "sessions": pooled_days,
            "results": summarise(pooled, rel_all),
            "per_session": {d: r for d, r in zip(pooled_days, pooled)}}


def settings() -> Dict[str, Any]:
    return {"ema": [EMA_FAST, EMA_SLOW], "vol_window": VOL_WINDOW, "slopes": [SLOPE_FAST, SLOPE_SLOW], "clip": CLIP,
            "damp": DAMP, "neutral": NEUTRAL, "z_winsor": Z_WINSOR, "z_cap": Z_CAP, "valid_share": VALID_SHARE,
            "alphas": list(ALPHAS), "families": list(DIRECTION_FAMILIES), "context": list(CONTEXT_FAMILIES),
            "bands": list(BANDS)}


def settings_hash() -> str:
    return hashlib.sha256(json.dumps(settings(), sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def _pct(p: Optional[Dict[str, Any]]) -> str:
    if p is None:
        return "-"
    iv = p["interval"]
    mark = {"better": " *", "worse": " !"}.get(p["verdict"], "")
    return (f"{100 * p['share']:+.2f} %{mark}" + (f" [{iv[0]:+.4f}, {iv[1]:+.4f}]" if iv else "")
            if p["share"] is not None else "-")


def write_report(res: Dict[str, Any], path: str) -> str:
    """The rolling-origin evaluation as markdown at ``path``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    st = res["settings"]
    L = [f"# Direction: {res['version']} on {res['experiment']}, {res['target']}", "",
         f"Code {res['code_revision'][:12]}; settings `{res['settings_hash']}`; experiment "
         f"`{res['experiment_hash'][:16]}`. Development only: the holdout blocks were examined by the scale "
         "experiment before this one existed - confirmation needs sessions after 2026-10-05.", "",
         f"Three centres on one scale ({res['scale']}, refitted per block; fan_rw_v2's shape): **zero** (no drift), "
         f"**ema_damped** ({st['damp']} x h x the EMA {st['ema'][0]}'s slope per minute), **ridge** (a learned shift "
         f"in the scale's sigmas, capped at +-{st['z_cap']}). Every origin of {len(res['sessions'])} sessions; each "
         "block trained on every session before it. Arm minus zero per session, 95 % moving-block bootstrap interval "
         "in the metric's units (* = whole interval below zero, better; ! = above, worse). Brier: P(up) against a "
         f"move above zero, moves of exactly zero left out. RPS: below / within / above +-{st['neutral']} sigma.", "",
         "## Pooled", "",
         "| Horizon | Arm | CRPS (bps) | vs zero | Brier | vs zero | RPS | vs zero | Hit rate | 50 / 80 / 90 % "
         "coverage | Mean shift (sigma) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, per in res["results"].items():
        for arm in ARMS:
            r = per[arm]
            v = r.get("vs_zero", {})
            cov = " / ".join(f"{100 * r['coverage'][b]:.1f}" for b in ("50", "80", "90"))
            hit = f"{100 * r['hit_rate']:.1f} %" if r["hit_rate"] is not None else "-"
            L.append(f"| {key[1:]} min | {arm} | {r['crps']:.4f} | {_pct(v.get('crps'))} | {r['brier']:.4f} | "
                     f"{_pct(v.get('brier'))} | {r['rps']:.4f} | {_pct(v.get('rps'))} | {hit} | {cov} | "
                     f"{r['mean_abs_shift_sigma']:.3f} |")
    L += ["", "## Per block (CRPS against zero)", "",
          "| Block | Sessions | Trained on | ema_damped 5 / 15 min | ridge 5 / 15 min | ridge's out-of-sample "
          "correlation 5 / 15 min |", "|---|---|---|---|---|---|"]
    for b in res["blocks"]:
        rr = b["results"]
        e = " / ".join(_pct(rr[f"h{h}"]["ema_damped"]["vs_zero"]["crps"]).split(" [")[0] for h in HORIZONS)
        g = " / ".join(_pct(rr[f"h{h}"]["ridge"]["vs_zero"]["crps"]).split(" [")[0] for h in HORIZONS)
        c = " / ".join("-" if b["ridge_corr"][f"h{h}"] is None else f"{b['ridge_corr'][f'h{h}']:+.3f}"
                       for h in HORIZONS)
        L.append(f"| {b['block']} ({b['first']} to {b['last']}) | {b['scored']} | {b['train']['sessions']} | "
                 f"{e} | {g} | {c} |")
    L += ["", "## The ridge as fitted per block", "",
          "Penalty chosen on the last 20 % of each block's training sessions; 'gain' is the validation MSE's "
          "improvement over predicting zero. Coefficients on standardised inputs (|coef| >= 0.002).", ""]
    for b in res["blocks"]:
        for h, s in b["ridge"].items():
            big = ", ".join(f"{c} {v:+.4f}" for c, v in zip(s["columns"], s["coef"]) if abs(v) >= 0.002) or "none"
            L.append(f"- {b['block']}, {h} min: alpha {s['alpha']:.0f}, gain {100 * s['valid_gain']:.3f} %"
                     f"{'' if s['used'] else ' (not used: zero)'}; {big}")
    L += ["", "## Reliability of P(up), pooled", "", "| Horizon | Arm | P(up) bin: forecast -> observed (n) |",
          "|---|---|---|"]
    for key, per in res["results"].items():
        for arm in ("ema_damped", "ridge"):
            cells = "; ".join(f"{x['p']:.2f} -> {x['freq']:.3f} ({x['n']:,})" for x in per[arm]["reliability"])
            L.append(f"| {key[1:]} min | {arm} | {cells} |")
    L.append("")
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path
