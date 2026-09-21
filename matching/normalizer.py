# matching/normalizer.py
"""
Historical Analogue Matching engine for the NQ Opening Forecast System.
Normalizes pre-open price distances by historical volatility to find similar sessions.
"""

import numpy as np

_DEFAULT_PREV_CLOSE = 15000.0
_DEFAULT_VOL = 0.01
_DIRECTION_MAP = {"UP": 1.0, "FLAT": 0.0, "DOWN": -1.0}


def normalize_features(snapshot):
    """
    Normalizes a feature snapshot into a comparable vector:
      [ normalized gap, normalized overnight range, direction ]

    Gap and overnight range are scaled by (previous RTH close * historical
    volatility) so the units are "sigma of a typical day", which makes sessions
    at different price levels / volatility regimes directly comparable.
    """
    prev_close = snapshot.get("previous_rth_close") or _DEFAULT_PREV_CLOSE
    vol = snapshot.get("historical_volatility") or _DEFAULT_VOL
    if not vol or vol <= 0:
        vol = _DEFAULT_VOL
    if not prev_close or prev_close <= 0:
        prev_close = _DEFAULT_PREV_CLOSE

    scale = prev_close * vol
    gap = snapshot.get("gap", 0.0) or 0.0
    ovn_range = snapshot.get("overnight_range", 0.0) or 0.0
    direction = _DIRECTION_MAP.get(snapshot.get("pre_open_direction", "FLAT"), 0.0)

    return np.array([gap / scale, ovn_range / scale, direction], dtype=float)


def find_analogues(target_snapshot, historical_snapshots, k=5):
    """
    Finds the top k matching historical days using volatility-normalized
    Euclidean distance. The target day itself is always excluded.
    """
    target_vector = normalize_features(target_snapshot)
    target_day = target_snapshot.get("trading_day")

    matches = []
    for hist in historical_snapshots:
        hist_day = hist.get("trading_day")
        if hist_day is None or hist_day == target_day:
            continue

        distance = float(np.linalg.norm(target_vector - normalize_features(hist)))
        matches.append({
            "match_date": hist_day,
            "distance": distance,
            "similarity_score": 1.0 / (1.0 + distance),
            "snapshot_data": hist,
        })

    matches.sort(key=lambda x: x["distance"])
    results = []
    for rank, match in enumerate(matches[:k], start=1):
        match["ranking"] = rank
        results.append(match)
    return results
