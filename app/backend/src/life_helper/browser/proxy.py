"""Loopback HTTP proxy that carries all of Chromium's traffic.

Playwright's request routing never sees redirect hops or WebSockets, so the destination check lives here: every
connection is vetted by host (``netguard``) and then made to one of the vetted public addresses, which also closes
the DNS-rebinding gap between the check and the connection.

Plain HTTP requests are vetted as whole URLs (``url_rejection``), so credentials, sensitive data and secrets in the
path or query never leave. For HTTPS only the CONNECT authority is visible, so those connections are vetted by host;
what the page then sends over the tunnel is limited by the checks on the tools themselves (``browser/guard.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..netguard import host_rejection, resolve_public, url_rejection

if TYPE_CHECKING:
    from ..security import SecretMasker

logger = logging.getLogger(__name__)

HEAD_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 15
CHUNK = 64 * 1024
HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization"}
BLOCKED_BODY = "life-helper によりブロックされました: {reason}"
REASON_PHRASES = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}


def _split_authority(authority: str) -> tuple[str, int]:
    parts = urlsplit("//" + authority)
    if not parts.hostname or parts.port is None:
        raise ValueError("bad CONNECT target")
    return parts.hostname, parts.port


def _without_hop_by_hop(head: bytes) -> list[bytes]:
    lines = head.split(b"\r\n")
    return [lines[0]] + [h for h in lines[1:] if h and h.split(b":", 1)[0].strip().lower() not in HOP_BY_HOP]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _relay_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Marks a plain-HTTP response as closing, so Chromium never reuses this upstream for a request to another host."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        lines = _without_hop_by_hop(head)
        closing = b"Connection: close\r\nProxy-Connection: close\r\n\r\n"
        writer.write(b"".join(line + b"\r\n" for line in lines) + closing)
        await writer.drain()
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        with contextlib.suppress(Exception):
            writer.close()
        return
    await _pipe(reader, writer)


class EgressProxy:
    def __init__(self, masker: SecretMasker | None = None) -> None:
        self._masker = masker
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        lines = head.split(b"\r\n")
        method, target, version = lines[0].decode("latin-1").split(" ")
        tunnel = method.upper() == "CONNECT"
        if tunnel:
            host, port = _split_authority(target)
            # Only the authority is visible on CONNECT: the request line inside the tunnel is encrypted.
            reason = host_rejection(host)
        else:
            parts = urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                await self._reply(writer, 400, "http の URL だけ中継できます")
                return
            host, port = parts.hostname, parts.port or 80
            # The whole URL is visible here, so it gets the same check as browser_open (never echoed back).
            reason = url_rejection(target, self._masker)

        addresses: list[str] = []
        if reason is None:
            addresses, reason = await resolve_public(host)
        if reason is not None:
            logger.info("browser proxy blocked a connection: %s", reason)
            await self._reply(writer, 403, reason)
            return
        upstream = await self._connect(addresses, port)
        if upstream is None:
            await self._reply(writer, 502, "接続先に接続できませんでした")
            return
        up_reader, up_writer = upstream

        if tunnel:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
            return
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = _without_hop_by_hop(head)[1:]
        # One request per upstream connection: the next request may target another host and must be vetted.
        request = f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(h + b"\r\n" for h in headers)
        up_writer.write(request + b"Connection: close\r\n\r\n")
        await asyncio.gather(_pipe(reader, up_writer), _relay_response(up_reader, writer))

    @staticmethod
    async def _connect(addresses: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        # IPv4 first: containers often have no IPv6 route, and each dead address costs a full connect timeout.
        ordered = sorted(dict.fromkeys(addresses), key=lambda a: ":" in a)
        for address in ordered[:4]:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS)
            except (OSError, TimeoutError):
                continue
        return None

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = BLOCKED_BODY.format(reason=reason).encode()
        phrase = REASON_PHRASES.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
