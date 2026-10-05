# dashboard/app.py
"""
NiceGUI web application entrypoint for the trading pipeline.
Opens the database connection, applies the shared chrome, and routes the views.

Run from the project root:  python -m dashboard.app
"""

from __future__ import annotations

import os
import sys

# Ensure the project root is importable when this file is run directly.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from nicegui import app, ui

from config import Config
from dashboard.components.pipeline import PipelinePanel
from dashboard.views.candles import show_candles_page
from dashboard.views.evaluation import show_evaluation_page
from dashboard.views.forecast import show_forecast_page
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


def chrome(active: str, conn) -> PipelinePanel:
    """
    Header and navigation shared by every page, with the pipeline's "Update data"
    control. The page adds its own reload to the returned panel's ``on_update``.
    """
    ui.query("body").style(f"background:{_PAGE_BACKGROUND}")
    with ui.header().classes("items-center gap-6 px-4 py-2").style("background:#1c212e"):
        ui.label("Trading Pipeline").classes("text-lg font-medium")
        for label, target in (("Session Explorer", "/"), ("Forecast", "/forecast"), ("Evaluation", "/evaluation")):
            button = ui.button(label, on_click=lambda t=target: ui.navigate.to(t))
            button.props("flat no-caps" if label != active else "flat no-caps color=primary")
        ui.space()
        panel = PipelinePanel()
        panel.build()
        status = ui.label(_db_status(conn)).classes("text-xs").style("color:#787b86")
    panel.on_update.append(lambda: status.set_text(_db_status(conn)))
    return panel


@ui.page("/")
def index() -> None:
    conn = connection()
    panel = chrome("Session Explorer", conn)
    panel.on_update.append(show_candles_page(conn).reload)


@ui.page("/evaluation")
def evaluation() -> None:
    conn = connection()
    chrome("Evaluation", conn)
    show_evaluation_page(conn)


@ui.page("/forecast")
def forecast(run: str = None) -> None:
    conn = connection()
    panel = chrome("Forecast", conn)
    panel.on_update.append(show_forecast_page(conn, run).reload)


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
