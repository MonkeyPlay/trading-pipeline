# forecaster/live_synthesis.py
"""
Arm D in a live capture (forecaster/live_capture.py, ``nq_journal.py live --with-d``):
the synthesis of arm B's live evidence, in force only when the database issues and
acknowledges it by the deadline - otherwise the numerical forecast stands.

  1. one request per live snapshot: a snapshot with a synthesis request in the
     inference ledger is never sent again - its run, or the reason it has none, is
     what the session has
  2. no request without a manual approval (structure_llm.manual_requests: a person
     typed "send" or redeemed a one-time dashboard approval), and none once the
     deadline has passed on the capture's clock
  3. the request is built from arm B's frozen live evidence - the live snapshot, its
     rule-based annotation and analogue set, the ids of the live B run - and recorded
     in the inference ledger before it is sent
  4. the answer is awaited until the deadline; then the forecast in force is decided
     and recorded (delivered: the first timely run in DELIVERY_ORDER, or none)
  5. an answer after the deadline is still stored, for at most LATE_WAIT_S more: the
     database makes it late (no issued_at) and it never replaces the forecast
     delivered. No answer by then: a failed run says so, and the late answer is dropped.

Timely is the database's verdict (forecast_service.timely): issued and acknowledged by
the deadline on its clock. The capture's clock only decides how long to wait - on the
database's own host the two agree.

Nothing here has run on a trading day; it is tested with simulated provider answers
(tests/test_nq_journal.py). Paid requests need the approval in 2.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from contracts import nq_forecast as fc
from contracts import nq_prompt_v2 as defs
from database import journal_store as store
from features import calendar as cal
from forecaster import llm_arms as la
from forecaster import structure_llm as sl
from forecaster.forecast_service import timely
from forecaster.forecast_validation import ForecastInputError

# The forecast in force at the deadline: D when timely, else the numerical forecasts - arm B, then arm A. A draft
# until the prospective A/B/D comparison is frozen at its cutoff (docs/forecasting_audit.md).
DELIVERY_ORDER = (fc.SYNTHESIS_VERSION, fc.BASELINE_VERSION, fc.PRIOR_VERSION)
ARM = {fc.SYNTHESIS_VERSION: "D", fc.BASELINE_VERSION: "B", fc.PRIOR_VERSION: "A"}
# How long an answer is still awaited after the deadline, to be stored as late (D took 25-30 s at effort medium).
LATE_WAIT_S = 120.0


def deadline(day: str) -> datetime:
    return cal.ny_instant(date.fromisoformat(day), fc.LIVE_DEADLINE_ET)


def delivered(runs: Sequence[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The forecast in force at the deadline among a session's live runs: ``(run, why)`` for the first timely one in
    DELIVERY_ORDER, ``(None, why)`` when none was issued and acknowledged by the deadline."""
    passed = []
    for algorithm in DELIVERY_ORDER:
        mine = [r for r in runs if r["algorithm_version"] == algorithm and r["mode"] == "live"]
        on_time = [r for r in mine if timely(r)]
        if on_time:
            why = f"{ARM[algorithm]} timely" + (f" ({'; '.join(passed)})" if passed else "")
            return on_time[0], why
        passed.append(f"{ARM[algorithm]} " + (", ".join(sorted({_state(r) for r in mine})) if mine else "not run"))
    return None, "no live run issued and acknowledged by the deadline (" + "; ".join(passed) + ")"


def _state(run: Dict[str, Any]) -> str:
    return "issued, not acknowledged by the deadline" if run["lifecycle_status"] == "issued" else run[
        "lifecycle_status"]


def session_delivered(conn, day: str, profile: str = defs.DEFAULT_PROFILE) -> Tuple[Optional[Dict[str, Any]], str]:
    """delivered() over every stored live run of the session (a check of the capture's 'delivered' step)."""
    runs = [store.get_forecast_run(conn, r["run_id"]) for r in store.list_forecast_runs(conn, day, day, profile,
                                                                                         "live")]
    return delivered(runs)


