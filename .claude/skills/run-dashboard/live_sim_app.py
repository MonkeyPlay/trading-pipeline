"""The dashboard with 2026-06-12 treated as today's live session and a 3 s poll (run with DATABASE_URL=tp_test)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

from dashboard.views import candles

candles.SessionExplorer._is_live = lambda self: self.date == "2026-06-12"
candles.LIVE_POLL_SECONDS = 3.0

from dashboard import app  # noqa: E402

app.main()
