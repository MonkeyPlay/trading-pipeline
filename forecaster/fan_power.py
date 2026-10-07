# forecaster/fan_power.py
"""
The activation rule's power (docs/fan_cond_ema.md, "Power"): on synthetic sessions with a
planted conditional directional signal of known size, how often does the selection and
activation of forecaster/fan_cond_ema.py switch a shift on - at the validation sizes the real
blocks had (31 to 55 sessions)? Synthetic results describe the rule, not the market.

  synthetic(kappa, n_sessions, seed)   sessions whose next moves depend on a persistent input
                                       g (stored as the EMA set's m5_gap) with a sign by phase:
                                       reversal overnight, continuation midday and afternoon
  oracle(frames, table, sessions)      the true conditional mean's correlation with the move
                                       (in sigmas) and its CRPS gain over zero - the effect size
  power(kappas, sizes, seeds)          activation rates per signal size and validation size
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from scipy.signal import lfilter

from forecaster import fan_cond_ema as ce
from forecaster import fan_direction as fd
from forecaster import fan_harness as fh
from forecaster.fan_benchmark import DAY_SLOTS
from forecaster.fan_features import Feature, FeatureTable
from forecaster.fan_scoring import PHASE_OF_SLOT, PHASES

SIGN = {"overnight": -1.0, "midday": 1.0, "afternoon": 1.0}
RHO = 0.98
SIGMA = 2e-4
PHASE_SIGN = np.array([SIGN.get(PHASES[p][0], 0.0) for p in PHASE_OF_SLOT])


def synthetic(kappa: float, n_sessions: int = 150, seed: int = 0, names: Sequence[str] = (),
              planted: str = "NQ.m5_gap"
              ) -> Tuple[Dict[str, fh.Frame], FeatureTable, List[Dict[str, Any]], List[str], Dict[str, np.ndarray]]:
    """``(frames, table, blocks, sessions, g)``: a drift of kappa x g x sign(phase) per minute in units of the
    minute's sigma; every other input is noise. The last 20 sessions are the evaluation block. kappa = 0 is the
    no-signal control. ``names``: the table's inputs (default the Conditional EMA set); ``planted``: the input that
    carries g."""
    rng = np.random.default_rng(seed)
    names = list(names) or [f"NQ.{k}" for k in (*ce.OWN_CONTEXT, *ce.BASE_CONTEXT, *ce.EMA_FEATURES)]
    X = rng.normal(size=(n_sessions, DAY_SLOTS, len(names))).astype(np.float32)
    frames, sessions, gs = {}, [], {}
    var = np.array([np.full(DAY_SLOTS, SIGMA ** 2 * h) for h in fh.FRAME_HORIZONS])
    for i in range(n_sessions):
        e = rng.normal(size=DAY_SLOTS)
        e[0] = 0.0
        g = lfilter([0.2], [1, -RHO], e)
        g /= g.std()
        X[i, :, names.index(planted)] = g
        r = SIGMA * (rng.normal(size=DAY_SLOTS) + kappa * g * PHASE_SIGN)
        lp = np.log(20000) + np.concatenate([[0.0], np.cumsum(r[:-1])])
        d = str(np.datetime64("2026-01-01") + np.timedelta64(i, "D"))
        frames[d] = fh.Frame(d, DAY_SLOTS, lp, var, np.tile(fh.NORMAL_Q, (len(fh.FRAME_HORIZONS), 1)),
                             np.zeros_like(var, dtype=bool))
        sessions.append(d)
        gs[d] = g
    table = FeatureTable(sessions, "NQ", [Feature(n, n.split(".")[0], ce.GROUP, n.split(".")[1]) for n in names], X)
    return frames, table, [{"block": "test", "first": sessions[-20], "last": sessions[-1]}], sessions, gs


def oracle(frames: Dict[str, fh.Frame], gs: Dict[str, np.ndarray], kappa: float, days: Sequence[str]
           ) -> Dict[str, Dict[str, float]]:
    """Per horizon over ``days`` (every origin): the true conditional mean of the move -
    kappa x sigma x g_t x sum_k rho^k sign(phase at t + k) - its correlation with the realised move in sigmas, and the
    CRPS gain of the fan shifted by it over zero (a share of zero's CRPS): the most any model could gain."""
    out = {}
    for h in ce.HORIZONS:
        w = RHO ** np.arange(h)
        sgn = np.concatenate([PHASE_SIGN, np.zeros(h)])
        drift = np.array([np.dot(w, sgn[t:t + h]) for t in range(DAY_SLOTS)])
        z, m, c0, c1 = [], [], [], []
        for d in days:
            rows = fd._rows(frames, [d], 1)
            s = rows.horizon == h
            sig = np.sqrt(rows.var[s])
            mu = kappa * SIGMA * gs[d][rows.slot[s]] * drift[rows.slot[s]]
            y = rows.y[s]
            z.append(y / sig)
            m.append(mu / sig)
            c0.append(np.mean(sig * fh.crps(y / sig, fh.NORMAL_Q)))
            c1.append(np.mean(sig * fh.crps((y - mu) / sig, fh.NORMAL_Q)))
        z, m = np.concatenate(z), np.concatenate(m)
        out[f"h{h}"] = {"corr": float(np.corrcoef(z, m)[0, 1]) if m.std() > 0 else 0.0,
                        "crps_gain": float(1 - np.mean(c1) / np.mean(c0))}
    return out


def power(kappas: Sequence[float], sizes: Sequence[int], seeds: int, log=print) -> List[Dict[str, Any]]:
    """Per (kappa, sessions): the oracle effect size and how often each fitted arm activated at each horizon, over
    ``seeds`` independent synthetic histories; the validation is the last 20 % of the sessions before the
    evaluation block."""
    out = []
    for n in sizes:
        for kappa in kappas:
            act = {f"{a}|h{h}": 0 for a in ("context", "ema") for h in ce.HORIZONS}
            eff = []
            for k in range(seeds):
                frames, table, blocks, sessions, gs = synthetic(kappa, n + 20, seed=1000 * n + k)
                shape_of = lambda d, h: frames[d].shape[fh.FRAME_HORIZONS.index(h)]
                _, shifts, info = ce.fit_block(frames, table, sessions[:n], shape_of, fh.Identity)
                for a, h in shifts:
                    act[f"{a}|h{h}"] += shifts[(a, h)].active
                eff.append(oracle(frames, gs, kappa, sessions[n:]))
                val = info["val_days"][2]
            row = {"kappa": kappa, "train_sessions": n, "valid_sessions": val, "seeds": seeds,
                   "activation": {k: v / seeds for k, v in act.items()},
                   "oracle": {f"h{h}": {m: float(np.mean([e[f"h{h}"][m] for e in eff])) for m in ("corr", "crps_gain")}
                              for h in ce.HORIZONS}}
            out.append(row)
            log(f"n {n} (valid {val}) kappa {kappa}: oracle corr 5/15 "
                f"{row['oracle']['h5']['corr']:.3f}/{row['oracle']['h15']['corr']:.3f}, gain "
                f"{100 * row['oracle']['h5']['crps_gain']:.2f}/{100 * row['oracle']['h15']['crps_gain']:.2f} %; "
                f"activation {row['activation']}")
    return out
