# dashboard/views/candles.py
"""
Candlestick exploration view of the stored sessions.

Three selectors pick what is shown, in this order: the session day, then an
instrument with bars that day (ES, NQ, ...), then one of its contracts holding
the day (the one active that day first). The chart shows the regular session
with 15 minutes either side - 09:15 to 16:15 ET, or to 13:15 on an early close -
and draws those extra minutes grey on a grey background, with the session VWAP,
the opening range, the pre-open reference levels and the TradingView indicator's
three moving averages (features.calculations.calculate_moving_averages).

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing chart. The chart is created once per page
load, so a redraw leaves the user's zoom, scroll and crosshair exactly where
they were.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
from nicegui import ui

from config import Config
from dashboard.components.coverage_map import coverage_map
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import build_chart_spec
from database.queries import (
    contracts_for_day,
    get_bars,
    get_day_bars,
    get_session_day,
    list_contracts,
    list_session_days,
    symbols_with_day,
)
from features import calendar as cal
from features.calculations import (
    MA_WARMUP_BARS,
    calculate_moving_averages,
    calculate_vwap,
    enrich_candle_timezones,
    pre_open_levels,
)

_RESAMPLE_FREQ = {"5m": "5min", "15m": "15min", "30m": "30min"}

# The instrument the page opens on; its current contract is preselected.
DEFAULT_SYMBOL = "NQ"

# Minutes shown either side of the regular session, drawn muted.
_EXTRA = pd.Timedelta(minutes=15)
_NY = "America/New_York"
_BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
_GLOBEX_DAY_MINUTES = 23 * 60
_MA_COLUMNS = ["tema", "ema_trend", "ema_trigger"]


def _resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregates 1-minute bars up to the selected display interval."""
    if timeframe == "1m" or df is None or df.empty:
        return df

    agg_rules = {
        "timestamp_utc": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "price_type": "first",
    }
    resampled = df.sort_values("timestamp_ny").set_index("timestamp_ny")
    return resampled.resample(_RESAMPLE_FREQ[timeframe]).agg(agg_rules).dropna().reset_index()


def session_window(day: str) -> Dict[str, pd.Timestamp]:
    """
    New York times of ``day``'s regular session (``open``, ``close``: the
    scheduled close, 13:00 on an early close) and the window shown around it
    (``start`` = open - 15 min, ``end`` = close + 15 min). Days outside the
    exchange calendar fall back to 09:30-16:00.
    """
    try:
        s = cal.session(day)
        if s.is_open:
            open_ = pd.Timestamp(s.rth_open_at).tz_convert(_NY)
            close = pd.Timestamp(s.scheduled_close_at).tz_convert(_NY)
            return {"start": open_ - _EXTRA, "open": open_, "close": close, "end": close + _EXTRA}
    except cal.CalendarCoverageError:
        pass
    base = pd.Timestamp(str(day)).tz_localize(_NY)
    open_, close = base + pd.Timedelta(hours=9, minutes=30), base + pd.Timedelta(hours=16)
    return {"start": open_ - _EXTRA, "open": open_, "close": close, "end": close + _EXTRA}


def window_bars(df: Optional[pd.DataFrame], day: str, timeframe: str = "1m") -> Optional[pd.DataFrame]:
    """
    The bars of ``df`` (at ``timeframe``, with ``timestamp_ny`` bar starts) that
    overlap ``day``'s shown window, with ``muted`` true for those starting
    outside the regular session - the 15 minutes before the open and after the
    close. Indicators such as VWAP are computed on the whole day beforehand.
    """
    if df is None or df.empty:
        return df
    w = session_window(day)
    width = pd.Timedelta(minutes=_BAR_MINUTES.get(timeframe, 1))
    ts = df["timestamp_ny"]
    out = df[(ts + width > w["start"]) & (ts < w["end"])].copy()
    out["muted"] = (out["timestamp_ny"] < w["open"]) | (out["timestamp_ny"] >= w["close"])
    return out


def opening_range(df: Optional[pd.DataFrame], day: str, minutes: int = 15) -> Optional[Dict[str, Any]]:
    """
    ``{'high', 'low', 'start', 'end'}`` of ``day``'s opening range: the high and
    low of the 1-minute bars in its first ``minutes`` after the open (New York
    timestamps), or None when none of those bars is stored.
    """
    if df is None or df.empty:
        return None
    start = session_window(day)["open"]
    end = start + pd.Timedelta(minutes=minutes)
    ts = df["timestamp_ny"]
    w = df[(ts >= start) & (ts < end)]
    if w.empty:
        return None
    return {"high": float(w["high"].max()), "low": float(w["low"].min()), "start": start, "end": end}


def _is_context_only(symbol: str) -> bool:
    """Collected as intermarket context (VIX, TNX, DX, SMH, ...), not a target future."""
    if symbol in Config.SYMBOLS:
        return False
    instrument = Config.instrument(symbol)
    return symbol in Config.CONTEXT_SYMBOLS or (instrument is not None and not instrument.is_future)


