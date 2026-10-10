# forecaster/market_labels.py
"""
Each market's own NQ-v2 labels (contracts/market_labels.py), for the pooled ML bundle's ES and RTY
rows and for the parity check of NQ's.

  market_snapshot(conn, symbol, day)   the market's evidence frozen at the profile's cutoff - the same
                                       payload features/nq_evidence.build_snapshot freezes for NQ
                                       (references, ATRs, first-level candidates, overnight bars),
                                       built for ``symbol`` and never stored - with its thresholds
                                       recomputed under the market's convention
  market_outcome(conn, snapshot)       the label version's compute_outcome on the market's own realised
                                       bars: every target, reason and measurement exactly as NQ's
  with_thresholds(snapshot, market)    a snapshot whose T / B / A are the market's convention's (the
                                       NQ convention reproduces the stored ones)

Nothing here copies NQ's labels or levels onto another market: each market's candidates, thresholds
and outcomes come from its own bars and contract.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from contracts import market_labels as mlab
from contracts import nq_prompt_v2 as defs
from features.nq_evidence import SnapshotError, build_snapshot
from forecaster.labels_prompt_v2 import compute_outcome, load_realised_bars


def _plain(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The payload as the journal would store it (Decimals and Fractions as strings) - so a built snapshot and a
    stored one are read alike."""
    return json.loads(defs.canonical_json(payload))


def with_thresholds(snapshot: Dict[str, Any], market: str) -> Dict[str, Any]:
    """A copy of ``snapshot`` whose payload thresholds are ``market``'s convention's (contracts/market_labels)."""
    payload = dict(snapshot["payload"])
    payload["thresholds"] = mlab.thresholds(market, payload)
    return {**snapshot, "payload": payload}


def market_snapshot(conn, symbol: str, day: str, profile: str = defs.DEFAULT_PROFILE) -> Optional[Dict[str, Any]]:
    """``symbol``'s snapshot of ``day`` (never stored), its thresholds under its market convention; None when none can
    be built (no session, no contract)."""
    try:
        snap = build_snapshot(conn, day, profile, symbol=symbol)
    except SnapshotError:
        return None
    out = {"snapshot_id": None, "symbol": symbol, "session_date": day, "contract_id": snap.contract_id,
           "cutoff_at": snap.cutoff_at, "payload": _plain(snap.payload), "data_mode": snap.data_mode}
    return with_thresholds(out, symbol)


def market_outcome(conn, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """The label version's outcome of ``snapshot`` from its own contract's realised bars."""
    return compute_outcome(snapshot, load_realised_bars(conn, snapshot))
