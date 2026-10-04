# dashboard/views/candles.py
"""
Candlestick exploration view of the stored sessions.

Three selectors pick what is shown, in this order: the session day - a calendar
on which only the days with stored bars can be picked, with buttons stepping to
the previous and the next of them - then an instrument with bars that day (ES,
NQ, ...), then one of its contracts holding the day (the one active that day
first). The chart shows the regular session with 15 minutes either side - 09:15
to 16:15 ET, or to 13:15 on an early close - and draws those extra minutes grey
on a grey background, with the session VWAP, the opening range, the pre-open
reference levels and the TradingView indicator's three moving averages
(features.calculations.calculate_moving_averages).

Beside it, one of the day's structural analogues (dashboard/views/analogues.py;
NQ, the journal symbol, on the days the journal holds a pre-open snapshot of),
the most similar first: that session on its own contract, drawn the same way at
the same timeframe. The two charts are linked by time of day - scrolling or
zooming either moves the other. Below them, the comparison of the session with
its analogues; a date there picks the analogue shown.

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing charts. The charts are created once per page
load, so a redraw leaves the user's zoom, scroll and crosshair exactly where
they were.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
from nicegui import ui

from config import Config
from contracts import nq_prompt_v2 as defs
from dashboard.components.coverage_map import coverage_map
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import build_chart_spec, resample, to_epoch
from dashboard.views.analogues import AnaloguesPanel
from database import journal_store as store
from database.queries import (
    contracts_for_day,
    get_bars,
    get_contract,
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

# The instrument the page opens on; its current contract is preselected.
DEFAULT_SYMBOL = "NQ"

# The two charts - the session and an analogue - show the same time of day.
_SYNC_GROUP = "session-explorer"
_EMPTY_SPEC = {"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []}
_MUTED = "color:#787b86"

# Minutes shown either side of the regular session, drawn muted.
_EXTRA = pd.Timedelta(minutes=15)
_NY = "America/New_York"
_BAR_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30}
_GLOBEX_DAY_MINUTES = 23 * 60
_MA_COLUMNS = ["tema", "ema_trend", "ema_trigger"]


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


class SessionPane:
    """
    One chart of the explorer: a contract's session at a timeframe - the window
    around its regular hours with the session VWAP, the opening range, the
    pre-open reference levels and the moving averages. The explorer has two side
    by side, the selected session and one of its analogues, linked by time of day.
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        self.contract = None
        self.date: Optional[str] = None
        self.timeframe = "1m"

        # Loaded per session.
        self.day_df: Optional[pd.DataFrame] = None
        self.opening_range: Optional[Dict[str, Any]] = None
        self.levels: Optional[Dict[str, Any]] = None    # pre-open reference levels; None without a previous day

        self.chart: Optional[LightweightChart] = None

    def show(self, contract, date: Optional[str], timeframe: str, keep_view: bool = False,
             follow: bool = False) -> None:
        """
        Loads ``contract``'s session of ``date`` at ``timeframe`` and draws it -
        an empty chart without a contract. A new session opens on the default
        window, ``keep_view`` keeps the window being looked at, and ``follow``
        shows the time of day the lead chart shows.
        """
        self.contract, self.date, self.timeframe = contract, date, timeframe
        self.day_df, self.opening_range, self.levels = None, None, None
        if contract is not None and date is not None:
            self.load_day()
        self.push(reset_view=not keep_view, follow=follow)

    @property
    def has_bars(self) -> bool:
        return self.day_df is not None and not self.day_df.empty

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_day(self) -> None:
        """Pulls the pane's session and everything derived from it."""
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
        self.day_df = calculate_vwap(resample(df, self.timeframe))

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
        bars = calculate_moving_averages(resample(enrich_candle_timezones(history), self.timeframe))
        return self.day_df.merge(bars[["timestamp_ny", *_MA_COLUMNS]], on="timestamp_ny", how="left")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def push(self, reset_view: bool = False, follow: bool = False) -> None:
        """
        Sends the current state to the chart. ``reset_view`` (a new day) shows
        the default window, 09:15 to 10:45; otherwise the window being looked at
        is kept (a timeframe change). ``follow``: the lead chart's time of day
        instead, when the lead shows a session.
        """
        if self.chart is None:
            return
        if not self.has_bars:
            self.chart.apply(dict(_EMPTY_SPEC))
            return
        w = session_window(self.date)
        spec = build_chart_spec(
            window_bars(self.day_df, self.date, self.timeframe), levels=self.levels,
            opening_range=self.opening_range,
            visible_range=(w["start"], w["open"] + pd.Timedelta(minutes=75)) if reset_view else None,
            keep_view=not reset_view,
        )
        spec["anchor"] = int(to_epoch([pd.Timestamp(self.date)])[0])    # the day's 00:00 on the chart's clock
        spec["follow"] = follow
        self.chart.apply(spec)


