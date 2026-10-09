# forecaster/ml_train.py
"""
Training the NQ direction models offline (contracts/nq_ml.py): the dataset both the
development comparison (forecaster/ml_eval.py) and the artifacts are built from, and the
artifacts themselves.

    python scripts/nq_journal.py ml-train      # data/models/nq_ml/<version>/ (never overwritten)

  dataset(conn)   the research_0929 pool's historical snapshots, direction_15m as labelled
                  (the latest outcome revision), the features of every config as of each
                  cutoff, and the pooled candidate's rows (NQ, ES and RTY, each with its own
                  features and its own label - forecaster/ml_features.own_direction)
  train(conn)     every model of contracts/nq_ml.ALGORITHMS: its config's rows of the
                  labelled sessions, the family contracts/nq_ml.FAMILY names, tuned on the
                  window's last quarter, refitted on all of it, saved with its manifest
                  (training dates, sessions, rows per instrument, class counts, tuning scores,
                  software versions, sha256). The forecasts it issues start after its last
                  training session (forecaster/ml_service.py)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from contracts import nq_ml as ml
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import ml_features as mf
from forecaster import ml_model as mm


@dataclass
class Data:
    snaps: List[Dict[str, Any]]
    days: List[str]
    y: Dict[str, Optional[str]]
    fs: mf.FeatureSet
    loaded: Dict[str, mf.InstrumentBars]
    X: Dict[str, pd.DataFrame]                 # config -> NQ rows (the pooled config's NQ rows included)
    PX: pd.DataFrame                           # the pooled candidate's rows, indexed (instrument, day)
    py: pd.Series                              # their labels


def pool(conn, profile: str = ml.PROFILE) -> List[Dict[str, Any]]:
    """The profile's historical snapshots (one per session, live captures left out), oldest first."""
    version = defs.PROFILES[profile].snapshot_version
    return [s for s in store.list_snapshots(conn, "2000-01-01", "2100-01-01", version)
            if s["data_mode"] != "live_capture"]


def labels(conn, snaps: Sequence[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """day -> direction_15m as labelled (the latest outcome revision of the session's snapshot), None without one."""
    history = store.outcome_history(conn, defs.LABEL_VERSION)
    out = {}
    for s in snaps:
        revs = history.get(s["snapshot_id"]) or []
        out[str(s["session_date"])] = ((revs[-1]["labels"].get(ml.TARGET) or {}).get("label") if revs else None)
    return out


def dataset(conn, profile: str = ml.PROFILE) -> Data:
    snaps = pool(conn, profile)
    days = [str(s["session_date"]) for s in snaps]
    y = labels(conn, snaps)
    loaded = mf.load(conn, mf.ALL_SYMBOLS, (cal.session(days[0]).session_date - timedelta(days=200)).isoformat(),
                     days[-1])
    fs = mf.build(conn, days, {str(s["session_date"]): s for s in snaps}, loaded=loaded)
    X = {cfg: mf.matrix(fs, days, cfg) for cfg in ml.CONFIGS}
    p = defs.PROFILES[profile]
    rows, labs, index = [], [], []
    for sym in ml.POOLED_INSTRUMENTS:
        own = fs.own[sym]
        for d in days:
            if d not in own.index:
                continue
            s = cal.session(d)
            lab = y[d] if sym == "NQ" else mf.own_direction(loaded[sym], s, cal.ny_instant(s.session_date, p.cutoff))
            rows.append([own.loc[d, f.name] for f in ml.OWN_FEATURES]
                        + [fs.rows[d]["event_preopen"], fs.rows[d]["event_session"]]
                        + [1.0 if sym == x else 0.0 for x in ml.POOLED_INSTRUMENTS[1:]])
            labs.append(lab)
            index.append((sym, d))
    PX = pd.DataFrame(rows, columns=ml.POOLED_FEATURES, index=pd.MultiIndex.from_tuples(index))
    return Data(snaps, days, y, fs, loaded, X, PX, pd.Series(labs, index=PX.index, dtype=object))


def training_rows(data: Data, version: str, days: Sequence[str]):
    """``(X, y, rows per instrument)`` of a model's training sessions ``days`` (labelled ones only)."""
    cfg = ml.CONFIG_OF[version]
    if cfg == "pooled":
        keep = set(days)
        idx = [(s, d) for (s, d) in data.PX.index if d in keep and isinstance(data.py[(s, d)], str)]
        per = {s: sum(1 for x, _ in idx if x == s) for s in ml.POOLED_INSTRUMENTS}
        return data.PX.loc[idx].to_numpy(), [data.py[i] for i in idx], per
    lab = [d for d in days if isinstance(data.y[d], str)]
    return data.X[cfg].loc[lab].to_numpy(), [data.y[d] for d in lab], {"NQ": len(lab)}


def train(conn, root: Optional[str] = None, data: Optional[Data] = None, until: Optional[str] = None,
          versions: Sequence[str] = ml.ALGORITHMS) -> Dict[str, Dict[str, Any]]:
    """Trains and saves every model (see the module docstring); returns the manifests."""
    data = data or dataset(conn)
    days = [d for d in data.days if until is None or d <= until]
    labelled = [d for d in days if isinstance(data.y[d], str)]
    out = {}
    for version in versions:
        X, y, per = training_rows(data, version, labelled)
        family = ml.FAMILY[version]
        model, params, tuning = mm.tune(X, y, family)
        training = {"from": labelled[0], "to": labelled[-1], "sessions": len(labelled), "rows": per,
                    "class_counts": {c: int(sum(1 for v in y if v == c)) for c in ml.CLASSES},
                    "label_version": defs.LABEL_VERSION, "labels": "the latest outcome revision of each session's "
                                                                   "snapshot when trained",
                    "profile": ml.PROFILE, "snapshot_version": defs.PROFILES[ml.PROFILE].snapshot_version}
        out[version] = mm.save(version, model, family, params, training, root, tuning)
    return out
