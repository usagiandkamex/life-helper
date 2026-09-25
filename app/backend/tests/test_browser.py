from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from life_helper.browser import proxy as proxy_module
from life_helper.browser.guard import fill_rejection
from life_helper.browser.proxy import EgressProxy
from life_helper.browser.service import BrowserError, BrowserService, BrowserSession, save_screenshot, screenshot_dir
from life_helper.copilot_integration.events import extract_screenshot
from life_helper.copilot_integration.manager import ActiveSession
from life_helper.copilot_integration.system_prompt import build_system_message
from life_helper.security import SecretMasker

from .conftest import sign_in

MASKER = SecretMasker(["SUPERSECRETKEY"])
SHOT_ID = "0123456789abcdef0123456789abcdef"


# -- form input -------------------------------------------------------------------------------------------


def field(**overrides):
    return {"tag": "input", "type": "search", "autocomplete": "", "editable": False, "form_has_password": False} | (
        overrides
    )


def test_fill_allows_search_terms():
    assert fill_rejection("ふるさと納税 上限", field(), MASKER) is None
    assert fill_rejection("東京都", field(tag="select", type=""), MASKER) is None
    assert fill_rejection("メモ", field(tag="div", type="", editable=True), MASKER) is None


@pytest.mark.parametrize(
    ("value", "attrs"),
    [
        ("x" * 501, field()),
        ("abc", field(tag="button", type="")),
        ("abc", field(type="password")),
        ("abc", field(type="file")),
        ("abc", field(type="hidden")),
        ("abc", field(autocomplete="cc-number")),
        ("abc", field(autocomplete="new-password")),
        ("123456", field(autocomplete="one-time-code")),
        ("abc", field(form_has_password=True)),
        ("4111 1111 1111 1111", field()),
        ("口座番号: 1234567", field()),
        ("key SUPERSECRETKEY", field()),
    ],
)
def test_fill_rejects_credentials_and_sensitive_data(value, attrs):
    assert fill_rejection(value, attrs, MASKER) is not None


# -- egress proxy -----------------------------------------------------------------------------------------


