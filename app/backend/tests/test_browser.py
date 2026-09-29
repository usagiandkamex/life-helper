from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from life_helper.browser import proxy as proxy_module
from life_helper.browser.guard import fill_rejection
from life_helper.browser.proxy import EgressProxy
from life_helper.browser.service import (
    LIMITS,
    MAX_RESULT_BYTES,
    SNAPSHOT_JS,
    BrowserError,
    BrowserService,
    BrowserSession,
    limit_result,
    prune_screenshots,
    result_json,
    save_screenshot,
    screenshot_dir,
)
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
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: keep-alive\r\n\r\nhello")
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
    p = EgressProxy(MASKER)
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
        # The response must not invite Chromium to reuse this upstream for a request to another host.
        assert b"keep-alive" not in answer and b"Proxy-Connection: close" in answer
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


async def test_proxy_blocks_credentials_and_sensitive_data_in_http_urls(egress):
    """Page requests and redirects reach the proxy directly, so the whole URL is checked there too."""
    requests: list[bytes] = []
    origin, origin_port = await start_origin(requests)
    host = f"public.example.com:{origin_port}"
    try:
        for head in (
            f"GET http://taro:pw@{host}/ HTTP/1.1\r\nHost: {host}\r\n\r\n",
            f"GET http://{host}/?k=SUPERSECRETKEY HTTP/1.1\r\nHost: {host}\r\n\r\n",
            f"GET http://{host}/?k=SUPER%53ECRETKEY HTTP/1.1\r\nHost: {host}\r\n\r\n",
            f"GET http://{host}/?card=4111111111111111 HTTP/1.1\r\nHost: {host}\r\n\r\n",
        ):
            answer = await exchange(egress.port, head.encode())
            assert answer.startswith(b"HTTP/1.1 403"), head
            assert b"SUPERSECRETKEY" not in answer and b"4111111111111111" not in answer
    finally:
        origin.close()
    assert requests == []


@pytest.fixture
async def websocket_origin():
    requests: list[bytes] = []
    messages: list[bytes] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head)
            headers = dict(line.lower().split(b":", 1) for line in head.split(b"\r\n")[1:] if b":" in line)
            if b"upgrade" not in headers.get(b"connection", b""):
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            key = next(
                line.split(b":", 1)[1].strip()
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11", usedforsecurity=False).digest()
            )
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: "
                + accept
                + b"\r\n\r\n"
            )
            await writer.drain()
            # Echo one small masked text frame, enough to exercise both directions of the upgraded connection.
            opcode, length = await reader.readexactly(2)
            assert opcode == 0x81 and 0x80 <= length < 0xFE
            mask = await reader.readexactly(4)
            data = await reader.readexactly(length & 0x7F)
            message = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
            messages.append(message)
            writer.write(bytes([0x81, len(message)]) + message)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], requests, messages
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("scheme", ["http", "ws"])
async def test_proxy_relays_websocket_upgrade_and_frames(egress, websocket_origin, monkeypatch, scheme):
    port, requests, messages = websocket_origin

    async def vetted(host):
        assert host == "public.example.com"
        return ["127.0.0.1"], None

    monkeypatch.setattr(proxy_module, "resolve_public", vetted)
    reader, writer = await asyncio.open_connection("127.0.0.1", egress.port)
    try:
        writer.write(
            f"GET {scheme}://public.example.com:{port}/socket?q=test HTTP/1.1\r\nHost: public.example.com\r\n"
            "Connection: keep-alive, UpGrAdE\r\nUpgrade: WebSocket\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n".encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 101")
        assert b"Connection: Upgrade\r\n" in head and b"Connection: close" not in head
        writer.write(b"\x81\x82\x00\x00\x00\x00hi")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"\x81\x02hi"
        assert messages == [b"hi"]
        assert requests[0].startswith(b"GET /socket?q=test HTTP/1.1")
        assert b"Connection: Upgrade\r\n" in requests[0] and b"keep-alive" not in requests[0]
    finally:
        writer.close()


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1/",
        "ws://api.github.com/",
        "ws://internal.example.com/",
        "ws://public.example.com/?k=SUPER%53ECRETKEY",
        "ws://public.example.com/?card=4111111111111111",
    ],
)
async def test_proxy_checks_websocket_urls_before_connecting(egress, fake_dns, monkeypatch, url):
    fake_dns["internal.example.com"] = ["10.0.0.8"]

    async def unexpected_connect(*args):
        raise AssertionError("blocked WebSockets must not connect upstream")

    monkeypatch.setattr(egress, "_connect", unexpected_connect)
    head = f"GET {url} HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n".encode()
    assert (await exchange(egress.port, head)).startswith(b"HTTP/1.1 403")


