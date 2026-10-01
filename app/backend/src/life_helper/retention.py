"""Deletes history and internal data left unused for ``LH_DATA_RETENTION_DAYS`` (180 by default).

Two scopes, each run at most once a day by whichever process gets there first (they share the volume):

- "automation" (the scheduled job and the app): run records and their index, hidden chat entries of runs that are gone,
  monthly run counts, and the automation Copilot sessions.
- "chat" (the app only, the one process that knows which conversations are answering): conversations and the chat
  Copilot sessions.

The knowledge base, automation definitions, settings and secrets are never touched. Copilot sessions are deleted
through Copilot itself, so a pass that cannot reach it (no valid GitHub token) is retried an hour later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .automation import chat as automation_chat
from .automation.locks import FileLock
from .automation.store import UnreadableRunError, is_link, parse_timestamp
from .copilot_integration.manager import NoTokenError, SessionStateError
from .knowledge.store import atomic_write

if TYPE_CHECKING:
    from .automation.models import Automation
    from .context import AppContext
    from .copilot_integration.manager import CopilotManager

logger = logging.getLogger(__name__)

Scope = Literal["automation", "chat"]
DAILY = timedelta(hours=24)
RETRY = timedelta(hours=1)
PASS_BUDGET_SECONDS = 120
START_DELAY_SECONDS = 60
CHECK_INTERVAL_SECONDS = 60 * 60
SHUTDOWN_WAIT_SECONDS = 10

# Session folders are named by their session id; anything else (dot files, odd names) is left alone.
_SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# "auto-<automation>" is the shared session of a "continue" automation; "auto-<automation>-<run>-<attempt>" belongs to
# one run of a "new" automation (deleted after the run; one is left behind only when that failed).
_AUTO_SESSION_RE = re.compile(r"auto-(?P<aid>[0-9a-f]{6,32})(?P<run>-[0-9a-f]{6,32}-\d+)?")


@dataclass
class PassResult:
    # False when something was left for a later pass (no token, busy, out of time); the pass is retried in an hour.
    complete: bool = True
    deleted: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, count: int = 1) -> None:
        if count:
            self.deleted[key] = self.deleted.get(key, 0) + count


def cutoff_for(ctx: AppContext, now: datetime) -> datetime:
    return now - timedelta(days=ctx.settings.data_retention_days)


class RetentionGate:
    """Remembers when a scope last ran, so it runs once a day (an hour later when it could not finish)."""

    def __init__(self, ctx: AppContext, scope: Scope) -> None:
        state_dir = ctx.settings.app_state_dir
        self.path = state_dir / f"retention-{scope}.json"
        # A job is stopped after 25 minutes, so a lock left by a stopped pass is taken over after 30.
        self.lock = FileLock(state_dir / "locks" / f"retention-{scope}.lock", ttl_seconds=30 * 60)

    def _state(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def due(self, now: datetime) -> bool:
        state = self._state()
        last_success = parse_timestamp(state.get("last_success"))
        last_attempt = parse_timestamp(state.get("last_attempt"))
        if last_success is not None and now - last_success < DAILY:
            return False
        return last_attempt is None or now - last_attempt >= RETRY

    def record(self, now: datetime, *, complete: bool) -> None:
        state = self._state()
        state["last_attempt"] = now.isoformat()
        if complete:
            state["last_success"] = now.isoformat()
        atomic_write(self.path, json.dumps(state))


# -- Copilot session folders ------------------------------------------------------------------------------


def session_dirs(base_dir: Path) -> list[tuple[str, Path]]:
    """The session folders Copilot keeps under ``base_dir`` (``session-state/<session id>``)."""
    root = base_dir / "session-state"
    if is_link(root) or not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        if _SESSION_NAME_RE.fullmatch(path.name) and not is_link(path) and path.is_dir():
            found.append((path.name, path))
    return found


def session_last_used(path: Path) -> datetime | None:
    """When a session was last written to: Copilot appends every exchange to the files in its folder."""
    newest: float | None = None
    try:
        newest = path.stat(follow_symlinks=False).st_mtime
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    mtime = os.stat(os.path.join(root, name), follow_symlinks=False).st_mtime
                except OSError:
                    continue
                newest = max(newest, mtime)
    except OSError:
        return None
    return datetime.fromtimestamp(newest, UTC)


def _automation_session_last_used(
    name: str, path: Path, automations: dict[str, Automation], runs: list[dict]
) -> datetime | None:
    """A "continue" session counts as used whenever its automation ran in that mode, even when the run stopped
    before reaching Copilot (a missing connector, the monthly limit), so its memory is kept while it is in use."""
    times = [session_last_used(path)]
    m = _AUTO_SESSION_RE.fullmatch(name)
    if m and not m.group("run"):
        automation_id = m.group("aid")
        automation = automations.get(automation_id)
        if automation is not None and automation.conversation_mode == "continue":
            times.append(parse_timestamp(automation.state.last_run_at))
        times += [
            parse_timestamp(r.get("started_at"))
            for r in runs
            if r.get("automation_id") == automation_id and r.get("conversation_mode") == "continue"
        ]
    known = [t for t in times if t is not None]
    return max(known) if known else None


async def _delete_session(manager: CopilotManager, name: str, result: PassResult, key: str) -> bool:
    """Deletes one Copilot session. Returns False when Copilot cannot be used at all (the rest is left for later)."""
    try:
        await manager.delete_session(name)
    except SessionStateError:
        logger.warning("could not delete an expired Copilot session")
        result.complete = False
        return True
    except NoTokenError:
        result.complete = False
        return False
    except Exception as exc:  # noqa: BLE001 - e.g. the CLI does not start with a revoked token
        logger.warning("could not reach Copilot to delete expired sessions: %s", type(exc).__name__)
        result.complete = False
        return False
    result.add(key)
    return True


# -- automation scope -----------------------------------------------------------------------------------


async def prune_automation_data(
    ctx: AppContext, manager: CopilotManager | None, now: datetime, should_stop: Callable[[], bool]
) -> PassResult:
    store = ctx.automations
    assert store is not None
    cutoff = cutoff_for(ctx, now)
    result = PassResult()
    result.add("runs", len(await asyncio.to_thread(store.prune_runs, cutoff, should_stop)))
    # Each later step reads the whole history or every session again, so none starts once the time is up.
    if should_stop():
        return result
    await asyncio.to_thread(store.remove_empty_run_dirs)
    try:
        result.add("hidden", await asyncio.to_thread(lambda: store.prune_hidden(automation_chat.runs_by_thread(store))))
        result.add("usage_months", await asyncio.to_thread(store.prune_usage, cutoff))
    except TimeoutError:
        result.complete = False
    if manager is not None and not should_stop():
        await _prune_automation_sessions(ctx, manager, cutoff, should_stop, result)
    return result


def _scan_automation_sessions(
    ctx: AppContext, base_dir: Path, cutoff: datetime, should_stop: Callable[[], bool]
) -> list[tuple[str, Path]]:
    store = ctx.automations
    assert store is not None
    automations = {a.id: a for a in store.list()}
    # Strict: a run that cannot be read could be the one that shows a session is still in use.
    runs = store.list_run_meta(strict=True)
    expired = []
    for name, path in session_dirs(base_dir):
        if should_stop():
            break
        last = _automation_session_last_used(name, path, automations, runs)
        if last is not None and last < cutoff:
            expired.append((name, path))
    return expired


async def _prune_automation_sessions(
    ctx: AppContext, manager: CopilotManager, cutoff: datetime, should_stop: Callable[[], bool], result: PassResult
) -> None:
    store = ctx.automations
    assert store is not None
    try:
        expired = await asyncio.to_thread(_scan_automation_sessions, ctx, manager.base_dir, cutoff, should_stop)
    except UnreadableRunError:
        logger.warning("a run record cannot be read; automation sessions are kept for now")
        result.complete = False
        return
    if not expired:
        return
    if not ctx.github_token():
        result.complete = False
        return
    for name, path in expired:
        if should_stop():
            result.complete = False
            return
        m = _AUTO_SESSION_RE.fullmatch(name)
        # A run holds its automation's lock while it uses the session, so the session is not deleted under it.
        lock = (
            FileLock(store.locks_dir / f"automation-{m.group('aid')}.lock", ctx.settings.automation_lock_ttl_seconds)
            if m
            else None
        )
        if lock is not None and not lock.try_acquire():
            result.complete = False
            continue
        try:
            # Decide again under the lock: a run may have used the session since it was scanned.
            try:
                last = await asyncio.to_thread(_recheck_automation_session, ctx, name, path)
            except UnreadableRunError:
                result.complete = False
                continue
            if last is None or last >= cutoff:
                continue
            if not await _delete_session(manager, name, result, "automation_sessions"):
                return
        finally:
            if lock is not None:
                lock.release()


def _recheck_automation_session(ctx: AppContext, name: str, path: Path) -> datetime | None:
    store = ctx.automations
    assert store is not None
    automations = {a.id: a for a in store.list()}
    return _automation_session_last_used(name, path, automations, store.list_run_meta(strict=True))


# -- chat scope -------------------------------------------------------------------------------------------


async def prune_chat_data(ctx: AppContext, now: datetime, should_stop: Callable[[], bool]) -> PassResult:
    from .copilot_integration.turns import TurnBusyError

    conversations = ctx.extras["conversations"]
    assert ctx.turns is not None and ctx.copilot is not None
    cutoff = cutoff_for(ctx, now)
    result = PassResult()
    has_token = bool(ctx.github_token())
    copilot_usable = has_token

    def expired(conv) -> bool:
        updated = parse_timestamp(conv.updated_at)
        return updated is not None and updated < cutoff

    for conv in await asyncio.to_thread(conversations.list):
        if should_stop():
            result.complete = False
            return result
        if not expired(conv):
            continue
        try:
            async with ctx.turns.reserve(conv.id):
                # Checked again under the store's lock: a conversation renamed or used meanwhile is kept.
                removed = await asyncio.to_thread(conversations.delete_if, conv.id, expired)
                if removed is None:
                    continue
                result.add("conversations")
                if not removed.started:
                    continue
                # The conversation is gone either way; a session that cannot be deleted now is deleted by a later
                # pass as one without a conversation.
                if not copilot_usable:
                    result.complete = False
                else:
                    copilot_usable = await _delete_session(ctx.copilot, conv.id, result, "chat_sessions")
        except TurnBusyError:
            result.complete = False

    known = await asyncio.to_thread(conversations.ids_if_readable)
    if known is None:
        # Without the list every session would look like one without a conversation.
        logger.warning("the conversation list cannot be read; sessions without a conversation are kept for now")
        result.complete = False
        return result

    def scan_orphans() -> list[str]:
        found = []
        for name, path in session_dirs(ctx.copilot.base_dir):
            if should_stop():
                break
            if name not in known and (last := session_last_used(path)) is not None and last < cutoff:
                found.append(name)
        return found

    for name in await asyncio.to_thread(scan_orphans):
        if should_stop() or not copilot_usable:
            result.complete = False
            break
        if ctx.turns.busy(name):
            continue
        copilot_usable = await _delete_session(ctx.copilot, name, result, "chat_sessions")
    return result


# -- running the scopes -----------------------------------------------------------------------------------


async def run_scope(
    ctx: AppContext,
    scope: Scope,
    *,
    automation_manager: CopilotManager | None = None,
    now: datetime | None = None,
    stop: threading.Event | None = None,
    budget_seconds: float = PASS_BUDGET_SECONDS,
) -> PassResult | None:
    """Runs one scope if it is due and no other process is running it. Returns None when it was skipped."""
    now = now or datetime.now(UTC)
    gate = RetentionGate(ctx, scope)
    if not gate.due(now) or not gate.lock.try_acquire():
        return None
    try:
        if not gate.due(now):  # another process may have finished a pass just before the lock was taken
            return None
        deadline = time.monotonic() + budget_seconds

        def should_stop() -> bool:
            return (stop is not None and stop.is_set()) or time.monotonic() > deadline

        try:
            if scope == "automation":
                result = await prune_automation_data(ctx, automation_manager, now, should_stop)
            else:
                result = await prune_chat_data(ctx, now, should_stop)
        except BaseException:
            gate.record(now, complete=False)
            raise
        if should_stop():  # stopped or out of time: whatever is left is done by the next pass
            result.complete = False
        gate.record(now, complete=result.complete)
        logger.info(
            "data retention (%s, %d days): deleted %s%s",
            scope,
            ctx.settings.data_retention_days,
            json.dumps(result.deleted, sort_keys=True),
            "" if result.complete else "; the rest is retried later",
        )
        return result
    finally:
        gate.lock.release()


class RetentionScheduler:
    """Runs both scopes from the web app: shortly after start-up and then every hour (each is due once a day).

    The app scales to zero, so the scheduled job covers the automation scope while the app is not running.
    """

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.stop = threading.Event()
        self.task: asyncio.Task | None = None
        self._in_pass = False

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    def _automation_manager(self) -> CopilotManager:
        from .automation.runner import AutomationRunner

        assert self.ctx.automations is not None
        # Shared with "run now", so both use one Copilot client (and its per-session locks) for automation sessions.
        runner = self.ctx.extras.get("automation_runner")
        if runner is None:
            runner = self.ctx.extras.setdefault("automation_runner", AutomationRunner(self.ctx, self.ctx.automations))
        return runner.manager

    async def run_once(self, now: datetime | None = None) -> None:
        for scope in ("chat", "automation"):
            if self.stop.is_set():
                return
            try:
                manager = self._automation_manager() if scope == "automation" else None
                await run_scope(self.ctx, scope, automation_manager=manager, now=now, stop=self.stop)
            except Exception as exc:  # noqa: BLE001 - never stops the app; the pass is retried in an hour
                logger.error("data retention (%s) failed: %s", scope, type(exc).__name__)

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY_SECONDS)
        while not self.stop.is_set():
            self._in_pass = True
            try:
                await self.run_once()
            finally:
                self._in_pass = False
            if self.stop.is_set():
                return
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def shutdown(self) -> None:
        self.stop.set()
        task = self.task
        if task is None or task.done():
            return
        if not self._in_pass:
            task.cancel()
        # A pass in progress notices the stop flag between deletions; it is given a moment before it is cancelled.
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=SHUTDOWN_WAIT_SECONDS)
