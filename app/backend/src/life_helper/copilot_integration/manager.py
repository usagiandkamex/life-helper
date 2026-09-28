"""Owns the Copilot client and builds sessions with the app's policy, tools and system message."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copilot import CopilotClient, CopilotSession, ToolSet

from ..tools.registry import ToolSpec, build_tools
from .agents import build_research_agent
from .knowledge_tools import build_knowledge_tools
from .policy import ALLOWED_BUILTINS, TASK_TOOL, ToolPolicy, WriteScope, knowledge_write_lock_path
from .system_prompt import build_system_message

if TYPE_CHECKING:
    from ..context import AppContext

logger = logging.getLogger(__name__)

KEEP = object()
"""Sentinel for ``open_session(write_scope=KEEP)``: leave the cached session's write scope untouched."""


class NoTokenError(RuntimeError):
    """Raised when no GitHub token is stored (the user must sign in again)."""


class SessionStateError(RuntimeError):
    """Stored Copilot session state exists but could not be resumed or deleted."""


@dataclass
class ActiveSession:
    session: CopilotSession
    policy: ToolPolicy
    model: str
    extra: dict[str, Any] = field(default_factory=dict)
    releasers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def release(self) -> None:
        """Ends a turn or run: frees per-session tool resources. The session stays usable."""
        for release in self.releasers:
            try:
                await release()
            except Exception:  # noqa: BLE001
                logger.warning("failed to release tool resources")


def available_toolset(has_skills: bool, subagents: bool = False) -> ToolSet:
    builtins = [t for t in ALLOWED_BUILTINS if (t != "skill" or has_skills) and (t != TASK_TOOL or subagents)]
    return ToolSet().add_builtin(builtins).add_custom("*")