def requests_of(conn, snapshot_id: str) -> List[Dict[str, Any]]:
    """The synthesis requests recorded for ``snapshot_id``, with the run each produced (None: no stored answer)."""
    rows = conn.execute(
        "SELECT r.request_id, r.created_at, f.run_id FROM journal.inference_requests r "
        "LEFT JOIN journal.forecast_runs f ON f.request_id = r.request_id "
        "WHERE r.snapshot_id = %s AND r.protocol_version = %s ORDER BY r.created_at;",
        (snapshot_id, fc.SYNTHESIS_VERSION)).fetchall()
    return [{"request_id": str(r["request_id"]), "created_at": r["created_at"],
             "run_id": None if r["run_id"] is None else str(r["run_id"])} for r in rows]


@dataclass
class Attempt:
    """Arm D of one capture: skipped (``reason``), stored before (``run``), or sent and awaited."""
    status: str                                   # skipped | stored | sent
    reason: Optional[str] = None
    run: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None
    prep: Optional[Dict[str, Any]] = None
    started: Optional[datetime] = None
    answer: Optional[Future] = field(default=None, repr=False)

    def wait(self, conn, seconds: float) -> Optional[Dict[str, Any]]:
        """The run once the answer has come within ``seconds`` - stored as it came (an error a failed run) - else
        None (still awaited)."""
        if self.run is not None or self.answer is None:
            return self.run
        try:
            message = self.answer.result(timeout=max(0.0, seconds))
        except FutureTimeout:
            return None
        except Exception as e:                                           # the provider's error: a failed run
            self.run = la._finish(conn, self.prep, self.request_id, self.started, error=f"{type(e).__name__}: {e}")
            return self.run
        self.run = la._finish(conn, self.prep, self.request_id, self.started, message)
        return self.run

    def abandon(self, conn, waited_s: float) -> Dict[str, Any]:
        """A failed run for an answer that never came (a later one is dropped: the request is closed)."""
        if self.run is None:
            self.run = la._finish(conn, self.prep, self.request_id, self.started,
                                  error=f"no answer {waited_s:.0f} s after the deadline")
        return self.run


@dataclass
class LiveSynthesis:
    """Arm D for a capture: the Claude client (a simulated one in the tests), how a request is sent and how long a
    late answer is awaited."""
    client: Any
    send: Callable[[Any, Dict[str, Any]], Any] = sl.send
    late_wait_s: float = LATE_WAIT_S

    def request(self, conn, snapshot: Dict[str, Any], annotation: Optional[Dict[str, Any]],
                aset: Optional[Dict[str, Any]], profile: str, clock: Callable[[], datetime],
                event: Callable[..., Any]) -> Attempt:
        """Sends the synthesis of the live evidence (see the module docstring, 1-3) without waiting for it; every
        outcome is a capture step."""
        def skip(reason, **detail):
            event("synthesis_skipped", reason=reason, **detail)
            return Attempt("skipped", reason=reason)

        if annotation is None or aset is None:
            return skip("no rule-based annotation and analogue set to synthesise")
        if annotation["integrity_status"] != "ok":
            return skip("the annotation is contaminated (P1 stop rule)")
        earlier = requests_of(conn, snapshot["snapshot_id"])
        if earlier:
            answered = [r for r in earlier if r["run_id"]]
            if answered:
                run = store.get_forecast_run(conn, answered[-1]["run_id"])
                event("synthesis_skipped", reason="requested before: never sent twice", run_id=run["run_id"],
                      status=run["lifecycle_status"])
                return Attempt("stored", reason="requested before", run=run)
            return skip("requested before without a stored answer: never sent twice",
                        request_id=earlier[-1]["request_id"])
        if not sl._manual:
            return skip("not approved: Claude requests are started by hand only")
        due = deadline(str(snapshot["session_date"]))
        if clock() >= due:
            return skip("the deadline had passed before the request", deadline=due.isoformat())
        try:
            prep = la._prepare(conn, snapshot, annotation, aset, profile,
                               store.outcome_history(conn, defs.LABEL_VERSION), mode="live")
        except ForecastInputError as e:
            return skip(f"the live evidence does not fit together: {e}")
        started = datetime.now(timezone.utc)
        request_id = la._record(conn, prep, "live")
        event("synthesis_requested", request_id=request_id, request_hash=prep["request_hash"])
        answer: Future = Future()

        def work():
            try:
                answer.set_result(self.send(self.client, prep["params"]))
            except BaseException as e:                                   # noqa: BLE001 - every outcome is recorded
                answer.set_exception(e)
        threading.Thread(target=work, name="LiveSynthesis", daemon=True).start()
        return Attempt("sent", request_id=request_id, prep=prep, started=started, answer=answer)
