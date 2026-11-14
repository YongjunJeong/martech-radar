"""Screenshot the opening and closing cards."""

from __future__ import annotations

import asyncio
import pathlib

from playwright.async_api import async_playwright

import os
ROOT = pathlib.Path(__file__).resolve().parent.parent
DEMO = pathlib.Path(os.environ.get("RADAR_HOME", ROOT)) / "data" / "demo"


async def main() -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    cards = ROOT / "demo" / "cards.html"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await (await browser.new_context(
            viewport={"width": 1280, "height": 800})).new_page()
        for card in ("title", "outro"):
            await page.goto(f"{cards.as_uri()}?c={card}")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(DEMO / f"card-{card}.png"))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
