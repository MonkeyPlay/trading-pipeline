# dashboard/views/candles.py
"""
Candlestick exploration view for the NQ Opening Forecast System.

Three selectors pick what is shown, in this order: the session day, then an
instrument with bars that day (ES, NQ, ...), then one of its contracts holding
the day (the one active that day first). The chart shows the regular session
with 15 minutes either side - 09:15 to 16:15 ET, or to 13:15 on an early close -
and draws those extra minutes grey on a grey background. The pre-open panel
shows the feature snapshot, and the forecast panel runs the opening model
against it - or shows the forecast already stored for that day; both mostly call
directions, which nothing has predicted, so they are folded away. A dropdown
overlays one of the closest pre-open matches on the same chart: a day with similar
volatility before the open, whose path is no forecast of the day's.

The range nowcast (forecaster/range_nowcast.py) says how far the price still
travels before 09:45, 10:30 and 16:00, drawn as a cone from the current price.
The 'as of' slider replays it minute by minute through a past session; on a live
session (today, with the real-time streamer storing bars) the page polls for new
bars and, while following, moves the nowcast on with every completed minute.

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing chart. The chart is created once per page
load, so a redraw leaves the user's zoom, scroll and crosshair exactly where
they were.
"""

from __future__ import annotations

import ast
import json
import math
from typing import Any, Dict, List, Optional

import pandas as pd
from nicegui import run, ui

from config import Config
from dashboard.components.coverage_map import coverage_map
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.model_forecast import ModelForecastPanel
from dashboard.components.spec import build_chart_spec
from database.queries import (
    contracts_for_day,
    get_analogue_matches,
    get_bars,
    get_daily_rth_closes,
    get_day_bars,
    get_day_prediction,
    get_session_day,
    list_contracts,
    list_session_days,
    symbols_with_day,
)
from features import calendar as cal
from features.calculations import (
    calculate_pre_open_snapshot,
    calculate_vwap,
    enrich_candle_timezones,
)
from forecaster import first_hour, range_nowcast
from forecaster.analogue import analogue_forecast, day_snapshot, rank_preopen, store_forecast, vix_bars
from forecaster.client import HORIZON as FIRST_HOUR_HORIZON

_RESAMPLE_FREQ = {"5m": "5min", "15m": "15min", "30m": "30min"}

# Earlier sessions the forecast searches for analogues (see ``_analogue_candidates``).
_ANALOGUE_CANDIDATES = 60

# The instrument the page opens on; its current contract is preselected.
DEFAULT_SYMBOL = "NQ"

_MUTED = "color:#787b86"

# Minutes shown either side of the regular session, drawn muted.
_EXTRA = pd.Timedelta(minutes=15)
_NY = "America/New_York"
_BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}


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


def analogue_contracts(conn, symbol: str, match_date: str) -> List[Any]:
    """
    Contracts of ``symbol`` holding bars for ``match_date``, best first
    (``database.queries.contracts_for_day``): the closest contract expiring on or
    after the day. A forecast on the December contract can match an August day,
    which is shown from September's bars, not from December's thin history.
    """
    return contracts_for_day(conn, symbol, match_date)


def day_contracts(conn, symbol: str, day: str) -> List[Any]:
    """
    Contracts of ``symbol`` holding ``day``, for the contract selector: the one
    the collector made active that day first (it holds the day's session
    before the roll and after it), then as ``analogue_contracts`` orders them.
    """
    ordered = analogue_contracts(conn, symbol, day)
    row = conn.execute("SELECT contract_id FROM active_contracts WHERE symbol = %s AND trading_day = %s;",
                       (symbol, day)).fetchone()
    if row is not None:
        active = [c for c in ordered if c["contract_id"] == row["contract_id"]]
        ordered = active + [c for c in ordered if c["contract_id"] != row["contract_id"]]
    return ordered


def _contract_label(contract) -> str:
    return f"{contract['symbol']} {contract['expiry']}"


