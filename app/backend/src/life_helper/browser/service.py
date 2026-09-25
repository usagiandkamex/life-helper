"""Shared headless Chromium and the per-Copilot-session browser page.

One Chromium process per app process, started on first use and stopped when the last page closes (the container has
1 CPU / 2 GiB). All traffic goes through ``EgressProxy``, so only public http(s) destinations are reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ..netguard import outbound_rejection
from ..security import SecretMasker
from .guard import fill_rejection
from .proxy import EgressProxy

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route

    from ..config import Settings
    from ..context import AppContext

logger = logging.getLogger(__name__)

MAX_SESSIONS = 2
SLOT_WAIT_SECONDS = 30
TOOL_TIMEOUT_SECONDS = 60
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
IDLE_WAIT_MS = 3_000
VIEWPORT = {"width": 1280, "height": 800}
MAX_SCREENSHOT_HEIGHT = 4000
SCREENSHOT_RETENTION_SECONDS = 3 * 24 * 60 * 60
MAX_SCREENSHOTS = 200
LIMITS = {
    "maxText": 15_000,
    "maxLinks": 60,
    "maxTables": 5,
    "maxRows": 30,
    "maxCols": 12,
    "maxCell": 200,
    "maxInputs": 30,
}

LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--dns-prefetch-disable",
    # WebRTC could otherwise send UDP straight to internal addresses, around the proxy.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]
CONTEXT_OPTIONS: dict[str, Any] = {
    "locale": "ja-JP",
    "timezone_id": "Asia/Tokyo",
    "viewport": VIEWPORT,
    "accept_downloads": False,
    "service_workers": "block",
}

SNAPSHOT_JS = """
(args) => {
  const root = args.selector ? document.querySelector(args.selector) : document.body;
  if (!root) return null;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const full = (root.innerText || '').replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
  const links = [];
  const seen = new Set();
  for (const a of root.querySelectorAll('a[href]')) {
    if (links.length >= args.maxLinks) break;
    const url = a.href;
    const text = clean(a.innerText || a.getAttribute('aria-label') || a.title).slice(0, 120);
    if (!/^https?:/i.test(url) || !text || seen.has(url)) continue;
    seen.add(url);
    links.push({text, url});
  }
  const tables = [];
  for (const t of root.querySelectorAll('table')) {
    if (tables.length >= args.maxTables) break;
    const rows = [];
    for (const tr of t.rows) {
      if (rows.length >= args.maxRows) break;
      const cells = Array.from(tr.cells).slice(0, args.maxCols).map((c) => clean(c.innerText).slice(0, args.maxCell));
      if (cells.some((c) => c)) rows.push(cells);
    }
    if (rows.length) tables.push({caption: clean(t.caption ? t.caption.innerText : '').slice(0, 200), rows});
  }
  const inputs = [];
  for (const el of root.querySelectorAll('input, textarea, select')) {
    if (inputs.length >= args.maxInputs) break;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'password', 'file', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) continue;
    let selector = null;
    if (el.id) selector = '#' + CSS.escape(el.id);
    else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + el.name.replace(/["\\\\]/g, '\\\\$&') + '"]';
    if (!selector) continue;
    const label = (el.labels && el.labels[0] ? el.labels[0].innerText : '')
      || el.getAttribute('aria-label') || el.placeholder;
    inputs.push({selector, tag: el.tagName.toLowerCase(), type, label: clean(label).slice(0, 80)});
  }
  return {text: full.slice(0, args.maxText), text_truncated: full.length > args.maxText, links, tables, inputs};
}
"""

FIELD_JS = """
(el) => {
  const form = el.form || el.closest('form');
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    editable: !!el.isContentEditable,
    form_has_password: !!(form && form.querySelector('input[type=password]')),
  };
}
"""


class BrowserError(RuntimeError):
    """A browser failure with a message that is safe to show to the model and the user."""


def screenshot_dir(settings: Settings) -> Path:
    return settings.app_state_dir / "browser-screenshots"


def save_screenshot(directory: Path, shot_id: str, png: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{shot_id}.png").write_bytes(png)
    now = time.time()
    shots = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for index, path in enumerate(shots):
        if index >= MAX_SCREENSHOTS or now - path.stat().st_mtime > SCREENSHOT_RETENTION_SECONDS:
            path.unlink(missing_ok=True)


class BrowserService:
    def __init__(self, *, max_sessions: int = MAX_SESSIONS) -> None:
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_sessions)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._proxy: EgressProxy | None = None
        self._open = 0

    async def new_context(self) -> BrowserContext:
        try:
            await asyncio.wait_for(self._slots.acquire(), SLOT_WAIT_SECONDS)
        except TimeoutError:
            raise BrowserError("ほかのブラウザ操作が続いているため開けませんでした。少し待ってください") from None
        try:
            async with self._lock:
                browser = await self._browser_locked()
                context = await browser.new_context(**CONTEXT_OPTIONS)
                self._open += 1
                return context
        except BaseException:
            self._slots.release()
            raise

    async def release_context(self, context: BrowserContext) -> None:
        try:
            with contextlib.suppress(Exception):
                await context.close()
            async with self._lock:
                self._open = max(self._open - 1, 0)
                if self._open == 0:
                    await self._stop_locked()
        finally:
            self._slots.release()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _browser_locked(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        await self._stop_locked()
        from playwright.async_api import async_playwright

        try:
            self._proxy = EgressProxy()
            await self._proxy.start()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True, args=LAUNCH_ARGS, proxy={"server": f"http://127.0.0.1:{self._proxy.port}"}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not launch Chromium: %s", type(exc).__name__)
            await self._stop_locked()
            raise BrowserError(
                "ブラウザを起動できませんでした（Chromium がインストールされていない可能性があります）"
            ) from None
        return self._browser

    async def _stop_locked(self) -> None:
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        proxy, self._proxy = self._proxy, None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
        if proxy is not None:
            await proxy.stop()


def get_browser_service(ctx: AppContext) -> BrowserService:
    service = ctx.extras.get("browser_service")
    if service is None:
        service = ctx.extras["browser_service"] = BrowserService()
    return service


async def shutdown_browser(ctx: AppContext) -> None:
    service = ctx.extras.get("browser_service")
    if service is not None:
        await service.shutdown()


def _describe(error: PlaywrightError) -> str:
    first = (str(error).strip().splitlines() or [""])[0]
    return "ブラウザの操作に失敗しました: " + first[:300]


class BrowserSession:
    """The page one Copilot session works on. Closed at the end of every turn or automation run."""

    def __init__(
        self,
        service: BrowserService,
        masker: SecretMasker,
        screenshots: Path,
        *,
        on_use: Callable[[], None] | None = None,
    ) -> None:
        self._service = service
        self._masker = masker
        self._screenshots = screenshots
        self._on_use = on_use
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        context, self._context, self._page = self._context, None, None
        if context is not None:
            await self._service.release_context(context)

    async def run(self, action: Callable[[], Awaitable[dict]]) -> dict:
        try:
            async with self._lock:
                return await asyncio.wait_for(action(), TOOL_TIMEOUT_SECONDS)
        except BrowserError as exc:
            return {"error": str(exc)}
        except TimeoutError:
            return {"error": "ブラウザの操作が時間内に終わりませんでした"}
        except PlaywrightError as exc:
            return {"error": _describe(exc)}

    # -- page lifecycle ----------------------------------------------------------------------------------

    async def _new_page(self) -> Page:
        if self._context is None:
            context = await self._service.new_context()
            self._context = context
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            await context.route("**/*", self._route)
            context.on("page", self._adopt)
        # A fresh page per open: a blocked or failed page may still retry navigations and interrupt the next goto.
        for old in list(self._context.pages):
            with contextlib.suppress(PlaywrightError):
                await old.close()
        self._page = await self._context.new_page()
        return self._page

    def _adopt(self, page: Page) -> None:
        # Links with target=_blank open a new tab: continue on it, like a person would.
        self._page = page

    def _current(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise BrowserError("先に browser_open でページを開いてください")
        return self._page

    @staticmethod
    async def _route(route: Route) -> None:
        if route.request.resource_type == "media":
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    @staticmethod
    async def _settle(page: Page) -> None:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT_MS)

    async def _snapshot(self, page: Page, selector: str | None = None, *, status: int | None = None) -> dict:
        data = await page.evaluate(SNAPSHOT_JS, {**LIMITS, "selector": selector})
        if data is None:
            raise BrowserError(f"要素が見つかりませんでした: {selector}")
        result: dict[str, Any] = {"url": page.url, "title": await page.title()}
        if status is not None:
            result["status"] = status
        return result | data

    # -- actions -----------------------------------------------------------------------------------------

    async def open(self, url: str, wait_for_selector: str | None = None) -> dict:
        reason = await outbound_rejection(url, self._masker)
        if reason:
            raise BrowserError(reason)
        page = await self._new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise BrowserError(
                "ページを開けませんでした（接続できないか、接続先がブロックされました）: " + _describe(exc)
            ) from None
        await self._settle(page)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector)
        if self._on_use is not None:
            self._on_use()
        return await self._snapshot(page, status=response.status if response is not None else None)

    async def read(self, selector: str | None = None) -> dict:
        return await self._snapshot(self._current(), selector)

    async def click(self, selector: str | None = None, text: str | None = None) -> dict:
        page = self._current()
        if selector:
            target = page.locator(selector).first
        elif text:
            target = page.get_by_text(text).first
        else:
            raise BrowserError("selector か text のどちらかを指定してください")
        await target.click()
        await self._settle(self._current())
        return await self._snapshot(self._current())

    async def fill(self, selector: str, value: str, submit: bool = False) -> dict:
        page = self._current()
        target = page.locator(selector).first
        attrs = await target.evaluate(FIELD_JS)
        reason = fill_rejection(value, attrs, self._masker)
        if reason:
            raise BrowserError(reason)
        if attrs.get("tag") == "select":
            try:
                await target.select_option(label=value)
            except PlaywrightError:
                await target.select_option(value=value)
        else:
            await target.fill(value)
        if submit:
            await target.press("Enter")
            await self._settle(self._current())
        return await self._snapshot(self._current())

    async def scroll(self, times: int = 1, wait_for_selector: str | None = None) -> dict:
        page = self._current()
        for _ in range(times):
            await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
            with contextlib.suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT_MS)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector)
        return await self._snapshot(page)

    async def screenshot(self, full_page: bool = False) -> dict:
        page = self._current()
        if full_page:
            height = int(await page.evaluate("() => document.documentElement.scrollHeight") or VIEWPORT["height"])
            clip = {"x": 0, "y": 0, "width": VIEWPORT["width"], "height": min(height, MAX_SCREENSHOT_HEIGHT)}
            png = await page.screenshot(type="png", full_page=True, clip=clip)
        else:
            png = await page.screenshot(type="png")
        shot_id = uuid.uuid4().hex
        await asyncio.to_thread(save_screenshot, self._screenshots, shot_id, png)
        return {
            "summary": "スクリーンショットを利用者の画面に表示しました（画像はあなたには渡りません）",
            "url": page.url,
            "title": await page.title(),
            "screenshot": {"id": shot_id},
        }
