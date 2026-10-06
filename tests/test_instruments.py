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
def test_default_context_collects_the_etfs():
    assert set(ETFS) <= set(Config.collect_symbols())


def test_2yy_is_gone():
    """Micro 2-Year Yield futures were dropped entirely on 2026-10-06 (too little history to use): no instrument,
    no intermarket source (migration 0018 removed its stored data)."""
    assert "2YY" not in INSTRUMENTS
    assert all(src.symbol != "2YY" for src in ASSET_SOURCES.values())
    assert "us2y_yield_fut" not in ASSET_SOURCES
