# collector/rolls.py
"""
Which futures contract stands for a symbol on each trading day.

The intermarket features compare a future's pre-open value with its value at the
previous NQ RTH close, and both must come from the same contract: a return that
straddles two expiries measures the calendar spread, not the market. So each
trading day gets exactly one contract, chosen by the instrument's ``RollRule``
from the contract chain IB reports, and the collector stores:

  - every day a contract is active, and
  - the ``warmup`` trading days *before* it becomes active (or before the window
    starts): the last of them gives the first active day a same-contract
    reference close, and all of them give intraday indicators computed on the
    new contract alone (e.g. a 5-minute EMA200, ~4 sessions) enough history on
    the roll day. ``config.Config.ROLL_WARMUP_SESSIONS`` sets how many (7).

The resulting day -> contract map is recorded in ``active_contracts``; features
read it rather than re-deriving the roll. Nothing is back-adjusted or spliced.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

from config import MONTH_CODES, RollRule
from collector.coverage import is_trading_day, previous_trading_days


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
    warmup_days: List[date]          # trading days before first_day, stored from this contract

    @property
    def reference_day(self) -> date:
        """The trading day before first_day: its close is the first day's reference."""
        return self.warmup_days[-1]


def segments(assignment: Dict[date, object], warmup: int = 1) -> List[Segment]:
    """Groups a day -> contract map into contiguous per-contract runs."""
    if warmup < 1:
        raise ValueError("warmup must be >= 1: the day before activation holds the reference close")
    out: List[Segment] = []
    for d in sorted(assignment):
        row = assignment[d]
        if out and out[-1].contract["contract_id"] == row["contract_id"]:
            out[-1].last_day = d
        else:
            out.append(Segment(row, d, d, previous_trading_days(d, warmup)))
    return out


def upcoming_roll(chain, after: date, rule: RollRule, warmup: int,
                  horizon_days: int = 45) -> Optional[Segment]:
    """
    The next contract to become front after ``after``, if its warm-up period has
    already started by ``after``: a Segment whose ``warmup_days`` are the
    warm-up days on or before ``after`` (``first_day``/``last_day`` are the
    activation day). Collecting those days as they occur means the new contract
    already has its history on the morning it takes over.
    """
    days = [after + timedelta(days=i) for i in range(horizon_days + 1)]
    fronts = front_contracts(chain, days, rule)
    current = fronts.get(after)
    if current is None:
        return None
    for d in days[1:]:
        row = fronts.get(d)
        if row is None:
            return None
        if row["contract_id"] != current["contract_id"]:
            if not is_trading_day(d):
                continue
            due = [w for w in previous_trading_days(d, warmup) if w <= after]
            return Segment(row, d, d, due) if due else None
    return None
