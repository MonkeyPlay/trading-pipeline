# dashboard/app.py
"""
NiceGUI web application entrypoint for the NQ Opening Forecast System.
Opens the SQLite connection, applies the shared chrome, and routes the views.

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
from dashboard.views.candles import show_candles_page
from dashboard.views.evaluation import show_evaluation_page
from database.connection import get_db_connection, init_database

_PAGE_BACKGROUND = "#131722"

_connection = None


def connection():
    """
    The process-wide SQLite connection.

    Opened lazily on first use and shared by every page: the connection is
    created with ``check_same_thread=False`` and all writes go through
    ``with conn:`` transactions, which serialise access.
    """
    global _connection
    if _connection is None:
        init_database(db_path=Config.DB_PATH)  # idempotent: applies pending migrations
        _connection = get_db_connection(Config.DB_PATH)
    return _connection


@app.on_shutdown
def _close_connection() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def _db_status(conn) -> str:
    try:
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
        bars = conn.execute("SELECT COUNT(*) FROM bars;").fetchone()[0]
        return f"{os.path.basename(Config.DB_PATH)} · schema v{version:04d} · {bars:,} bars"
    except Exception:
        return f"{os.path.basename(Config.DB_PATH)} · schema unknown"


def chrome(active: str, conn) -> None:
    """Header and navigation shared by every page."""
    ui.query("body").style(f"background:{_PAGE_BACKGROUND}")
    with ui.header().classes("items-center gap-6 px-4 py-2").style("background:#1c212e"):
        ui.label("NQ Opening Forecast").classes("text-lg font-medium")
        for label, target in (("Session Explorer", "/"), ("Evaluation", "/evaluation")):
            button = ui.button(label, on_click=lambda t=target: ui.navigate.to(t))
            button.props("flat no-caps" if label != active else "flat no-caps color=primary")
        ui.space()
        ui.label(_db_status(conn)).classes("text-xs").style("color:#787b86")
        ui.label(f"model {Config.LLM_MODEL}").classes("text-xs").style("color:#787b86")


@ui.page("/")
def index() -> None:
    conn = connection()
    chrome("Session Explorer", conn)
    show_candles_page(conn)


@ui.page("/evaluation")
def evaluation() -> None:
    conn = connection()
    chrome("Evaluation", conn)
    show_evaluation_page(conn)


def main() -> None:
    ui.run(
        title="NQ Opening Forecast",
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