def _status_badge(conn, pane: SessionPane) -> None:
    """The pane's bar count, or how much of its session is stored when incomplete."""
    if not pane.has_bars:
        ui.badge("no data", color="red")
        return
    row = get_session_day(conn, pane.contract["contract_id"], pane.date)
    if row is not None and row["status"] != "COMPLETE":
        ui.badge(
            f"{row['status']} — {row['bar_count']}/{row['expected_bar_count'] or '?'} bars",
            color="orange",
        ).tooltip("Re-run the collector to complete this session.")
    else:
        ui.badge(f"{len(pane.day_df):,} bars", color="green")


def _caption(day: str, contract) -> str:
    return f"{pd.Timestamp(day):%a} {day}" + (f" · {_contract_label(contract)}" if contract is not None else "")


class SessionExplorer:
    """Holds the view's state and keeps the two charts in step with it."""

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
        self.days: List[str] = []                 # the days with bars, newest first
        self.date: Optional[str] = None
        self.symbol: Optional[str] = None
        self.contract = None
        self.day_contracts: Dict[int, Any] = {}   # contract_id -> contract row, for the selector
        self.timeframe = "1m"
        # Set while the selectors are updated from code, so their change events
        # do not reload the session once per control.
        self._syncing = False

        self.main = SessionPane(conn)             # the selected session
        self.mirror = SessionPane(conn)           # one of its analogues, beside it
        self.analogues = AnaloguesPanel(conn, on_pick=self.pick_analogue)
        self.members: Dict[str, Dict[str, Any]] = {}   # the day's analogues by snapshot id, best first
        self.analogue: Optional[str] = None             # the snapshot id of the one beside the session

    # ------------------------------------------------------------------
    # The two charts
    # ------------------------------------------------------------------

    def refresh_session(self, keep_view: bool = False) -> None:
        """
        Reloads the selected session and redraws it. A new day opens on the
        default window; ``keep_view`` (a display change only) keeps the window
        being looked at.
        """
        self.main.show(self.contract, self.date, self.timeframe, keep_view=keep_view)
        self.main_caption.text = _caption(self.date, self.contract) if self.date else ""
        self.status.clear()
        with self.status:
            _status_badge(self.conn, self.main)

    def _show_analogues(self) -> None:
        """The day's analogues: the comparison below the charts, and the most similar beside the session."""
        self.analogue_box.text = f"Analogues of {self.date}" if self.symbol == defs.SYMBOL else "Analogues"
        self.members = {m["snapshot_id"]: m for m in self.analogues.show(self.date, self.symbol)}
        options = {sid: f"#{m['rank']}  {m['session_date']}  ·  {float(m['similarity']):.1f}%"
                   for sid, m in self.members.items()}
        first = next(iter(options), None)
        self._syncing = True
        try:
            self.analogue_select.set_options(options, value=first)
        finally:
            self._syncing = False
        self.analogue_select.set_enabled(bool(options))
        self.show_analogue(first)

    def show_analogue(self, snapshot_id: Optional[str], keep_view: bool = False) -> None:
        """
        Draws the analogue ``snapshot_id`` beside the session: its own day on its
        own contract (never rebased), in the same window with the same
        indicators at the same timeframe, at the time of day the session shows.
        """
        self.analogue = snapshot_id
        member = self.members.get(snapshot_id) if snapshot_id else None
        snap = store.get_snapshot(self.conn, snapshot_id) if member is not None else None
        contract = get_contract(self.conn, snap["contract_id"]) if snap is not None else None
        day = member["session_date"] if member is not None else None
        self.mirror.show(contract, day, self.timeframe, keep_view=keep_view, follow=True)
        self.analogues.mark(snapshot_id)
        self.mirror_status.clear()
        with self.mirror_status:
            if member is not None:
                ui.label(_caption(day, contract)).classes("text-sm whitespace-nowrap")
                _status_badge(self.conn, self.mirror)
        self.mirror_note.text = "" if member is not None else (self.analogues.reason or "No analogue to show.")
        self.mirror_note.set_visibility(member is None)

    def fit(self) -> None:
        """Both charts show their whole window, 09:15 to 16:15."""
        for pane in (self.main, self.mirror):
            if pane.chart is not None:
                pane.chart.fit()

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
        if self._syncing:
            return
        if not event.value:          # the calendar unpicks its day when that day is clicked again
            self._set_picker(self.date)
            return
        self.date_menu.close()
        self.date = event.value
        self._mark_day()
        self._sync_selectors()
        self.refresh_session()
        self._show_analogues()

    def step_day(self, older: int) -> None:
        """The session ``older`` positions back in the list of days with bars (negative: forward)."""
        i = self.days.index(self.date) + older if self.date in self.days else -1
        if 0 <= i < len(self.days):
            self.date_picker.value = self.days[i]          # on_date follows

    def on_symbol(self, event) -> None:
        if self._syncing or not event.value:
            return
        self.symbol = event.value
        self._sync_selectors(symbols=False)
        self.refresh_session()
        self._show_analogues()

    def on_contract(self, event) -> None:
        if self._syncing or event.value is None:
            return
        self.contract = self.day_contracts[int(event.value)]
        self.refresh_session()

    def on_timeframe(self, event) -> None:
        self.timeframe = event.value
        self.refresh_session(keep_view=True)
        self.show_analogue(self.analogue, keep_view=True)

    def on_analogue(self, event) -> None:
        if self._syncing or not event.value:
            return
        self.show_analogue(event.value)

    def pick_analogue(self, snapshot_id: str) -> None:
        """An analogue's date clicked in the comparison: shown beside the session."""
        self.analogue_select.value = snapshot_id          # on_analogue follows

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _set_picker(self, day: Optional[str]) -> None:
        self._syncing = True
        try:
            self.date_picker.value = day
        finally:
            self._syncing = False

    def _mark_day(self) -> None:
        """The day field shows the selected day; the step buttons stop at the oldest and the newest day."""
        self.date_field.value = self.date
        i = self.days.index(self.date) if self.date in self.days else None
        self.older_button.set_enabled(i is not None and i + 1 < len(self.days))
        self.newer_button.set_enabled(i is not None and i > 0)

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
            self._build_charts()
            self.analogue_box = ui.expansion("Analogues", icon="compare", value=True).classes("w-full")
            with self.analogue_box:
                self.analogues.build()

        if self.contract is None:
            ui.notify("No contract holds bars for the selected day.", type="warning")
        self.refresh_session()
        self._show_analogues()

    def _build_charts(self) -> None:
        """The selected session on the left, one of its analogues on the right, linked by time of day."""
        with ui.grid(columns=2).classes("w-full gap-3"):
            with ui.column().classes("min-w-0 gap-1"):
                with ui.row().classes("w-full h-10 items-center gap-3 no-wrap"):
                    self.main_caption = ui.label().classes("text-sm whitespace-nowrap")
                    self.status = ui.row().classes("items-center gap-2")
                self.main.chart = LightweightChart(height=620, sync_group=_SYNC_GROUP, sync_lead=True)
            with ui.column().classes("min-w-0 gap-1"):
                with ui.row().classes("w-full h-10 items-center gap-3 no-wrap"):
                    self.analogue_select = ui.select(
                        {}, label="Analogue", on_change=self.on_analogue,
                    ).props("dense options-dense").classes("w-60")
                    self.mirror_status = ui.row().classes("items-center gap-3 no-wrap")
                with ui.element("div").classes("relative w-full"):
                    self.mirror.chart = LightweightChart(height=620, sync_group=_SYNC_GROUP)
                    self.mirror_note = ui.label().classes(
                        "absolute inset-0 flex items-center justify-center text-sm text-center px-10",
                    ).style(f"{_MUTED};pointer-events:none;z-index:4")

    def _build_day_picker(self, days: List[str]) -> None:
        """
        The session day: a field opening a calendar (weeks from Monday) on
        which only ``days`` can be picked, between buttons stepping to the
        previous and the next of them.
        """
        self.days = days
        months = f'navigation-min-year-month="{days[-1][:7].replace("-", "/")}" ' \
                 f'navigation-max-year-month="{days[0][:7].replace("-", "/")}"'
        with ui.row().classes("items-center gap-0 no-wrap"):
            self.older_button = ui.button(icon="chevron_left", on_click=lambda: self.step_day(1)).props(
                'flat dense round aria-label="Previous session"').tooltip("Previous session")
            with ui.input("Session day (NY trading day)", value=self.date).props("readonly").classes(
                    "w-48") as self.date_field:
                with ui.menu() as self.date_menu:
                    self.date_picker = ui.date(self.date, on_change=self.on_date).props(
                        f"first-day-of-week=1 {months}")
                    self.date_picker._props["options"] = [d.replace("-", "/") for d in days]
                with self.date_field.add_slot("append"):
                    ui.icon("event").classes("cursor-pointer")
            self.newer_button = ui.button(icon="chevron_right", on_click=lambda: self.step_day(-1)).props(
                'flat dense round aria-label="Next session"').tooltip("Next session")
        self._mark_day()

    def _build_controls(self, days: List[str], symbols: List[str], contract_options: Dict[int, str]) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            self._build_day_picker(days)
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
            ui.button("Fit", icon="fit_screen", on_click=self.fit).props(
                "flat dense no-caps").tooltip("Show the whole 09:15–16:15 window of both sessions")
            ui.space()
            coverage_map(self.conn)


def show_candles_page(conn) -> None:
    SessionExplorer(conn).build()
