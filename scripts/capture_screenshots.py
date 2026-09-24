"""Capture three PNGs for the README.

Requires the UI running at http://localhost:8000. Chromium via playwright.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "img"
OUT.mkdir(parents=True, exist_ok=True)


def _shot(page, path: Path, wait_ms: int = 800):
    page.wait_for_load_state("networkidle")
    time.sleep(wait_ms / 1000)
    page.screenshot(path=str(path), full_page=False)
    print(f"  → wrote {path.relative_to(REPO)}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        # 1. Cases list
        page.goto("http://localhost:8000", wait_until="domcontentloaded")
        time.sleep(2)
        _shot(page, OUT / "cases.png")

        # 2. HHG-014 → Graph tab
        page.click('text=HHG-014')
        time.sleep(1)
        page.click('button[data-view="graph"]')
        # Give d3 force layout time to settle
        time.sleep(4)
        _shot(page, OUT / "hhg014_graph.png", wait_ms=2000)

        # 3. Backtest tab (reliability plot)
        page.click('button[data-view="backtest"]')
        time.sleep(3)
        _shot(page, OUT / "backtest.png", wait_ms=2000)

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
