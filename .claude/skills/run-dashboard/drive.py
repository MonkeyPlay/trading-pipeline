"""
Drive the dashboard in headless Chromium and screenshot what a user sees.

    python drive.py explorer|direction|backtests [--port 8093] [--out /tmp/dashboard-shots]

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


def explorer(page, url, out):
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector("text=Predicted move from P", timeout=180000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{out}/explorer.png", full_page=True)
    print(page.locator(".q-table").first.inner_text())
    for line in page.locator("text=/Likely hour high|Walk-forward over|Scheduled today|No scheduled release/").all():
        print(line.inner_text())
    page.get_by_role("button", name="5m", exact=True).click()           # the generated hour at 5 minutes
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{out}/explorer_5m.png")


def direction(page, url, out):
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector("text=Predicted move from P", timeout=180000)
    header = page.locator("text=Direction forecasts · trained model and scenario generator").first
    header.scroll_into_view_if_needed()
    print("collapsed:", not page.locator("text=/Overall bias/").first.is_visible())
    header.click()
    page.wait_for_selector("text=/Overall bias/", state="visible", timeout=60000)
    page.screenshot(path=f"{out}/direction_open.png", full_page=True)
    print("opened: model panel", page.locator("text=Model forecast").first.is_visible())


def backtests(page, url, out):
    page.goto(url + "/backtests", timeout=180000)
    page.wait_for_selector("text=/squared error vs no move/", timeout=300000)
    page.screenshot(path=f"{out}/backtests.png", full_page=True)
    print(page.locator(".q-card").filter(has_text="First-hour model").first.inner_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("explorer", "direction", "backtests"))
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
