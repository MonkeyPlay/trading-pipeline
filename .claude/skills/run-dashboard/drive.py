"""
Drive the dashboard in headless Chromium and screenshot what a user sees.

    python drive.py explorer|forecast|evaluation|fan [--port 8093] [--out /tmp/dashboard-shots]

Run with a Python that has Playwright (see SKILL.md); it uses the cached
headless Chromium under ~/.cache/ms-playwright.
"""
import argparse
import glob
import os

from playwright.sync_api import sync_playwright



def chromium() -> str:
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))
    if not found:
        raise SystemExit("no cached headless Chromium: run `/tmp/pw/bin/playwright install chromium-headless-shell`")
    return found[-1]


# Each chart's visible range as clock times of its own day (the explorer's two charts are linked by them).
CLOCKS = """() => [...document.querySelectorAll('.nq-chart-root')].map(el => {
    const c = getElement(parseInt(el.id.slice(1)));
    const k = c.ready && c.ready() ? c.clockRange() : null;
    const hm = s => new Date(s * 1000).toISOString().slice(11, 19);
    return k ? hm(k.from) + '-' + hm(k.to) : null;
})"""


def explorer(page, url, out):
    """The session chart and the analogue beside it (linked by time of day), the comparison below them, the day
    calendar, the previous session, then 5 minutes."""
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector(".q-badge", timeout=180000)           # the session's bar count
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{out}/explorer.png", full_page=True)
    day = page.get_by_label("Session day (NY trading day)")
    print("day:", day.input_value(), "status:", page.locator(".q-badge").all_inner_texts())
    print("beside it:", page.locator(".q-field", has_text="Analogue").first.inner_text().replace("\n", " "))
    analogues = page.locator(".q-expansion-item__content").first.inner_text().split("\n")
    print("analogues:", next((t for t in analogues if "analogue(s) from" in t or t.startswith(("No ", "Analogues are"))),
                             analogues[:2]))
    box = page.locator(".nq-chart-root").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 250)
    page.mouse.wheel(0, -480)                                           # zoom the session: the analogue follows
    page.wait_for_timeout(1000)
    print("clock ranges (session, analogue):", page.evaluate(CLOCKS))
    day.click()                                                         # the calendar: only days with bars
    page.wait_for_selector(".q-date", timeout=10000)
    page.wait_for_timeout(500)
    page.screenshot(path=f"{out}/explorer_calendar.png", clip={"x": 0, "y": 0, "width": 900, "height": 560})
    print("pickable this month:", page.locator(".q-date__calendar-item--in").all_inner_texts())
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Previous session").click()
    page.wait_for_timeout(2500)
    print("previous session:", day.input_value())
    page.get_by_role("button", name="5m", exact=True).click()           # the session at 5 minutes
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{out}/explorer_5m.png")


def forecast(page, url, out):
    """The Session Explorer's forecast (at the bottom): the session day's newest run - provenance, frozen chart,
    per-target table, P1 record (the realised outcome stays hidden)."""
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector(".q-badge", timeout=180000)
    day = page.get_by_label("Session day (NY trading day)").input_value()
    box = page.locator(".q-expansion-item", has_text="Forecast of NQ").first
    box.scroll_into_view_if_needed()
    page.wait_for_timeout(2500)
    print("day:", day, "-", box.locator(".q-item").first.inner_text().replace("\n", " "))
    run = page.locator("text=/^Arm [A-D] · .* Run [0-9a-f-]{36}/")
    print(run.first.inner_text() if run.count() else "no run shown")
    box.screenshot(path=f"{out}/forecast.png")


def evaluation(page, url, out):
    """The session day carried from the Session Explorer to Evaluation (the previous session), and its cases."""
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector(".q-badge", timeout=180000)
    page.get_by_role("button", name="Previous session").click()
    page.wait_for_timeout(2000)
    day = page.get_by_label("Session day (NY trading day)").input_value()
    page.get_by_role("button", name="Evaluation", exact=True).click()
    page.wait_for_url("**/evaluation?*", timeout=60000)
    page.wait_for_selector("text=Session day (NY trading day)", timeout=60000)
    page.wait_for_timeout(2000)
    carried = page.get_by_label("Session day (NY trading day)").input_value()
    print("explorer day:", day, "evaluation day:", carried, "url:", page.url)
    found = page.locator(f"text=/^Session day {carried}:/")
    print(found.first.inner_text() if found.count() else "no session-day section (no experiment?)")
    page.screenshot(path=f"{out}/evaluation.png", full_page=True)


def fan(page, url, out):
    """The session in progress (only then): the fan (fan_rw_v2) right of the newest candle - on NQ with the learned
    fan's brackets at the horizons it passed, recorded or computed - its readout, then playback 30 candles back (a
    recomputed historical preview, so the caption says) - hidden, then with what followed."""
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector(".q-badge", timeout=180000)
    note = page.locator("text=/^Fan fan_rw_v\\d|^No fan|^Loading the fan|^Recomputed historical preview/")
    if not page.locator("text=Playback").first.is_visible():
        print("no session in progress: no fan and no playback (current_session is None)")
        return
    page.wait_for_selector("text=/^Fan fan_rw_v\\d|^No fan/", timeout=120000)
    try:                                              # NQ: the learned fan's brackets arrive a moment later
        page.wait_for_selector("text=/learned fan \\(|recorded forecast/", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    print(note.first.inner_text()[:500])
    page.screenshot(path=f"{out}/fan_live.png")
    box = page.locator(".nq-chart-root").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] - 120, box["y"] + 300)     # over a fan column
    page.wait_for_timeout(400)
    print("readout:", page.locator(".nq-chart-legend").first.inner_text().split("\n")[0])
    for _ in range(30):
        page.get_by_role("button", name="Previous candle").click()
    page.wait_for_timeout(2000)
    print("playback:", page.locator(".font-mono").first.inner_text())
    print(note.first.inner_text()[:500])
    page.screenshot(path=f"{out}/fan_playback.png")
    page.get_by_text("Show what followed").click()
    page.wait_for_timeout(1500)
    page.screenshot(path=f"{out}/fan_reveal.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("explorer", "forecast", "evaluation", "fan"))
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument("--out", default="/tmp/dashboard-shots")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium(), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1700, "height": 1300})
        problems = []
        page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        globals()[args.step](page, f"http://127.0.0.1:{args.port}", args.out)
        print("\nscreenshots in", args.out)
        print("PROBLEMS:", problems or "none")
        browser.close()


if __name__ == "__main__":
    main()
