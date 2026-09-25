"""Common base for external API connectors.

API keys are read from environment variables (ACA secrets) only at call time. The model never sees them: it calls a
tool with key-free arguments, the connector adds the key, calls a fixed host, and returns only the parsed result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..knowledge.store import atomic_write
from ..security import SecretMasker

# httpx logs full request URLs (including query strings that carry API keys) at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """A connector failure with a message that is safe to show to the model and the user."""


@dataclass
class ConnectorInfo:
    name: str
    label: str
    hosts: tuple[str, ...]
    secret_names: tuple[str, ...]
    cost: str


class Connector:
    info: ConnectorInfo
    min_interval_seconds: float = 1.0

    def __init__(self, secrets: dict[str, str], masker: SecretMasker, state_path: Path, *, transport=None) -> None:
        self._secrets = secrets
        self._masker = masker
        self._state_path = state_path
        self._transport = transport
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        for value in secrets.values():
            if value:
                masker.add(value)

    @property
    def configured(self) -> bool:
        return all(self._secrets.get(name) for name in self.info.secret_names)

    def secret(self, name: str) -> str:
        value = self._secrets.get(name, "")
        if not value:
            raise ConnectorError(
                f"{self.info.label} の API キーが登録されていません（Azure のシークレットに登録してください）"
            )
        return value

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        return await self._request("GET", url, params=params, headers=headers, max_bytes=max_bytes)

    async def post(
        self,
        url: str,
        *,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """Sends ``body`` as JSON, under the same host allowlist, throttling and error masking as ``get``."""
        return await self._request("POST", url, params=params or {}, body=body, headers=headers, max_bytes=max_bytes)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any],
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https" or host not in self.info.hosts:
            raise ConnectorError("接続先がこのコネクタで許可されていません")
        async with self._lock:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=20, transport=self._transport, follow_redirects=False) as client:
                    request = client.build_request(method, url, params=params, json=body, headers=headers)
                    await self._before_send()
                    if max_bytes is None:
                        response = await client.send(request)
                    else:
                        response = await self._capped(client, request, max_bytes)
            except httpx.HTTPError as e:
                # Never propagate the raw exception: its message can contain the full URL with the API key.
                logger.warning("%s request failed: %s", self.info.name, type(e).__name__)
                raise ConnectorError(f"{self.info.label} に接続できませんでした") from None
            finally:
                self._last_call = time.monotonic()
        self._record_use()
        return response

    async def _before_send(self) -> None:
        """Runs right before each request is sent, after the per-instance interval (e.g. a cross-process limit)."""

    async def _capped(self, client: httpx.AsyncClient, request: httpx.Request, max_bytes: int) -> httpx.Response:
        """Reads at most ``max_bytes`` of the body, so an oversized (or compressed) answer never fills memory."""
        streamed = await client.send(request, stream=True)
        try:
            body = bytearray()
            async for chunk in streamed.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise ConnectorError(f"{self.info.label} の応答が想定より大きいため取り込みませんでした")
        finally:
            await streamed.aclose()
        # aiter_bytes already decompressed the body, so the encoding and length of the wire form must go.
        passthrough = httpx.Headers(streamed.headers)
        for name in ("content-encoding", "content-length"):
            if name in passthrough:
                del passthrough[name]
        return httpx.Response(streamed.status_code, headers=passthrough, content=bytes(body), request=streamed.request)

    def mask(self, value: Any) -> Any:
        return self._masker.mask(value)

    def _record_use(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8")) if self._state_path.exists() else {}
        except (OSError, ValueError):
            state = {}
        state[self.info.name] = {"last_used": datetime.now(UTC).isoformat()}
        try:
            atomic_write(self._state_path, json.dumps(state, ensure_ascii=False))
        except OSError:
            logger.debug("could not record connector usage", exc_info=True)

    def last_used(self) -> str | None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state.get(self.info.name, {}).get("last_used")