async def test_failed_websocket_upgrade_still_closes_http_connection(egress, monkeypatch):
    requests = []
    origin, port = await start_origin(requests)

    async def vetted(host):
        return ["127.0.0.1"], None

    monkeypatch.setattr(proxy_module, "resolve_public", vetted)
    try:
        answer = await exchange(
            egress.port,
            f"GET http://public.example.com:{port}/ HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n".encode(),
        )
        assert answer.startswith(b"HTTP/1.1 200")
        assert b"Connection: close\r\n" in answer and b"Upgrade:" not in answer
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


@pytest.mark.parametrize("character", ["x", "漢", "😀", '\x00"\\', "\ud800"])
async def test_browser_results_have_a_serialized_byte_limit(tmp_path, character):
    value = character * (LIMITS["maxText"] * 2)
    raw = {
        "url": "https://example.com/" + value,
        "title": value,
        "status": 200,
        "text": value,
        "text_truncated": False,
        "links": [{"text": value, "url": value}] * 100,
        "tables": [{"caption": value, "rows": [[value] * 20] * 50}] * 10,
        "inputs": [{"selector": value, "tag": value, "type": value, "label": value}] * 50,
        "unexpected": value,
    }

    async def action():
        return raw

    session = BrowserSession(NoBrowser(), MASKER, tmp_path)
    result = await session.run(action)
    # The limit applies to the text the tools hand to the model, which is valid UTF-8 even for a lone surrogate.
    assert len(result_json(result).encode("utf-8")) <= MAX_RESULT_BYTES
    assert json.loads(await session.run_json(action)) == result
    assert result["result_truncated"] is True
    assert result["text_truncated"] is True
    assert result["status"] == 200 and "unexpected" not in result
    assert len(result["url"]) <= LIMITS["maxUrl"] and len(result["title"]) <= LIMITS["maxTitle"]
    assert len(result["text"]) <= LIMITS["maxText"]
    assert len(result["links"]) <= LIMITS["maxLinks"]
    assert all(len(link["url"]) <= LIMITS["maxUrl"] and len(link["text"]) <= 120 for link in result["links"])
    assert len(result["inputs"]) <= LIMITS["maxInputs"]
    assert all(len(item["selector"]) <= LIMITS["maxSelector"] for item in result["inputs"])


def test_results_keep_japanese_text_instead_of_ascii_escapes():
    """The SDK would escape a dict result as ``\\uXXXX`` (six characters per character): the tools serialize it."""
    page = {"url": "https://example.com/", "title": "ふるさと納税", "text": "上限の目安", "text_truncated": False}
    payload = result_json(limit_result(page))
    assert "ふるさと納税" in payload and "\\u" not in payload
    assert json.loads(payload) == page
    # A page may hold an unpaired surrogate: it is escaped so the payload stays encodable.
    assert result_json({"title": "\ud800"}).encode("utf-8") == b'{"title": "\\ud800"}'


def test_a_japanese_page_now_fits_without_losing_its_links_and_tables():
    page = {
        "url": "https://example.com/",
        "title": "ふるさと納税",
        "text": "あ" * LIMITS["maxText"],
        "text_truncated": False,
        "links": [{"text": f"リンク{i}", "url": f"https://example.com/{i}"} for i in range(LIMITS["maxLinks"])],
        "tables": [{"caption": "控除", "rows": [["収入", "控除額"]] * LIMITS["maxRows"]}] * LIMITS["maxTables"],
        "inputs": [{"selector": "#q", "tag": "input", "type": "search", "label": "検索"}],
    }
    assert limit_result(page) == page


def test_result_limits_preserve_small_results_and_reject_unexpected_shapes():
    result = {
        "url": "https://example.com/",
        "title": "調べ物",
        "text": "少しの内容",
        "text_truncated": False,
        "links": [{"text": "次へ", "url": "https://example.com/next"}],
        "tables": [{"caption": "表", "rows": [["a", "b"]]}],
        "inputs": [{"selector": "#q", "tag": "input", "type": "search", "label": "検索"}],
    }
    assert limit_result(result) == result
    result = limit_result({"title": ["x"] * 1000, "links": {"x": "y"}, "text_truncated": "x" * 1000})
    assert result == {"title": "", "links": [], "text_truncated": None, "result_truncated": True}


