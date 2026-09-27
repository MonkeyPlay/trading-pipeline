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


# --------------------------------------------------------------------------
# Standardised matching (analogue_baseline_v2)
# --------------------------------------------------------------------------

def v1_match_vector(snapshot):
    """
    ``({'gap_sigma', 'overnight_range_sigma'}, scale)`` from a v1 snapshot: gap and
    overnight range in units of one day's volatility (previous RTH close x
    historical volatility). No direction flag: the gap's sign is already in it.
    Values are None when the snapshot lacks what they need.
    """
    prev_close = snapshot.get("previous_rth_close")
    vol = snapshot.get("historical_volatility")
    if not prev_close or not vol or prev_close <= 0 or vol <= 0:
        return {"gap_sigma": None, "overnight_range_sigma": None}, None
    scale = float(prev_close) * float(vol)
    gap, ovn = snapshot.get("gap"), snapshot.get("overnight_range")
    return ({"gap_sigma": None if gap is None else float(gap) / scale,
             "overnight_range_sigma": None if ovn is None else float(ovn) / scale}, scale)


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def rank_analogues(target, candidates, keys, min_dims=1):
    """
    ``candidates`` (dicts with 'session_date' and 'vector' {key: value}) ranked by
    their distance to ``target`` ({key: value}), closest first.

    Each key is standardised by its mean and standard deviation over the
    candidates - all earlier sessions, so nothing about the target's future is
    used - and the distance is the root mean square of the standardised
    differences over the keys both sides have, so a missing input neither
    counts as a match nor as a mismatch. Candidates sharing fewer than
    ``min_dims`` keys with the target are left out. Each returned candidate
    gains 'distance', 'similarity_score' (1 / (1 + distance)) and 'n_dims'.
    """
    stats = {}
    for key in keys:
        vals = np.array([v for c in candidates if (v := _finite(c["vector"].get(key))) is not None])
        if len(vals) >= 2 and vals.std(ddof=1) > 0:
            stats[key] = (float(vals.mean()), float(vals.std(ddof=1)))
    ranked = []
    for c in candidates:
        diffs = []
        for key, (mean, sd) in stats.items():
            t, v = _finite(target.get(key)), _finite(c["vector"].get(key))
            if t is not None and v is not None:
                diffs.append((t - v) / sd)
        if len(diffs) < max(1, min_dims):
            continue
        distance = float(np.sqrt(np.mean(np.square(diffs))))
        ranked.append({**c, "distance": distance, "similarity_score": 1.0 / (1.0 + distance),
                       "n_dims": len(diffs)})
    ranked.sort(key=lambda m: (m["distance"], -int(str(m["session_date"]).replace("-", ""))))
    return ranked