def day_contracts(conn, symbol: str, day: str) -> List[Any]:
    """
    Contracts of ``symbol`` holding ``day``, for the contract selector: the one
    the collector made active that day first (it holds the day's session
    before the roll and after it), then as ``database.queries.contracts_for_day``
    orders them - the closest contract expiring on or after the day first.
    """
    ordered = contracts_for_day(conn, symbol, day)
    row = conn.execute("SELECT contract_id FROM active_contracts WHERE symbol = %s AND trading_day = %s;",
                       (symbol, day)).fetchone()
    if row is not None:
        active = [c for c in ordered if c["contract_id"] == row["contract_id"]]
        ordered = active + [c for c in ordered if c["contract_id"] != row["contract_id"]]
    return ordered


def _contract_label(contract) -> str:
    return f"{contract['symbol']} {contract['expiry']}"


class SessionExplorer:
    """Holds the view's state and keeps the chart in step with it."""

    def __init__(self, conn) -> None:
        self.conn = conn
        # Only the target futures are offered: context instruments are collected for
        # intermarket reference. The collector also records whole futures chains to
        # plan rolls, so only contracts that actually hold bars are offered.
        self.symbols: List[str] = []
        for c in list_contracts(conn, with_data_only=True):
            if not _is_context_only(c["symbol"]) and c["symbol"] not in self.symbols:
                self.symbols.append(c["symbol"])

        # Selected, in the order the controls pick them: day -> symbol -> contract.
        self.date: Optional[str] = None
        self.symbol: Optional[str] = None
        self.contract = None
        self.day_contracts: Dict[int, Any] = {}   # contract_id -> contract row, for the selector
        self.timeframe = "1m"
        # Set while the selectors are updated from code, so their change events
        # do not reload the session once per control.
        self._syncing = False

        # Loaded per session.
        self.day_df: Optional[pd.DataFrame] = None
        self.opening_range: Optional[Dict[str, Any]] = None
        self.levels: Optional[Dict[str, Any]] = None    # pre-open reference levels; None without a previous day

        self.chart: Optional[LightweightChart] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_day(self) -> None:
        """Pulls the selected session and everything derived from it."""
        contract_id = self.contract["contract_id"]

        self._set_day_bars(get_day_bars(self.conn, contract_id, self.date, interval="1m"))
        if self.day_df is None:
            return

        # The reference levels need the session before the day, the moving averages
        # MA_WARMUP_BARS bars at the chart's timeframe before it.
        history = self._load_bars(self._history_start(self._warmup_days()))
        self.levels = pre_open_levels(history, self.date)
        self.day_df = self._with_moving_averages(history)

    def _set_day_bars(self, rows: List[Any]) -> None:
        """The day's own bars and what is drawn from them alone: chart bars with VWAP, opening range."""
        self.opening_range = None
        if not rows:
            self.day_df = None
            return
        df = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        self.opening_range = opening_range(df, self.date)        # from the 1-minute bars
        self.day_df = calculate_vwap(_resample(df, self.timeframe))

    def _warmup_days(self) -> int:
        """Trading days holding the moving averages' warm-up at the chart's timeframe, plus one spare."""
        return -(-MA_WARMUP_BARS * _BAR_MINUTES[self.timeframe] // _GLOBEX_DAY_MINUTES) + 1

    def _history_start(self, days: int = 1) -> Optional[str]:
        """
        First trading day to load: ``days`` sessions of the contract before the
        displayed day (the one just before it holds the reference levels' RTH),
        or as many as it has. The displayed day itself when there is nothing
        earlier to load.
        """
        row = self.conn.execute(
            "SELECT MIN(trading_day) FROM ("
            "  SELECT trading_day FROM session_days "
            "  WHERE contract_id = %s AND interval = '1m' AND price_type = 'TRADES' "
            "    AND bar_count > 0 AND trading_day < %s ORDER BY trading_day DESC LIMIT %s) AS earlier",
            (self.contract["contract_id"], self.date, days),
        ).fetchone()
        return row[0] or self.date

    def _load_bars(self, start_day: Optional[str]) -> pd.DataFrame:
        return pd.DataFrame([
            dict(r) for r in get_bars(
                self.conn, self.contract["contract_id"], interval="1m",
                start_day=start_day, end_day=self.date,
            )
        ])

    def _with_moving_averages(self, history: pd.DataFrame) -> pd.DataFrame:
        """The day's bars with the moving averages, run over ``history`` at the chart's timeframe."""
        if history.empty:
            return self.day_df
        bars = calculate_moving_averages(_resample(enrich_candle_timezones(history), self.timeframe))
        return self.day_df.merge(bars[["timestamp_ny", *_MA_COLUMNS]], on="timestamp_ny", how="left")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def push(self, reset_view: bool = False) -> None:
        """
        Sends the current state to the chart. ``reset_view`` (a new day) shows
        the default window, 09:15 to 10:45; otherwise the window being looked at
        is kept (a timeframe change).
        """
        if self.chart is None:
            return
        if self.day_df is None or self.day_df.empty:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []})
            return
        w = session_window(self.date)
        spec = build_chart_spec(
            window_bars(self.day_df, self.date, self.timeframe), levels=self.levels,
            opening_range=self.opening_range,
            visible_range=(w["start"], w["open"] + pd.Timedelta(minutes=75)) if reset_view else None,
            keep_view=not reset_view,
        )
        self.chart.apply(spec)

    def refresh_session(self, keep_view: bool = False) -> None:
        """
        Reloads the day from the database and redraws everything. A new day
        opens on the default window; ``keep_view`` (a display change only) keeps
        the window being looked at.
        """
        if self.contract is None or self.date is None:
            return
        self.load_day()
        self._render_status()
        self.push(reset_view=not keep_view)

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------

    def _choose_symbol(self) -> List[str]:
        """The instruments with bars on the selected day; keeps the current one when it has."""
        symbols = symbols_with_day(self.conn, self.date, self.symbols) if self.date else []
        if self.symbol not in symbols:
            self.symbol = DEFAULT_SYMBOL if DEFAULT_SYMBOL in symbols else (symbols[0] if symbols else None)
        return symbols

    def _choose_contract(self) -> Dict[int, str]:
        """The selected instrument's contracts holding the day; the first (active that day) is chosen."""
        contracts = day_contracts(self.conn, self.symbol, self.date) if self.symbol and self.date else []
        self.day_contracts = {int(c["contract_id"]): c for c in contracts}
        self.contract = contracts[0] if contracts else None
        return {int(c["contract_id"]): _contract_label(c) for c in contracts}

    def _sync_selectors(self, symbols: bool = True) -> None:
        """Pushes the chosen symbol / contract (and their options) to the controls."""
        self._syncing = True
        try:
            if symbols:
                self.symbol_select.set_options(self._choose_symbol(), value=self.symbol)
            options = self._choose_contract()
            self.contract_select.set_options(
                options, value=int(self.contract["contract_id"]) if self.contract is not None else None)
        finally:
            self._syncing = False

    def on_date(self, event) -> None:
        if self._syncing or not event.value:
            return
        self.date = event.value
        self._sync_selectors()
        self.refresh_session()

    def on_symbol(self, event) -> None:
        if self._syncing or not event.value:
            return
        self.symbol = event.value
        self._sync_selectors(symbols=False)
        self.refresh_session()

    def on_contract(self, event) -> None:
        if self._syncing or event.value is None:
            return
        self.contract = self.day_contracts[int(event.value)]
        self.refresh_session()

    def on_timeframe(self, event) -> None:
        self.timeframe = event.value
        self.refresh_session(keep_view=True)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _render_status(self) -> None:
        self.status.clear()
        with self.status:
            if self.day_df is None or self.day_df.empty:
                ui.badge("no data", color="red")
                return
            row = get_session_day(self.conn, self.contract["contract_id"], self.date)
            if row is not None and row["status"] != "COMPLETE":
                ui.badge(
                    f"{row['status']} — {row['bar_count']}/{row['expected_bar_count'] or '?'} bars",
                    color="orange",
                ).tooltip("Re-run the collector to complete this session.")
            else:
                ui.badge(f"{len(self.day_df):,} bars", color="green")

    def build(self) -> None:
        days = list_session_days(self.conn, self.symbols)
        if not days:
            with ui.card().classes("w-full"):
                ui.label("No sessions found.").classes("text-lg")
                ui.label("Run the collector, or populate_mock_data.py, before opening this view.")
            return

        # Opens on the newest NQ session (else the newest of any instrument), on
        # the contract active that day.
        newest_default = list_session_days(self.conn, [DEFAULT_SYMBOL], limit=1)
        self.date = newest_default[0] if newest_default else days[0]
        symbols = self._choose_symbol()
        contract_options = self._choose_contract()

        with ui.column().classes("w-full p-4 gap-3"):
            self._build_controls(days, symbols, contract_options)

            self.chart = LightweightChart(height=620)

        if self.contract is None:
            ui.notify("No contract holds bars for the selected day.", type="warning")
            return
        self.refresh_session()

    def _build_controls(self, days: List[str], symbols: List[str], contract_options: Dict[int, str]) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            self.date_select = ui.select(
                days, value=self.date, label="Session day (NY trading day)", with_input=True,
                on_change=self.on_date,
            ).classes("w-52")
            self.symbol_select = ui.select(
                symbols, value=self.symbol, label="Instrument", on_change=self.on_symbol,
            ).classes("w-32")
            self.contract_select = ui.select(
                contract_options, value=int(self.contract["contract_id"]) if self.contract is not None else None,
                label="Contract", on_change=self.on_contract,
            ).classes("w-44")
            ui.toggle(
                ["1m", "5m", "15m", "30m"], value="1m", on_change=self.on_timeframe,
            ).props("dense")
            ui.button("Fit", icon="fit_screen", on_click=lambda: self.chart and self.chart.fit()).props(
                "flat dense no-caps").tooltip("Show the whole 09:15–16:15 window")
            self.status = ui.row().classes("items-center gap-2")
            ui.space()
            coverage_map(self.conn)


def show_candles_page(conn) -> None:
    SessionExplorer(conn).build()
