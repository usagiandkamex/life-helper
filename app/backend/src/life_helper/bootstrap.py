"""Wires services and routers together (kept separate so the job entrypoint can reuse it)."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .automation.store import AutomationStore
from .context import AppContext
from .knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


def ensure_directories(ctx: AppContext) -> None:
    s = ctx.settings
    for path in (
        s.data_dir,
        s.app_state_dir,
        s.knowledge_dir,
        s.copilot_chat_dir,
        s.copilot_automation_dir,
        s.copilot_workdir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def init_core(ctx: AppContext) -> None:
    """Initialises services shared by the web app and the scheduled job."""
    ensure_directories(ctx)
    ctx.knowledge = KnowledgeStore(ctx.settings.knowledge_dir, ctx.settings.seed_dir)
    ctx.knowledge.ensure_seeded()
    ctx.automations = AutomationStore(ctx.settings.app_state_dir)


def init_chat(ctx: AppContext) -> None:
    from .copilot_integration.conversations import ConversationStore
    from .copilot_integration.manager import CopilotManager
    from .copilot_integration.turns import TurnManager

    ctx.copilot = CopilotManager(ctx, ctx.settings.copilot_chat_dir)
    conversations = ConversationStore(ctx.settings.app_state_dir / "conversations.json")
    ctx.extras["conversations"] = conversations
    ctx.turns = TurnManager(ctx.copilot, conversations, ctx.masker)
    ctx.add_token_listener(ctx.copilot.reset)


async def start_services(ctx: AppContext) -> None:
    init_core(ctx)
    init_chat(ctx)


async def shutdown_services(ctx: AppContext) -> None:
    for task in list(ctx.extras.get("automation_tasks", ())):
        task.cancel()
    runner = ctx.extras.get("automation_runner")
    if runner is not None:
        await runner.manager.reset()
    if ctx.turns is not None:
        await ctx.turns.shutdown()
    if ctx.copilot is not None:
        await ctx.copilot.reset()


def include_routers(app: FastAPI) -> None:
    from .automation.api import router as automation_router
    from .connectors.registry import router as connectors_router
    from .copilot_integration.api import router as chat_router
    from .knowledge.api import router as knowledge_router
    from .market.api import router as portfolio_router

    app.include_router(knowledge_router)
    app.include_router(chat_router)
    app.include_router(connectors_router)
    app.include_router(portfolio_router)
    app.include_router(automation_router)
