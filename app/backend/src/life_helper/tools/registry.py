"""Registry of the app's custom Copilot tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from copilot import Tool

if TYPE_CHECKING:
    from ..context import AppContext


@dataclass
class ToolSpec:
    tool: Tool
    writes: bool = False
    # External service this tool calls; automations only receive the connectors they selected.
    connector: str | None = None


def build_tools(
    ctx: AppContext, *, extra: list[ToolSpec] | None = None, connectors: list[str] | None = None
) -> list[ToolSpec]:
    """Returns every custom tool available to a session. ``connectors`` (automations) restricts connector tools."""
    specs: list[ToolSpec] = []
    for module_builder in _builders():
        specs.extend(module_builder(ctx))
    if connectors is not None:
        specs = [s for s in specs if s.connector is None or s.connector in connectors]
    specs.extend(extra or [])
    return specs


def _builders():
    from ..connectors import registry as connector_registry
    from . import finance, portfolio_tools

    return (finance.build_tools, portfolio_tools.build_tools, connector_registry.build_tools)
