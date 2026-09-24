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

    async def get(self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https" or host not in self.info.hosts:
            raise ConnectorError("接続先がこのコネクタで許可されていません")
        async with self._lock:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=20, transport=self._transport, follow_redirects=False) as client:
                    response = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:
                # Never propagate the raw exception: its message can contain the full URL with the API key.
                logger.warning("%s request failed: %s", self.info.name, type(e).__name__)
                raise ConnectorError(f"{self.info.label} に接続できませんでした") from None
            finally:
                self._last_call = time.monotonic()
        self._record_use()
        return response

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
