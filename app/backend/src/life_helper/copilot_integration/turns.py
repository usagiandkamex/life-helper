"""Background chat turns with a replayable event buffer.

ACA's ingress ends any HTTP request after 240 seconds of wall-clock time, so a turn runs as a background task
and the browser follows it over SSE. Every event carries a sequence id, so a dropped connection resumes from
``Last-Event-ID`` without losing output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..security import SecretMasker
from .conversations import ConversationStore
from .events import map_event
from .manager import ActiveSession, CopilotManager, NoTokenError, SessionStateError

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
TURN_RETENTION_SECONDS = 15 * 60
MAX_EVENTS_PER_TURN = 20_000


class TurnBusyError(RuntimeError):
    pass


@dataclass
class Turn:
    id: str
    conversation_id: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    finished_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    active: ActiveSession | None = None
    task: asyncio.Task | None = None


class TurnManager:
    def __init__(
        self,
        manager: CopilotManager,
        conversations: ConversationStore,
        masker: SecretMasker,
        *,
        timeout_seconds: float = 20 * 60,
    ) -> None:
        self.manager = manager
        self.conversations = conversations
        self.masker = masker
        self.timeout_seconds = timeout_seconds
        self._turns: dict[str, Turn] = {}
        self._busy: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def busy(self, conversation_id: str) -> bool:
        return conversation_id in self._busy

    def active_turn_id(self, conversation_id: str) -> str | None:
        turn_id = self._busy.get(conversation_id)
        return turn_id if turn_id in self._turns else None

    @contextlib.asynccontextmanager
    async def reserve(self, conversation_id: str) -> AsyncIterator[None]:
        """Blocks new turns while a destructive operation (such as deletion) runs on the conversation."""
        if self.busy(conversation_id):
            raise TurnBusyError("this conversation is answering")
        self._busy[conversation_id] = "__reserved__"
        try:
            yield
        finally:
            self._busy.pop(conversation_id, None)

    def get(self, turn_id: str) -> Turn | None:
        return self._turns.get(turn_id)

    def _schedule_expiry(self, turn: Turn) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_later(TURN_RETENTION_SECONDS, self._turns.pop, turn.id, None)

    async def start(self, conversation_id: str, prompt: str, model: str) -> Turn:
        if self.busy(conversation_id):
            raise TurnBusyError("this conversation is already answering")
        self._loop = asyncio.get_running_loop()
        turn = Turn(id=uuid.uuid4().hex, conversation_id=conversation_id)
        self._turns[turn.id] = turn
        self._busy[conversation_id] = turn.id
        turn.task = asyncio.create_task(self._run(turn, prompt, model))
        return turn

    def _emit(self, turn: Turn, event: dict) -> None:
        if len(turn.events) >= MAX_EVENTS_PER_TURN and event.get("type") == "delta":
            # The final "message" event still carries the full answer, so dropping deltas loses nothing.
            return
        turn.events.append(event)
        previous, turn.changed = turn.changed, asyncio.Event()
        previous.set()

    def _emit_threadsafe(self, turn: Turn, event: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._emit(turn, event)
        else:
            loop.call_soon_threadsafe(self._emit, turn, event)

    async def _run(self, turn: Turn, prompt: str, model: str) -> None:
        conversation = self.conversations.get(turn.conversation_id)
        try:
            if conversation is None:
                raise LookupError("conversation not found")
            turn.active = await self.manager.open_session(
                turn.conversation_id,
                model=model,
                resume=conversation.started,
                on_write=lambda path, diff: self._emit_threadsafe(
                    turn, {"type": "file_write", "path": path, "diff": self.masker.mask_text(diff[:4000])}
                ),
            )
            self.conversations.update(turn.conversation_id, started=True, model=model)
            unsubscribe = turn.active.session.on(lambda ev: self._on_event(turn, ev))
            try:
                await turn.active.session.send_and_wait(prompt, timeout=self.timeout_seconds)
            finally:
                unsubscribe()
            self._emit(turn, {"type": "done"})
        except NoTokenError:
            self._emit(turn, {"type": "error", "code": "reauth", "message": "GitHub への再ログインが必要です"})
        except TimeoutError:
            await self._abort_quietly(turn)
            self._emit(
                turn, {"type": "error", "code": "timeout", "message": "時間内に回答が終わらなかったため中断しました"}
            )
        except asyncio.CancelledError:
            self._emit(turn, {"type": "error", "code": "cancelled", "message": "中断しました"})
            raise
        except SessionStateError as exc:
            self._emit(turn, {"type": "error", "code": "session", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.error("turn failed: %s", type(exc).__name__)
            # The session may be in an unknown state: reopen it (resume from disk) at the next turn.
            with contextlib.suppress(Exception):
                await self.manager.close_session(turn.conversation_id)
            self._emit(turn, {"type": "error", "message": self.masker.mask_text(str(exc)) or "エラーが発生しました"})
        finally:
            if turn.active is not None:
                await turn.active.release()
            turn.done = True
            turn.finished_at = time.monotonic()
            self._busy.pop(turn.conversation_id, None)
            self._emit(turn, {"type": "end"})
            self._schedule_expiry(turn)

    def _on_event(self, turn: Turn, event: Any) -> None:
        mapped = map_event(event, self.masker)
        if mapped is not None:
            self._emit_threadsafe(turn, mapped)

    async def _abort_quietly(self, turn: Turn) -> None:
        if turn.active is not None:
            try:
                await turn.active.session.abort()
            except Exception:  # noqa: BLE001
                logger.debug("abort failed", exc_info=True)

    async def abort(self, turn_id: str) -> bool:
        turn = self._turns.get(turn_id)
        if turn is None or turn.done:
            return False
        await self._abort_quietly(turn)
        return True

    async def stream(self, turn: Turn, last_event_id: int = -1) -> AsyncIterator[str]:
        index = max(last_event_id + 1, 0)
        while True:
            waiter = turn.changed
            while index < len(turn.events):
                payload = json.dumps(turn.events[index], ensure_ascii=False)
                yield f"id: {index}\ndata: {payload}\n\n"
                index += 1
            if turn.done and index >= len(turn.events):
                return
            try:
                await asyncio.wait_for(waiter.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": heartbeat\n\n"

    async def shutdown(self) -> None:
        for turn in self._turns.values():
            if turn.task and not turn.task.done():
                turn.task.cancel()
