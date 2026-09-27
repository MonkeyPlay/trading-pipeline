---
name: run-dashboard
description: Launch the NiceGUI dashboard on a spare port against the local database and drive it with headless Chromium (Playwright) - Session Explorer (range nowcast card, as-of slider, cone), the folded direction panels, the Backtests page, and a simulated live session in the disposable tp_test database. Use to see a dashboard change working, not just its tests.
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
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py explorer    # card, slider to 09:45, session cone
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py direction   # the folded direction panels open
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py backtests   # waits for the Range nowcast section
```

`--port` (default 8093) and `--out` (default `/tmp/dashboard-shots`) are optional. Each step prints what the page shows and ends with `PROBLEMS: none` or the browser errors. **Look at the screenshots** it writes; then check the server log for tracebacks.

## 4. A live session, simulated

Live following only runs on today's session while the real-time streamer stores bars. To exercise it any day, fill the disposable **tp_test** database (the test suite resets it anyway) with 139 synthetic sessions plus a partial "today" (2026-06-12), and run a dashboard that treats that day as live and polls every 3 s:

```bash
.venv/bin/python .claude/skills/run-dashboard/live_sim.py setup 23          # RESETS tp_test; today up to 09:53
(DATABASE_URL=postgresql://trading:trading@localhost:5432/tp_test DASHBOARD_PORT=8094 \
   nohup .venv/bin/python .claude/skills/run-dashboard/live_sim_app.py > /tmp/dash-8094.log 2>&1 &)
timeout 90 bash -c 'until curl -sf http://127.0.0.1:8094 >/dev/null; do sleep 1; done'
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py live --port 8094   # stores new minutes while watching
```

Expected: as of 09:53 with Follow live on; two new minutes move it to 09:55 on their own; moving the slider back pauses following; switching it on jumps to the latest minute.

## Gotchas

- NiceGUI talks over a websocket: wait for the text you need (`wait_for_selector`), never for network idle.
- The first page load per instrument builds the nowcast history (~4 s) before the card appears.
- The as-of slider acts on Quasar's `change` event (release): click its track rather than setting a value.
- The session day select takes typed input: fill it, then click the matching `.q-item`.
