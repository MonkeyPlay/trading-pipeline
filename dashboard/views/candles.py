# dashboard/views/candles.py
"""
Candlestick exploration view for the NQ Opening Forecast System.

Selectors load a stored session, indicator panels recompute overlays, and the
forecast panel runs the opening model against the pre-open snapshot.

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing chart. The chart is created once per page
load, so changing an indicator length redraws that one series and leaves the
user's zoom, scroll and crosshair exactly where they were.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
from nicegui import run, ui

from config import Config
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import build_chart_spec
from database.queries import (
    get_bars,
    get_contract_by_expiry,
    get_daily_rth_closes,
    get_day_bars,
    get_outcome,
    get_session_day,
    list_contracts,
    list_trading_days,
    save_analogue_matches,
    save_feature_snapshot,
    save_prediction,
)
from features.calculations import (
    calculate_pre_open_snapshot,
    calculate_vwap,
    enrich_candle_timezones,
)
from features.session_windows import NY_TZ
from forecaster.client import ForecastClient
from indicator import (
    ANCHOR_PERIOD_OPTIONS,
    CALC_MODE_OPTIONS,
    LINE_STYLE_OPTIONS,
    SOURCE_OPTIONS,
    AutoAnchoredVwapSettings,
    TemaSessionSettings,
    compute_auto_anchored_vwap,
    compute_tema_session,
)
from matching.normalizer import find_analogues

_RESAMPLE_FREQ = {"5m": "5min", "15m": "15min", "30m": "30min"}

# Sessions of 1-minute history fed to the indicator ahead of the displayed day, so the
# EMAs are past their warm-up and the previous-RTH levels have a prior session to read.
_INDICATOR_WARMUP_DAYS = 5

# Anchor periods that can sit outside the warm-up window, so the history for them
# is loaded back to the start of the period (see ``_period_start``).
_LONG_ANCHORS = ("Week", "Month", "Quarter", "Year")

# Earlier sessions the forecast searches for analogues (see ``_analogue_candidates``).
_ANALOGUE_CANDIDATES = 60


def _period_start(trading_day: str, period: str) -> str:
    """First calendar day of the anchor period containing ``trading_day``.

    Matches ``indicator.auto_anchored_vwap._period_keys``, which keys bars by
    session date: ISO week, calendar month, quarter or year.
    """
    d = date.fromisoformat(trading_day)
    if period == "Week":
        start = d - timedelta(days=d.isoweekday() - 1)
    elif period == "Month":
        start = d.replace(day=1)
    elif period == "Quarter":
        start = d.replace(month=3 * ((d.month - 1) // 3) + 1, day=1)
    elif period == "Year":
        start = d.replace(month=1, day=1)
    else:
        raise ValueError(f"Not a long anchor period: {period!r}")
    return start.isoformat()


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


def _cutoff_utc_iso(session_date: str) -> str:
    """09:30 ET of the given YYYY-MM-DD, as a UTC ISO-8601 string."""
    d = datetime.strptime(session_date, "%Y-%m-%d")
    return NY_TZ.localize(datetime(d.year, d.month, d.day, 9, 30)).astimezone(pytz.utc).isoformat()


def _is_cash_index(symbol: str) -> bool:
    instrument = Config.instrument(symbol)
    return instrument is not None and instrument.is_index


def _safe(value, default=0.0):
    return default if value is None else value


def _metric(label: str, value: str, *, hint: str = "", accent: str = "#d1d4dc") -> None:
    """One labelled figure in the pre-open panel."""
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs uppercase tracking-wide").style("color:#787b86")
        ui.label(value).classes("text-lg font-medium").style(f"color:{accent}")
        if hint:
            ui.label(hint).classes("text-xs").style("color:#787b86")


class SessionExplorer:
    """Holds the view's state and keeps the chart in step with it."""

    def __init__(self, conn) -> None:
        self.conn = conn
        # Cash indices (VIX) are collected as pre-open context, not forecast, and every
        # control on this page — snapshot, analogues, forecast — assumes a future. They
        # feed in through the snapshot instead of being selectable here.
        self.contracts = {
            f"{c['symbol']} ({c['expiry']})": c
            for c in list_contracts(conn)
            if not _is_cash_index(c["symbol"])
        }

        self.contract = None
        self.date: Optional[str] = None
        self.timeframe = "1m"

        self.tema_on = True
        self.tema = TemaSessionSettings()
        self.aavwap_on = False
        self.aavwap = AutoAnchoredVwapSettings()

        # Loaded per session and reused across indicator changes.
        self.day_df: Optional[pd.DataFrame] = None
        self.recent_bars: Optional[pd.DataFrame] = None
        self.vix_bars: Optional[pd.DataFrame] = None
        # {trading_day: last RTH close} for the whole history up to the displayed
        # day: historical volatility spans all of it, recent_bars only a window.
        self.rth_closes: Dict[str, float] = {}
        self.snapshot: Dict[str, Any] = {}
        self.snapshot_is_real = False
        self._warmup_cache: Dict[Any, pd.DataFrame] = {}

        self.chart: Optional[LightweightChart] = None
        self.analogues: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_day(self) -> None:
        """Pulls the selected session and everything derived from it."""
        self._warmup_cache.clear()
        contract_id = self.contract["contract_id"]

        rows = get_day_bars(self.conn, contract_id, self.date, interval="1m")
        if not rows:
            self.day_df = None
            return

        df = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        self.day_df = calculate_vwap(_resample(df, self.timeframe))

        # The snapshot and the analogue search need earlier sessions: each candidate
        # plus the session before it. That window also covers the indicator warm-up.
        self.recent_bars = self._load_bars(self._history_start())
        self.rth_closes = get_daily_rth_closes(self.conn, contract_id, interval="1m", end_day=self.date)
        self.vix_bars = self._load_vix_bars(self._history_start())
        self._compute_snapshot()

    def _analogue_candidates(self) -> List[str]:
        """The earlier sessions a forecast compares against, newest first (no look-ahead)."""
        rows = self.conn.execute(
            "SELECT trading_day FROM session_days "
            "WHERE contract_id = %s AND interval = '1m' AND bar_count > 0 "
            "  AND trading_day < %s ORDER BY trading_day DESC LIMIT %s",
            (self.contract["contract_id"], self.date, _ANALOGUE_CANDIDATES),
        ).fetchall()
        return [r["trading_day"] for r in rows]

    def _history_start(self) -> Optional[str]:
        """
        First trading day ``recent_bars`` must hold: the session before the oldest
        analogue candidate (or before the displayed day), since every snapshot reads
        its previous session's RTH. None when there is nothing earlier to load.
        """
        candidates = self._analogue_candidates()
        oldest = candidates[-1] if candidates else self.date
        row = self.conn.execute(
            "SELECT MAX(trading_day) FROM session_days "
            "WHERE contract_id = %s AND interval = '1m' AND price_type = 'TRADES' "
            "  AND bar_count > 0 AND trading_day < %s",
            (self.contract["contract_id"], oldest),
        ).fetchone()
        return row[0] or oldest

    def _load_bars(self, start_day: Optional[str]) -> pd.DataFrame:
        return pd.DataFrame([
            dict(r) for r in get_bars(
                self.conn, self.contract["contract_id"], interval="1m",
                start_day=start_day, end_day=self.date,
            )
        ])

    def _load_vix_bars(self, start_day: Optional[str]) -> Optional[pd.DataFrame]:
        """Cash VIX over the same window, for pre-open context. None when absent."""
        if "VIX" not in Config.CONTEXT_SYMBOLS:
            return None
        contract = get_contract_by_expiry(self.conn, "VIX", Config.expiry_for("VIX"))
        if contract is None:
            return None
        rows = get_bars(
            self.conn, contract["contract_id"], interval="1m",
            start_day=start_day, end_day=self.date,
        )
        return pd.DataFrame([dict(r) for r in rows]) if rows else None

    def _compute_snapshot(self) -> None:
        snapshot = calculate_pre_open_snapshot(
            self.recent_bars, self.date, self.rth_closes, vix_df=self.vix_bars
        )
        self.snapshot_is_real = bool(snapshot) and "error" not in snapshot
        if self.snapshot_is_real:
            self.snapshot = snapshot
            return

        # Fall back to an approximate snapshot built from the displayed day alone,
        # so the chart still has reference levels. It is never persisted.
        df = self.day_df
        eth = df[df["session_scope"] == "ETH"] if "session_scope" in df.columns else df.iloc[0:0]
        prev_close = float(df["close"].iloc[0])
        ovn_high = float(eth["high"].max()) if not eth.empty else prev_close
        ovn_low = float(eth["low"].min()) if not eth.empty else prev_close
        self.snapshot = {
            "trading_day": self.date,
            "previous_rth_high": float(df["high"].max()),
            "previous_rth_low": float(df["low"].min()),
            "previous_rth_close": prev_close,
            "overnight_high": ovn_high,
            "overnight_low": ovn_low,
            "overnight_range": ovn_high - ovn_low,
            "gap": 0.0,
            "pre_open_direction": "FLAT",
            "historical_volatility": 0.01,
            "vwap": float(df["vwap"].iloc[0]) if "vwap" in df.columns else prev_close,
        }

    def _warmup_frame(self, sessions: Optional[int]) -> Optional[pd.DataFrame]:
        """
        The displayed session plus prior ones, resampled to the display interval.

        ``sessions=None`` keeps every loaded session. Timezone enrichment walks
        every row, so results are cached — indicator settings changes reuse the
        frame instead of rebuilding it.
        """
        if self.recent_bars is None or self.recent_bars.empty:
            return None

        anchor = self.aavwap.anchor_period if sessions is None else None
        cache_key = (self.date, self.timeframe, sessions, anchor)
        if cache_key in self._warmup_cache:
            return self._warmup_cache[cache_key]

        bars = self.recent_bars
        if sessions is None:
            # A long anchor needs every bar since its period began, and one earlier
            # bar so the boundary itself is visible; a week's margin covers holidays.
            start = (date.fromisoformat(_period_start(self.date, self.aavwap.anchor_period))
                     - timedelta(days=7)).isoformat()
            loaded_from = bars["trading_day"].min()
            if start < loaded_from:
                earlier = get_bars(
                    self.conn, self.contract["contract_id"], interval="1m",
                    start_day=start, end_day=loaded_from,
                )
                earlier = pd.DataFrame([dict(r) for r in earlier if r["trading_day"] < loaded_from])
                bars = pd.concat([earlier, bars], ignore_index=True) if not earlier.empty else bars

        enriched = enrich_candle_timezones(bars)
        enriched = enriched[enriched["trading_day"] <= self.date]
        days = sorted(enriched["trading_day"].dropna().unique())
        if not days:
            return None
        if sessions is not None:
            enriched = enriched[enriched["trading_day"].isin(set(days[-(sessions + 1):]))]

        frame = _resample(enriched, self.timeframe)
        self._warmup_cache[cache_key] = frame
        return frame

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _indicators(self) -> List[Any]:
        """Recomputes whichever overlays are switched on."""
        results = []
        if self.day_df is None or self.day_df.empty:
            return results

        if self.tema_on:
            try:
                warmup = self._warmup_frame(_INDICATOR_WARMUP_DAYS)
                if warmup is not None and not warmup.empty:
                    results.append(
                        compute_tema_session(warmup, self.tema)
                        .reindex_lines(self.day_df["timestamp_ny"])
                    )
            except ValueError as e:
                ui.notify(f"TEMA & Session Levels settings rejected: {e}", type="warning")

        if self.aavwap_on:
            try:
                sessions = None if self.aavwap.anchor_period in _LONG_ANCHORS else _INDICATOR_WARMUP_DAYS
                warmup = self._warmup_frame(sessions)
                if warmup is not None and not warmup.empty:
                    result = compute_auto_anchored_vwap(warmup, self.aavwap)
                    self.anchor_label.set_text(
                        f"Anchored VWAP: {result.meta['anchor_period']} anchor at "
                        f"{result.meta['anchor_timestamp']:%Y-%m-%d %H:%M %Z} "
                        f"({result.meta['anchor_bars_back']:,} bars back)."
                    )
                    self.anchor_label.set_visibility(True)
                    results.append(result.reindex_lines(self.day_df["timestamp_ny"]))
            except ValueError as e:
                ui.notify(f"Auto Anchored VWAP settings rejected: {e}", type="warning")
        else:
            self.anchor_label.set_visibility(False)

        return results

    def push(self, *, reload_candles: bool = False) -> None:
        """
        Sends the current state to the chart.

        ``reload_candles`` is false for indicator changes, which leaves the OHLC
        payload out of the message entirely — only the overlay series that
        actually changed travel over the socket.
        """
        if self.chart is None:
            return
        if self.day_df is None or self.day_df.empty:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []})
            return

        spec = build_chart_spec(
            self.day_df,
            features=self.snapshot,
            indicators=self._indicators(),
            fit=reload_candles,
        )
        if not reload_candles:
            spec.pop("candles", None)
            spec.pop("volume", None)
        self.chart.apply(spec)

    def refresh_session(self) -> None:
        """Reloads the day from the database and redraws everything."""
        if self.contract is None or self.date is None:
            return
        self.load_day()
        self._render_status()
        self._render_features()
        self.push(reload_candles=True)

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------

    def on_contract(self, event) -> None:
        self.contract = self.contracts[event.value]
        days = list_trading_days(self.conn, self.contract["contract_id"], limit=100)
        self.date_select.set_options(days, value=days[0] if days else None)
        self.date = days[0] if days else None
        self.refresh_session()

    def on_date(self, event) -> None:
        self.date = event.value
        self.refresh_session()

    def on_timeframe(self, event) -> None:
        self.timeframe = event.value
        self.refresh_session()

    def on_tema(self, field: str, value) -> None:
        self.tema = replace_setting(self.tema, field, value)
        self.push()

    def on_aavwap(self, field: str, value) -> None:
        self.aavwap = replace_setting(self.aavwap, field, value)
        self.push()

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
            if not self.snapshot_is_real:
                ui.badge("approximate features", color="orange").tooltip(
                    "Not enough prior history for an exact pre-open snapshot; forecasts will not be saved."
                )

    def _render_features(self) -> None:
        self.features_panel.clear()
        with self.features_panel:
            snapshot = self.snapshot
            prev_close = _safe(snapshot.get("previous_rth_close"))
            gap = _safe(snapshot.get("gap"))
            pct = (gap / prev_close * 100) if prev_close else 0.0
            direction = snapshot.get("pre_open_direction", "FLAT")

            _metric("Trade date", str(snapshot.get("trading_day", "—")))
            _metric("Previous RTH close", f"${prev_close:,.2f}")
            _metric(
                "Opening gap",
                f"{gap:+,.2f}",
                hint=f"{pct:+.2f}% of previous close",
                accent="#26a69a" if gap > 0 else "#ef5350" if gap < 0 else "#d1d4dc",
            )
            _metric("Historical volatility", f"{_safe(snapshot.get('historical_volatility')) * 100:.2f}%")
            _metric(
                "Overnight range",
                f"${_safe(snapshot.get('overnight_range')):,.2f}",
                hint=f"H {_safe(snapshot.get('overnight_high')):,.2f}  ·  "
                     f"L {_safe(snapshot.get('overnight_low')):,.2f}",
            )
            _metric(
                "Pre-open bias",
                direction,
                accent={"UP": "#26a69a", "DOWN": "#ef5350"}.get(direction, "#d1d4dc"),
            )
            _metric("Overnight VWAP", f"{_safe(snapshot.get('vwap')):,.2f}")

    def build(self) -> None:
        if not self.contracts:
            with ui.card().classes("w-full"):
                ui.label("No contracts found.").classes("text-lg")
                ui.label("Run the collector, or populate_mock_data.py, before opening this view.")
            return

        first_label = next(iter(self.contracts))
        self.contract = self.contracts[first_label]
        days = list_trading_days(self.conn, self.contract["contract_id"], limit=100)
        self.date = days[0] if days else None

        # The drawer is a top-level layout element, so it has to be created as a
        # direct child of the page rather than inside the body column.
        self._build_indicator_drawer()

        with ui.column().classes("w-full p-4 gap-3"):
            self._build_controls(first_label, days)

            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=620)
                    self.anchor_label = ui.label("").classes("text-xs").style("color:#787b86")
                    self.anchor_label.set_visibility(False)
                with ui.card().classes("w-72 shrink-0").style("background:#1c212e"):
                    ui.label("Pre-open features").classes("text-sm font-medium")
                    self.features_panel = ui.column().classes("gap-3 w-full")

            self._build_forecast_panel()

        if self.date is None:
            ui.notify("The selected contract has no stored sessions.", type="warning")
            return
        self.refresh_session()

    def _build_controls(self, first_label: str, days: List[str]) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            ui.select(
                list(self.contracts), value=first_label, label="Contract",
                on_change=self.on_contract,
            ).classes("w-56")
            self.date_select = ui.select(
                days, value=self.date, label="Session date (NY trading day)",
                on_change=self.on_date,
            ).classes("w-56")
            ui.toggle(
                ["1m", "5m", "15m", "30m"], value="1m", on_change=self.on_timeframe,
            ).props("dense")
            self.status = ui.row().classes("items-center gap-2")
            ui.space()
            ui.button("Indicators", icon="tune", on_click=lambda: self.drawer.toggle()).props("flat")

    def _build_indicator_drawer(self) -> None:
        self.drawer = ui.right_drawer(value=False).classes("p-3").style("background:#1c212e")
        with self.drawer:
            ui.label("Indicators").classes("text-lg font-medium")
            self._tema_controls()
            self._aavwap_controls()

    def _tema_controls(self) -> None:
        d = self.tema
        with ui.expansion("TEMA & Session Levels", icon="show_chart", value=True).classes("w-full"):
            ui.checkbox(
                "Overlay on candles", value=self.tema_on,
                on_change=lambda e: (setattr(self, "tema_on", e.value), self.push()),
            )

            ui.label("Moving averages").classes("text-xs uppercase mt-2").style("color:#787b86")
            for label, field, lo, hi in (
                ("TEMA length", "tema_length", 1, 500),
                ("TEMA smoothing", "tema_smoothing_length", 1, 100),
                ("Trigger EMA length", "trigger_ema_length", 1, 500),
                ("EMA9 smoothing", "ema9_smoothing_length", 1, 100),
                ("Trend EMA length", "trend_ema_length", 1, 1000),
            ):
                ui.number(
                    label, value=getattr(d, field), min=lo, max=hi, precision=0,
                    on_change=lambda e, f=field: self.on_tema(f, int(e.value or 1)),
                ).props("dense outlined").classes("w-full")

            ui.label("Sessions").classes("text-xs uppercase mt-2").style("color:#787b86")
            ui.input("Timezone", value=d.timezone,
                     on_change=lambda e: self.on_tema("timezone", e.value)).props("dense outlined")
            for label, field in (
                ("RTH session", "rth_session"),
                ("Overnight session", "overnight_session"),
                ("Premarket session", "premarket_session"),
            ):
                ui.input(label, value=getattr(d, field),
                         on_change=lambda e, f=field: self.on_tema(f, e.value)).props("dense outlined")

            ui.label("Levels").classes("text-xs uppercase mt-2").style("color:#787b86")
            for label, field in (
                ("Previous RTH high/low/open", "show_prev_rth"),
                ("Overnight high/low/open", "show_overnight"),
                ("Premarket high/low", "show_premarket"),
                ("Price labels on the axis", "show_labels"),
            ):
                ui.checkbox(label, value=getattr(d, field),
                            on_change=lambda e, f=field: self.on_tema(f, e.value))

            ui.label("Line width").classes("text-xs mt-2").style("color:#787b86")
            ui.slider(min=1, max=4, value=d.line_width,
                      on_change=lambda e: self.on_tema("line_width", int(e.value))).props("label-always")
            ui.select(list(LINE_STYLE_OPTIONS), value=d.line_style, label="Line style",
                      on_change=lambda e: self.on_tema("line_style", e.value)).props("dense outlined")

    def _aavwap_controls(self) -> None:
        d = self.aavwap
        with ui.expansion("Auto Anchored VWAP", icon="anchor").classes("w-full"):
            ui.checkbox(
                "Overlay on candles", value=self.aavwap_on,
                on_change=lambda e: (setattr(self, "aavwap_on", e.value), self.push()),
            )

            ui.select(
                list(ANCHOR_PERIOD_OPTIONS), value=d.anchor_period, label="Anchor period",
                on_change=lambda e: self.on_aavwap("anchor_period", e.value),
            ).props("dense outlined").tooltip(
                "Auto resolves to Session on the intraday timeframes this view shows."
            )
            ui.number(
                "Lookback (for HH / LL / HV)", value=d.lookback_length, min=2, max=5000, precision=0,
                on_change=lambda e: self.on_aavwap("lookback_length", int(e.value or 2)),
            ).props("dense outlined")
            ui.select(list(SOURCE_OPTIONS), value=d.source, label="Source",
                      on_change=lambda e: self.on_aavwap("source", e.value)).props("dense outlined")
            ui.select(list(CALC_MODE_OPTIONS), value=d.calc_mode, label="Bands calculation",
                      on_change=lambda e: self.on_aavwap("calc_mode", e.value)).props("dense outlined")

            ui.label("VWAP line width").classes("text-xs mt-2").style("color:#787b86")
            ui.slider(min=1, max=4, value=d.vwap_width,
                      on_change=lambda e: self.on_aavwap("vwap_width", int(e.value))).props("label-always")

            ui.label("Bands").classes("text-xs uppercase mt-2").style("color:#787b86")
            for number in (1, 2, 3):
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.checkbox(
                        f"Band {number}", value=getattr(d, f"show_band{number}"),
                        on_change=lambda e, n=number: self.on_aavwap(f"show_band{n}", e.value),
                    )
                    ui.number(
                        value=getattr(d, f"mult{number}"), min=0.0, max=10.0, step=0.5, precision=1,
                        on_change=lambda e, n=number: self.on_aavwap(f"mult{n}", float(e.value or 0)),
                    ).props("dense outlined").classes("w-24")
            ui.checkbox(
                "Fill inner band", value=d.fill_bands,
                on_change=lambda e: self.on_aavwap("fill_bands", e.value),
            )

    # ------------------------------------------------------------------
    # Forecasting
    # ------------------------------------------------------------------

    def _build_forecast_panel(self) -> None:
        with ui.card().classes("w-full mt-2").style("background:#1c212e"):
            with ui.row().classes("items-center w-full"):
                ui.label("Opening scenario generator").classes("text-lg font-medium")
                ui.space()
                self.persist = ui.checkbox("Save to database", value=True)
                ui.button("Generate forecast", icon="auto_awesome",
                          on_click=self.generate_forecast).props("color=primary")
            ui.label(
                "Builds opening scenarios and path probabilities from the pre-open snapshot "
                "and the closest historical analogues."
            ).classes("text-sm").style("color:#787b86")
            self.forecast_panel = ui.column().classes("w-full")

    async def generate_forecast(self) -> None:
        if self.day_df is None or self.day_df.empty:
            ui.notify("Load a session first.", type="warning")
            return

        self.forecast_panel.clear()
        with self.forecast_panel:
            spinner = ui.spinner(size="lg")

        try:
            forecast, analogues = await run.io_bound(self._run_forecast)
        except Exception as e:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.forecast_panel.clear()
            with self.forecast_panel:
                ui.label(f"Forecast failed: {e}").style("color:#ef5350")
            return

        spinner.delete()
        self.analogues = analogues
        self._render_forecast(forecast, analogues)

    def _run_forecast(self):
        """Analogue search plus the model call. Runs off the event loop."""
        contract_id = self.contract["contract_id"]
        # Analogue candidates: strictly earlier sessions only (no look-ahead).
        hist_snapshots = []
        for day in self._analogue_candidates():
            hist = calculate_pre_open_snapshot(
                self.recent_bars, day, self.rth_closes, vix_df=self.vix_bars
            )
            if "error" in hist:
                continue
            outcome = get_outcome(self.conn, contract_id, day)
            if outcome is not None:
                hist["outcome"] = dict(outcome)
            hist_snapshots.append(hist)

        analogues = find_analogues(self.snapshot, hist_snapshots, k=3)
        client = ForecastClient(model_name=Config.LLM_MODEL)
        forecast = client.get_forecast(
            self.date, self.snapshot, analogues,
            instrument=Config.describe_instrument(self.contract["symbol"]),
        )

        if self.persist.value and self.snapshot_is_real:
            self._persist(client, forecast, analogues)
        return forecast, analogues

    def _persist(self, client, forecast, analogues) -> None:
        contract_id = self.contract["contract_id"]
        cutoff = _cutoff_utc_iso(self.date)
        snapshot = self.snapshot
        snapshot_id = save_feature_snapshot(
            conn=self.conn, contract_id=contract_id, timestamp_utc=cutoff,
            previous_rth_high=snapshot.get("previous_rth_high"),
            previous_rth_low=snapshot.get("previous_rth_low"),
            previous_rth_close=snapshot.get("previous_rth_close"),
            overnight_high=snapshot.get("overnight_high"),
            overnight_low=snapshot.get("overnight_low"),
            overnight_range=snapshot.get("overnight_range"),
            gap=snapshot.get("gap"),
            pre_open_direction=snapshot.get("pre_open_direction", "FLAT"),
            historical_volatility=snapshot.get("historical_volatility"),
            vwap=snapshot.get("vwap"),
            raw_features=snapshot,
            feature_version=Config.FEATURE_VERSION,
            vix_pre_open=snapshot.get("vix_pre_open"),
            vix_change=snapshot.get("vix_change"),
        )
        prediction_id = save_prediction(
            conn=self.conn, contract_id=contract_id, forecast_cutoff=cutoff,
            snapshot_id=snapshot_id, model_version=client.model_name,
            prompt_version=Config.PROMPT_VERSION,
            opening_bias=forecast.get("opening_bias"),
            scenarios=forecast.get("scenarios", {}),
            probabilities=forecast.get("probabilities", {}),
            raw_response=str(forecast),
            created_at=datetime.now(pytz.utc).isoformat(),
        )
        if analogues:
            save_analogue_matches(self.conn, prediction_id, [
                {"match_date": a["match_date"], "similarity_score": a["similarity_score"],
                 "ranking": a["ranking"]}
                for a in analogues
            ])
        self._saved_as = prediction_id

    def _render_forecast(self, forecast: Dict[str, Any], analogues: List[Dict[str, Any]]) -> None:
        probabilities = forecast.get("probabilities", {})
        primary = forecast.get("scenarios", {}).get("primary_scenario", {})
        bias = forecast.get("opening_bias", "UNKNOWN")

        with self.forecast_panel:
            with ui.row().classes("w-full no-wrap gap-6 items-start"):
                with ui.column().classes("grow gap-2"):
                    ui.label(f"Overall bias: {bias}").classes("text-xl font-medium").style(
                        f"color:{ {'BULLISH': '#26a69a', 'BEARISH': '#ef5350'}.get(bias, '#d1d4dc') }"
                    )
                    ui.label(f"Primary scenario — {primary.get('name', 'n/a')}").classes("font-medium")
                    ui.label(primary.get("description", "")).classes("text-sm").style("color:#b2b5be")

                    for heading, key in (("Triggers to watch", "triggers"),
                                         ("Invalidation levels", "invalidations")):
                        items = primary.get(key) or []
                        if not items:
                            continue
                        ui.label(heading).classes("text-xs uppercase mt-2").style("color:#787b86")
                        for item in items:
                            ui.label(f"· {item}").classes("text-sm font-mono").style("color:#d1d4dc")

                    rationale = forecast.get("analogue_rationale")
                    if rationale:
                        ui.label("Analogue rationale").classes("text-xs uppercase mt-2").style("color:#787b86")
                        ui.label(rationale).classes("text-sm").style("color:#b2b5be")

                with ui.column().classes("w-96 shrink-0 gap-2"):
                    ui.label("Path probabilities").classes("text-xs uppercase").style("color:#787b86")
                    for label, key, color in (
                        ("Bullish continuation", "bullish_continuation_pct", "#26a69a"),
                        ("Mean reversion / gap fill", "mean_reversion_gap_fill_pct", "#ab47bc"),
                        ("Bearish rejection", "bearish_rejection_pct", "#ef5350"),
                    ):
                        value = float(probabilities.get(key, 0.0) or 0.0)
                        with ui.row().classes("items-center w-full no-wrap gap-2"):
                            ui.label(label).classes("text-sm w-48 shrink-0").style("color:#b2b5be")
                            ui.linear_progress(
                                value=min(max(value / 100.0, 0.0), 1.0), show_value=False,
                            ).props("rounded size=14px").style(f"color:{color}").classes("grow")
                            ui.label(f"{value:.1f}%").classes("text-sm w-14 text-right font-mono")

                    ui.label("Matching historical days").classes("text-xs uppercase mt-3").style("color:#787b86")
                    if not analogues:
                        ui.label("No earlier sessions with computable features.").classes("text-sm").style(
                            "color:#787b86"
                        )
                    for a in analogues:
                        with ui.row().classes("items-center w-full justify-between"):
                            ui.label(a["match_date"]).classes("text-sm font-mono")
                            ui.label(
                                f"{a['similarity_score'] * 100:.1f}% · d={a['distance']:.4f}"
                            ).classes("text-xs").style("color:#787b86")

            saved = getattr(self, "_saved_as", None)
            if self.persist.value and not self.snapshot_is_real:
                ui.label("Not saved: the feature snapshot was approximate, not exact.").classes(
                    "text-xs mt-2"
                ).style("color:#ffa726")
            elif saved is not None:
                ui.label(f"Saved as prediction #{saved}.").classes("text-xs mt-2").style("color:#787b86")
                self._saved_as = None


def replace_setting(settings, field: str, value):
    """Returns a copy of a settings dataclass with one field changed."""
    return replace(settings, **{field: value})


def show_candles_page(conn) -> None:
    SessionExplorer(conn).build()
