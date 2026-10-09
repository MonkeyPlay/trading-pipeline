# forecaster/delivery.py
"""
The forecast in force for a session: the first usable run in a delivery order, with the
reason any earlier one was passed over - so a fallback is always recorded, never silent.

  usable    live runs: issued and acknowledged by the deadline (forecast_service.timely);
            historical replays: issued
  order     algorithm versions, most preferred first (contracts/nq_ml.delivery_order():
            the ML forecasts only once one is promoted to production, then B, then A)

delivered() is pure; record() stores the decision (journal.forecast_deliveries, migration
0030), stamped by the database clock.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, Tuple


def usable(run: Dict[str, Any]) -> bool:
    from forecaster.forecast_service import timely
    return timely(run) if run["mode"] == "live" else run["lifecycle_status"] == "issued"


def _state(run: Dict[str, Any]) -> str:
    if run["lifecycle_status"] == "issued":
        return "issued, not acknowledged by the deadline" if run["mode"] == "live" else "issued"
    reason = run.get("failure_reason")
    return run["lifecycle_status"] + (f": {reason}" if reason else "")


def delivered(runs: Sequence[Dict[str, Any]], order: Sequence[str], name: Callable[[str], str],
              ok: Callable[[Dict[str, Any]], bool] = usable) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(run, why)`` for the first usable run in ``order`` (``name`` labels an algorithm), ``(None, why)`` when no
    run is usable; ``why`` says what each earlier algorithm's runs were."""
    passed = []
    for algorithm in order:
        mine = [r for r in runs if r["algorithm_version"] == algorithm]
        good = [r for r in mine if ok(r)]
        if good:
            return good[0], f"{name(algorithm)}" + (f" (fallback: {'; '.join(passed)})" if passed else "")
        passed.append(f"{name(algorithm)} " + (", ".join(sorted({_state(r) for r in mine})) if mine else "not run"))
    return None, "no usable forecast (" + "; ".join(passed) + ")"