def load_analogue_session(conn, symbol: str, match_date: str, timeframe: str = "1m") -> Dict[str, Any]:
    """
    A matched historical day, ready to chart in the same window as the selected
    day: its regular session with 15 minutes either side (muted).

    Bars come from the best contract holding the day (``analogue_contracts``:
    the closest one expiring on or after it), whatever contract the forecast
    itself was made on. Returns ``{'contract', 'bars', 'levels', 'stats',
    'snapshot'}``: the window's bars at ``timeframe`` with session VWAP, the
    day's own pre-open levels (previous RTH close, overnight high/low), its RTH
    open/high/low/close, and its pre-open feature snapshot ({} when it cannot
    be computed). ``bars`` is None when no contract holds RTH bars for the day.
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
        # the same window as the main chart is shown.
        shown = window_bars(calculate_vwap(_resample(full, timeframe)), match_date, timeframe)
        stats = {
            "open": float(rth["open"].iloc[0]), "high": float(rth["high"].max()),
            "low": float(rth["low"].min()), "close": float(rth["close"].iloc[-1]), "bars": int(len(rth)),
        }
        return {"contract": contract, "bars": shown, "levels": levels, "stats": stats,
                "snapshot": day_snapshot(conn, contract["contract_id"], match_date),
                "opening_range": opening_range(full, match_date),
                "close_0929": float(pre_open["close"].iloc[-1]) if not pre_open.empty else None}
    return {"contract": None, "bars": None, "levels": {}, "stats": None, "snapshot": {}, "opening_range": None,
            "close_0929": None}


def overlay_bars(session: Optional[Dict[str, Any]], match_date: str, day: str,
                 anchor: Optional[float]) -> Optional[pd.DataFrame]:
    """
    A matched session's bars placed on ``day``'s chart: shifted to the same
    minutes from the open, and scaled so its last pre-open close (09:29) sits at
    ``anchor``, the selected day's - its moves in percent, from where the
    selected day stood before the open.
    """
    if not session or session.get("bars") is None or session["bars"].empty:
        return None
    base = session.get("close_0929")
    if not base or not anchor:
        return None
    factor = float(anchor) / float(base)
    shift = session_window(day)["open"] - session_window(match_date)["open"]
    out = session["bars"][["timestamp_ny", "open", "high", "low", "close"]].copy()
    out["timestamp_ny"] = out["timestamp_ny"] + shift
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    return out


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


def _feature_rows(snapshot: Dict[str, Any]) -> List[tuple]:
    """(label, value, hint, accent) of the pre-open figures of one snapshot."""
    prev_close = _safe(snapshot.get("previous_rth_close"))
    gap = _safe(snapshot.get("gap"))
    pct = (gap / prev_close * 100) if prev_close else 0.0
    direction = snapshot.get("pre_open_direction", "FLAT")
    return [
        ("Trade date", str(snapshot.get("trading_day", "—")), "", "#d1d4dc"),
        ("Previous RTH close", f"${prev_close:,.2f}", "", "#d1d4dc"),
        ("Opening gap", f"{gap:+,.2f}", f"{pct:+.2f}% of previous close",
         "#26a69a" if gap > 0 else "#ef5350" if gap < 0 else "#d1d4dc"),
        ("Historical volatility", f"{_safe(snapshot.get('historical_volatility')) * 100:.2f}%", "", "#d1d4dc"),
        ("Overnight range", f"${_safe(snapshot.get('overnight_range')):,.2f}",
         f"H {_safe(snapshot.get('overnight_high')):,.2f}  ·  L {_safe(snapshot.get('overnight_low')):,.2f}",
         "#d1d4dc"),
        ("Pre-open bias", direction, "", {"UP": "#26a69a", "DOWN": "#ef5350"}.get(direction, "#d1d4dc")),
        ("Overnight VWAP", f"{_safe(snapshot.get('vwap')):,.2f}", "", "#d1d4dc"),
    ]


MATCH_COLOUR = "#8fb0ab"     # the overlaid matching day: desaturated, as its candles


def render_features(snapshot: Dict[str, Any], match: Optional[Dict[str, Any]] = None) -> None:
    """
    The pre-open feature figures of one snapshot, in the current container;
    with ``match`` (the overlaid matching day's snapshot) each figure also shows
    the match's value beneath it.
    """
    rows = _feature_rows(snapshot)
    match_rows = _feature_rows(match) if match else None
    for i, (label, value, hint, accent) in enumerate(rows):
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-xs uppercase tracking-wide").style("color:#787b86")
            ui.label(value).classes("text-lg font-medium").style(f"color:{accent}")
            if hint:
                ui.label(hint).classes("text-xs").style("color:#787b86")
            if match_rows:
                m_value, m_hint = match_rows[i][1], match_rows[i][2]
                ui.label(f"match {m_value}" + (f" · {m_hint}" if m_hint else "")).classes("text-xs").style(
                    f"color:{MATCH_COLOUR}")


# A live session's bars are polled this often; a new minute moves the nowcast on.
LIVE_POLL_SECONDS = 15.0

_NOWCAST_COLUMNS = [
    {"name": "horizon", "label": "Horizon", "field": "horizon", "align": "left"},
    {"name": "so_far", "label": "Range so far", "field": "so_far", "align": "right"},
    {"name": "remaining", "label": "Still to come", "field": "remaining", "align": "right"},
    {"name": "band50", "label": "50 % band", "field": "band50", "align": "right"},
    {"name": "band80", "label": "80 % band", "field": "band80", "align": "right"},
    {"name": "usual", "label": "Usual", "field": "usual", "align": "right"},
    {"name": "final", "label": "Final range (50 %)", "field": "final", "align": "right"},
    {"name": "high_low", "label": "Likely high · low", "field": "high_low", "align": "right"},
    {"name": "actual", "label": "Actual", "field": "actual", "align": "left"},
]
_INPUT_ORDER = ("prev_rth_range", "rth_range_5d", "overnight_range", "last_hour_range", "vix_vs_usual",
                "rv_since_open", "rv_last_15m", "range_since_open")


def _pts(v: Optional[float]) -> str:
    """A range in points: whole points from 100 up, else one decimal."""
    if v is None or not math.isfinite(v):
        return "—"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


def nowcast_rows(nc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    A range nowcast (``range_nowcast.nowcast``) as table rows, one per horizon,
    in points: still to come (median, 50 % and 80 % bands, the usual), the
    final range, the likely high and low, and - once the day has held the whole
    horizon - the actual final range against its band.
    """
    rows = []
    for h in nc.get("horizons", []):
        row = {"key": h["key"], "horizon": h["label"], "so_far": "", "remaining": "", "band50": "", "band80": "",
               "usual": "", "final": "", "high_low": "", "actual": ""}
        actual = h.get("actual_final")
        if h.get("status") == "unavailable":
            row["remaining"] = h.get("reason", "unavailable")
        elif h.get("status") == "done":
            row["remaining"] = "over"
            row["actual"] = f"final {_pts(actual)}" if actual is not None else ""
        else:
            r, f = h["remaining"], h["final"]
            row.update(so_far=_pts(h["so_far"]) if nc["t"] else "—", remaining=_pts(r[50]),
                       band50=f"{_pts(r[25])} – {_pts(r[75])}", band80=f"{_pts(r[10])} – {_pts(r[90])}",
                       usual=_pts(h["usual"]), final=f"{_pts(f[50])} ({_pts(f[25])} – {_pts(f[75])})",
                       high_low=f"{h['final_high'][50]:,.2f} · {h['final_low'][50]:,.2f}")
            if actual is not None:
                row["actual"] = f"final {_pts(actual)}, {range_nowcast.band_position(actual, f)}"
        rows.append(row)
    return rows


def describe_inputs(inputs: Dict[str, Optional[float]], t: int) -> str:
    """The main inputs as multiples of their usual ('1.27×'); those since the open once it has begun."""
    parts = []
    for name in _INPUT_ORDER:
        v = inputs.get(name)
        if v is None or (name in range_nowcast.INTRADAY_INPUTS and not t):
            continue
        if name == "vix_vs_usual" and inputs.get("vix_level"):
            parts.append(f"VIX {inputs['vix_level']:.1f} ({v:.2f}×)")
        else:
            parts.append(f"{range_nowcast.INPUT_LABELS[name]} {v:.2f}×")
    return " · ".join(parts)


class SessionExplorer:
    """Holds the view's state and keeps the chart in step with it."""

    def __init__(self, conn) -> None:
        self.conn = conn
        # Context instruments are collected, not forecast, and every control on this
        # page — snapshot, analogues, forecast — assumes a forecast target. They feed in
        # through the snapshot instead. The collector also records whole futures chains
        # to plan rolls, so only contracts that actually hold bars are offered.
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
        self._day_rows: List[Any] = []               # the day's stored 1-minute bars, as read
        self.opening_range: Optional[Dict[str, Any]] = None
        # Each symbol's first hours (the overlay's matches need one; the opening-range
        # break rates come from them) and range-nowcast history, loaded once.
        self._first_hours: Dict[str, Dict[str, Any]] = {}
        self._nowcast_histories: Dict[str, range_nowcast.History] = {}
        # The range nowcast: the selected day as a nowcast session, the minute it is shown
        # as of, whether that minute follows a live session, and the horizon the cone draws.
        self.nowcast_day: Optional[range_nowcast.Session] = None
        self.nowcast: Optional[Dict[str, Any]] = None
        self.as_of = 0
        self.follow_live = True
        self.cone_choice = "auto"
        self._live_signature: Optional[tuple] = None
        self.recent_bars: Optional[pd.DataFrame] = None
        self.vix_bars: Optional[pd.DataFrame] = None
        # {trading_day: last RTH close} for the whole history up to the displayed
        # day: historical volatility spans all of it, recent_bars only a window.
        self.rth_closes: Dict[str, float] = {}
        self.snapshot: Dict[str, Any] = {}
        self.snapshot_is_real = False

        self.chart: Optional[LightweightChart] = None
        self.analogues: List[Dict[str, Any]] = []
        self.close_0929: Optional[float] = None       # the selected day's last pre-open close

        # The matching historical day overlaid on the chart: the closest pre-open
        # matches (from the first-hour outlook), the chosen one and its session.
        self.matches: Dict[str, Dict[str, Any]] = {}
        self.match_choice: Optional[str] = None
        self.match_session: Optional[Dict[str, Any]] = None
        self.matches_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_day(self) -> None:
        """Pulls the selected session and everything derived from it."""
        contract_id = self.contract["contract_id"]

        self._set_day_bars(get_day_bars(self.conn, contract_id, self.date, interval="1m"))
        if self.day_df is None:
            return

        # The snapshot and the analogue search need earlier sessions: each candidate
        # plus the session before it.
        self.recent_bars = self._load_bars(self._history_start())
        self.rth_closes = get_daily_rth_closes(self.conn, contract_id, interval="1m", end_day=self.date)
        self.vix_bars = vix_bars(self.conn, self._history_start(), self.date)
        self._compute_snapshot()

    def _set_day_bars(self, rows: List[Any]) -> None:
        """The day's own bars and what is drawn from them alone: chart bars with VWAP, opening range, 09:29 close."""
        self._day_rows = list(rows)
        self._live_signature = (len(rows), str(rows[-1]["timestamp_utc"])) if rows else None
        self.opening_range = None
        self.close_0929 = None
        if not rows:
            self.day_df = None
            return
        df = enrich_candle_timezones(pd.DataFrame([dict(r) for r in rows]))
        self.opening_range = opening_range(df, self.date)        # from the 1-minute bars
        before_open = df[df["timestamp_ny"] < session_window(self.date)["open"]]
        self.close_0929 = float(before_open["close"].iloc[-1]) if not before_open.empty else None
        self.day_df = calculate_vwap(_resample(df, self.timeframe))

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

    def push(self, reset_view: bool = False) -> None:
        """
        Sends the current state to the chart. ``reset_view`` (a new day) shows
        the default window, 09:15 to 10:45; otherwise the window being looked at
        is kept (a timeframe or match change).
        """
        if self.chart is None:
            return
        if self.day_df is None or self.day_df.empty:
            self.chart.apply({"candles": [], "volume": [], "series": {}, "bands": {}, "legend": [],
                              "overlay_candles": []})
            return
        w = session_window(self.date)
        overlay = overlay_bars(self.match_session, self.match_choice, self.date, self.close_0929)
        spec = build_chart_spec(
            window_bars(self.day_df, self.date, self.timeframe), features=self.snapshot,
            opening_range=self.opening_range, cone=self._cone(), overlay=overlay,
            visible_range=(w["start"], w["open"] + pd.Timedelta(minutes=75)) if reset_view else None,
            keep_view=not reset_view,
        )
        if overlay is not None:
            spec["legend"].append({"key": "overlay", "label": f"Match {self.match_choice} (scaled to the 09:29 close)",
                                   "color": MATCH_COLOUR})
        self.chart.apply(spec)

    # ------------------------------------------------------------------
    # Range nowcast
    # ------------------------------------------------------------------

    def _hours(self, symbol: str) -> Dict[str, Any]:
        """Every stored first hour of ``symbol`` (forecaster/first_hour.load_first_hours), loaded once."""
        if symbol not in self._first_hours:
            self._first_hours[symbol] = first_hour.load_first_hours(self.conn, symbol)
        return self._first_hours[symbol]

    def _compute_matches(self) -> None:
        """The closest pre-open matches with a first hour, offered for the overlay; the closest is chosen."""
        symbol = self.contract["symbol"]
        self.matches_error = None
        try:
            found = rank_preopen(self.conn, symbol, self.date, self.snapshot, self._v1_history)
            matches = first_hour.matches_with_first_hour(found["ranked"], self.date, self._hours(symbol))
        except Exception as e:  # noqa: BLE001 - the page must still load; the match row says why
            matches, self.matches_error = [], str(e)
        self.matches = {m["match_date"]: m for m in matches}
        self.match_choice = matches[0]["match_date"] if matches else None

    def _is_live(self) -> bool:
        """The selected day is today (New York) and its session, with the 15 minutes after it, is not over."""
        now = pd.Timestamp.now(tz=_NY)
        return self.date == now.strftime("%Y-%m-%d") and now < session_window(self.date)["end"]

    def _compute_nowcast(self) -> None:
        """The selected day as a nowcast session (from the shown contract's bars), and its nowcast."""
        symbol = self.contract["symbol"]
        try:
            if symbol not in self._nowcast_histories:
                self._nowcast_histories[symbol] = range_nowcast.load_history(self.conn, symbol)
            self.nowcast_day = range_nowcast.day_session(self.conn, self._day_rows, self._nowcast_histories[symbol])
            self._update_nowcast()
        except Exception as e:  # noqa: BLE001 - the page must still load; the card says why
            self.nowcast_day = None
            self.nowcast = {"available": False, "reason": str(e), "horizons": []}

    def _update_nowcast(self) -> None:
        """The nowcast at ``as_of`` - on a live session followed, its latest minute."""
        day = self.nowcast_day
        if day is None:
            self.nowcast = {"available": False, "reason": "no bars stored for the day yet", "horizons": []}
            return
        if self.follow_live and self._is_live():
            self.as_of = day.held
        self.as_of = max(0, min(int(self.as_of), day.held, day.minutes))
        self.nowcast = range_nowcast.nowcast(self._nowcast_histories[self.contract["symbol"]], day, self.as_of)

    def _clock(self, t: int) -> str:
        """Minute ``t`` of the selected day's session as its New York time, 'HH:MM'."""
        return (session_window(self.date)["open"] + pd.Timedelta(minutes=int(t))).strftime("%H:%M")

    def _cone(self) -> Optional[Dict[str, Any]]:
        """The cone to draw: the chosen horizon (by default the first hour, then the session) while it is open."""
        nc = self.nowcast or {}
        if not nc.get("available"):
            return None
        rows = {h["key"]: h for h in nc["horizons"]}
        key = self.cone_choice if self.cone_choice != "auto" else ("first_hour" if nc["t"] < 60 else "session")
        row = rows.get(key)
        if row is None or row.get("status") != "open":
            return None
        return {"open": session_window(self.date)["open"], "t": nc["t"], "end": row["end"], "price": nc["price"],
                "bar_minutes": _BAR_MINUTES.get(self.timeframe, 1), "prices": row["cone"],
                "label": f"Range nowcast · {row['label'].split(' · ')[0].lower()} from {self._clock(nc['t'])} "
                         f"(median, 25–75 %, 10–90 %)"}

    def _render_nowcast(self) -> None:
        self._sync_as_of()
        self.nowcast_panel.clear()
        with self.nowcast_panel:
            nc = self.nowcast or {}
            if not nc.get("available"):
                ui.label(f"Not available: {nc.get('reason', 'no data')}").classes("text-sm").style("color:#ffa726")
                return
            ui.table(columns=_NOWCAST_COLUMNS, rows=nowcast_rows(nc), row_key="key").classes("w-full").props(
                "dense flat").style("background:#1c212e")
            inputs = describe_inputs(nc.get("inputs") or {}, nc["t"])
            if inputs:
                ui.label(f"Against the usual: {inputs}").classes("text-xs mt-1").style(_MUTED)
            brk = first_hour.break_rates(self._hours(self.contract["symbol"]), self.date)
            if brk["n"]:
                ui.label(f"The opening range breaks by 10:30 on {brk['any'] * 100:.0f} % of sessions (first above "
                         f"{brk['above'] * 100:.0f} %, below {brk['below'] * 100:.0f} %) - the usual rates over "
                         f"{brk['n']} sessions; which side has not been predictable.").classes("text-xs").style(_MUTED)
            n = max((h.get("scenarios") or 0) for h in nc["horizons"]) if nc["horizons"] else 0
            ui.label(f"Median and bands from {n} earlier sessions' own moves after the same minute, scaled by "
                     f"today's forecast over theirs. Run nq_forecast_v2.py range-backtest, or see Backtests, for "
                     f"how it has scored.").classes("text-xs").style(_MUTED)

    def _build_nowcast_controls(self) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            ui.label("As of").classes("text-sm").style(_MUTED)
            self.as_of_slider = ui.slider(min=0, max=range_nowcast.SESSION_MINUTES, step=1, value=0).classes("w-96")
            self.as_of_slider.on("change", lambda e: self.on_as_of(e.args))
            self.as_of_label = ui.label().classes("text-sm font-mono w-24")
            self.as_of_label.bind_text_from(self.as_of_slider, "value",
                                            backward=lambda v: f"{self._clock(v or 0)} ET" if self.date else "")
            self.live_switch = ui.switch("Follow live", value=True, on_change=self.on_follow_live)
            ui.toggle({"auto": "Cone: auto", "first_hour": "First hour", "session": "Session"}, value="auto",
                      on_change=self.on_cone_choice).props("dense no-caps")

    def _sync_as_of(self) -> None:
        """The slider and live switch in step with the nowcast, without firing their handlers."""
        day = self.nowcast_day
        self.as_of_slider.props(f"max={day.minutes if day is not None else range_nowcast.SESSION_MINUTES}")
        self.as_of_slider.value = self.as_of
        live = self._is_live()
        self.live_switch.set_visibility(live)
        self.live_switch.value = self.follow_live and live

    def on_as_of(self, value) -> None:
        if self.nowcast_day is None or value is None:
            return
        self.as_of = int(value)
        self.follow_live = self._is_live() and self.as_of >= self.nowcast_day.held
        self._update_nowcast()
        self._render_nowcast()
        self.push()

    def on_follow_live(self, event) -> None:
        if bool(event.value) == self.follow_live:
            return
        self.follow_live = bool(event.value)
        if self.follow_live and self.nowcast_day is not None:
            self._update_nowcast()
            self._render_nowcast()
            self.push()

    def on_cone_choice(self, event) -> None:
        self.cone_choice = event.value
        self.push()

    def _live_tick(self) -> None:
        """On a live session: new bars redraw the chart and, while following, move the nowcast on."""
        if self.contract is None or self.date is None or not self._is_live():
            return
        rows = get_day_bars(self.conn, self.contract["contract_id"], self.date, interval="1m")
        if ((len(rows), str(rows[-1]["timestamp_utc"])) if rows else None) == self._live_signature:
            return
        self._set_day_bars(rows)
        history = self._nowcast_histories.get(self.contract["symbol"])
        if history is not None:
            self.nowcast_day = range_nowcast.day_session(self.conn, rows, history)
            self._update_nowcast()
        self._render_status()
        self._render_nowcast()
        self.push()

    def refresh_session(self, keep_forecast: bool = False) -> None:
        """
        Reloads the day from the database and redraws everything. A new day
        shows the forecast stored for it, if any, and its nowcast as of the open
        (a live session: its latest minute); ``keep_forecast`` (a display change
        only) keeps both on screen and redraws its matching day.
        """
        if self.contract is None or self.date is None:
            return
        self.load_day()
        if not keep_forecast:
            self.matches, self.match_choice, self.matches_error = {}, None, None
            if self.day_df is not None:
                self._compute_matches()
            self.as_of, self.follow_live = 0, True
            self._compute_nowcast()
        self._load_match()
        self._render_status()
        self._render_features()
        self._render_nowcast()
        self._render_match_row()
        self.push(reset_view=not keep_forecast)
        if not keep_forecast:
            self.model_panel.show(self.contract["symbol"], self.date)
            self._show_stored_forecast()

    # ------------------------------------------------------------------
    # The matching historical day, overlaid on the chart
    # ------------------------------------------------------------------

    def _load_match(self) -> None:
        self.match_session = (load_analogue_session(self.conn, self.contract["symbol"], self.match_choice,
                                                    self.timeframe) if self.match_choice else None)

    def _render_match_row(self) -> None:
        self.match_row.clear()
        with self.match_row:
            if not self.matches:
                reason = f" ({self.matches_error})" if self.matches_error else ""
                ui.label(f"No pre-open matches to overlay for this day{reason}.").classes("text-xs").style(_MUTED)
                return
            ui.select(
                {d: f"#{m['ranking']} · {d} · {m['similarity_score'] * 100:.1f}% match" for d, m in self.matches.items()},
                value=self.match_choice, label="Similar pre-open day (overlaid)", on_change=self.on_match,
            ).classes("w-80")
            session = self.match_session or {}
            contract = session.get("contract")
            if session.get("bars") is None or not self.close_0929 or not session.get("close_0929"):
                ui.label("The match's bars or a 09:29 close are missing; nothing to overlay.").classes(
                    "text-xs").style("color:#ffa726")
                return
            h = self._hours(self.contract["symbol"]).get(self.match_choice)
            detail = [f"{contract['symbol']} {contract['expiry']}" if contract else "",
                      f"first hour {h.path[-1]:+.2f} %, range {h.width:.2f} %" if h is not None else "",
                      f"scaled ×{self.close_0929 / session['close_0929']:.3f} to the selected day's 09:29 close",
                      "similar volatility before the open - its path is no forecast of the day's"]
            ui.label(" · ".join(x for x in detail if x)).classes("text-xs").style(f"color:{MATCH_COLOUR}")

    def on_match(self, event) -> None:
        if not event.value or event.value == self.match_choice:
            return
        self.match_choice = event.value
        self._load_match()
        self._render_features()
        self._render_match_row()
        self.push()

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
        self.refresh_session(keep_forecast=True)

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
            render_features(self.snapshot, (self.match_session or {}).get("snapshot") or None)

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
            self._build_nowcast_controls()

            self.match_row = ui.row().classes("w-full items-center gap-4")
            with ui.row().classes("w-full no-wrap gap-4 items-start"):
                with ui.column().classes("grow gap-1 min-w-0"):
                    self.chart = LightweightChart(height=620)
                with ui.card().classes("w-72 shrink-0").style("background:#1c212e"):
                    ui.label("Pre-open features").classes("text-sm font-medium")
                    self.features_panel = ui.column().classes("gap-3 w-full")

            with ui.card().classes("w-full").style("background:#1c212e"):
                with ui.column().classes("gap-0"):
                    ui.label("Range nowcast").classes("text-lg font-medium")
                    ui.label("How far the price still travels before 09:45, 10:30 and 16:00, as of the minute "
                             "chosen above - no direction, which has not been predictable. Re-estimated each minute "
                             "from the pre-open ranges and the volatility since the open, each against its usual."
                             ).classes("text-sm").style(_MUTED)
                self.nowcast_panel = ui.column().classes("w-full gap-1")

            # The trained model and the scenario generator mostly call directions, which
            # nothing has predicted walk-forward: folded away, kept for reference.
            with ui.expansion("Direction forecasts · trained model and scenario generator",
                              caption="Mostly direction calls, which have not beaten the base rates walk-forward "
                                      "(see Backtests) - kept for reference", icon="visibility_off",
                              value=False).classes("w-full").style("background:#1c212e"):
                self.model_panel = ModelForecastPanel(self.conn)
                self.model_panel.build()
                self._build_forecast_panel()

        # A live session's new bars are picked up every LIVE_POLL_SECONDS (a no-op on other days).
        ui.timer(LIVE_POLL_SECONDS, self._live_tick)
        if self.contract is None:
            self.model_panel.show(self.symbol, None)
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

    def _v1_history(self) -> List[Dict[str, Any]]:
        """The earlier sessions' v1 snapshots on this contract - the matching pool when
        an instrument has no point-in-time snapshots (strictly earlier: no look-ahead)."""
        out = []
        for day in self._analogue_candidates():
            hist = calculate_pre_open_snapshot(self.recent_bars, day, self.rth_closes, vix_df=self.vix_bars)
            if "error" not in hist:
                out.append(hist)
        return out

    def _run_forecast(self):
        """Analogue search plus the forecast. Runs off the event loop."""
        forecast, analogues, client = analogue_forecast(
            self.conn, self.contract["symbol"], self.date, self.snapshot, self._v1_history,
            instrument=Config.describe_instrument(self.contract["symbol"]),
        )

        if self.persist.value and self.snapshot_is_real:
            self._persist(client, forecast, analogues)
        return forecast, analogues

    def _persist(self, client, forecast, analogues) -> None:
        prediction_id = store_forecast(self.conn, self.contract["contract_id"], self.date, self.snapshot,
                                       forecast, analogues, client.model_name)
        self._saved_as = prediction_id

    def _show_stored_forecast(self) -> None:
        """Shows the newest forecast stored for the selected day, or says there is none."""
        self.forecast_panel.clear()
        row = get_day_prediction(self.conn, self.contract["symbol"], self.date, self.contract["contract_id"])
        if row is None:
            with self.forecast_panel:
                ui.label(
                    f"No forecast stored for {self.date} yet. Generate one, or run "
                    f"nq_forecast_v2.py scenario-backfill for past sessions."
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
                    first_hour = forecast.get("forecast_horizon") == FIRST_HOUR_HORIZON
                    ui.label("First hour of the matched sessions" if first_hour else "Path probabilities"
                             ).classes("text-xs uppercase").style("color:#787b86")
                    band = probabilities.get("flat_band_points")
                    rows = ((("Up", "bullish_continuation_pct", "#26a69a"),
                             (f"Flat (±{band:,.2f} pts)" if band else "Flat", "mean_reversion_gap_fill_pct",
                              "#ab47bc"),
                             ("Down", "bearish_rejection_pct", "#ef5350")) if first_hour else
                            (("Bullish continuation", "bullish_continuation_pct", "#26a69a"),
                             ("Mean reversion / gap fill", "mean_reversion_gap_fill_pct", "#ab47bc"),
                             ("Bearish rejection", "bearish_rejection_pct", "#ef5350")))
                    for label, key, color in rows:
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
                            outcome = a.get("outcome")
                            if outcome:
                                ui.label(f"{outcome['label']} {outcome['move']:+,.2f}").classes(
                                    "text-xs font-mono").style(
                                    f"color:{ {'up': '#26a69a', 'down': '#ef5350'}.get(outcome['label'], '#b2b5be') }")
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



def show_candles_page(conn) -> None:
    SessionExplorer(conn).build()
