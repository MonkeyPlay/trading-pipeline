# collector/rolls.py
"""
Which futures contract stands for a symbol on each trading day.

The intermarket features compare a future's pre-open value with its value at the
previous NQ RTH close, and both must come from the same contract: a return that
straddles two expiries measures the calendar spread, not the market. So each
trading day gets exactly one contract, chosen by the instrument's ``RollRule``
from the contract chain IB reports, and the collector stores:

  - every day a contract is active, and
  - the one trading day *before* it becomes active (or before the window
    starts), so the first active day still has a same-contract reference close.

The resulting day -> contract map is recorded in ``active_contracts``; features
read it rather than re-deriving the roll. Nothing is back-adjusted or spliced.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from config import MONTH_CODES, RollRule
from collector.coverage import previous_trading_day


def contract_month(row) -> Optional[str]:
    """'YYYYMM' of a contract; falls back to its expiry month for older rows."""
    month = row["contract_month"] if "contract_month" in row.keys() else None
    if month:
        return str(month)[:6]
    expiry = row["expiry"]
    return str(expiry)[:6] if expiry else None


def expiry_date(row) -> Optional[date]:
    """Last trading date of a contract, or None when IB gave only a month."""
    expiry = str(row["expiry"] or "")
    if len(expiry) < 8 or not expiry[:8].isdigit():
        return None
    return datetime.strptime(expiry[:8], "%Y%m%d").date()


def eligible_chain(chain, rule: RollRule) -> list:
    """The contracts the rule may pick, nearest expiry first."""
    out = []
    for row in chain:
        month, last = contract_month(row), expiry_date(row)
        if month is None or last is None:
            continue
        if MONTH_CODES[int(month[4:6]) - 1] in rule.months:
            out.append(row)
    return sorted(out, key=expiry_date)


def front_contracts(chain, days: Iterable[date], rule: RollRule) -> Dict[date, object]:
    """
    ``{day: contract row}`` - for each day, the nearest eligible contract whose
    expiry is more than ``rule.days_before_expiry`` calendar days away. A day the
    chain cannot cover is left out; the caller treats that as "discover the chain".
    """
    ordered = eligible_chain(chain, rule)
    out = {}
    for d in days:
        for row in ordered:
            if expiry_date(row) - timedelta(days=rule.days_before_expiry) > d:
                out[d] = row
                break
    return out


@dataclass
class Segment:
    """One contract's share of the window."""
    contract: object                 # contracts row
    first_day: date                  # first active day inside the window
    last_day: date                   # last active day inside the window
    reference_day: date              # trading day before first_day, stored for its close


def segments(assignment: Dict[date, object]) -> List[Segment]:
    """Groups a day -> contract map into contiguous per-contract runs."""
    out: List[Segment] = []
    for d in sorted(assignment):
        row = assignment[d]
        if out and out[-1].contract["contract_id"] == row["contract_id"]:
            out[-1].last_day = d
        else:
            out.append(Segment(row, d, d, previous_trading_day(d)))
    return out