async def test_screenshot_metadata_and_errors_are_also_bounded(tmp_path):
    session = BrowserSession(NoBrowser(), MASKER, tmp_path)

    async def screenshot():
        return {"url": "x" * 100_000, "title": "y" * 100_000, "screenshot": {"id": SHOT_ID}, "summary": "撮影しました"}

    shot = await session.run(screenshot)
    assert len(result_json(shot).encode("utf-8")) <= MAX_RESULT_BYTES
    assert shot["screenshot"]["id"] == SHOT_ID and shot["result_truncated"]

    async def fail():
        raise BrowserError("x" * 100_000)

    failure = await session.run(fail)
    assert len(failure["error"]) == 1000 and failure["result_truncated"]


def test_screenshots_are_pruned(tmp_path):
    old = tmp_path / f"{'a' * 32}.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    os.utime(old, (0, 0))
    save_screenshot(tmp_path, SHOT_ID, b"png")
    assert (tmp_path / f"{SHOT_ID}.png").read_bytes() == b"png"
    assert not old.exists()


def test_expired_screenshots_are_pruned_without_a_new_one(tmp_path):
    """Nothing may be saved for days, so the retention period is also applied on its own (start-up, serving)."""
    shot = tmp_path / f"{SHOT_ID}.png"
    shot.write_bytes(b"png")
    os.utime(shot, (0, 0))
    prune_screenshots(tmp_path)
    assert not shot.exists()
    prune_screenshots(tmp_path / "missing")  # never fails when nothing has been saved yet


def test_startup_prunes_expired_screenshots(app, ctx):
    from fastapi.testclient import TestClient

    directory = screenshot_dir(ctx.settings)
    directory.mkdir(parents=True, exist_ok=True)
    expired = directory / f"{SHOT_ID}.png"
    expired.write_bytes(b"old")
    os.utime(expired, (0, 0))
    fresh = directory / f"{'a' * 32}.png"
    fresh.write_bytes(b"new")
    with TestClient(app):
        assert not expired.exists()
        assert fresh.read_bytes() == b"new"


async def test_blocked_redirect_targets_are_not_shown(tmp_path, fake_dns):
    """The proxy stops the request, but the page may still sit on the rejected URL: it must go back to blank."""

    class FakePage:
        url = "http://127.0.0.1:8000/secret"

        def is_closed(self) -> bool:
            return False

        async def goto(self, url: str, **kwargs) -> None:
            self.url = url

    session = BrowserSession(NoBrowser(), MASKER, tmp_path)
    page = FakePage()
    session._page = page  # type: ignore[assignment]
    result = await session.run(session.read)
    assert "移動先" in result["error"] and "127.0.0.1" in result["error"]
    assert page.url == "about:blank"
    await session._guard_url(page)  # type: ignore[arg-type]  # a blank page is not a destination


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
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content.startswith(b"\x89PNG")
    for bad in ("f" * 32, "SECRET", "..%2Fsecret", "%2E%2E%2Fsecret"):
        assert client.get(f"/api/browser/screenshots/{bad}").status_code == 404, bad
    client.cookies.clear()
    assert client.get(f"/api/browser/screenshots/{SHOT_ID}").status_code == 401


