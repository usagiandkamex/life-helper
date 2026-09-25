"""Registry of the app's custom Copilot tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from copilot import Tool

if TYPE_CHECKING:
    from ..context import AppContext


@dataclass
class ToolSpec:
    tool: Tool
    writes: bool = False
    # External services this tool can call; automations only receive it when they selected every one of them.
    connector: str | tuple[str, ...] | None = None
    # Frees per-session resources (such as a browser page) when a turn or automation run ends.
    release: Callable[[], Awaitable[None]] | None = None

    def allowed(self, connectors: list[str]) -> bool:
        if self.connector is None:
            return True
        names = (self.connector,) if isinstance(self.connector, str) else self.connector
        return bool(names) and all(name in connectors for name in names)


def build_tools(
    ctx: AppContext, *, extra: list[ToolSpec] | None = None, connectors: list[str] | None = None
) -> list[ToolSpec]:
    """Returns every custom tool available to a session. ``connectors`` (automations) restricts connector tools."""
    specs: list[ToolSpec] = []
    for module_builder in _builders():
        specs.extend(module_builder(ctx))
    if connectors is not None:
        specs = [s for s in specs if s.allowed(connectors)]
    specs.extend(extra or [])
    return specs


def _builders():
    from ..connectors import registry as connector_registry
    from . import finance, portfolio_tools

    return (finance.build_tools, portfolio_tools.build_tools, connector_registry.build_tools)