class CopilotManager:
    """One Copilot CLI process per stored token, with per-conversation sessions cached in memory.

    All session operations for one id are serialised with a per-id lock, and sessions opened on a client that was
    restarted meanwhile (token change) are discarded instead of cached.
    """

    def __init__(self, ctx: AppContext, base_dir: Path, *, automation: bool = False) -> None:
        self.ctx = ctx
        self.base_dir = base_dir
        self.automation = automation
        self._client: CopilotClient | None = None
        self._token: str | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, ActiveSession] = {}
        self._stale: set[str] = set()

    def _slock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def state_exists(self, session_id: str) -> bool:
        return (self.base_dir / "session-state" / session_id).exists()

    # -- client lifecycle --------------------------------------------------------------------------------

    async def client(self) -> tuple[CopilotClient, int]:
        token = self.ctx.github_token()
        if not token:
            raise NoTokenError("GitHub token is not available; sign in again")
        async with self._lock:
            if self._client is not None and self._token == token:
                return self._client, self._generation
            await self._stop_locked()
            settings = self.ctx.settings
            settings.copilot_workdir.mkdir(parents=True, exist_ok=True)
            self.base_dir.mkdir(parents=True, exist_ok=True)
            client = CopilotClient(
                mode="empty",
                github_token=token,
                base_directory=str(self.base_dir),
                working_directory=str(settings.copilot_workdir),
                log_level="warning",
            )
            await client.start()
            self._client, self._token = client, token
            return client, self._generation

    async def reset(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        self._generation += 1
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._stale.clear()
        for active in sessions:
            await active.release()
            await _disconnect_quietly(active.session)
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:  # noqa: BLE001
                logger.warning("failed to stop Copilot client")
        self._client, self._token = None, None

    async def list_models(self) -> list[dict]:
        client, _ = await self.client()
        models = await client.list_models()
        return [
            {
                "id": m.id,
                "name": getattr(m, "name", None) or m.id,
                "vision": bool(getattr(getattr(getattr(m, "capabilities", None), "supports", None), "vision", False)),
            }
            for m in models
        ]

    # -- sessions ----------------------------------------------------------------------------------------

    def build_policy(self, specs: list[ToolSpec], *, allow_write: bool) -> ToolPolicy:
        s = self.ctx.settings
        return ToolPolicy(
            knowledge_root=s.knowledge_dir,
            skills_root=s.skills_dir,
            masker=self.ctx.masker,
            custom_tools={spec.tool.name for spec in specs},
            write_custom_tools={spec.tool.name for spec in specs if spec.writes},
            allow_write=allow_write,
            # Unattended automations cannot answer an approval card; they follow their own allow_write setting.
            require_approval=not self.automation,
            # Chat and automation alike may run look-ups in parallel with the read-only research sub-agent (``task``).
            # The automation runner waits for its own agent's idle (send_and_wait_own), so a sub-agent's idle does not
            # cut the run short.
            allow_subagents=True,
            write_lock_path=knowledge_write_lock_path(s),
        )

    def build_session_tools(
        self, *, allow_write: bool, extra_tools: list[ToolSpec] | None = None, connectors: list[str] | None = None
    ) -> tuple[list[ToolSpec], ToolPolicy]:
        """Custom tools plus the knowledge-base write tools, which are bound to the session's policy."""
        specs = build_tools(self.ctx, extra=extra_tools, connectors=connectors)
        policy = self.build_policy(specs, allow_write=allow_write)
        knowledge_specs = build_knowledge_tools(policy)
        policy.custom_tools |= {spec.tool.name for spec in knowledge_specs}
        policy.write_custom_tools |= {spec.tool.name for spec in knowledge_specs}
        return specs + knowledge_specs, policy

    def session_options(
        self, *, model: str, policy: ToolPolicy, specs: list[ToolSpec], allow_write: bool
    ) -> dict[str, Any]:
        s = self.ctx.settings
        has_skills = s.skills_dir.is_dir() and any(s.skills_dir.glob("*/SKILL.md"))
        subagents = policy.allow_subagents
        system_message = build_system_message(
            s.knowledge_dir,
            automation=self.automation,
            allow_write=allow_write,
            approval=policy.require_approval,
            browser=any(spec.tool.name.startswith("browser_") for spec in specs),
            subagents=subagents,
        )
        options: dict[str, Any] = {
            "model": model,
            "on_permission_request": policy.handle_permission,
            "hooks": policy.hooks(),
            "tools": [spec.tool for spec in specs],
            "available_tools": available_toolset(has_skills, subagents),
            "system_message": {"mode": "append", "content": system_message},
            "working_directory": str(s.knowledge_dir),
            "streaming": True,
            "infinite_sessions": {"enabled": True},
            "enable_skills": has_skills,
        }
        if subagents:
            options["custom_agents"] = [build_research_agent(s.knowledge_dir.resolve().as_posix())]
            # The sub-agents' own token stream stays out of the answer; their tool calls are still shown.
            options["include_sub_agent_streaming_events"] = False
        if has_skills:
            options["skill_directories"] = [str(s.skills_dir)]
        return options

    async def open_session(
        self,
        session_id: str,
        *,
        model: str,
        resume: bool,
        allow_write: bool = True,
        extra_tools: list[ToolSpec] | None = None,
        connectors: list[str] | None = None,
        write_scope: WriteScope | None | object = KEEP,
    ) -> ActiveSession:
        """Returns a cached session or resumes/creates one. Tools and hooks are re-supplied on resume because they
        are not persisted in Copilot's session state. ``write_scope`` belongs to the current turn, so a cached
        session receives the new one (``KEEP`` leaves it as it is)."""
        fingerprint = (
            allow_write,
            tuple(sorted(spec.tool.name for spec in extra_tools or [])),
            None if connectors is None else tuple(sorted(connectors)),
        )
        async with self._slock(session_id):
            cached = self._sessions.get(session_id)
            if cached is not None and (session_id in self._stale or cached.extra.get("fingerprint") != fingerprint):
                await self._close_locked(session_id)
                cached = None
            if cached is not None:
                if write_scope is not KEEP:
                    cached.policy.write_scope = write_scope  # type: ignore[assignment]
                if cached.model != model:
                    await cached.session.set_model(model)
                    cached.model = model
                return cached

            client, generation = await self.client()
            specs, policy = self.build_session_tools(
                allow_write=allow_write, extra_tools=extra_tools, connectors=connectors
            )
            policy.write_scope = None if write_scope is KEEP else write_scope  # type: ignore[assignment]
            options = self.session_options(model=model, policy=policy, specs=specs, allow_write=allow_write)
            if resume and self.state_exists(session_id):
                try:
                    session = await client.resume_session(session_id, **options)
                except Exception as exc:
                    # The state exists, so creating a fresh session would silently drop or fork the history.
                    raise SessionStateError("保存されている会話を再開できませんでした") from exc
            else:
                session = await client.create_session(session_id=session_id, **options)
            if generation != self._generation:
                # The client was restarted (token change) while we were opening: do not cache a dead session.
                await _disconnect_quietly(session)
                raise SessionStateError("Copilot の接続が切り替わったため、もう一度お試しください")
            active = ActiveSession(session=session, policy=policy, model=model, extra={"fingerprint": fingerprint})
            for spec in specs:
                if spec.release is not None and spec.release not in active.releasers:
                    active.releasers.append(spec.release)
            self._sessions[session_id] = active
            return active

    async def close_session(self, session_id: str) -> None:
        async with self._slock(session_id):
            await self._close_locked(session_id)

    async def _close_locked(self, session_id: str) -> None:
        self._stale.discard(session_id)
        active = self._sessions.pop(session_id, None)
        if active is not None:
            await active.release()
            await _disconnect_quietly(active.session)

    def mark_sessions_stale(self) -> None:
        """Cached sessions are reopened at their next turn so a freshly built system message applies (profile edits).
        Running turns are not interrupted. Must be called on the event loop."""
        self._stale.update(self._sessions)

    async def delete_session(self, session_id: str) -> None:
        async with self._slock(session_id):
            await self._close_locked(session_id)
            if not self.state_exists(session_id):
                return
            client, _ = await self.client()
            try:
                await client.delete_session(session_id)
            except Exception as exc:
                raise SessionStateError("会話の履歴を削除できませんでした") from exc
            if self.state_exists(session_id):
                raise SessionStateError("会話の履歴を削除できませんでした")


async def _disconnect_quietly(session: CopilotSession) -> None:
    try:
        await session.disconnect()
    except Exception:  # noqa: BLE001
        logger.debug("failed to disconnect session")
