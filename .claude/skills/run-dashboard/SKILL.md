---
name: run-dashboard
description: Launch the NiceGUI dashboard on a spare port against the local database and drive it with headless Chromium (Playwright) - the Session Explorer (the session chart and its reference levels) and the Review page (label review of a review set). Use to see a dashboard change working, not just its tests.
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
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py explorer    # the chart and status badge, then at 5 minutes
/tmp/pw/bin/python .claude/skills/run-dashboard/drive.py review      # the Review page (never presses Save: it writes verdicts)
```

`--port` (default 8093) and `--out` (default `/tmp/dashboard-shots`) are optional. The step prints what the page shows and ends with `PROBLEMS: none` or the browser errors. **Look at the screenshots** it writes; then check the server log for tracebacks.

## Gotchas

- NiceGUI talks over a websocket: wait for the text you need (`wait_for_selector`), never for network idle.
- `tp_test` is the disposable database for anything that writes (the test suite resets it).
- The session day select takes typed input: fill it, then click the matching `.q-item`.
