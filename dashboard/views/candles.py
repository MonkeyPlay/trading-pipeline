# dashboard/views/candles.py
"""
Candlestick exploration view for the NQ Opening Forecast System.

Selectors load a stored session (NQ's current contract by default), the
pre-open panel shows the feature snapshot, and the forecast panel runs the
opening model against it - or shows the forecast already stored for that day.
Below it, a second chart replays the regular session of the best-matching
historical day, and a dropdown switches between the other matches.

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing chart. The chart is created once per page
load, so a redraw leaves the user's zoom, scroll and crosshair exactly where
they were.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
from nicegui import run, ui

from config import Config
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.spec import build_chart_spec
from database.queries import (
    contracts_with_day,
    get_analogue_matches,
    get_bars,
    get_contract_by_expiry,
    get_daily_rth_closes,
    get_day_bars,
    get_day_prediction,
    get_latest_contract,
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
from forecaster.client import PROMPT_VERSION, ForecastClient
from matching.normalizer import find_analogues

_RESAMPLE_FREQ = {"5m": "5min", "15m": "15min", "30m": "30min"}

# Earlier sessions the forecast searches for analogues (see ``_analogue_candidates``).
_ANALOGUE_CANDIDATES = 60

# The instrument the page opens on; its current contract is preselected.
DEFAULT_SYMBOL = "NQ"

_MUTED = "color:#787b86"


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


def _is_context_only(symbol: str) -> bool:
    """Collected as intermarket context (VIX, TNX, DX, SMH, ...) rather than forecast."""
    if symbol in Config.SYMBOLS:
        return False
    instrument = Config.instrument(symbol)
    return symbol in Config.CONTEXT_SYMBOLS or (instrument is not None and not instrument.is_future)


def _safe(value, default=0.0):
    return default if value is None else value


def _load_json(value) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def stored_forecast(row) -> Dict[str, Any]:
    """
    A stored v1 prediction row as the forecast dict the panel renders. The
    horizon and rationale are only in ``raw_response`` (the forecast's Python
    repr), which is read as a literal - never evaluated - when it is one.
    """
    forecast: Dict[str, Any] = {
        "opening_bias": row["opening_bias"],
        "scenarios": _load_json(row["scenarios"]),
        "probabilities": _load_json(row["probabilities"]),
    }
    try:
        raw = ast.literal_eval(row["raw_response"] or "")
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        raw = None
    if isinstance(raw, dict):
        for key in ("forecast_horizon", "analogue_rationale"):
            if raw.get(key):
                forecast[key] = raw[key]
    return forecast


def _previous_rth_close(conn, contract_id: int, trading_day: str) -> Optional[float]:
    """Close of the last RTH bar of the stored session before ``trading_day``."""
    row = conn.execute(
        "SELECT MAX(trading_day) FROM session_days WHERE contract_id = %s AND interval = '1m' "
        "AND price_type = 'TRADES' AND bar_count > 0 AND trading_day < %s",
        (contract_id, trading_day),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    rth = get_day_bars(conn, contract_id, str(row[0]), interval="1m", session_scope="RTH")
    return float(rth[-1]["close"]) if rth else None


def _expires_on_or_after(expiry: Optional[str], day: str) -> bool:
    """Whether a contract expiry (YYYYMMDD, or a YYYYMM contract month) is not before ``day``."""
    digits = "".join(ch for ch in str(expiry or "") if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8] >= day.replace("-", "")
    if len(digits) == 6:
        return digits >= day.replace("-", "")[:6]
    return False


def analogue_contracts(conn, symbol: str, match_date: str) -> List[Any]:
    """
    Contracts of ``symbol`` holding bars for ``match_date``, best first: the
    closest contract expiring on or after the day (the one that was trading
    then), then later ones, then - only if nothing later holds it - earlier ones,
    closest first. A forecast on the December contract can match an August day,
    which is shown from September's bars, not from December's thin history.
    """
    held = list(contracts_with_day(conn, symbol, match_date))   # nearest expiry first
    later = [c for c in held if _expires_on_or_after(c["expiry"], match_date)]
    earlier = [c for c in held if not _expires_on_or_after(c["expiry"], match_date)]
    return later + earlier[::-1]


def load_analogue_session(conn, symbol: str, match_date: str, timeframe: str = "1m") -> Dict[str, Any]:
    """
    The regular session of a matched historical day, ready to chart.

    Bars come from the best contract holding the day (``analogue_contracts``:
    the closest one expiring on or after it), whatever contract the forecast
    itself was made on. Returns ``{'contract', 'bars', 'levels', 'stats'}``:
    RTH bars at ``timeframe`` with session VWAP, the day's own pre-open levels
    (previous RTH close, overnight high/low) and its RTH open/high/low/close.
    ``bars`` is None when no contract holds RTH bars for the day.
    """
    match_date = str(match_date)
    for contract in analogue_contracts(conn, symbol, match_date):
        rows = get_day_bars(conn, contract["contract_id"], match_date, interval="1m")
        if not rows:
            continue
        full = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        rth = full[full["session_scope"] == "RTH"]
        if rth.empty:
            continue
        pre_open = full[(full["session_scope"] == "ETH") & (full["timestamp_ny"] < rth["timestamp_ny"].iloc[0])]
        levels = {
            "previous_rth_close": _previous_rth_close(conn, contract["contract_id"], match_date),
            "overnight_high": float(pre_open["high"].max()) if not pre_open.empty else None,
            "overnight_low": float(pre_open["low"].min()) if not pre_open.empty else None,
        }
        # VWAP is anchored at the 18:00 ET session open like the main chart, then
        # only the regular session is shown.
        shown = calculate_vwap(_resample(full, timeframe))
        shown = shown[shown["session_scope"] == "RTH"]
        stats = {
            "open": float(rth["open"].iloc[0]), "high": float(rth["high"].max()),
            "low": float(rth["low"].min()), "close": float(rth["close"].iloc[-1]), "bars": int(len(rth)),
        }
        return {"contract": contract, "bars": shown, "levels": levels, "stats": stats}
    return {"contract": None, "bars": None, "levels": {}, "stats": None}


def _analogue_label(analogue: Dict[str, Any]) -> str:
    rank = analogue.get("ranking")
    prefix = f"#{rank} · " if rank is not None else ""
    return f"{prefix}{analogue['match_date']} · {analogue['similarity_score'] * 100:.1f}% match"


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
        # Context instruments are collected, not forecast, and every control on this
        # page — snapshot, analogues, forecast — assumes a forecast target. They feed in
        # through the snapshot instead. The collector also records whole futures chains
        # to plan rolls, so only contracts that actually hold bars are offered.
        self.contracts = {
            f"{c['symbol']} ({c['expiry']})": c
            for c in list_contracts(conn, with_data_only=True)
            if not _is_context_only(c["symbol"])
        }

        self.contract = None
        self.date: Optional[str] = None
        self.timeframe = "1m"

        # Loaded per session.
        self.day_df: Optional[pd.DataFrame] = None
        self.recent_bars: Optional[pd.DataFrame] = None
        self.vix_bars: Optional[pd.DataFrame] = None
        # {trading_day: last RTH close} for the whole history up to the displayed
        # day: historical volatility spans all of it, recent_bars only a window.
        self.rth_closes: Dict[str, float] = {}
        self.snapshot: Dict[str, Any] = {}
        self.snapshot_is_real = False

        self.chart: Optional[LightweightChart] = None
        self.analogues: List[Dict[str, Any]] = []

        # The matching-historical-day view, shown while a forecast is displayed.
        self.analogue_chart: Optional[LightweightChart] = None
        self.analogue_options: Dict[str, Dict[str, Any]] = {}
        self.analogue_choice: Optional[str] = None
        self.analogue_symbol: Optional[str] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_day(self) -> None:
        """Pulls the selected session and everything derived from it."""
        contract_id = self.contract["contract_id"]

        rows = get_day_bars(self.conn, contract_id, self.date, interval="1m")
        if not rows:
            self.day_df = None
            return

        df = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        self.day_df = calculate_vwap(_resample(df, self.timeframe))

        # The snapshot and the analogue search need earlier sessions: each candidate
        # plus the session before it.
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

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def push(self) -> None:
        """Sends the current state to the chart."""
        if self.chart is None:
            return
        if self.day_df is None or self.day_df.empty:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": []})
            return

        self.chart.apply(build_chart_spec(self.day_df, features=self.snapshot, fit=True))

    def refresh_session(self, keep_forecast: bool = False) -> None:
        """
        Reloads the day from the database and redraws everything. A new day
        shows the forecast stored for it, if any; ``keep_forecast`` (a display
        change only) keeps the forecast on screen and redraws its matching day.
        """
        if self.contract is None or self.date is None:
            return
        self.load_day()
        self._render_status()
        self._render_features()
        self.push()
        if keep_forecast:
            self._push_analogue()
        else:
            self._show_stored_forecast()

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------

    def on_contract(self, event) -> None:
        self.contract = self.contracts[event.value]
        days = list_trading_days(self.conn, self.contract["contract_id"], limit=100)
        self.date_select.set_options(days, value=days[0] if days else None)
        self.date = days[0] if days else None
        if self.date is None:
            self.forecast_panel.clear()
            self._render_analogue_view([], None)
        self.refresh_session()

    def on_date(self, event) -> None:
        self.date = event.value
        self.refresh_session()

    def on_timeframe(self, event) -> None:
        self.timeframe = event.value
        self.refresh_session(keep_forecast=True)

    def on_analogue(self, event) -> None:
        self.analogue_choice = event.value
        self._push_analogue()

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

    def _default_label(self) -> str:
        """NQ's current contract (see get_latest_contract), else any NQ contract, else the first."""
        latest = get_latest_contract(self.conn, DEFAULT_SYMBOL)
        if latest is not None:
            for label, contract in self.contracts.items():
                if contract["contract_id"] == latest["contract_id"]:
                    return label
        labels = [label for label, c in self.contracts.items() if c["symbol"] == DEFAULT_SYMBOL]
        return labels[-1] if labels else next(iter(self.contracts))

    def build(self) -> None:
        if not self.contracts:
            with ui.card().classes("w-full"):
                ui.label("No contracts found.").classes("text-lg")
                ui.label("Run the collector, or populate_mock_data.py, before opening this view.")
            return

        first_label = self._default_label()
        self.contract = self.contracts[first_label]
        days = list_trading_days(self.conn, self.contract["contract_id"], limit=100)
        self.date = days[0] if days else None

        with ui.column().classes("w-full p-4 gap-3"):
            self._build_controls(first_label, days)

            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=620)
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
        # Below the forecast: the best-matching historical day's regular session.
        self.analogue_section = ui.column().classes("w-full mt-2")

    async def generate_forecast(self) -> None:
        if self.day_df is None or self.day_df.empty:
            ui.notify("Load a session first.", type="warning")
            return

        self.forecast_panel.clear()
        self._render_analogue_view([], None)
        with self.forecast_panel:
            ui.spinner(size="lg")

        try:
            forecast, analogues = await run.io_bound(self._run_forecast)
        except Exception as e:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.forecast_panel.clear()
            with self.forecast_panel:
                ui.label(f"Forecast failed: {e}").style("color:#ef5350")
            return

        self.forecast_panel.clear()
        self._show_forecast(forecast, analogues, self.contract["symbol"])

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
        client = ForecastClient()
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
            prompt_version=PROMPT_VERSION,
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

    def _show_stored_forecast(self) -> None:
        """Shows the newest forecast stored for the selected day, or says there is none."""
        self.forecast_panel.clear()
        row = get_day_prediction(self.conn, self.contract["symbol"], self.date, self.contract["contract_id"])
        if row is None:
            self._render_analogue_view([], None)
            with self.forecast_panel:
                ui.label(
                    f"No forecast stored for {self.date} yet. Generate one to see it and its "
                    f"matching historical days."
                ).classes("text-sm").style(_MUTED)
            return
        analogues = [
            {"match_date": str(m["match_date"]), "similarity_score": float(m["similarity_score"]),
             "ranking": int(m["ranking"])}
            for m in get_analogue_matches(self.conn, row["prediction_id"])
        ]
        self._show_forecast(stored_forecast(row), analogues, row["symbol"], stored=row)

    def _show_forecast(self, forecast: Dict[str, Any], analogues: List[Dict[str, Any]], symbol: str,
                       stored=None) -> None:
        self.analogues = analogues
        self._render_forecast(forecast, analogues, stored)
        self._render_analogue_view(analogues, symbol)

    def _render_forecast(self, forecast: Dict[str, Any], analogues: List[Dict[str, Any]], stored=None) -> None:
        probabilities = forecast.get("probabilities", {})
        primary = forecast.get("scenarios", {}).get("primary_scenario", {})
        bias = forecast.get("opening_bias", "UNKNOWN")

        with self.forecast_panel:
            if stored is not None:
                note = (f"Stored forecast #{stored['prediction_id']} · {stored['model_version']} · "
                        f"made {str(stored['created_at'])[:16]} UTC")
                if stored["contract_id"] != self.contract["contract_id"]:
                    note += f" · on the {stored['symbol']} {stored['expiry']} contract"
                ui.label(note).classes("text-xs").style(_MUTED)
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
                            detail = f"{a['similarity_score'] * 100:.1f}%"
                            if a.get("distance") is not None:
                                detail += f" · d={a['distance']:.4f}"
                            ui.label(detail).classes("text-xs").style("color:#787b86")

            saved = getattr(self, "_saved_as", None)
            if stored is not None:   # already in the database; the save notices are for new ones
                return
            if self.persist.value and not self.snapshot_is_real:
                ui.label("Not saved: the feature snapshot was approximate, not exact.").classes(
                    "text-xs mt-2"
                ).style("color:#ffa726")
            elif saved is not None:
                ui.label(f"Saved as prediction #{saved}.").classes("text-xs mt-2").style("color:#787b86")
                self._saved_as = None


    # ------------------------------------------------------------------
    # Matching historical day
    # ------------------------------------------------------------------

    def _render_analogue_view(self, analogues: List[Dict[str, Any]], symbol: Optional[str]) -> None:
        """The best match's regular session on its own chart, with a dropdown for the others."""
        self.analogue_section.clear()
        self.analogue_chart = None
        ranked = sorted(analogues, key=lambda a: (-a["similarity_score"], a.get("ranking") or 0))
        self.analogue_options = {a["match_date"]: a for a in ranked}
        self.analogue_choice = ranked[0]["match_date"] if ranked else None
        self.analogue_symbol = symbol
        if not ranked:
            return

        session = self._analogue_session()
        with self.analogue_section:
            with ui.card().classes("w-full").style("background:#1c212e"):
                with ui.row().classes("items-center w-full"):
                    with ui.column().classes("gap-0"):
                        ui.label("Matching historical day — RTH").classes("text-lg font-medium")
                        ui.label(
                            "The regular session (09:30–16:00 ET) of a day the forecast matched, with "
                            "that day's own previous close and overnight range."
                        ).classes("text-sm").style(_MUTED)
                    ui.space()
                    ui.select(
                        {d: _analogue_label(a) for d, a in self.analogue_options.items()},
                        value=self.analogue_choice, label="Matching day", on_change=self.on_analogue,
                    ).classes("w-80")
                self.analogue_summary = ui.row().classes("w-full gap-8 items-start")
                self.analogue_chart = LightweightChart(height=420, spec=self._analogue_spec(session))
        self._render_analogue_summary(session)

    def _analogue_session(self) -> Dict[str, Any]:
        return load_analogue_session(self.conn, self.analogue_symbol, self.analogue_choice, self.timeframe)

    @staticmethod
    def _analogue_spec(session: Dict[str, Any]) -> Dict[str, Any]:
        return build_chart_spec(session["bars"], features=session["levels"], fit=True)

    def _push_analogue(self) -> None:
        """Redraws the matching-day chart for the current choice and timeframe."""
        if self.analogue_chart is None or self.analogue_choice is None:
            return
        session = self._analogue_session()
        self.analogue_chart.apply(self._analogue_spec(session))
        self._render_analogue_summary(session)

    def _render_analogue_summary(self, session: Dict[str, Any]) -> None:
        self.analogue_summary.clear()
        analogue = self.analogue_options[self.analogue_choice]
        with self.analogue_summary:
            stats = session["stats"]
            if stats is None:
                ui.label(f"No RTH bars are stored for {self.analogue_choice}.").classes("text-sm").style(
                    "color:#ffa726")
                return
            contract = session["contract"]
            change = stats["close"] - stats["open"]
            prev_close = session["levels"].get("previous_rth_close")
            _metric("Session", str(self.analogue_choice),
                    hint=f"{contract['symbol']} {contract['expiry']}")
            _metric("Match", f"{analogue['similarity_score'] * 100:.1f}%",
                    hint=f"rank #{analogue['ranking']}" if analogue.get("ranking") is not None else "")
            _metric("RTH open", f"{stats['open']:,.2f}",
                    hint=f"gap {stats['open'] - prev_close:+,.2f} vs prev close" if prev_close else "")
            _metric("RTH high / low", f"{stats['high']:,.2f} / {stats['low']:,.2f}",
                    hint=f"range {stats['high'] - stats['low']:,.2f}")
            _metric("RTH close", f"{stats['close']:,.2f}",
                    hint=f"{change:+,.2f} ({change / stats['open'] * 100:+.2f}%) from the open",
                    accent="#26a69a" if change > 0 else "#ef5350" if change < 0 else "#d1d4dc")


def show_candles_page(conn) -> None:
    SessionExplorer(conn).build()
