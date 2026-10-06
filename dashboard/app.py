# dashboard/app.py
"""
NiceGUI web application entrypoint for the trading pipeline.
Opens the database connection, applies the shared chrome - the header and, below
it, the session bar whose day every page shows - and routes the views.

Run from the project root:  python -m dashboard.app
"""

from __future__ import annotations

import os
import sys

# Ensure the project root is importable when this file is run directly.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from typing import Optional, Tuple
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from nicegui import app, ui

from config import Config
from contracts import nq_prompt_v2 as defs
from dashboard.components.pipeline import PipelinePanel
from dashboard.components.session_bar import SessionBar
from dashboard.views.candles import show_candles_page
from dashboard.views.evaluation import show_evaluation_page
from dashboard.views.forecast import run_day
from database.connection import describe_dsn, get_db_connection, init_database
from database.migrations import get_user_version

_PAGE_BACKGROUND = "#131722"

_connection = None


def connection():
    """
    The process-wide database connection.

    Opened lazily on first use and shared by every page: statements and
    ``with conn:`` transactions take the connection's lock, which serialises
    access across NiceGUI's worker threads.
    """
    global _connection
    if _connection is None:
        init_database(Config.DATABASE_URL)  # idempotent: applies pending migrations
        _connection = get_db_connection(Config.DATABASE_URL)
    return _connection


@app.on_shutdown
def _close_connection() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def _db_status(conn) -> str:
    try:
        version = get_user_version(conn)
        bars = conn.execute("SELECT COUNT(*) FROM bars;").fetchone()[0]
        return f"{describe_dsn(Config.DATABASE_URL)} · schema v{version:04d} · {bars:,} bars"
    except Exception:
        return f"{describe_dsn(Config.DATABASE_URL)} · schema unknown"


def chrome(active: str, conn, day: Optional[str] = None, symbol: Optional[str] = None,
           contract: Optional[str] = None) -> Tuple[PipelinePanel, SessionBar]:
    """
    Header and navigation shared by every page, with the pipeline's "Update data"
    control, then the session bar (opened on ``day``, ``symbol`` and ``contract``
    when given): the navigation carries its selection to the other page, and it
    reads the database again when a job ends. The page follows the bar's
    ``on_change`` and adds its own reload to the panel's ``on_update``.
    """
    ui.query("body").style(f"background:{_PAGE_BACKGROUND}")
    bar = SessionBar(conn, day, symbol, contract)
    with ui.header().classes("items-center gap-6 px-4 py-2").style("background:#1c212e"):
        ui.label("Trading Pipeline").classes("text-lg font-medium")
        for label, target in (("Session Explorer", "/"), ("Evaluation", "/evaluation")):
            button = ui.button(label, on_click=lambda t=target: ui.navigate.to(f"{t}?{bar.query()}"))
            button.props("flat no-caps" if label != active else "flat no-caps color=primary")
        ui.space()
        panel = PipelinePanel(conn)
        panel.show_preview = lambda: ui.navigate.to(f"/?{bar.query()}&view=preview")
        panel.build()
        status = ui.label(_db_status(conn)).classes("text-xs").style("color:#787b86")
    bar.build()
    panel.on_update.append(lambda: status.set_text(_db_status(conn)))
    panel.on_update.append(bar.reload)
    return panel, bar


@ui.page("/")
def index(day: str = None, symbol: str = None, contract: str = None, run: str = None, view: str = None) -> None:
    conn = connection()
    if run and not day:                                    # a run opens on its own day
        day, symbol = run_day(conn, run), defs.SYMBOL
    panel, bar = chrome("Session Explorer", conn, day, symbol, contract)
    explorer = show_candles_page(conn, bar, panel, run, view)
    if bar.date is not None:
        panel.show_preview = explorer.forecast.show_preview
        panel.on_update.append(explorer.forecast.reload)


@ui.page("/evaluation")
def evaluation(day: str = None, symbol: str = None, contract: str = None) -> None:
    conn = connection()
    _, bar = chrome("Evaluation", conn, day, symbol, contract)
    show_evaluation_page(conn, bar)


@app.get("/forecast")
def forecast(run: str = None, view: str = None) -> RedirectResponse:
    """The forecast is part of the Session Explorer now; old links land there."""
    params = {k: v for k, v in (("run", run), ("view", view)) if v}
    return RedirectResponse("/" + (f"?{urlencode(params)}" if params else ""))


def main() -> None:
    ui.run(
        title="Trading Pipeline",
        favicon="📈",
        dark=True,
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("DASHBOARD_PORT", "8080")),
        reload=False,
        show=False,
    )


# NiceGUI re-imports this module in the reloader's child process, where the
# module name is __mp_main__ rather than __main__.
if __name__ in {"__main__", "__mp_main__"}:
    main()
