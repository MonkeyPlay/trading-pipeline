# tests/test_instruments.py
"""
The cash-index ETFs (QQQ, SPY, IWM): collected for the research datasets, never read
by an evidence snapshot - features/nq_evidence.py reads every collected
ASSET_SOURCES entry, so mapping one there would change what the registered snapshot
versions contain.
"""

import os
from datetime import date

import pytest

from collector.coverage import day_expectation
from config import ASSET_SOURCES, INSTRUMENTS, Config

ETFS = ("QQQ", "SPY", "IWM")


@pytest.mark.parametrize("symbol", ETFS)
def test_etf_is_a_stock_routed_smart(symbol):
    inst = INSTRUMENTS[symbol]
    assert inst.sec_type == "STK" and not inst.is_future and not inst.is_index
    assert inst.exchange == "SMART" and inst.primary_exchange in ("NASDAQ", "ARCA")
    assert inst.roll is None and inst.value_unit == "price"


@pytest.mark.parametrize("symbol", ETFS)
def test_etf_is_never_a_snapshot_input(symbol):
    assert symbol not in {src.symbol for src in ASSET_SOURCES.values()}


@pytest.mark.parametrize("symbol", ETFS)
def test_etf_day_needs_its_whole_regular_session(symbol):
    inst = INSTRUMENTS[symbol]
    assert day_expectation(inst, date(2026, 6, 10)) == (390, 390)
    assert day_expectation(inst, date(2026, 11, 27)) == (210, 210)     # early close


@pytest.mark.skipif("CONTEXT_SYMBOLS" in os.environ, reason="CONTEXT_SYMBOLS overridden in the environment")
def test_default_context_collects_the_etfs_and_not_2yy():
    assert set(ETFS) <= set(Config.collect_symbols())
    assert "2YY" not in Config.collect_symbols()
    assert ASSET_SOURCES["us2y_yield_fut"].optional