def test_screenshot_api_drops_screenshots_past_the_retention_period(client, ctx):
    directory = screenshot_dir(ctx.settings)
    directory.mkdir(parents=True, exist_ok=True)
    shot = directory / f"{SHOT_ID}.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.utime(shot, (0, 0))
    sign_in(client, ctx)
    assert client.get(f"/api/browser/screenshots/{SHOT_ID}").status_code == 404
    assert not shot.exists()


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
<a href="https://site.test/popup" target="_blank">別のタブで開く</a>
<table><caption>控除</caption><tr><th>収入</th><th>控除額</th></tr><tr><td>200万円</td><td>68万円</td></tr></table>
<form action="https://site.test/search"><label for="q">検索</label><input id="q" name="q" type="search"></form>
<form><input name="user"><input name="pw" type="password"></form>
<div id="more"></div><button onclick="document.getElementById('more').textContent='続きの内容'">もっと見る</button>
</body></html>"""


@pytest.mark.skipif(not _chromium_installed(), reason="Chromium for Playwright is not installed")
async def test_large_page_attributes_are_bounded_before_leaving_chromium(tmp_path, monkeypatch):
    async def site(route):
        await route.fulfill(
            content_type="text/html",
            body="""<html><body><a id="big">large</a><a href="/next">next</a>
            <input id="field"><input id="q"><script>
              document.title = 'x'.repeat(100000);
              document.querySelector('#big').href = 'https://site.test/' + 'a'.repeat(100000);
              document.querySelector('#field').id = 'b'.repeat(100000);
            </script></body></html>""",
        )

    monkeypatch.setattr(BrowserSession, "_route", staticmethod(site))
    session = BrowserSession(BrowserService(), MASKER, tmp_path)
    try:
        result = await session.run(lambda: session.open("https://site.test/"))
        assert "error" not in result, result
        # These bounds apply in the renderer too, not only after Playwright has transferred the data.
        data = await session._current().evaluate(SNAPSHOT_JS, LIMITS)
        assert len(data["title"]) == LIMITS["maxTitle"]
        assert data["links"] == [{"text": "next", "url": "https://site.test/next"}]
        assert [item["selector"] for item in data["inputs"]] == ["#q"]
        assert data["result_truncated"] and result["result_truncated"]
        assert len(result_json(result).encode("utf-8")) <= MAX_RESULT_BYTES
    finally:
        await session.close()


@pytest.mark.skipif(not _chromium_installed(), reason="Chromium for Playwright is not installed")
async def test_collection_caps_are_reported_as_truncated(tmp_path, monkeypatch):
    links = "".join(f'<a href="https://site.test/{i}">link {i}</a>' for i in range(LIMITS["maxLinks"] + 5))
    inputs = "".join(f'<input name="f{i}" type="text">' for i in range(LIMITS["maxInputs"] + 5))
    cells = "".join(f"<td>c{c}</td>" for c in range(LIMITS["maxCols"] + 3))
    rows = "".join(f"<tr>{cells}</tr>" for _ in range(LIMITS["maxRows"] + 3))
    body = f"<html><body>{links}{inputs}<table>{rows}</table></body></html>"

    async def site(route):
        await route.fulfill(content_type="text/html", body=body)

    monkeypatch.setattr(BrowserSession, "_route", staticmethod(site))
    session = BrowserSession(BrowserService(), MASKER, tmp_path)
    try:
        result = await session.run(lambda: session.open("https://site.test/"))
        assert "error" not in result, result
        assert len(result["links"]) == LIMITS["maxLinks"]
        assert len(result["inputs"]) == LIMITS["maxInputs"]
        assert len(result["tables"][0]["rows"]) == LIMITS["maxRows"]
        assert all(len(row) == LIMITS["maxCols"] for row in result["tables"][0]["rows"])
        assert result["result_truncated"]
        # The renderer stops one item past each cap so the Python side can detect and report the overflow.
        data = await session._current().evaluate(SNAPSHOT_JS, {**LIMITS, "selector": None})
        assert len(data["links"]) == LIMITS["maxLinks"] + 1
        assert len(data["inputs"]) == LIMITS["maxInputs"] + 1
        assert len(data["tables"][0]["rows"]) == LIMITS["maxRows"] + 1
        assert len(data["tables"][0]["rows"][0]) == LIMITS["maxCols"] + 1
    finally:
        await session.close()


@pytest.mark.skipif(not _chromium_installed(), reason="Chromium for Playwright is not installed")
async def test_chromium_websocket_uses_egress_proxy(tmp_path, websocket_origin, monkeypatch):
    port, requests, messages = websocket_origin

    async def vetted(host):
        assert host == "public.example.com"
        return ["127.0.0.1"], None

    async def site(route):
        await route.fulfill(content_type="text/html", body="<html><body>WebSocket</body></html>")

    monkeypatch.setattr(proxy_module, "resolve_public", vetted)
    monkeypatch.setattr(BrowserSession, "_route", staticmethod(site))
    session = BrowserSession(BrowserService(), MASKER, tmp_path)
    try:
        result = await session.run(lambda: session.open("http://public.example.com/"))
        assert "error" not in result, result
        echo = await asyncio.wait_for(
            session._current().evaluate(
                """url => new Promise((resolve, reject) => {
                    const socket = new WebSocket(url);
                    socket.onopen = () => socket.send('hi');
                    socket.onmessage = event => { resolve(event.data); socket.close(); };
                    socket.onerror = () => reject(new Error('WebSocket failed'));
                })""",
                f"ws://public.example.com:{port}/socket",
            ),
            10,
        )
        assert echo == "hi" and messages == [b"hi"] and len(requests) == 1
    finally:
        await session.close()


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

        popped = await session.run(lambda: session.click(text="別のタブで開く"))
        assert popped["url"] == "https://site.test/popup", popped
        assert len(session._context.pages) == 1  # the page it came from is closed, so nothing piles up

        shot = await session.run(lambda: session.screenshot(full_page=True))
        assert (tmp_path / "shots" / f"{shot['screenshot']['id']}.png").stat().st_size > 0

        # Redirect hops never reach Playwright's routing: the proxy must stop them.
        redirected = await session.run(lambda: session.open("https://site.test/redirect"))
        assert "移動先" in redirected.get("error", ""), redirected
        assert session._current().url == "about:blank"  # the rejected URL is not left on screen
        direct = await session._current().goto(f"http://127.0.0.1:{origin_port}/direct")
        assert direct is not None and direct.status == 403
        assert requests == []
    finally:
        await session.close()
        origin.close()
    assert service._browser is None  # the last page closed, so Chromium stopped


TAMPERED_PAGE = """<!doctype html><html><head><title>わな</title><script>
const realGetAttribute = Element.prototype.getAttribute;
Element.prototype.getAttribute = function (name) {
  return name === 'type' || name === 'autocomplete' ? 'search' : realGetAttribute.call(this, name);
};
Element.prototype.querySelector = function () { return null; };
Element.prototype.closest = function () { return null; };
Object.defineProperty(Element.prototype, 'tagName', {get() { return 'TEXTAREA'; }});
Object.defineProperty(HTMLInputElement.prototype, 'form', {get() { return null; }});
</script></head><body>
<form><input id="u" name="user"><input id="pw" name="pw" type="password"></form>
<form id="ログイン"><input type="password" name="pw2"></form>
<input id="outside" name="other" form="ログイン">
<form id="署名"><input id="inside" name="account"></form>
<input id="pw3" name="pw3" type="password" form="署名">
</body></html>"""


@pytest.mark.skipif(not _chromium_installed(), reason="Chromium for Playwright is not installed")
async def test_form_checks_survive_a_page_that_rewrites_dom_apis(tmp_path, monkeypatch):
    """A page can fake getAttribute / querySelector in its own world: the checks must not rely on them."""

    async def site(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=TAMPERED_PAGE)

    monkeypatch.setattr(BrowserSession, "_route", staticmethod(site))
    session = BrowserSession(BrowserService(), MASKER, tmp_path / "shots")
    try:
        page = await session.run(lambda: session.open("https://trap.test/"))
        if "起動できませんでした" in page.get("error", ""):
            pytest.skip("Chromium could not be launched")
        assert "error" not in page, page
        assert "パスワード" in (await session.run(lambda: session.fill("#pw", "taro")))["error"]
        assert "ログイン" in (await session.run(lambda: session.fill("#u", "taro")))["error"]
        # The owner form is named by the form attribute, and its id is not safe to put into a selector.
        assert "ログイン" in (await session.run(lambda: session.fill("#outside", "taro")))["error"]
        # The password box sits outside the form and is attached to it by its own form attribute.
        assert "ログイン" in (await session.run(lambda: session.fill("#inside", "taro")))["error"]
    finally:
        await session.close()


async def test_browser_error_is_reported_when_chromium_cannot_start(tmp_path, monkeypatch):
    service = BrowserService(max_sessions=1)

    async def fail(self):
        raise BrowserError("ブラウザを起動できませんでした")

    monkeypatch.setattr(BrowserService, "_browser_locked", fail)
    session = BrowserSession(service, MASKER, tmp_path)
    # The second call only gets a slot if the failed first call gave its slot back.
    for _ in range(2):
        assert "起動できませんでした" in (await session.run(lambda: session.open("https://example.com/")))["error"]
