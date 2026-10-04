"""
Drive the dashboard in headless Chromium and screenshot what a user sees.

    python drive.py explorer|review [--port 8093] [--out /tmp/dashboard-shots]

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
    page.wait_for_selector(".q-badge", timeout=180000)           # the session's bar count
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{out}/explorer.png", full_page=True)
    print("status:", page.locator(".q-badge").all_inner_texts())
    page.get_by_role("button", name="5m", exact=True).click()           # the session at 5 minutes
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{out}/explorer_5m.png")


def review(page, url, out):
    """The Review page: first session's chart and P2 record. Never presses Save (it writes verdicts)."""
    page.goto(url + "/review", timeout=180000)
    page.wait_for_selector("text=Realised Opening Type", timeout=180000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{out}/review.png", full_page=True)
    print(page.locator("text=/ reviewed/").first.inner_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("explorer", "review"))
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