async def start_origin(requests: list[bytes]) -> tuple[asyncio.Server, int]:
    """A tiny HTTP server on 127.0.0.1 that records request heads (stands in for a public site or an internal one)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def exchange(port: int, data: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(65536), 5)
    finally:
        writer.close()


@pytest.fixture
async def egress():
    p = EgressProxy()
    await p.start()
    yield p
    await p.stop()


async def test_proxy_blocks_internal_and_connector_destinations(egress, fake_dns):
    requests: list[bytes] = []
    origin, origin_port = await start_origin(requests)
    fake_dns["internal.example.com"] = ["10.0.0.8"]
    try:
        for head in (
            f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            f"GET http://localhost:{origin_port}/ HTTP/1.1\r\nHost: localhost\r\n\r\n",
            f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            "CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
            "CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n",
            "CONNECT internal.example.com:443 HTTP/1.1\r\n\r\n",
            "CONNECT api.github.com:443 HTTP/1.1\r\n\r\n",
        ):
            answer = await exchange(egress.port, head.encode())
            assert answer.startswith(b"HTTP/1.1 403"), head
        assert (await exchange(egress.port, b"GET ftp://example.com/ HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 400")
    finally:
        origin.close()
    assert requests == []


async def test_proxy_forwards_vetted_http_and_tunnels(egress, monkeypatch):
    requests: list[bytes] = []
    origin, origin_port = await start_origin(requests)

    async def vetted(host: str):
        assert host == "public.example.com"
        return ["127.0.0.1"], None

    monkeypatch.setattr(proxy_module, "resolve_public", vetted)
    try:
        answer = await exchange(
            egress.port,
            (
                f"GET http://public.example.com:{origin_port}/a?b=1 HTTP/1.1\r\nHost: public.example.com\r\n"
                "Proxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n"
            ).encode(),
        )
        assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"hello")
        head = requests[-1].decode()
        assert head.startswith("GET /a?b=1 HTTP/1.1\r\n")
        assert "Host: public.example.com" in head and "Connection: close" in head
        assert "Proxy-Connection" not in head

        reader, writer = await asyncio.open_connection("127.0.0.1", egress.port)
        writer.write(f"CONNECT public.example.com:{origin_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"GET /tunnel HTTP/1.1\r\nHost: public.example.com\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(65536), 5)).endswith(b"hello")
        writer.close()
        assert requests[-1].startswith(b"GET /tunnel HTTP/1.1")
    finally:
        origin.close()


# -- session without Chromium -----------------------------------------------------------------------------


class NoBrowser(BrowserService):
    async def new_context(self):  # pragma: no cover - must not be reached
        raise AssertionError("Chromium must not start for rejected requests")


async def test_session_rejects_before_launching(tmp_path, fake_dns):
    session = BrowserSession(NoBrowser(), MASKER, tmp_path)
    fake_dns["internal.example.com"] = ["192.168.1.10"]
    for url in (
        "http://127.0.0.1:8000/healthz",
        "https://internal.example.com/",
        "file:///etc/passwd",
        "https://openapi.rakuten.co.jp/engine/api",
        "https://example.com/?card=4111111111111111",
        "https://example.com/?k=SUPERSECRETKEY",
    ):
        result = await session.run(lambda url=url: session.open(url))
        assert "error" in result, url
        assert "SUPERSECRETKEY" not in result["error"] and "4111111111111111" not in result["error"]
    assert "browser_open" in (await session.run(session.read))["error"]
    assert "browser_open" in (await session.run(lambda: session.click(text="次へ")))["error"]


async def test_active_session_release_runs_every_releaser():
    calls: list[str] = []

    async def ok():
        calls.append("ok")

    async def broken():
        calls.append("broken")
        raise RuntimeError("boom")

    active = ActiveSession(session=None, policy=None, model="m", releasers=[broken, ok])  # type: ignore[arg-type]
    await active.release()
    assert calls == ["broken", "ok"]


def test_screenshots_are_pruned(tmp_path):
    old = tmp_path / f"{'a' * 32}.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    os.utime(old, (0, 0))
    save_screenshot(tmp_path, SHOT_ID, b"png")
    assert (tmp_path / f"{SHOT_ID}.png").read_bytes() == b"png"
    assert not old.exists()


# -- wiring -----------------------------------------------------------------------------------------------


def test_browser_tools_follow_settings_and_automation_connectors(ctx, settings):
    from life_helper.tools.registry import build_tools

    names = {"browser_open", "browser_read", "browser_click", "browser_fill", "browser_scroll", "browser_screenshot"}
    specs = [s for s in build_tools(ctx) if s.tool.name.startswith("browser_")]
    assert {s.tool.name for s in specs} == names
    assert len({s.release for s in specs}) == 1  # one page per session, closed once
    assert not any(s.tool.name.startswith("browser_") for s in build_tools(ctx, connectors=[]))
    assert names <= {s.tool.name for s in build_tools(ctx, connectors=["browser"])}
    settings.browser_enabled = False
    ctx.extras.pop("connectors", None)
    assert not any(s.tool.name.startswith("browser_") for s in build_tools(ctx))


def test_browser_is_listed_as_a_connector(client, ctx):
    sign_in(client, ctx)
    browser = next(c for c in client.get("/api/connectors").json() if c["name"] == "browser")
    assert browser["configured"] is True and browser["label"]


def test_system_message_mentions_browser_rules_only_with_the_tools(tmp_path):
    assert "browser_open" in build_system_message(tmp_path, browser=True)
    assert "browser_open" not in build_system_message(tmp_path)


def test_extract_screenshot():
    assert extract_screenshot({"screenshot": {"id": SHOT_ID}}) == {"url": f"/api/browser/screenshots/{SHOT_ID}"}
    assert extract_screenshot(f'{{"screenshot": {{"id": "{SHOT_ID}"}}}}') is not None
    for bad in ({"screenshot": {"id": "../../etc/passwd"}}, {"screenshot": "x"}, "not json", None, {"chart": {}}):
        assert extract_screenshot(bad) is None


def test_screenshot_api_requires_sign_in_and_a_valid_id(client, ctx):
    directory = screenshot_dir(ctx.settings)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{SHOT_ID}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (directory.parent / "secret.png").write_bytes(b"secret")
    assert client.get(f"/api/browser/screenshots/{SHOT_ID}").status_code == 401
    sign_in(client, ctx)
    resp = client.get(f"/api/browser/screenshots/{SHOT_ID}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG")
    for bad in ("f" * 32, "SECRET", "..%2Fsecret", "%2E%2E%2Fsecret"):
        assert client.get(f"/api/browser/screenshots/{bad}").status_code == 404, bad


# -- real Chromium (skipped when it is not installed) -----------------------------------------------------


def _chromium_installed() -> bool:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root and root != "0":
        base = Path(root)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        base = Path.home() / ".cache" / "ms-playwright"
    return any(base.glob("chromium_headless_shell-*")) or any(base.glob("chromium-*"))


PAGE = """<!doctype html><html><head><title>テストのページ</title></head><body>
<h1>ふるさと納税</h1><p>上限の目安を調べます。</p>
<a href="https://site.test/next">次のページ</a>
<table><caption>控除</caption><tr><th>収入</th><th>控除額</th></tr><tr><td>200万円</td><td>68万円</td></tr></table>
<form action="https://site.test/search"><label for="q">検索</label><input id="q" name="q" type="search"></form>
<form><input name="user"><input name="pw" type="password"></form>
<div id="more"></div><button onclick="document.getElementById('more').textContent='続きの内容'">もっと見る</button>
</body></html>"""


@pytest.mark.skipif(not _chromium_installed(), reason="Chromium for Playwright is not installed")
async def test_browser_session_end_to_end(tmp_path, monkeypatch):
    requests: list[bytes] = []
    origin, origin_port = await start_origin(requests)

    async def site(route):
        url = route.request.url
        if url.startswith("https://site.test/redirect"):
            await route.fulfill(status=302, headers={"Location": f"http://127.0.0.1:{origin_port}/secret"})
        elif url.startswith("https://site.test/"):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=PAGE)
        else:
            await route.continue_()

    monkeypatch.setattr(BrowserSession, "_route", staticmethod(site))
    service = BrowserService()
    used: list[bool] = []
    session = BrowserSession(service, MASKER, tmp_path / "shots", on_use=lambda: used.append(True))
    try:
        page = await session.run(lambda: session.open("https://site.test/"))
        if "起動できませんでした" in page.get("error", ""):
            pytest.skip("Chromium could not be launched")
        assert page["status"] == 200 and page["title"] == "テストのページ", page
        assert "上限の目安" in page["text"] and used == [True]
        assert {"text": "次のページ", "url": "https://site.test/next"} in page["links"]
        assert page["tables"][0]["rows"][1] == ["200万円", "68万円"]
        assert {"selector": "#q", "tag": "input", "type": "search", "label": "検索"} in page["inputs"]
        assert all(i["type"] != "password" for i in page["inputs"])

        clicked = await session.run(lambda: session.click(text="もっと見る"))
        assert "続きの内容" in clicked["text"]
        filled = await session.run(lambda: session.fill("#q", "ふるさと納税"))
        assert "error" not in filled
        refused = await session.run(lambda: session.fill('input[name="user"]', "taro"))
        assert "ログイン" in refused["error"]
        assert "error" not in await session.run(lambda: session.scroll(2))

        shot = await session.run(lambda: session.screenshot(full_page=True))
        assert (tmp_path / "shots" / f"{shot['screenshot']['id']}.png").stat().st_size > 0

        # Redirect hops never reach Playwright's routing: the proxy must stop them.
        redirected = await session.run(lambda: session.open("https://site.test/redirect"))
        assert redirected.get("status") == 403 or "error" in redirected
        direct = await session._current().goto(f"http://127.0.0.1:{origin_port}/direct")
        assert direct is not None and direct.status == 403
        assert requests == []
    finally:
        await session.close()
        origin.close()
    assert service._browser is None  # the last page closed, so Chromium stopped


async def test_browser_error_is_reported_when_chromium_cannot_start(tmp_path, monkeypatch):
    service = BrowserService(max_sessions=1)

    async def fail(self):
        raise BrowserError("ブラウザを起動できませんでした")

    monkeypatch.setattr(BrowserService, "_browser_locked", fail)
    session = BrowserSession(service, MASKER, tmp_path)
    # The second call only gets a slot if the failed first call gave its slot back.
    for _ in range(2):
        assert "起動できませんでした" in (await session.run(lambda: session.open("https://example.com/")))["error"]
