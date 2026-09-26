# config.py
"""
Configuration manager for the Opening Forecast System.
Loads and validates settings from environment variables or a local .env file.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Instrument:
    """Static metadata for one tracked instrument."""
    symbol: str
    name: str
    exchange: str
    tick_size: float
    multiplier: Optional[str]
    sec_type: str = "FUT"

    @property
    def is_index(self):
        """Cash indices have no expiry and no traded volume."""
        return self.sec_type == "IND"


# The instruments the pipeline follows.
#
# The three futures share the same RTH window (09:30-16:00 ET), the same 18:00 ET
# Globex roll and the same holiday calendar, so features/session_windows.py and
# collector/coverage.py apply to each without change. They also share the
# quarterly Mar/Jun/Sep/Dec contract cycle, so one EXPIRY covers all of them.
#
# VIX is the cash volatility index, not a future: it has no expiry and no volume,
# and it is collected as pre-open *context* for the futures rather than forecast
# in its own right. See CONTEXT_SYMBOLS below.
INSTRUMENTS: Dict[str, Instrument] = {
    "ES": Instrument("ES", "S&P 500 E-mini", "CME", 0.25, "50"),
    "NQ": Instrument("NQ", "Nasdaq-100 E-mini", "CME", 0.25, "20"),
    "RTY": Instrument("RTY", "Russell 2000 E-mini", "CME", 0.10, "50"),
    "VIX": Instrument("VIX", "CBOE Volatility Index", "CBOE", 0.01, None, sec_type="IND"),
}


class Config:
    # Database Settings (PostgreSQL + TimescaleDB connection URLs)
    #   DATABASE_URL     - the long-lived production store (collector + pipeline write here)
    #   DEV_DATABASE_URL - throwaway store for populate_mock_data.py / experiments
    DATABASE_URL = os.getenv(
        "DATABASE_URL", "postgresql://trading:trading@localhost:5432/trading_pipeline"
    )
    DEV_DATABASE_URL = os.getenv(
        "DEV_DATABASE_URL", "postgresql://trading:trading@localhost:5432/trading_pipeline_dev"
    )
    SCHEMA_PATH = os.getenv("SCHEMA_PATH", os.path.join(_PROJECT_ROOT, "database", "schema.sql"))

    # IBKR Connection Settings
    IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
    # 4002 = Gateway paper, 4001 = Gateway live, 7497 = TWS paper, 7496 = TWS live
    IB_PORT = int(os.getenv("IB_PORT", "4002"))
    IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

    # LLM Settings
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")  # e.g. gpt-4o-mini, claude-3-5-sonnet-latest

    # Instrument Settings
    #   SYMBOLS         - instruments that are forecast (and therefore collected)
    #   CONTEXT_SYMBOLS - collected only as pre-open context for those forecasts;
    #                     never forecast themselves
    #   EXPIRY          - the shared quarterly contract month; override one
    #                     instrument on its own with e.g. RTY_EXPIRY=202612
    SYMBOLS: List[str] = [
        s.strip().upper() for s in os.getenv("SYMBOLS", "ES,NQ,RTY").split(",") if s.strip()
    ]
    CONTEXT_SYMBOLS: List[str] = [
        s.strip().upper() for s in os.getenv("CONTEXT_SYMBOLS", "VIX").split(",") if s.strip()
    ]
    EXPIRY = os.getenv("EXPIRY", "202612")

    # Application Settings
    TIMEZONE = "America/New_York"
    FEATURE_VERSION = "v1.0"
    PROMPT_VERSION = "v1.0"

    @classmethod
    def collect_symbols(cls):
        """Everything the collector should fetch: forecast targets plus context."""
        return cls.SYMBOLS + [s for s in cls.CONTEXT_SYMBOLS if s not in cls.SYMBOLS]

    @classmethod
    def expiry_for(cls, symbol):
        """
        The contract month for one symbol, honouring a per-symbol override.
        None for a cash index, which has no expiry.
        """
        instrument = cls.instrument(symbol)
        if instrument is not None and instrument.is_index:
            return None
        return os.getenv(f"{symbol.upper()}_EXPIRY", cls.EXPIRY)

    @classmethod
    def instrument(cls, symbol):
        """Metadata for a symbol, or None if it is not one of INSTRUMENTS."""
        return INSTRUMENTS.get(symbol.upper())

    @classmethod
    def describe_instrument(cls, symbol):
        """Human label for prompts and log lines, e.g. 'S&P 500 E-mini (ES)'."""
        instrument = cls.instrument(symbol)
        return f"{instrument.name} ({instrument.symbol})" if instrument else symbol.upper()

    @classmethod
    def validate(cls):
        """Validates critical config values."""
        # Check LLM keys (optional warnings rather than hard crashes)
        if not cls.OPENAI_API_KEY and not cls.ANTHROPIC_API_KEY:
            print("WARNING: Neither OPENAI_API_KEY nor ANTHROPIC_API_KEY found in environment variables.")

        unknown = [s for s in cls.collect_symbols() if s not in INSTRUMENTS]
        if unknown:
            print(f"WARNING: unknown instrument(s) configured: {', '.join(unknown)}. "
                  f"Known: {', '.join(INSTRUMENTS)}.")

        indices = [s for s in cls.SYMBOLS
                   if (inst := cls.instrument(s)) is not None and inst.is_index]
        if indices:
            print(f"WARNING: SYMBOLS contains cash index/indices ({', '.join(indices)}), which "
                  f"cannot be forecast (no volume, no expiry). Use CONTEXT_SYMBOLS instead.")

if __name__ == "__main__":
    Config.validate()
    from database.connection import describe_dsn
    print("Database:", describe_dsn(Config.DATABASE_URL))
    print("IBKR Gateway Port:", Config.IB_PORT)
    for sym in Config.SYMBOLS:
        print(f"Forecast: {Config.describe_instrument(sym)} expiry {Config.expiry_for(sym)}")
    for sym in Config.CONTEXT_SYMBOLS:
        print(f"Context : {Config.describe_instrument(sym)}")
