# config.py
"""
Configuration manager for the Opening Forecast System.
Loads and validates settings from environment variables or a local .env file.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


# IB futures month codes, in calendar order.
MONTH_CODES = "FGHJKMNQUVXZ"

# Recorded values are kept exactly as the source publishes them; the unit says how
# to read them. Yield units convert to basis points with BPS_PER_UNIT, so the
# conversion is configured here once rather than repeated in every feature.
VALUE_UNITS = ("price", "index_points", "percent", "percent_x10", "decimal")
BPS_PER_UNIT = {"percent": 100.0, "percent_x10": 10.0, "decimal": 10_000.0}


@dataclass(frozen=True)
class RollRule:
    """
    How a futures series picks its contract for each trading day.

    The front contract for a day is the earliest-expiring contract, among the
    listed ``months`` (IB month codes), whose expiry is more than
    ``days_before_expiry`` calendar days after that day. On the roll day the next
    contract takes over; nothing is ever back-adjusted or spliced.
    """
    months: str
    days_before_expiry: int

    def describe(self):
        return f"front of {self.months}, roll {self.days_before_expiry}d before expiry"


@dataclass(frozen=True)
class Instrument:
    """Static metadata for one tracked instrument, and how IB is asked for it."""
    symbol: str
    name: str
    exchange: str
    tick_size: float
    multiplier: Optional[str]
    sec_type: str = "FUT"
    currency: str = "USD"
    primary_exchange: Optional[str] = None   # listing venue of a SMART-routed stock/ETF
    what_to_show: str = "TRADES"             # IB whatToShow; also the ledger's price_type
    # Rough 1-minute bars per stored trading day, used only to judge completeness
    # (a day holding < 90% of it stays PARTIAL and is re-fetched). A lower bound
    # for thinly traded or partly disseminated series, not an exact count.
    expected_bars: int = 1290
    roll: Optional[RollRule] = None          # None: one contract (index, stock, or pinned)
    value_kind: str = "price"                # price | index_level | yield
    value_unit: str = "price"                # one of VALUE_UNITS
    # A day whose median close falls outside this range is refused rather than
    # stored: it almost always means value_unit is wrong for what the source sends.
    plausible_range: Optional[Tuple[float, float]] = None

    @property
    def is_index(self):
        """Cash indices have no expiry and no traded volume."""
        return self.sec_type == "IND"

    @property
    def is_future(self):
        return self.sec_type == "FUT"

    @property
    def bps_per_unit(self):
        """Basis points per one unit of a yield series, None for anything else."""
        return BPS_PER_UNIT.get(self.value_unit) if self.value_kind == "yield" else None


_EQUITY_ROLL = RollRule("HMUZ", 8)           # second Thursday before the third-Friday expiry
_MONTHLY_YIELD_ROLL = RollRule(MONTH_CODES, 3)

# The instruments the pipeline follows.
#
# The three equity-index futures share the same RTH window (09:30-16:00 ET), the
# same 18:00 ET Globex roll and the same holiday calendar, so
# features/session_windows.py and collector/coverage.py apply to each without
# change. They are the forecast targets.
#
# Everything else is intermarket *context*: collected, never forecast. Its bars are
# filed under the same 18:00 ET trading day as the futures, so "the bars of day D"
# means the same wall-clock window for every instrument.
INSTRUMENTS: Dict[str, Instrument] = {
    "ES": Instrument("ES", "S&P 500 E-mini", "CME", 0.25, "50", roll=_EQUITY_ROLL),
    "NQ": Instrument("NQ", "Nasdaq-100 E-mini", "CME", 0.25, "20", roll=_EQUITY_ROLL),
    "RTY": Instrument("RTY", "Russell 2000 E-mini", "CME", 0.10, "50", roll=_EQUITY_ROLL),

    # Spot volatility indices (not VX futures). VIX is disseminated in Cboe's global
    # trading hours as well as RTH; VXN may only print in RTH, in which case its
    # pre-open value is the prior close and the freshness rule decides whether it
    # is usable. Either way only genuine prints are stored.
    "VIX": Instrument("VIX", "Cboe Volatility Index", "CBOE", 0.01, None, sec_type="IND",
                      expected_bars=390, value_kind="index_level", value_unit="index_points",
                      plausible_range=(5.0, 150.0)),
    "VXN": Instrument("VXN", "Cboe Nasdaq-100 Volatility Index", "CBOE", 0.01, None,
                      sec_type="IND", expected_bars=390, value_kind="index_level",
                      value_unit="index_points", plausible_range=(5.0, 150.0)),

    # Cboe's 10-year yield index quotes ten times the yield (42.50 = 4.250%). The
    # plausible range refuses a day that looks like plain percent, which would mean
    # this unit is wrong and every bps change would be off by 10x.
    "TNX": Instrument("TNX", "Cboe 10-Year Treasury Yield Index", "CBOE", 0.001, None,
                      sec_type="IND", expected_bars=300, value_kind="yield",
                      value_unit="percent_x10", plausible_range=(5.0, 200.0)),

    # CME Micro Treasury *Yield* futures: the price is quoted directly as a yield in
    # percent (4.213 = 4.213%), tied to the most recently auctioned note. They trade
    # on Globex around the clock, but they are futures, not spot yields.
    "10Y": Instrument("10Y", "Micro 10-Year Yield futures", "CBOT", 0.001, "1000",
                      expected_bars=300, roll=_MONTHLY_YIELD_ROLL, value_kind="yield",
                      value_unit="percent", plausible_range=(-1.0, 20.0)),
    "2YY": Instrument("2YY", "Micro 2-Year Yield futures", "CBOT", 0.001, "1000",
                      expected_bars=300, roll=_MONTHLY_YIELD_ROLL, value_kind="yield",
                      value_unit="percent", plausible_range=(-1.0, 20.0)),

    # ICE does not license the cash DXY index to IB, so the dollar is recorded as the
    # US Dollar Index future (IB exchange code NYBOT = ICE US).
    "DX": Instrument("DX", "US Dollar Index futures (ICE)", "NYBOT", 0.005, "1000",
                     expected_bars=600, roll=RollRule("HMUZ", 10)),

    "SMH": Instrument("SMH", "VanEck Semiconductor ETF", "SMART", 0.01, None, sec_type="STK",
                      primary_exchange="NASDAQ", expected_bars=390),

    # Optional commodity context; not collected unless added to CONTEXT_SYMBOLS.
    "GC": Instrument("GC", "Gold futures (COMEX)", "COMEX", 0.10, "100",
                     expected_bars=1100, roll=RollRule("GJMQVZ", 33)),
    "CL": Instrument("CL", "WTI Crude Oil futures (NYMEX)", "NYMEX", 0.01, "1000",
                     expected_bars=1100, roll=RollRule(MONTH_CODES, 7)),
}


@dataclass(frozen=True)
class AssetSource:
    """
    Maps one logical intermarket asset (the prefix of its feature names) to the
    instrument actually recorded for it.

    ``symbol=None`` means no acceptable source is recorded: every feature built on
    the asset must be null, never filled from a neighbouring series.

    ``max_age_minutes`` is the documented freshness rule. An observation is usable
    only if the bar it comes from closed no more than this many minutes before the
    instant it stands for (the pre-open cutoff, or the previous NQ RTH close for
    the reference). Older than that, the value is null; the age is always reported.

    ``is_proxy`` marks a recorded instrument that is not the asset itself, so a
    proxy is never mistaken for the real series. Treasury- and volatility-futures
    proxies get their own asset keys (and therefore their own feature names).
    """
    asset: str
    symbol: Optional[str]
    description: str
    max_age_minutes: Optional[int] = None
    is_proxy: bool = False
    optional: bool = False
    notes: str = ""


# Source configuration for the predictive intermarket features. Feature names are
# built from ``asset`` (nq_preopen_return, vix_level, us10y_change_bps, ...).
ASSET_SOURCES: Dict[str, AssetSource] = {a.asset: a for a in (
    AssetSource("nq", "NQ", "Nasdaq-100 E-mini, front contract", 5),
    AssetSource("es", "ES", "S&P 500 E-mini, front contract", 5),
    AssetSource("rty", "RTY", "Russell 2000 E-mini, front contract", 5),
    AssetSource("vix", "VIX", "Cboe VIX spot index", 15),
    AssetSource("vxn", "VXN", "Cboe VXN spot index", 15,
                notes="May be disseminated in RTH only; a stale pre-open value is null, not carried."),
    AssetSource("us10y", "TNX", "10-year Treasury yield (Cboe TNX, yield x10)", 30),
    AssetSource("us2y", None, "2-year Treasury yield",
                notes="No spot 2-year yield series is available through IB (it serves yield "
                      "history for corporate bonds only). Left unmapped so us2y_change_bps and "
                      "the 10y-2y curve change stay null; see us2y_yield_fut."),
    AssetSource("dxy", None, "US Dollar Index (cash DXY)",
                notes="ICE does not license the cash DXY index to IB, so nothing is recorded and "
                      "dxy_preopen_return stays null. The DX future is its own asset, dx_fut."),
    AssetSource("dx_fut", "DX", "ICE US Dollar Index futures, front contract", 30, is_proxy=True,
                notes="A futures proxy for DXY, kept under its own feature names (dx_fut_*)."),
    AssetSource("smh", "SMH", "VanEck Semiconductor ETF (regular close -> premarket)", 30),
    AssetSource("us10y_yield_fut", "10Y", "Micro 10-Year Yield futures, front month", 30,
                is_proxy=True, notes="A futures yield, not the spot 10-year yield."),
    AssetSource("us2y_yield_fut", "2YY", "Micro 2-Year Yield futures, front month", 30,
                is_proxy=True, notes="A futures yield, not the spot 2-year yield."),
    AssetSource("gc", "GC", "Gold futures, active contract", 10, optional=True),
    AssetSource("cl", "CL", "WTI crude futures, front month", 10, optional=True),
)}


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

    # Instrument Settings
    #   SYMBOLS         - instruments that are forecast (and therefore collected)
    #   CONTEXT_SYMBOLS - collected only as intermarket context for those forecasts;
    #                     never forecast themselves (see ASSET_SOURCES)
    #   EXPIRY          - the contract month the forecast and dashboard read; the
    #                     collector instead follows each future's RollRule, unless
    #                     pinned with --expiry or a per-symbol <SYMBOL>_EXPIRY
    SYMBOLS: List[str] = [
        s.strip().upper() for s in os.getenv("SYMBOLS", "ES,NQ,RTY").split(",") if s.strip()
    ]
    CONTEXT_SYMBOLS: List[str] = [
        s.strip().upper() for s in os.getenv("CONTEXT_SYMBOLS", "VIX,VXN,TNX,DX,SMH,10Y,2YY").split(",") if s.strip()
    ]
    EXPIRY = os.getenv("EXPIRY", "202612")

    # Trading days of a future's next contract stored before it becomes front, so
    # indicators computed on that contract alone (the v2 5m EMA200 needs ~4 Globex
    # sessions) are warm on the roll day. The last one is also the first active
    # day's same-contract reference close, so this is at least 1.
    ROLL_WARMUP_SESSIONS = max(1, int(os.getenv("ROLL_WARMUP_SESSIONS", "7")))

    # Application Settings
    TIMEZONE = "America/New_York"
    FEATURE_VERSION = "v1.0"

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
    def pinned_expiry(cls, symbol):
        """
        The contract month the collector must stick to for ``symbol``, or None to
        let it follow the instrument's RollRule. Only an explicit per-symbol
        ``<SYMBOL>_EXPIRY`` pins; the shared EXPIRY does not.
        """
        instrument = cls.instrument(symbol)
        if instrument is not None and not instrument.is_future:
            return None
        return os.getenv(f"{symbol.upper()}_EXPIRY") or None

    @classmethod
    def asset_sources(cls):
        """The logical-asset -> recorded-instrument map, in feature order."""
        return list(ASSET_SOURCES.values())

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
        unknown = [s for s in cls.collect_symbols() if s not in INSTRUMENTS]
        if unknown:
            print(f"WARNING: unknown instrument(s) configured: {', '.join(unknown)}. "
                  f"Known: {', '.join(INSTRUMENTS)}.")

        not_futures = [s for s in cls.SYMBOLS
                       if (inst := cls.instrument(s)) is not None and not inst.is_future]
        if not_futures:
            print(f"WARNING: SYMBOLS contains non-futures ({', '.join(not_futures)}), which "
                  f"cannot be forecast. Use CONTEXT_SYMBOLS instead.")

        for inst in INSTRUMENTS.values():
            if inst.value_unit not in VALUE_UNITS:
                print(f"WARNING: {inst.symbol} has unknown value_unit {inst.value_unit!r}.")
            if inst.value_kind == "yield" and inst.bps_per_unit is None:
                print(f"WARNING: {inst.symbol} is a yield but {inst.value_unit!r} has no bps conversion.")

        for src in ASSET_SOURCES.values():
            if src.symbol is not None and src.symbol not in INSTRUMENTS:
                print(f"WARNING: asset {src.asset} maps to unknown instrument {src.symbol}.")
            elif (src.symbol is not None and not src.optional
                  and src.symbol not in cls.collect_symbols()):
                print(f"WARNING: asset {src.asset} maps to {src.symbol}, which is not collected; "
                      f"its features will be null. Add it to CONTEXT_SYMBOLS.")

if __name__ == "__main__":
    Config.validate()
    from database.connection import describe_dsn
    print("Database:", describe_dsn(Config.DATABASE_URL))
    print("IBKR Gateway Port:", Config.IB_PORT)
    for sym in Config.SYMBOLS:
        print(f"Forecast: {Config.describe_instrument(sym)} expiry {Config.expiry_for(sym)}")
    for sym in Config.CONTEXT_SYMBOLS:
        print(f"Context : {Config.describe_instrument(sym)}")
    collected = set(Config.collect_symbols())
    print("Intermarket sources:")
    for src in Config.asset_sources():
        if src.symbol is None:
            state = "UNMAPPED -> always null"
        else:
            state = f"{src.symbol}{' (proxy)' if src.is_proxy else ''}" + (
                "" if src.symbol in collected else "  [not collected]")
        print(f"  {src.asset:<16} {state}")
