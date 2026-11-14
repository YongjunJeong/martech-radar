"""Build and record the terminal scenes.

Every line of output is captured from a real run against the demo
workspace by `build.sh`, never typed by hand — a demo may not show output
the tool would not actually produce.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEMO = pathlib.Path(os.environ.get("RADAR_HOME", ROOT)) / "data" / "demo"
CAPS = DEMO / "caps"


def trim(text: str, width: int = 104, maxlines: int = 20) -> str:
    lines = []
    for line in text.rstrip("\n").splitlines()[:maxlines]:
        lines.append(line if len(line) <= width else line[: width - 1] + "…")
    return "\n".join(lines)


def scenes() -> list[dict]:
    cap = lambda name: (CAPS / f"{name}.txt").read_text(encoding="utf-8")  # noqa: E731
    return [
        {"caption": "1. 워크스페이스 하나에 설정·워치리스트·데이터가 전부 있다",
         "cmd": "radar home", "out": trim(cap("home")), "hold": 3600},
        {"caption": "2. 워치리스트를 넣고 — 실제 브라우저가 사이트를 방문한다",
         "cmd": "radar watchlist import targets.yaml && radar batch",
         "out": trim(cap("batch")), "hold": 4200},
        {"caption": "3. 한 기업의 스택 — 판정마다 어느 층에서 봤는지가 붙어 있다",
         "cmd": "radar stack shop_b", "out": trim(cap("stack")), "hold": 4400},
        {"caption": "4. 봇 차단·robots.txt·점검 페이지는 이탈로 오해되지 않는다",
         "cmd": "radar changes walled", "out": trim(cap("changes")), "hold": 3800},
        {"caption": "5. 쌓인 이력에서 영업 신호를 뽑는다 — 신규·이탈·교체·greenfield",
         "cmd": "radar signals", "out": trim(cap("signals")), "hold": 4600},
    ]


async def main() -> None:
    template = (ROOT / "demo" / "terminal.html").read_text(encoding="utf-8")
    built = DEMO / "terminal.built.html"
    built.write_text(template.replace(
        "const SCENES = [];", "const SCENES = " + json.dumps(scenes(), ensure_ascii=False) + ";"),
        encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(DEMO / "rec" / "terminal"),
            record_video_size={"width": 1280, "height": 800})
        page = await context.new_page()
        await page.goto(built.as_uri())
        await page.wait_for_function("window.__demoDone === true", timeout=180_000)
        await page.wait_for_timeout(900)
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
