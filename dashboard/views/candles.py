# dashboard/views/candles.py
"""
Candlestick exploration view of the stored sessions.

The session bar at the top of the page (dashboard/components/session_bar.py)
picks the session: the day, an instrument with bars that day (ES, NQ, ...) and
one of its contracts holding the day. The chart shows the regular session with
15 minutes either side - 09:15 to 16:15 ET, or to 13:15 on an early close - and
draws those extra minutes grey on a grey background, with the session VWAP, the
opening range, the pre-open reference levels and the TradingView indicator's
three moving averages (features.calculations.calculate_moving_averages).

Beside it, one of the day's structural analogues (dashboard/views/analogues.py;
NQ, the journal symbol, on the days the journal holds a pre-open snapshot of),
the most similar first: that session on its own contract, drawn the same way at
the same timeframe. The two charts are linked by time of day - scrolling or
zooming either moves the other. Below them, the comparison of the session with
its analogues; a date there picks the analogue shown. At the bottom, the day's
NQ forecast (dashboard/views/forecast.py): its stored runs, and the preview
made by Forecast now.

The session in progress (dashboard/components/fan.current_session) is drawn
whole, from its 18:00 ET Globex open, with the benchmark price fan to the right
of its newest candle (dashboard/components/fan.py): the distribution of the
price at each candle ahead, fading with its density and with its measured skill.
Playback steps back through the session's candles - the later ones hidden, or
drawn grey - with the fan as it stood at each. Without its official snapshot
yet, the day's analogues come from a preview as of its last stored bar
(forecaster/preview.preview_session), made off the event loop and labelled as
never stored. Auto (dashboard/jobs.AUTO) collects the session and forecasts once
a minute; each run's end redraws all of it in place, the view moving on with the
newest candle.

Unlike the page-rerun model this replaced, every control mutates view state and
pushes a new spec at the existing charts. The charts are created once per page
load, so a redraw leaves the user's zoom, scroll and crosshair exactly where
they were. That holds after the collector or the forecaster has run from the
header too (the bar's ``reload``): the session, its analogues and its forecast
are read again in place.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from nicegui import background_tasks, run, ui

from contracts import nq_prompt_v2 as defs
from dashboard.components import fan
from dashboard.components.lightweight_chart import LightweightChart
from dashboard.components.session_bar import SessionBar, contract_label
from dashboard.components.spec import build_chart_spec, resample, to_epoch
from dashboard.views.analogues import AnaloguesPanel
from dashboard.jobs import AUTO
from dashboard.views.forecast import ForecastPanel
from database import journal_store as store
from database.queries import get_bars, get_contract, get_day_bars, get_session_day
from features import calendar as cal
from features.calculations import (
    MA_WARMUP_BARS,
    calculate_moving_averages,
    calculate_vwap,
    enrich_candle_timezones,
    pre_open_levels,
)
from forecaster import preview as pv
from forecaster.fan_benchmark import end_slot, slot_at, slot_instant

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


def day_window(day: str) -> Dict[str, pd.Timestamp]:
    """
    ``session_window`` for the whole trading day - the current session's: from
    its Globex open (``start``, 18:00 ET the evening before) to the futures'
    day end (``end``, 17:00 ET; the early close's on a short day).
    """
    w = session_window(day)
    try:
        s = cal.session(day)
        if s.is_open:
            end = slot_instant(s.session_date, end_slot("FUT", s.schedule))
            return {**w, "start": pd.Timestamp(s.overnight_start_at).tz_convert(_NY),
                    "end": pd.Timestamp(end).tz_convert(_NY)}
    except cal.CalendarCoverageError:
        pass
    return {**w, "start": w["start"] - pd.Timedelta(hours=15, minutes=15), "end": w["close"] + pd.Timedelta(hours=1)}


def window_bars(df: Optional[pd.DataFrame], day: str, timeframe: str = "1m",
                full_day: bool = False) -> Optional[pd.DataFrame]:
    """
    The bars of ``df`` (at ``timeframe``, with ``timestamp_ny`` bar starts) that
    overlap ``day``'s shown window, with ``muted`` true for those starting
    outside the regular session - the 15 minutes before the open and after the
    close. ``full_day``: the whole trading day (``day_window``), nothing muted.
    Indicators such as VWAP are computed on the whole day beforehand.
    """
    if df is None or df.empty:
        return df
    w = day_window(day) if full_day else session_window(day)
    width = pd.Timedelta(minutes=_BAR_MINUTES.get(timeframe, 1))
    ts = df["timestamp_ny"]
    out = df[(ts + width > w["start"]) & (ts < w["end"])].copy()
    out["muted"] = False if full_day else (out["timestamp_ny"] < w["open"]) | (out["timestamp_ny"] >= w["close"])
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
        self.history: Optional[pd.DataFrame] = None     # the 1-minute bars behind the levels and moving averages

        # The current session (set by the explorer): the whole trading day, playback and the fan.
        self.full_day = False
        self.until: Optional[pd.Timestamp] = None       # playback: the start (New York) of the last candle shown
        self.reveal = False                             # ... the later candles drawn muted instead of hidden
        self.decorate: Optional[Callable[[Dict[str, Any]], None]] = None   # adds to each spec before it is sent
        self._edge: Optional[tuple] = None              # (date, timeframe, chart time) of the newest candle drawn

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
        self.day_df, self.opening_range, self.levels, self.history = None, None, None, None
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
        self.history = enrich_candle_timezones(history)
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

    def shown_bars(self) -> Optional[pd.DataFrame]:
        """The bars drawn: the window's, in playback only those up to ``until`` - or all, the later ones muted."""
        rows = window_bars(self.day_df, self.date, self.timeframe, full_day=self.full_day)
        if rows is None or self.until is None:
            return rows
        later = rows["timestamp_ny"] > self.until
        if self.reveal:
            rows = rows.copy()
            rows.loc[later, "muted"] = True
            return rows
        return rows[~later]

    def _as_of_until(self):
        """The levels and opening range as the last candle shown knew them (playback hides what came after)."""
        if self.until is None or self.reveal:
            return self.levels, self.opening_range
        end = self.until + pd.Timedelta(minutes=_BAR_MINUTES[self.timeframe])
        known = self.history[self.history["timestamp_ny"] < end] if self.history is not None else None
        levels = pre_open_levels(known, self.date) if known is not None and not known.empty else None
        opening = self.opening_range if self.opening_range and self.opening_range["end"] <= end else None
        return levels, opening

    def _default_range(self, rows: pd.DataFrame):
        """A new day's window: 09:15 to 10:45 - or, for the current session, the last 90 candles and 60 ahead."""
        if self.full_day:
            tf = pd.Timedelta(minutes=_BAR_MINUTES[self.timeframe])
            last = self.until if self.until is not None else rows["timestamp_ny"].max()
            return last - 90 * tf, last + 60 * tf
        w = session_window(self.date)
        return w["start"], w["open"] + pd.Timedelta(minutes=75)

    def push(self, reset_view: bool = False, follow: bool = False) -> None:
        """
        Sends the current state to the chart. ``reset_view`` (a new day) shows
        the default window (``_default_range``); otherwise the window being looked
        at is kept (a timeframe change) - moved on with the newest candle while a
        current session is followed. ``follow``: the lead chart's time of day
        instead, when the lead shows a session.
        """
        if self.chart is None:
            return
        rows = self.shown_bars() if self.has_bars else None
        if rows is None or rows.empty:
            self.chart.apply(dict(_EMPTY_SPEC, fan=None))
            return
        levels, opening = self._as_of_until()
        spec = build_chart_spec(
            rows, levels=levels, opening_range=opening,
            visible_range=self._default_range(rows) if reset_view else None,
            keep_view=not reset_view,
        )
        spec["anchor"] = int(to_epoch([pd.Timestamp(self.date)])[0])    # the day's 00:00 on the chart's clock
        spec["follow"] = follow
        if self.until is None:
            edge = (self.date, self.timeframe, int(to_epoch([rows["timestamp_ny"].max()])[0]))
            if self.full_day and not reset_view and self._edge is not None and self._edge[:2] == edge[:2]:
                spec["shift_by"] = edge[2] - self._edge[2]
            self._edge = edge
        if self.decorate is not None:
            self.decorate(spec)
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
    return f"{pd.Timestamp(day):%a} {day}" + (f" · {contract_label(contract)}" if contract is not None else "")


class SessionExplorer:
    """Follows the session bar's selection and keeps the two charts, the analogues and the forecast in step with it."""

    def __init__(self, conn, bar: SessionBar, panel=None) -> None:
        self.conn = conn
        self.bar = bar
        self.timeframe = "1m"
        # Set while selectors are updated from code, so their change events do not redraw twice.
        self._syncing = False

        self.main = SessionPane(conn)             # the selected session
        self.mirror = SessionPane(conn)           # one of its analogues, beside it
        self.main.decorate = self._decorate_main
        self.analogues = AnaloguesPanel(conn, on_pick=self.pick_analogue)
        self.members: Dict[str, Dict[str, Any]] = {}   # the day's analogues by snapshot id, best first
        self.analogue: Optional[str] = None             # the snapshot id of the one beside the session
        self.forecast = ForecastPanel(conn, panel)

        # The current session: the fan, playback and the analogue preview.
        self.live = False                                # the session shown is the one in progress
        self.fan_on = True
        self.fan_contexts: Dict[tuple, fan.FanContext] = {}     # (symbol, day) -> model and accuracy
        self._fan_loading: set = set()
        self.fan_marks: Dict[tuple, list] = {}           # (symbol, day, origin, newest slot) -> the learned fan's brackets
        self._marks_loading: set = set()
        self.fan_day = None                              # the session's minute grid (forecaster/fan_benchmark.Day)
        self.candle_starts: List[pd.Timestamp] = []      # the candles playback steps through, oldest first
        self.previews: Dict[str, tuple] = {}             # day -> (as_of, the analogue preview made as of then)
        self._preview_wanted: Optional[tuple] = None     # (day, as_of) being made

    # ------------------------------------------------------------------
    # The two charts
    # ------------------------------------------------------------------

    def refresh_session(self, keep_view: bool = False) -> None:
        """
        Reloads the selected session and redraws it. A new day opens on the
        default window; ``keep_view`` (a display change only) keeps the window
        being looked at. The session in progress is drawn whole, from its Globex
        open, with the fan and playback.
        """
        bar = self.bar
        live = bar.date is not None and bar.date == fan.current_session()
        if not live or not keep_view or not self.live:
            self.main.until = None                       # playback starts at the newest candle
        self.live = live
        self.main.full_day = self.mirror.full_day = live
        try:
            self.fan_day = fan.load_day(self.conn, bar.symbol, bar.date) if live and bar.symbol else None
        except ValueError:                                # an instrument the fan does not forecast
            self.fan_day = None
        if live:
            self._ensure_fan_context()
        self.main.show(bar.contract, bar.date, self.timeframe, keep_view=keep_view)
        self.main_caption.text = _caption(bar.date, bar.contract) if bar.date else ""
        self.status.clear()
        with self.status:
            _status_badge(self.conn, self.main)
        self._sync_playback()

    def _show_analogues(self, keep: Optional[str] = None) -> None:
        """
        The day's analogues: the comparison below the charts, and beside the
        session the most similar - or ``keep``, the one shown, while it is still
        among them. A day in progress without its snapshot shows its preview.
        """
        day, symbol = self.bar.date, self.bar.symbol
        preview, computing = self._preview(day, symbol)
        members = self.analogues.show(day, symbol, preview=preview, computing=computing)
        title = f"Analogues of {day}" if symbol == defs.SYMBOL else "Analogues"
        if self.analogues.preview is not None:
            title += f" · preview as of {self.analogues.preview['as_of_et']}"
        self.analogue_box.text = title
        self.members = {m["snapshot_id"]: m for m in members}
        options = {sid: f"#{m['rank']}  {m['session_date']}  ·  {float(m['similarity']):.1f}%"
                   for sid, m in self.members.items()}
        first = keep if keep in options else next(iter(options), None)
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
        """Both charts show their whole window (09:15 to 16:15; the current session from its Globex open)."""
        for pane in (self.main, self.mirror):
            if pane.chart is not None:
                pane.chart.fit()

    # ------------------------------------------------------------------
    # The fan and playback (the current session only)
    # ------------------------------------------------------------------

    def _ensure_fan_context(self) -> None:
        """The model and accuracy of the session's fan, loaded off the event loop once per instrument and day."""
        key = (self.bar.symbol, self.bar.date)
        if key in self.fan_contexts or key in self._fan_loading:
            return
        self._fan_loading.add(key)
        background_tasks.create(self._load_fan_context(key))

    async def _load_fan_context(self, key: tuple) -> None:
        try:
            ctx = await run.io_bound(fan.load_context, self.conn, *key)
        except Exception as e:                            # drawn as "No fan: ..."
            ctx = fan.FanContext(key[0], date.fromisoformat(key[1]), error=f"{type(e).__name__}: {e}")
        self.fan_contexts[key] = ctx
        self._fan_loading.discard(key)
        if self.live and (self.bar.symbol, self.bar.date) == key:
            self.main.push(reset_view=self.main.until is None)    # the newest candle with the fan ahead of it

    def _ensure_marks(self, ctx: "fan.FanContext", origin: int) -> Optional[dict]:
        """The learned fan's brackets from ``origin`` - recorded or computed (fan.load_marks): cached, or loaded off
        the event loop (None until then)."""
        if ctx.learned is None:
            return None
        key = (self.bar.symbol, self.bar.date, origin, fan.latest_slot(self.fan_day))
        if key in self.fan_marks:
            return self.fan_marks[key]
        if key not in self._marks_loading:
            self._marks_loading.add(key)
            background_tasks.create(self._load_marks(key, ctx, self.fan_day, origin))
        return None

    async def _load_marks(self, key: tuple, ctx: "fan.FanContext", day, origin: int) -> None:
        try:
            marks = await run.io_bound(fan.load_marks, self.conn, ctx, day, origin)
        except Exception as e:                            # drawn without them; the caption says why
            marks = {"source": "error", "error": f"{type(e).__name__}: {e}", "marks": []}
        self.fan_marks[key] = marks
        self._marks_loading.discard(key)
        if self.live and (self.bar.symbol, self.bar.date) == key[:2]:
            self.main.push(reset_view=False)

    def _fan_origin(self) -> Optional[int]:
        """The fan's origin slot: the last minute of the last candle shown - the newest bar unless in playback."""
        newest = fan.latest_slot(self.fan_day)
        if newest is None or self.main.until is None:
            return newest
        tf = pd.Timedelta(minutes=_BAR_MINUTES[self.timeframe])
        last_minute = (self.main.until + tf - pd.Timedelta(minutes=1)).tz_convert("UTC").to_pydatetime()
        return min(newest, slot_at(date.fromisoformat(self.bar.date), last_minute))

    def _decorate_main(self, spec: Dict[str, Any]) -> None:
        """The fan into the session's spec (the current session, its active contract), and the line describing it."""
        ctx, payload = None, None
        if self.live and self.fan_day is not None:
            ctx = self.fan_contexts.get((self.bar.symbol, self.bar.date))
            active = self.bar.contract is not None and int(self.bar.contract["contract_id"]) == self.fan_day.contract_id
            if ctx is not None and active and self.fan_on:
                origin = self._fan_origin()
                marks = self._ensure_marks(ctx, origin) if origin is not None else None
                payload = (fan.fan_payload(ctx, self.fan_day, origin, self.timeframe, marks)
                           if origin is not None else None)
            if not active:
                self.fan_note.text = "The fan follows the active contract: pick it in Contract to see the fan."
            elif not self.fan_on:
                self.fan_note.text = "Fan hidden."
            else:
                playback = self.main.until is not None
                now = None if playback else datetime.now(timezone.utc)     # newest: say how old
                self.fan_note.text = fan.describe(ctx, payload, now, playback=playback)
        fan.attach(spec, payload)

    def _sync_playback(self) -> None:
        """The playback row: shown for the current session, its slider over the candles drawn so far."""
        self.playback.set_visibility(self.live)
        if not self.live:
            return
        rows = window_bars(self.main.day_df, self.bar.date, self.timeframe, full_day=True)
        self.candle_starts = list(rows["timestamp_ny"]) if rows is not None and not rows.empty else []
        n = len(self.candle_starts)
        until = self.main.until
        index = n - 1 if until is None else max(0, sum(1 for t in self.candle_starts if t <= until) - 1)
        self._syncing = True
        try:
            self.slider._props["max"] = max(1, n - 1)
            self.slider.update()
            self.slider.value = index
        finally:
            self._syncing = False
        self.slider.set_enabled(n > 1)
        self._playback_label(index)

    def _playback_label(self, index: int) -> None:
        n = len(self.candle_starts)
        if not n:
            self.as_of_label.text = "no candle yet"
            return
        tf = pd.Timedelta(minutes=_BAR_MINUTES[self.timeframe])
        end = min(self.candle_starts[index] + tf, self.candle_starts[-1] + tf)
        latest = index >= n - 1
        self.as_of_label.text = f"{'live · ' if latest else ''}as of {end:%H:%M} ET"
        self.live_button.set_enabled(not latest)

    def on_playback(self, event) -> None:
        if self._syncing or event.value is None or not self.candle_starts:
            return
        self._play_to(int(event.value))

    def _play_to(self, index: int) -> None:
        """Shows the session up to its ``index``-th candle - the newest is live - with the fan from there."""
        n = len(self.candle_starts)
        index = max(0, min(n - 1, index))
        self.main.until = None if index >= n - 1 else self.candle_starts[index]
        self._playback_label(index)
        self.main.push()

    def step(self, candles: int) -> None:
        if self.candle_starts:
            self.slider.value = max(0, min(len(self.candle_starts) - 1, int(self.slider.value or 0) + candles))

    def go_live(self) -> None:
        """Back to the newest candle, shown in the default window."""
        self.main.until = None
        self._sync_playback()
        self.main.push(reset_view=True)

    def on_reveal(self, event) -> None:
        self.main.reveal = bool(event.value)
        self.main.push()

    def on_fan(self, event) -> None:
        self.fan_on = bool(event.value)
        self.main.push()

    # ------------------------------------------------------------------
    # The analogue preview of a day in progress
    # ------------------------------------------------------------------

    def _preview(self, day: Optional[str], symbol: Optional[str]) -> tuple:
        """
        ``(preview, computing)`` for the analogues: the day's latest preview when
        it is in progress without its snapshot - a newer one is made, off the
        event loop, when newer bars are stored - else ``(None, False)``.
        """
        if not day or symbol != defs.SYMBOL or not self.analogues.preview_wanted(day):
            return None, False
        as_of = pv.latest_as_of(self.conn, day)
        if as_of is None:
            return None, False
        made_as_of, preview = self.previews.get(day, (None, None))
        if made_as_of != as_of and self._preview_wanted != (day, as_of):
            self._preview_wanted = (day, as_of)
            background_tasks.create(self._make_preview(day, as_of))
        return preview, made_as_of != as_of

    async def _make_preview(self, day: str, as_of) -> None:
        try:
            preview = await run.io_bound(pv.preview_session, self.conn, day, as_of)
        except Exception as e:                            # shown as why there is no preview
            preview = {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}
        self.previews[day] = (as_of, preview)
        if self._preview_wanted == (day, as_of):
            self._preview_wanted = None
        if self.bar.date == day and self.bar.symbol == defs.SYMBOL:
            self._show_analogues(keep=self.analogue)

    # ------------------------------------------------------------------
    # Auto mode (dashboard/jobs.py AUTO)
    # ------------------------------------------------------------------

    def toggle_auto(self) -> None:
        """Auto mode on - collecting and forecasting once a minute, the session in progress shown - or off."""
        AUTO.switch(not AUTO.on)
        current = fan.current_session()
        if AUTO.on and current is not None and self.bar.date != current:
            self.bar.set_day(current)
        self._auto_state()

    def _auto_state(self) -> None:
        """The Auto button as auto mode is - it belongs to the dashboard process, so another page may switch it."""
        on = AUTO.on
        if on != self._auto_shown:
            self._auto_shown = on
            self.auto_button.props(remove="outline color=grey" if on else "color=positive",
                                   add="color=positive" if on else "outline color=grey")
            self.auto_button.set_text("Auto: on" if on else "Auto")
        status = AUTO.status(datetime.now(timezone.utc))
        if status != self.auto_tip.text:
            self.auto_tip.set_text(status)

    # ------------------------------------------------------------------
    # Control handlers
    # ------------------------------------------------------------------

    def on_selection(self, what: str) -> None:
        """
        The bar's selection changed (see dashboard/components/session_bar.py):
        a new day shows its session, analogues and forecast afresh; a new
        instrument its session and analogues; a new contract its session. After
        a job (``"data"``) the same day, analogue, run and window stay, redrawn
        from the database - a session still in progress grows, and its fan and
        analogue preview move on with it.
        """
        if what == "contract":
            self.refresh_session()
        elif what == "symbol":
            self.refresh_session()
            self._show_analogues()
        elif what == "day":
            self.refresh_session()
            self._show_analogues()
            self.forecast.show_day(self.bar.date)
        else:
            self.refresh_session(keep_view=True)
            self._show_analogues(keep=self.analogue)
            self.forecast.show_day(self.bar.date, keep=True)

    def on_timeframe(self, event) -> None:
        self.timeframe = event.value
        if self.main.until is not None:                  # playback stays at the candle holding its last minute
            self.main.until = self.main.until.floor(f"{_BAR_MINUTES[self.timeframe]}min")
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

    def build(self, run_id: Optional[str] = None, view: Optional[str] = None) -> None:
        """The page below the bar; ``run_id`` and ``view`` open the forecast on a run or on Forecast now."""
        if self.bar.date is None:                          # the bar says why there is nothing to show
            return
        with self.bar.tools:
            ui.toggle(
                ["1m", "5m", "15m", "30m"], value="1m", on_change=self.on_timeframe,
            ).props("dense")
            ui.button("Fit", icon="fit_screen", on_click=self.fit).props(
                "flat dense no-caps").tooltip("Show the whole window of both sessions")
            self.auto_button = ui.button("Auto", icon="autorenew", on_click=self.toggle_auto).props(
                "dense no-caps outline color=grey")
            with self.auto_button:
                self.auto_tip = ui.tooltip("")
            self._auto_shown: Optional[bool] = None
            self._auto_state()
            ui.timer(1.0, self._auto_state)

        with ui.column().classes("w-full px-4 pb-4 gap-3"):
            self._build_playback()
            self._build_charts()
            self.analogue_box = ui.expansion("Analogues", icon="compare", value=True).classes("w-full")
            with self.analogue_box:
                self.analogues.build()
            self.forecast.build(view)

        if self.bar.contract is None:
            ui.notify("No contract holds bars for the selected day.", type="warning")
        self.refresh_session()
        self._show_analogues()
        self.forecast.show_day(self.bar.date, run_id=run_id)
        self.bar.on_change.append(self.on_selection)
        if run_id or view:
            self.forecast.scroll_into_view()

    def _build_playback(self) -> None:
        """The current session's playback: back through its candles with the fan as it stood at each."""
        with ui.column().classes("w-full gap-0") as self.playback:
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label("Playback").classes("text-sm").style(_MUTED)
                ui.button(icon="chevron_left", on_click=lambda: self.step(-1)).props(
                    'flat dense round aria-label="Previous candle"').tooltip("Previous candle")
                self.slider = ui.slider(min=0, max=1, step=1, value=1, on_change=self.on_playback).classes(
                    "grow").props("dense")
                ui.button(icon="chevron_right", on_click=lambda: self.step(1)).props(
                    'flat dense round aria-label="Next candle"').tooltip("Next candle")
                self.as_of_label = ui.label().classes("text-sm font-mono whitespace-nowrap shrink-0")
                self.live_button = ui.button("Live", icon="skip_next", on_click=self.go_live).props(
                    "flat dense no-caps").classes("shrink-0").tooltip("Back to the newest candle")
                ui.switch("Show what followed", value=False, on_change=self.on_reveal).props("dense").classes(
                    "whitespace-nowrap shrink-0").tooltip("In playback, draw the later candles grey instead of "
                                                          "hiding them")
                ui.switch("Fan", value=True, on_change=self.on_fan).props("dense").classes("shrink-0")
            self.fan_note = ui.label().classes("text-xs").style(_MUTED)
        self.playback.set_visibility(False)

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


def show_candles_page(conn, bar: SessionBar, panel=None, run_id: Optional[str] = None,
                      view: Optional[str] = None) -> SessionExplorer:
    explorer = SessionExplorer(conn, bar, panel)
    explorer.build(run_id, view)
    return explorer
