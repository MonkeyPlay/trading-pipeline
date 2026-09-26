# tests/test_rolls.py
"""Front-contract choice from a possibly incomplete contract chain, and IB error triage."""

from datetime import date

from collector.ib_collector import is_pacing_violation
from collector.rolls import front_contracts
from config import INSTRUMENTS

ES_RULE = INSTRUMENTS["ES"].roll          # HMUZ, roll 8 days before expiry
YIELD_RULE = INSTRUMENTS["10Y"].roll      # every month, roll 3 days before expiry


def _row(cid, month, expiry):
    return {"contract_id": cid, "contract_month": month, "expiry": expiry}


ESM5 = _row(1, "202506", "20250620")
ESU5 = _row(2, "202509", "20250919")
ESZ5 = _row(3, "202512", "20251219")
ESH6 = _row(4, "202603", "20260320")
ESM6 = _row(5, "202606", "20260618")
ESU6 = _row(6, "202609", "20260918")
ESZ6 = _row(7, "202612", "20261218")
FULL = [ESM5, ESU5, ESZ5, ESH6, ESM6, ESU6, ESZ6]


def _front(chain, day, rule=ES_RULE):
    row = front_contracts(chain, [day], rule).get(day)
    return None if row is None else row["contract_id"]


def test_old_day_is_not_filed_under_the_current_contract():
    current_only = [ESU6, ESZ6]
    # The June 2025 contract is missing: May 2025 must be "not covered" (so the
    # collector discovers the expired chain), never September 2026.
    assert _front(current_only, date(2025, 5, 21)) is None
    assert _front(current_only, date(2026, 6, 5)) is None          # ESM6 missing and it was front
    # Days the chain does cover are unaffected.
    assert _front(current_only, date(2026, 9, 4)) == ESU6["contract_id"]
    assert _front(current_only, date(2026, 9, 11)) == ESZ6["contract_id"]   # after the Sep roll
    assert _front(current_only, date(2026, 9, 26)) == ESZ6["contract_id"]


def test_full_chain_follows_the_roll_rule():
    assert _front(FULL, date(2025, 5, 21)) == ESM5["contract_id"]
    assert _front(FULL, date(2025, 6, 11)) == ESM5["contract_id"]
    assert _front(FULL, date(2025, 6, 12)) == ESU5["contract_id"]   # the roll day: 8 days before 06-20
    assert _front(FULL, date(2026, 8, 7)) == ESU6["contract_id"]
    assert _front(FULL, date(2026, 9, 10)) == ESZ6["contract_id"]


def test_monthly_cycle_with_a_missing_month():
    oct_, nov = _row(10, "202610", "20261030"), _row(11, "202611", "20261127")
    assert _front([oct_, nov], date(2026, 9, 15), YIELD_RULE) is None     # September not discovered
    assert _front([oct_, nov], date(2026, 10, 15), YIELD_RULE) == 10
    sep = _row(9, "202609", "20260929")
    assert _front([sep, oct_, nov], date(2026, 9, 15), YIELD_RULE) == 9


def test_only_real_pacing_violations_back_off():
    assert is_pacing_violation(162, "Historical Market Data Service error message:Historical data request "
                                    "pacing violation")
    assert not is_pacing_violation(162, "Historical Market Data Service error message:HMDS query returned "
                                        "no data: ESU6@CME Trades")
    assert not is_pacing_violation(162, "Historical Market Data Service error message:API historical data "
                                        "query cancelled: 5")
    assert is_pacing_violation(420, "Invalid Real-time Query:Historical data request pacing violation")
    assert not is_pacing_violation(200, "No security definition has been found for the request")
