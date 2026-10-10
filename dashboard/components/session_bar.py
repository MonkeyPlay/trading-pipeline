# dashboard/components/session_bar.py
"""
The session selection at the top of every page, and the day every page shows.

Three selectors pick it, in this order: the session day - the page's heading,
opening a calendar on which only the days with stored bars can be picked, with
buttons stepping to the previous and the next of them - then an instrument with
bars that day (ES, NQ, ...), then one of its contracts holding the day (the one
active that day first). Right of them the stored data (stored_data.py). The page's own
controls go in ``tools``, a row ``build_tools`` draws where it is called (under
the session's timeline).

The selection travels with the page: ``?day=&symbol=&contract=`` opens a page
on it (the header's navigation carries it from page to page), and the address
bar follows every change, so a reload keeps it. Without one, or with a day
that holds no bars, a page opens on the newest NQ session (else the newest of
any instrument), on the contract active that day.

A page follows the selection through ``on_change``, called with what changed:
``"day"`` (a new day - also when a job's end moved the bar on to a newer
session), ``"symbol"``, ``"contract"``, or ``"data"`` (the same selection,
the database read again after a job: ``reload``).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from nicegui import ui

from config import Config
from dashboard.components.stored_data import StoredData
from database.queries import contracts_for_day, list_contracts, list_session_days, symbols_with_day

# The instrument a page opens on; its current contract is preselected.
DEFAULT_SYMBOL = "NQ"


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


def contract_label(contract) -> str:
    return f"{contract['symbol']} {contract['expiry']}"


def long_day(day: Optional[str]) -> str:
    """'2026-10-09' -> 'Friday 9 October 2026', the session bar's heading."""
    if not day:
        return ""
    d = date.fromisoformat(day)
    return f"{d:%A} {d.day} {d:%B %Y}"


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SessionBar:
    """The selection's state and its controls; ``build`` draws them where it is called."""

    def __init__(self, conn, day: Optional[str] = None, symbol: Optional[str] = None,
                 contract: Optional[str] = None) -> None:
        self.conn = conn
        self.symbols: List[str] = self._target_symbols()
        self.on_change: List[Callable[[str], Any]] = []

        # Selected, in the order the controls pick them: day -> symbol -> contract.
        self.days: List[str] = []                 # the days with bars, newest first
        self.date: Optional[str] = None
        self.symbol: Optional[str] = None
        self.contract = None
        self.day_contracts: Dict[int, Any] = {}   # contract_id -> contract row, for the selector
        self._requested = (day, symbol, _int(contract))
        # Set while the selectors are updated from code, so their change events
        # do not reload the page once per control.
        self._syncing = False
        self.tools = None                          # the page's own controls, under the session's timeline
        self.collect_gaps: Optional[Callable[[List[Dict[str, Any]]], Any]] = None   # set by the page's header

    def _target_symbols(self) -> List[str]:
        """
        Only the target futures are offered: context instruments are collected for
        intermarket reference. The collector also records whole futures chains to
        plan rolls, so only contracts that actually hold bars are offered.
        """
        symbols: List[str] = []
        for c in list_contracts(self.conn, with_data_only=True):
            if not _is_context_only(c["symbol"]) and c["symbol"] not in symbols:
                symbols.append(c["symbol"])
        return symbols

    def query(self) -> str:
        """The selection as a query string, for a link to another page."""
        params = {"day": self.date, "symbol": self.symbol,
                  "contract": self.contract["contract_id"] if self.contract is not None else None}
        return urlencode({k: v for k, v in params.items() if v is not None})

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _choose_symbol(self) -> List[str]:
        """The instruments with bars on the selected day; keeps the current one when it has."""
        symbols = symbols_with_day(self.conn, self.date, self.symbols) if self.date else []
        if self.symbol not in symbols:
            self.symbol = DEFAULT_SYMBOL if DEFAULT_SYMBOL in symbols else (symbols[0] if symbols else None)
        return symbols

    def _choose_contract(self, keep: Optional[int] = None) -> Dict[int, str]:
        """
        The selected instrument's contracts holding the day; the first (active
        that day) is chosen - or the contract ``keep`` when it is among them.
        """
        contracts = day_contracts(self.conn, self.symbol, self.date) if self.symbol and self.date else []
        self.day_contracts = {int(c["contract_id"]): c for c in contracts}
        self.contract = self.day_contracts.get(keep) or (contracts[0] if contracts else None)
        return {int(c["contract_id"]): contract_label(c) for c in contracts}

    def _sync_selectors(self, symbols: bool = True, keep_contract: Optional[int] = None) -> None:
        """Pushes the chosen symbol / contract (and their options) to the controls."""
        self._syncing = True
        try:
            if symbols:
                self.symbol_select.set_options(self._choose_symbol(), value=self.symbol)
            options = self._choose_contract(keep_contract)
            self.contract_select.set_options(
                options, value=int(self.contract["contract_id"]) if self.contract is not None else None)
        finally:
            self._syncing = False

    def _changed(self, what: str) -> None:
        """The address bar follows the selection; then the page."""
        ui.run_javascript(f"history.replaceState(history.state, '', location.pathname + '?{self.query()}')")
        for listener in self.on_change:
            listener(what)

    def set_day(self, day: str) -> bool:
        """Selects ``day`` as if picked on the calendar; False when no instrument holds bars that day."""
        if day not in self.days:
            return False
        self.date_picker.value = day          # on_date follows
        return True

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
        self._changed("day")

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
        self._changed("symbol")

    def on_contract(self, event) -> None:
        if self._syncing or event.value is None:
            return
        self.contract = self.day_contracts[int(event.value)]
        self._changed("contract")

    def reload(self) -> None:
        """
        Reads the stored sessions again, after a job ran: the day calendar and
        the stored data. Showing the newest session, the bar moves on to a
        newer one when there is one (``"day"``); otherwise the same day and
        contract stay (``"data"``).
        """
        days = list_session_days(self.conn, self._target_symbols())
        if not self.days or not days:                      # built without sessions: build the page again
            if days:
                ui.navigate.reload()
            return
        self.symbols = self._target_symbols()
        moved = self.date == self.days[0] and days[0] != self.date
        self.days = days
        self._set_day_options()
        if moved:
            self.date = days[0]
            self._set_picker(self.date)
        self._mark_day()
        contract = None if moved or self.contract is None else int(self.contract["contract_id"])
        self._sync_selectors(keep_contract=contract)
        self.stored.refresh()
        self._changed("day" if moved else "data")

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
        """The heading shows the selected day; the step buttons stop at the oldest and the newest day."""
        self.date_field.set_text(long_day(self.date))
        i = self.days.index(self.date) if self.date in self.days else None
        self.older_button.set_enabled(i is not None and i + 1 < len(self.days))
        self.newer_button.set_enabled(i is not None and i > 0)

    def build(self) -> None:
        """The selectors and the stored data - or, without any session, a note."""
        days = list_session_days(self.conn, self.symbols)
        if not days:
            with ui.card().classes("w-full"):
                ui.label("No sessions found.").classes("text-lg")
                ui.label("Run the collector (Update data, above), or populate_mock_data.py.")
            return

        day, self.symbol, contract = self._requested
        if day in days:
            self.date = day
        else:
            newest_default = list_session_days(self.conn, [DEFAULT_SYMBOL], limit=1)
            self.date = newest_default[0] if newest_default else days[0]
        symbols = self._choose_symbol()
        contract_options = self._choose_contract(keep=contract if day in days else None)

        with ui.row().classes("w-full items-center gap-x-7 gap-y-3 px-6 pt-5"):
            self._build_day_picker(days)
            self.symbol_select = ui.select(
                symbols, value=self.symbol, label="Instrument", on_change=self.on_symbol,
            ).props("outlined dense stack-label").classes("w-28")
            self.contract_select = ui.select(
                contract_options, value=int(self.contract["contract_id"]) if self.contract is not None else None,
                label="Contract", on_change=self.on_contract,
            ).props("outlined dense stack-label").classes("w-44")
            ui.space()
            self.stored = StoredData(self.conn, collect=lambda gaps: self.collect_gaps and self.collect_gaps(gaps))
            self.stored.build()

    def build_tools(self) -> None:
        """The row for the page's own controls (``tools``), where it is called - under the session's timeline."""
        if self.date is not None:
            self.tools = ui.row().classes("w-full items-center gap-x-4 gap-y-2 px-6 pt-1")

    def _build_day_picker(self, days: List[str]) -> None:
        """
        The session day as the page's heading, opening a calendar (weeks from
        Monday) on which only ``days`` can be picked, between buttons stepping
        to the previous and the next of them.
        """
        self.days = days
        with ui.row().classes("items-center gap-1 no-wrap"):
            self.older_button = ui.button(icon="chevron_left", on_click=lambda: self.step_day(1)).props(
                'flat round aria-label="Previous session"').tooltip("Previous session")
            with ui.label().classes("tp-day").props('role="button" tabindex="0"') as self.date_field:
                with ui.menu() as self.date_menu:
                    self.date_picker = ui.date(self.date, on_change=self.on_date).props("first-day-of-week=1")
                    self._set_day_options()
            self.date_field.tooltip("Pick a session day")
            self.newer_button = ui.button(icon="chevron_right", on_click=lambda: self.step_day(-1)).props(
                'flat round aria-label="Next session"').tooltip("Next session")
            ui.button(icon="event", on_click=self.date_menu.open).props(
                'flat round aria-label="Pick a session day"').tooltip("Pick a session day")
        self._mark_day()

    def _set_day_options(self) -> None:
        """The calendar offers ``self.days`` only, its months between the oldest and the newest."""
        self.date_picker.props(f'navigation-min-year-month="{self.days[-1][:7].replace("-", "/")}" '
                               f'navigation-max-year-month="{self.days[0][:7].replace("-", "/")}"')
        self.date_picker._props["options"] = [d.replace("-", "/") for d in self.days]
        self.date_picker.update()
