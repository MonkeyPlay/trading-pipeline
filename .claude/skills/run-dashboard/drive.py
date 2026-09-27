"""
Drive the dashboard in headless Chromium and screenshot what a user sees.

    python drive.py explorer|direction|backtests|live [--port 8093] [--out /tmp/dashboard-shots]

Run with a Python that has Playwright (see SKILL.md); it uses the cached
headless Chromium under ~/.cache/ms-playwright.
"""
import argparse
import glob
import os
import subprocess

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
AS_OF = "text=/\\d\\d:\\d\\d ET/"


def chromium() -> str:
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))
    if not found:
        raise SystemExit("no cached headless Chromium: run `/tmp/pw/bin/playwright install chromium-headless-shell`")
    return found[-1]


def slider_to(page, minute: float) -> None:
    """Clicks the as-of slider's track at ``minute`` of the 390-minute session."""
    box = page.locator(".q-slider").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] * (minute + 0.5) / 390, box["y"] + box["height"] / 2)
    page.wait_for_timeout(2500)


def first_hour_row(page) -> str:
    return page.locator(".q-table tr").filter(has_text="First hour").first.inner_text().replace("\t", " | ")


def explorer(page, url, out):
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector("text=Still to come", timeout=180000)
    page.wait_for_timeout(2500)
    page.screenshot(path=f"{out}/explorer.png", full_page=True)
    print("as of", page.locator(AS_OF).first.inner_text())
    print(page.locator(".q-table").first.inner_text())
    slider_to(page, 15)
    print("\nafter the slider: as of", page.locator(AS_OF).first.inner_text())
    print(page.locator(".q-table").first.inner_text())
    page.get_by_role("button", name="Session", exact=True).click()
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{out}/explorer_0945_session_cone.png")


def direction(page, url, out):
    page.goto(url + "/", timeout=180000)
    page.wait_for_selector("text=Still to come", timeout=180000)
    header = page.locator("text=Direction forecasts · trained model and scenario generator").first
    header.scroll_into_view_if_needed()
    print("collapsed:", not page.locator("text=/Overall bias/").first.is_visible())
    header.click()
    page.wait_for_selector("text=/Overall bias/", state="visible", timeout=60000)
    page.screenshot(path=f"{out}/direction_open.png", full_page=True)
    print("opened: model panel", page.locator("text=Model forecast").first.is_visible())


def backtests(page, url, out):
    page.goto(url + "/backtests", timeout=180000)
    page.wait_for_selector("text=/typical miss/", timeout=300000)
    page.screenshot(path=f"{out}/backtests.png", full_page=True)
    print(page.locator(".q-card").filter(has_text="Range nowcast").first.inner_text())


def live(page, url, out):
    def add(minutes):
        subprocess.run([os.path.join(PROJECT, ".venv/bin/python"), os.path.join(HERE, "live_sim.py"), "add",
                        str(minutes)], check=True)

    def state(label):
        follow = page.get_by_role("switch").first.get_attribute("aria-checked")
        print(f"{label}: as of {page.locator(AS_OF).first.inner_text()} · follow live {follow}\n    {first_hour_row(page)}")

    page.goto(url + "/", timeout=180000)
    page.wait_for_selector("text=Still to come", timeout=180000)
    page.wait_for_timeout(2000)
    state("opened")
    add(25)
    page.wait_for_timeout(8000)
    state("two minutes later")
    slider_to(page, 10)
    state("slider moved back")
    add(26)
    page.wait_for_timeout(8000)
    state("a minute later, not following")
    page.get_by_role("switch").first.click()
    page.wait_for_timeout(2500)
    state("following again")
    page.screenshot(path=f"{out}/live.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("explorer", "direction", "backtests", "live"))
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
