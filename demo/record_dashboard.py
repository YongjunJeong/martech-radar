import asyncio
import os
import pathlib
from playwright.async_api import async_playwright

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8860")
D = (
    pathlib.Path(os.environ.get("RADAR_HOME", pathlib.Path(__file__).resolve().parent.parent))
    / "data"
    / "demo"
)

OVERLAY = """
(() => {
  if (document.getElementById('__cur')) return;
  const c = document.createElement('div'); c.id='__cur';
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;pointer-events:none;'
    + 'left:0;top:0;transition:transform .08s linear;'
    + "background:url(\\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='22' height='22'>"
    + "<path d='M2 1 L2 17 L6.5 13 L9 19 L12 17.5 L9.5 12 L15 12 Z' fill='white' stroke='black' stroke-width='1.3'/></svg>\\") no-repeat";
  document.body.appendChild(c);
  const cap = document.createElement('div'); cap.id='__cap';
  cap.style.cssText='position:fixed;z-index:99998;left:0;right:0;bottom:0;padding:14px 26px;'
    + 'background:linear-gradient(transparent,rgba(13,17,23,.92) 40%);color:#e6edf3;font-size:19px;'
    + 'font-family:-apple-system,Pretendard,"Noto Sans KR",sans-serif;text-align:center;opacity:0;'
    + 'transition:opacity .3s;line-height:1.5;';
  document.body.appendChild(cap);
  window.__cursor = (x,y) => { c.style.transform = `translate(${x}px,${y}px)`; };
  window.__caption = (text) => { cap.textContent = text; cap.style.opacity = text ? 1 : 0; };
  const ring = document.createElement('div');
  ring.style.cssText='position:fixed;z-index:99997;width:34px;height:34px;border-radius:50%;'
    + 'border:2px solid #58a6ff;opacity:0;pointer-events:none;transition:all .35s;';
  document.body.appendChild(ring);
  window.__click = (x,y) => { ring.style.left=(x-17)+'px'; ring.style.top=(y-17)+'px';
    ring.style.opacity=1; ring.style.transform='scale(.4)';
    setTimeout(()=>{ring.style.transform='scale(1.5)';ring.style.opacity=0;},60); };
})();
"""


class Rec:
    def __init__(self, page):
        self.pg = page
        self.x = 640
        self.y = 400

    async def setup(self):
        await self.pg.evaluate(OVERLAY)
        await self.pg.evaluate("__cursor(640,400)")

    async def caption(self, text):
        await self.pg.evaluate("(t) => __caption(t)", text)

    async def move(self, x, y, steps=26):
        sx, sy = self.x, self.y
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            cx, cy = sx + (x - sx) * e, sy + (y - sy) * e
            await self.pg.evaluate("([x,y]) => __cursor(x,y)", [cx, cy])
            await self.pg.wait_for_timeout(14)
        self.x, self.y = x, y

    async def click_sel(self, selector):
        # Page length varies with the watchlist; never assume the target is
        # on screen. Bring it into view and wait until it is measurable.
        target = self.pg.locator(selector).first
        await target.scroll_into_view_if_needed()
        await target.wait_for(state="visible", timeout=10_000)
        await self.pg.wait_for_timeout(250)
        box = await target.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self.move(x, y)
        await self.pg.evaluate("([x,y]) => __click(x,y)", [x, y])
        await self.pg.wait_for_timeout(300)
        await self.pg.locator(selector).first.click()
        await self.pg.wait_for_load_state("load")
        await self.setup()

    async def scroll(self, dy, steps=22):
        for _ in range(steps):
            await self.pg.mouse.wheel(0, dy / steps)
            await self.pg.wait_for_timeout(26)

    async def goto(self, path):
        await self.pg.goto(BASE + path, wait_until="load")
        # Let fonts and the first layout settle; a locator measured during
        # the first paint can report a box that is not where it ends up.
        await self.pg.wait_for_load_state("networkidle")
        await self.setup()


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(D / "rec/dash"),
            record_video_size={"width": 1280, "height": 800},
        )
        pg = await ctx.new_page()
        r = Rec(pg)

        await r.goto("/")
        await r.caption(
            "개요: 업종별로 누가 engagement 플랫폼을 쓰고, 누가 안 쓰고, 누구를 판정 못 했는지"
        )
        await pg.wait_for_timeout(2600)
        await r.scroll(520)
        await pg.wait_for_timeout(2400)
        await r.caption("솔루션 점유 — 실제 관측으로만 집계된다")
        await r.scroll(700)
        await pg.wait_for_timeout(2800)

        await r.caption("신호함: 무엇이 바뀌었고 누구를 공략할 수 있는가")
        await r.scroll(-1200)
        await pg.wait_for_timeout(400)
        await r.click_sel("nav a[href='/signals']")
        await pg.wait_for_timeout(2400)
        await r.scroll(500)
        await pg.wait_for_timeout(2600)

        await r.caption("임계값에 걸러진 기업도 숨기지 않고 표시한다")
        await r.scroll(900)
        await pg.wait_for_timeout(2600)
        await r.caption("차단된 기업은 '미보유'가 아니라 '판정 불가' — 이 구분이 핵심이다")
        await pg.wait_for_timeout(2400)

        await r.goto("/companies/shop_b")
        await r.caption("기업 상세: 현재 스택 · 판정 근거 · 실행 타임라인")
        await pg.wait_for_timeout(2400)
        await r.scroll(420)
        await pg.wait_for_timeout(1500)
        await r.caption("각 판정을 펼치면 그렇게 본 실제 증거가 나온다")
        await r.click_sel("details.vendor summary")
        await pg.wait_for_timeout(3000)
        await r.scroll(340)
        await pg.wait_for_timeout(3000)

        await r.caption("한 판정이 여러 층에서 독립적으로 확인될수록 점수가 오른다")
        await pg.wait_for_timeout(2600)

        await r.goto("/vendors")
        await r.caption("솔루션별로도 볼 수 있다 — 누가 무엇을 쓰는지 역추적")
        await pg.wait_for_timeout(2400)
        await r.scroll(560)
        await pg.wait_for_timeout(2600)
        await r.caption("")
        await pg.wait_for_timeout(700)
        await ctx.close()
        await b.close()


asyncio.run(main())
