"""Shared headless Chromium and the per-Copilot-session browser page.

One Chromium process per app process, started on first use and stopped when the last page closes (the container has
1 CPU / 2 GiB). All traffic goes through ``EgressProxy``, so only public http(s) destinations are reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError

from ..netguard import outbound_rejection
from ..security import SecretMasker
from .guard import FILLABLE_TAGS, fill_rejection
from .proxy import EgressProxy

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Route

    from ..config import Settings
    from ..context import AppContext

logger = logging.getLogger(__name__)

MAX_SESSIONS = 2
SLOT_WAIT_SECONDS = 30
TOOL_TIMEOUT_SECONDS = 60
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
IDLE_WAIT_MS = 3_000
POPUP_WAIT_MS = 500
VIEWPORT = {"width": 1280, "height": 800}
MAX_SCREENSHOT_HEIGHT = 4000
SCREENSHOT_RETENTION_SECONDS = 3 * 24 * 60 * 60
MAX_SCREENSHOTS = 200
# The result is handed to the model as UTF-8 JSON (``result_json``), so this budget is spent on the characters the
# page actually shows: a full ``maxText`` Japanese body (about 90 KB) still leaves room for links and tables.
MAX_RESULT_BYTES = 128 * 1024
LIMITS = {
    "maxText": 30_000,
    "maxLinks": 120,
    "maxTables": 10,
    "maxRows": 60,
    "maxCols": 16,
    "maxCell": 200,
    "maxInputs": 60,
    "maxUrl": 2048,
    "maxTitle": 500,
    "maxSelector": 1024,
}
# String lengths and collection sizes are checked again outside the untrusted page, before JSON serialization.
RESULT_SCHEMA = {
    "url": LIMITS["maxUrl"],
    "title": LIMITS["maxTitle"],
    "status": int,
    "text": LIMITS["maxText"],
    "text_truncated": bool,
    "result_truncated": bool,
    "links": (LIMITS["maxLinks"], {"text": 120, "url": LIMITS["maxUrl"]}),
    "tables": (
        LIMITS["maxTables"],
        {"caption": 200, "rows": (LIMITS["maxRows"], (LIMITS["maxCols"], LIMITS["maxCell"]))},
    ),
    "inputs": (LIMITS["maxInputs"], {"selector": LIMITS["maxSelector"], "tag": 20, "type": 32, "label": 80}),
    "summary": 1000,
    "error": 1000,
    "screenshot": {"id": 32},
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

# Content for the model. It runs in the page's own world, so it is only what the page chose to show: what may be
# typed in is decided by _field_attrs, which does not rely on the page's scripts.
SNAPSHOT_JS = """
(args) => {
  const root = args.selector ? document.querySelector(args.selector) : document.body;
  if (!root) return null;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const full = (root.innerText || '').replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
  const title = document.title || '';
  let truncated = title.length > args.maxTitle;
  const links = [];
  const seen = new Set();
  for (const a of root.querySelectorAll('a[href]')) {
    if (links.length > args.maxLinks) break;
    const url = a.href;
    if (url.length > args.maxUrl) { truncated = true; continue; }
    const text = clean(a.innerText || a.getAttribute('aria-label') || a.title).slice(0, 120);
    if (!/^https?:/i.test(url) || !text || seen.has(url)) continue;
    seen.add(url);
    links.push({text, url});
  }
  const tables = [];
  for (const t of root.querySelectorAll('table')) {
    if (tables.length > args.maxTables) break;
    const rows = [];
    for (const tr of t.rows) {
      if (rows.length > args.maxRows) break;
      const cols = Array.from(tr.cells).slice(0, args.maxCols + 1);
      const cells = cols.map((c) => clean(c.innerText).slice(0, args.maxCell));
      if (cells.some((c) => c)) rows.push(cells);
    }
    if (rows.length) tables.push({caption: clean(t.caption ? t.caption.innerText : '').slice(0, 200), rows});
  }
  const inputs = [];
  for (const el of root.querySelectorAll('input, textarea, select')) {
    if (inputs.length > args.maxInputs) break;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'password', 'file', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) continue;
    let selector = null;
    if (el.id.length > args.maxSelector || el.name.length > args.maxSelector) { truncated = true; continue; }
    if (el.id) selector = '#' + CSS.escape(el.id);
    else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + el.name.replace(/["\\\\]/g, '\\\\$&') + '"]';
    if (!selector) continue;
    if (selector.length > args.maxSelector) { truncated = true; continue; }
    const label = (el.labels && el.labels[0] ? el.labels[0].innerText : '')
      || el.getAttribute('aria-label') || el.placeholder;
    inputs.push({selector, tag: el.tagName.toLowerCase().slice(0, 20), type: type.slice(0, 32),
      label: clean(label).slice(0, 80)});
  }
  return {title: title.slice(0, args.maxTitle), text: full.slice(0, args.maxText),
    text_truncated: full.length > args.maxText, result_truncated: truncated, links, tables, inputs};
}
"""

# Selectors handed to Playwright's engine instead of reading the element with the page's own DOM functions.
FIELD_SELECTORS = {
    # The element itself is content-editable, or it sits inside one (isContentEditable without page JavaScript).
    "editable": ", ".join(
        f"[contenteditable={value}]{suffix}" for value in ('""', '"true"', '"plaintext-only"') for suffix in ("", " *")
    ),
    # Login and sign-up forms: the field sits in a form that also holds a password box.
    "password_form": "form:has(input[type=password]) *",
    "password_form_root": "form:has(input[type=password])",
    # A password box may sit outside its form and name the owner with the form attribute.
    "detached_password": "input[type=password][form]",
    "owner_form": "xpath=ancestor::form[1]",
}
MAX_CHECKED_ELEMENTS = 50


class BrowserError(RuntimeError):
    """A browser failure with a message that is safe to show to the model and the user."""


def result_json(result: dict) -> str:
    """The exact text a browser tool hands to the model: compact JSON with the page's characters left as they are.

    The SDK JSON-serializes a dict result with ``ensure_ascii`` on, which turns every Japanese character into a
    six-character ``\\uXXXX`` escape: the model would then receive about a sixth of the page for the same budget.
    Serializing here instead keeps the text readable (in the chat's tool card too) and is what ``limit_result``
    measures. A page may hold a lone surrogate, which is not encodable as UTF-8, so it is escaped back into JSON's
    own ``\\uXXXX`` form and the result is always valid UTF-8.
    """
    text = json.dumps(result, ensure_ascii=False)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def limit_result(result: dict) -> dict:
    """Bound known fields first, then trim the serialized result, including JSON escaping and truncation flags."""
    truncated = False

    def bound(value: Any, schema: Any) -> Any:
        nonlocal truncated
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                truncated = True
                return {}
            output = {key: bound(value[key], child) for key, child in schema.items() if key in value}
            truncated |= len(output) != len(value)
            return output
        if isinstance(schema, tuple):
            if not isinstance(value, list):
                truncated = True
                return []
            count, child = schema
            truncated |= len(value) > count
            return [bound(item, child) for item in value[:count]]
        if isinstance(schema, int):
            if not isinstance(value, str):
                truncated = True
                return ""
            truncated |= len(value) > schema
            return value[:schema]
        if type(value) is schema:
            return value
        truncated = True
        return None

    output = bound(result, RESULT_SCHEMA)
    if "text" in output and output["text"] != result["text"]:
        output["text_truncated"] = True
    if truncated:
        output["result_truncated"] = True

    # Measured on the serialization the model is given, including JSON escaping.
    def size() -> int:
        return len(result_json(output).encode("utf-8"))

    if size() > MAX_RESULT_BYTES:
        output["result_truncated"] = True
        for key in ("tables", "links", "inputs", "text"):
            while output.get(key) and size() > MAX_RESULT_BYTES:
                output[key] = output[key][: len(output[key]) // 2]
                if key == "text":
                    output["text_truncated"] = True
        # The bounded metadata alone fits comfortably, even with every character JSON-escaped.
    return output


def screenshot_dir(settings: Settings) -> Path:
    return settings.app_state_dir / "browser-screenshots"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_expired(path: Path) -> bool:
    mtime = _mtime(path)
    return mtime is None or time.time() - mtime > SCREENSHOT_RETENTION_SECONDS


def prune_screenshots(directory: Path) -> None:
    """Drops screenshots past the retention period or the count limit. Also run at start-up and before serving one."""
    if not directory.is_dir():
        return
    shots = sorted(directory.glob("*.png"), key=lambda p: _mtime(p) or 0.0, reverse=True)
    for index, path in enumerate(shots):
        if index >= MAX_SCREENSHOTS or is_expired(path):
            path.unlink(missing_ok=True)


def save_screenshot(directory: Path, shot_id: str, png: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{shot_id}.png").write_bytes(png)
    prune_screenshots(directory)


class BrowserService:
    def __init__(self, masker: SecretMasker | None = None, *, max_sessions: int = MAX_SESSIONS) -> None:
        self._masker = masker
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
            self._proxy = EgressProxy(self._masker)
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
        service = ctx.extras["browser_service"] = BrowserService(ctx.masker)
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
                result = await asyncio.wait_for(action(), TOOL_TIMEOUT_SECONDS)
        except BrowserError as exc:
            result = {"error": str(exc)}
        except TimeoutError:
            result = {"error": "ブラウザの操作が時間内に終わりませんでした"}
        except PlaywrightError as exc:
            result = {"error": _describe(exc)}
        return limit_result(result)

    async def run_json(self, action: Callable[[], Awaitable[dict]]) -> str:
        """``run`` as the text the tools return: the bounded result, serialized as UTF-8 JSON (``result_json``)."""
        return result_json(await self.run(action))

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
        # Links with target=_blank open a new tab: continue on it, like a person would. The page it came from and
        # any extra popups are closed by _close_others, so one session never holds more than one page.
        self._page = page

    def _current(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise BrowserError("先に browser_open でページを開いてください")
        return self._page

    async def _active(self) -> Page:
        page = self._current()
        await self._close_others(page)
        await self._guard_url(page)
        return page

    async def _close_others(self, keep: Page) -> None:
        """Keeps one page only: an adopted popup replaces the page it came from, so nothing piles up in memory."""
        if self._context is None:
            return
        for page in list(self._context.pages):
            if page is not keep:
                with contextlib.suppress(PlaywrightError):
                    await page.close()

    async def _guard_url(self, page: Page) -> None:
        """Checks the URL the page actually shows: a redirect, a click or a popup may have left the allowed sites."""
        url = page.url
        if not url or url == "about:blank":
            return
        reason = await outbound_rejection(url, self._masker)
        if reason is None:
            return
        with contextlib.suppress(PlaywrightError):
            await page.goto("about:blank")
        raise BrowserError(f"移動先がブロックされました: {reason}")

    @staticmethod
    async def _matches(page: Page, target: Locator, css: str) -> bool:
        """Asks Playwright's own selector engine, which runs apart from the page's scripts and cannot be faked."""
        return await target.and_(page.locator(css)).count() > 0

    async def _field_attrs(self, page: Page, target: Locator) -> dict[str, Any]:
        """Describes the field for ``fill_rejection`` without running any JavaScript in the page's world."""
        tag = ""
        for name in FILLABLE_TAGS:
            if await self._matches(page, target, name):
                tag = name
                break
        form_has_password = await self._matches(page, target, FIELD_SELECTORS["password_form"])
        if not form_has_password:
            # The password box may sit outside the form and name its owner with the form attribute.
            owner_id = await self._owner_form_id(target)
            if owner_id:
                form_has_password = await self._owned_by_login_form(page, owner_id)
        return {
            "tag": tag,
            "type": (await target.get_attribute("type")) or "",
            "autocomplete": (await target.get_attribute("autocomplete")) or "",
            "editable": await self._matches(page, target, FIELD_SELECTORS["editable"]),
            "form_has_password": form_has_password,
        }

    @staticmethod
    async def _owner_form_id(target: Locator) -> str | None:
        """The form the field belongs to: the form attribute wins, otherwise the closest enclosing form."""
        form_id = await target.get_attribute("form")
        if form_id:
            return form_id
        owner = target.locator(FIELD_SELECTORS["owner_form"]).first
        return await owner.get_attribute("id") if await owner.count() else None

    @classmethod
    async def _owned_by_login_form(cls, page: Page, form_id: str) -> bool:
        """Looks for a password box owned by that form, inside it or attached from elsewhere by the form attribute."""
        if await cls._attr_equals(page, FIELD_SELECTORS["password_form_root"], "id", form_id):
            return True
        return await cls._attr_equals(page, FIELD_SELECTORS["detached_password"], "form", form_id)

    @staticmethod
    async def _attr_equals(page: Page, css: str, attribute: str, value: str) -> bool:
        """Compares the values in Python: an id may hold characters that cannot be put into a CSS selector safely."""
        elements = page.locator(css)
        total = await elements.count()
        if total > MAX_CHECKED_ELEMENTS:
            return True  # too many to look at one by one: refuse rather than guess
        for index in range(total):
            if await elements.nth(index).get_attribute(attribute) == value:
                return True
        return False

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

    async def _settle_after_action(self, opener: Page) -> Page:
        """Gives a new tab a moment to arrive, then settles the page actually in use."""
        if self._context is not None and self._page is opener and len(self._context.pages) <= 1:
            with contextlib.suppress(PlaywrightError):
                await self._context.wait_for_event("page", timeout=POPUP_WAIT_MS)
        page = self._current()
        await self._settle(page)
        return page

    async def _snapshot(self, page: Page, selector: str | None = None, *, status: int | None = None) -> dict:
        await self._close_others(page)
        await self._guard_url(page)
        data = await page.evaluate(SNAPSHOT_JS, {**LIMITS, "selector": selector})
        if data is None:
            raise BrowserError(f"要素が見つかりませんでした: {selector}")
        # The page may have navigated while it was read: nothing from a rejected destination may be returned.
        await self._guard_url(page)
        result: dict[str, Any] = {"url": page.url}
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
        # A redirect may have landed somewhere that must not be shown: check before reading or waiting.
        await self._guard_url(page)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector)
        if self._on_use is not None:
            self._on_use()
        return await self._snapshot(page, status=response.status if response is not None else None)

    async def read(self, selector: str | None = None) -> dict:
        return await self._snapshot(await self._active(), selector)

    async def click(self, selector: str | None = None, text: str | None = None) -> dict:
        page = await self._active()
        if selector:
            target = page.locator(selector).first
        elif text:
            target = page.get_by_text(text).first
        else:
            raise BrowserError("selector か text のどちらかを指定してください")
        await target.click()
        return await self._snapshot(await self._settle_after_action(page))

    async def fill(self, selector: str, value: str, submit: bool = False) -> dict:
        page = await self._active()
        target = page.locator(selector).first
        attrs = await self._field_attrs(page, target)
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
            page = await self._settle_after_action(page)
        return await self._snapshot(page)

    async def scroll(self, times: int = 1, wait_for_selector: str | None = None) -> dict:
        page = await self._active()
        for _ in range(times):
            await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
            with contextlib.suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=IDLE_WAIT_MS)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector)
        return await self._snapshot(page)

    async def screenshot(self, full_page: bool = False) -> dict:
        page = await self._active()
        if full_page:
            height = int(await page.evaluate("() => document.documentElement.scrollHeight") or VIEWPORT["height"])
            # clip and full_page cannot be given together: the clip alone reaches past the viewport, up to the limit.
            clip = {"x": 0, "y": 0, "width": VIEWPORT["width"], "height": min(height, MAX_SCREENSHOT_HEIGHT)}
            png = await page.screenshot(type="png", clip=clip)
        else:
            png = await page.screenshot(type="png")
        # The page may have navigated while it was captured: a rejected destination is not kept or shown.
        await self._guard_url(page)
        shot_id = uuid.uuid4().hex
        await asyncio.to_thread(save_screenshot, self._screenshots, shot_id, png)
        return {
            "summary": "スクリーンショットを利用者の画面に表示しました（画像はあなたには渡りません）",
            "url": page.url,
            "title": await page.evaluate("(limit) => (document.title || '').slice(0, limit)", LIMITS["maxTitle"]),
            "screenshot": {"id": shot_id},
        }
