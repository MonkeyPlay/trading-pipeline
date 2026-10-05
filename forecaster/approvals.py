# forecaster/approvals.py
"""
One-time approvals of Claude requests confirmed in the dashboard. Claude requests are
started by hand only (forecaster/structure_llm.manual_requests): from a terminal, a
person types "send"; in the dashboard - whose jobs have no terminal - the confirmation
dialog shows what would be sent and its rough cost, and a click on its Send button
issues an approval that the job redeems.

An approval names its scope (the command, the sessions, the arms and the most requests
it allows), lives for TTL_MINUTES and is used once: redeeming deletes it first. A job
started without one, or with a stale, used or different one, sends nothing. This keeps
the collector, catch-up, cron and anything else from sending Claude requests by
accident; it is not a security boundary against someone with access to the machine.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPROVAL_DIR = os.path.join(_PROJECT_ROOT, "data", "approvals")
TTL_MINUTES = 10


def issue(scope: Dict[str, Any], directory: str = APPROVAL_DIR) -> str:
    """Records an approval of ``scope`` and returns its token."""
    os.makedirs(directory, exist_ok=True)
    token = secrets.token_hex(16)
    path = os.path.join(directory, f"{token}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"scope": scope, "issued_at": datetime.now(timezone.utc).isoformat()}, f)
    return token


def redeem(token: str, scope: Dict[str, Any], directory: str = APPROVAL_DIR,
           now: Optional[datetime] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(approved scope or None, why)``: uses the approval ``token`` up - it is deleted before it is checked - and
    returns its scope (with the ``max_requests`` it allows) when it was issued within TTL_MINUTES for exactly the
    keys of ``scope``."""
    if not token or not all(c in "0123456789abcdef" for c in token):
        return None, "no valid approval token"
    path = os.path.join(directory, f"{token}.json")
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        os.remove(path)
    except (OSError, ValueError):
        return None, "the approval does not exist or was already used"
    issued = datetime.fromisoformat(record["issued_at"])
    if (now or datetime.now(timezone.utc)) - issued > timedelta(minutes=TTL_MINUTES):
        return None, f"the approval is older than {TTL_MINUTES} minutes"
    approved = record["scope"]
    for key, value in scope.items():
        if approved.get(key) != value:
            return None, f"the approval was for {key} {approved.get(key)!r}, not {value!r}"
    return approved, f"approved in the dashboard for at most {approved.get('max_requests')} request(s)"
