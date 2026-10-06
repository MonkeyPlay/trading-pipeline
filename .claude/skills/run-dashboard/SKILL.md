---
name: run-dashboard
description: Launch the NiceGUI dashboard on a spare port against the local database and drive it with headless Chromium (Playwright) - the session bar on every page (the day calendar, instrument, contract, coverage), the Session Explorer (the session chart beside an analogue's - linked by time of day - the comparison below, and the day's forecast at the bottom) and Evaluation (the bar's day carried over). Use to see a dashboard change working, not just its tests.
---

# Run and drive the dashboard

## 1. Launch on a spare port

The user usually keeps their own dashboard on **8080**: never stop or restart it unless asked.

```bash
cd /home/monkeyplay/trading_pipeline
(DASHBOARD_PORT=8093 nohup .venv/bin/python -m dashboard.app > /tmp/dash-8093.log 2>&1 &)
timeout 90 bash -c 'until curl -sf http://127.0.0.1:8093 >/dev/null; do sleep 1; done'
```

Stop it by the port's listener, never with a broad `pkill`:

```bash
pid=$(ss -ltnp | grep ':8093 ' | grep -o 'pid=[0-9]*' | cut -d= -f2); [ -n "$pid" ] && kill $pid
```

## 2. A browser

There is no `chromium-cli` and no Playwright in the project venv. Install Playwright into a throwaway venv and use the cached headless Chromium (the driver finds it under `~/.cache/ms-playwright`):

```bash
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install -q playwright
```

## 3. Drive

```bash
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py explorer    # both charts and their link, calendar, previous day, 5m
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py forecast    # the explorer's forecast: the day's newest run, outcome hidden
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py evaluation  # the previous session's day carried to Evaluation
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py fan         # the session in progress: fan, readout, playback
```

`--port` (default 8093) and `--out` (default `/tmp/dashboard-shots`) are optional. The step prints what the page shows and ends with `PROBLEMS: none` or the browser errors. **Look at the screenshots** it writes; then check the server log for tracebacks.

## Gotchas

- **Auto** (the explorer's tools, beside Fit) starts a real collector run against IB once a minute
  (`dashboard/jobs.py` AUTO) - never switch it on against production to test; its schedule is
  unit-tested (`tests/test_fan_view.py`). The fan and playback show only while a session is in
  progress (18:00-17:00 ET on session days); the `fan` step says so otherwise.
- **Update data** (header) runs real jobs against the dashboard's `DATABASE_URL`: **Run collector** and
  **Run forecaster** write to it (incremental, idempotent), **Forecast now** collects (writes bars) and then
  replaces `data/preview/forecast_preview.json` (nothing in the journal), **Live forecast** records an
  append-only live capture with its forecasts, and **Run LLM forecast**'s confirmation **Send** sends Claude
  requests that cost money - never click those two to test (opening the confirmation sends nothing). Arms C
  and D with a realised outcome exist in `tp_test` after `tests/test_nq_journal.py` (session 2026-06-12) -
  the place to look at the grading radar. Opening the dialog is harmless; point a
  test dashboard at `tp_test` before starting a job you only want to watch.
- NiceGUI talks over a websocket: wait for the text you need (`wait_for_selector`), never for network idle.
- `tp_test` is the disposable database for anything that writes (the test suite resets it).
- The session bar (day, instrument, contract) is on every page and travels in the URL
  (`?day=&symbol=&contract=`); `/?run=<id>` opens a forecast run on its day, `/?view=preview` the
  Forecast now tab. The session day is a calendar: click the field labelled "Session day (NY trading day)", then a
  `.q-date__calendar-item--in button` with the day's number (days without bars are `--out`); the
  arrows beside it are the buttons "Previous session" and "Next session".
- Instrument, contract and analogue are selects: click the field, then the item under
  `.q-menu .q-item` - a bare `.q-item` also matches the analogues header ("Analogues of ...").
- The charts are `.nq-chart-root` elements; `getElement(<id without the "c">)` in the page is the
  component, and its `clockRange()` the visible range in seconds after its day's midnight - equal
  on the two explorer charts while they are linked.
- `tp_test` holds journal snapshots only after the whole suite has run: running one test module
  resets it to that module's data.
