"""Shared application services, attached to ``app.state.ctx``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request

from .config import Settings
from .security import SecretMasker, TokenVault

if TYPE_CHECKING:
    from .automation.store import AutomationStore
    from .copilot_integration.manager import CopilotManager
    from .copilot_integration.turns import TurnManager
    from .knowledge.store import KnowledgeStore


@dataclass
class AppContext:
    settings: Settings
    vault: TokenVault
    masker: SecretMasker
    knowledge: KnowledgeStore | None = None
    copilot: CopilotManager | None = None
    turns: TurnManager | None = None
    automations: AutomationStore | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    _token_listeners: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def add_token_listener(self, listener: Callable[[], Awaitable[None]]) -> None:
        self._token_listeners.append(listener)

    async def on_token_changed(self) -> None:
        stored = self.vault.load()
        if stored and stored.get("token"):
            self.masker.add(stored["token"])
        for listener in self._token_listeners:
            await listener()

    def github_token(self) -> str | None:
        stored = self.vault.load()
        token = stored.get("token") if stored else None
        if token:
            # Whatever path loaded the token, make sure it can never leak through tool output or errors.
            self.masker.add(token)
        return token


def _encryption_key(settings: Settings) -> str:
    key = settings.token_encryption_key.get_secret_value()
    if key or not settings.is_dev:
        return key
    # Development convenience only: production refuses to start without LH_TOKEN_ENCRYPTION_KEY.
    from cryptography.fernet import Fernet

    key_file = settings.app_state_dir / "secrets" / "dev-token.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
    return key_file.read_text().strip()


def build_context(settings: Settings) -> AppContext:
    vault = TokenVault(settings.app_state_dir / "secrets" / "github_token.enc", _encryption_key(settings))
    masker = SecretMasker(settings.secret_values())
    ctx = AppContext(settings=settings, vault=vault, masker=masker)
    stored = vault.load()
    if stored and stored.get("token"):
        masker.add(stored["token"])
    return ctx


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
