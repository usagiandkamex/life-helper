"""Background chat turns with a replayable event buffer.

ACA's ingress ends any HTTP request after 240 seconds of wall-clock time, so a turn runs as a background task
and the browser follows it over SSE. Every event carries a sequence id, so a dropped connection resumes from
``Last-Event-ID`` without losing output.

While a turn answers, the user can send it more messages, with attachments like any other message: 「すぐに送信」
hands the message to Copilot at once and it joins the answer in progress; 「あとで送信」 waits in the turn and gets its
own answer once the answers before it are finished. The messages Copilot has not read when the turn ends (中断,
timeout, error) are returned in ``end.unsent``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from copilot.session_events import SessionErrorData, SessionIdleData, SessionMode, UserMessageData

from ..security import SecretMasker
from .attachments import ATTACHMENT_ONLY_PROMPT, PreparedAttachments
from .conversations import ConversationStore
from .events import map_event, subagent_event
from .manager import ActiveSession, CopilotManager, NoTokenError, SessionStateError
from .policy import Approval, WriteScope

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
TURN_RETENTION_SECONDS = 15 * 60
MAX_EVENTS_PER_TURN = 20_000
APPROVAL_TIMEOUT_SECONDS = 10 * 60
APPROVAL_DECISIONS = ("approve", "approve_all", "reject")
APPROVAL_REASONS = {
    "rejected": "利用者が書き込みを却下しました",
    "expired": f"{APPROVAL_TIMEOUT_SECONDS // 60} 分以内に承認されなかったため、書き込みませんでした",
    "cancelled": "回答が中断・終了したため、書き込みませんでした",
}
FOLLOW_UP_MODES = ("now", "later")
# Messages that may wait in a turn at once (「あとで送信」, and 「すぐに送信」 until it is handed to Copilot).
MAX_WAITING_MESSAGES = 10


class TurnBusyError(RuntimeError):
    pass


class TurnNotFoundError(LookupError):
    pass


class WaitingLimitError(RuntimeError):
    pass


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalResolvedError(RuntimeError):
    pass


@dataclass
class PendingApproval:
    id: str
    path: str
    future: asyncio.Future
    status: str = "pending"  # pending → approved / rejected / expired / cancelled


@dataclass
class FollowUp:
    """A message the user sent while the turn answered."""

    id: str
    text: str  # as typed; empty when the message has only attachments
    mode: str  # "now" (「すぐに送信」) or "later" (「あとで送信」)
    # The attached files as the chat shows them: {name, kind, index, truncated?}, images first.
    attachments: list[dict] = field(default_factory=list)
    # session.send's arguments: the prompt, and the images and the display prompt when there are any. Dropped once
    # the message is sent, cancelled or returned, so that its images are not kept in memory with the turn.
    request: dict = field(default_factory=dict)
    state: str = "waiting"  # waiting → sent (handed to Copilot), or cancelled (取り消し)
    message_id: str | None = None  # Copilot's id for it, once Copilot has taken it
    read_at: int | None = None  # Turn.idles when Copilot read it (its user.message arrived)

    @property
    def shown(self) -> str:
        """The text shown in the chat and stored in the session's history, as for the first message of a turn."""
        return self.text if self.text.strip() else ATTACHMENT_ONLY_PROMPT

    def take_request(self) -> tuple[str, dict]:
        """The prompt and the other arguments for session.send; the message keeps no copy of them."""
        request, self.request = self.request, {}
        return request.pop("prompt", self.shown), request

    @property
    def unsent(self) -> bool:
        """Copilot never got to it: it is still waiting, or the turn ended before Copilot read it. A message is
        read only when its user.message arrives (read_at is set), so one the runtime dropped on 中断 comes back."""
        if self.state != "sent":
            return self.state == "waiting"
        return self.read_at is None


@dataclass
class Turn:
    id: str
    conversation_id: str
    model: str = ""  # the model it answers with: the messages sent to it meanwhile are read by the same model
    events: list[dict] = field(default_factory=list)
    done: bool = False
    # Set as soon as the turn is stopping (中断・タイムアウト・終了), before anything is awaited: no write may go on.
    stopping: bool = False
    finished_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    active: ActiveSession | None = None
    task: asyncio.Task | None = None
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    # "この回答中はすべて承認": the remaining writes of this turn are approved without a card.
    approve_all: bool = False
    follow_ups: list[FollowUp] = field(default_factory=list)
    unsent: list[dict] = field(default_factory=list)
    # Progress of the answers, from the session's events: how often it went idle, the user messages Copilot has
    # read (message id → idles at that moment) and the first session error. wake is set on each of them.
    idles: int = 0
    read: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)


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

    async def start(
        self,
        conversation_id: str,
        prompt: str,
        model: str,
        *,
        attachments: list[dict] | None = None,
        display_prompt: str | None = None,
    ) -> Turn:
        if self.busy(conversation_id):
            raise TurnBusyError("this conversation is already answering")
        self._loop = asyncio.get_running_loop()
        turn = Turn(id=uuid.uuid4().hex, conversation_id=conversation_id, model=model)
        self._turns[turn.id] = turn
        self._busy[conversation_id] = turn.id
        turn.task = asyncio.create_task(self._run(turn, prompt, model, attachments or [], display_prompt))
        return turn

    # -- messages sent while the turn answers --------------------------------------------------------------

    def answering(self, conversation_id: str) -> Turn | None:
        """The turn answering in the conversation, while it still takes messages."""
        turn = self._turns.get(self._busy.get(conversation_id, ""))
        # A stopping turn takes nothing more: what is waiting in it goes back to the user as unsent.
        return turn if turn is not None and not turn.stopping else None

    def add_message(
        self,
        conversation_id: str,
        text: str,
        mode: str,
        attachments: PreparedAttachments | None = None,
        *,
        expected: Turn | None = None,
    ) -> tuple[Turn, FollowUp] | None:
        """Hands a message to the turn answering in the conversation; None when it is not answering (any more).

        Its attachments go as with the first message of a turn: the images as blobs, the files' text appended to the
        prompt and the typed text as the display prompt. At most MAX_WAITING_MESSAGES of them wait at once.

        ``expected`` is the turn the caller vision-checked; if the answering turn changed while that check awaited,
        the message is not queued (its images were not checked against the new turn's model) and None is returned.
        """
        if mode not in FOLLOW_UP_MODES:
            raise ValueError(f"unknown mode: {mode}")
        turn = self.answering(conversation_id)
        if turn is None or (expected is not None and turn is not expected):
            return None
        if sum(m.unsent for m in turn.follow_ups) >= MAX_WAITING_MESSAGES:
            raise WaitingLimitError(f"送信待ちのメッセージは {MAX_WAITING_MESSAGES} 件までです")
        files = attachments or PreparedAttachments()
        message = FollowUp(id=uuid.uuid4().hex, text=text, mode=mode, attachments=files.items)
        message.request = {"prompt": message.shown + files.text}
        if files.blobs:
            message.request["attachments"] = files.blobs
        if files.text:
            message.request["display_prompt"] = message.shown
        turn.follow_ups.append(message)
        self._emit(turn, self._message_event("queued", message))
        turn.wake.set()
        return turn, message

    def unqueue(self, turn_id: str, message_id: str) -> bool:
        """Cancels a 「あとで送信」 message; False once it has been sent or the turn is ending."""
        turn = self._turns.get(turn_id)
        if turn is None:
            raise TurnNotFoundError(turn_id)
        message = next((m for m in turn.follow_ups if m.id == message_id), None)
        if turn.stopping or message is None or message.mode != "later" or message.state != "waiting":
            return False
        message.state = "cancelled"
        message.request = {}
        self._emit(turn, {"type": "unqueued", "id": message.id})
        return True

    def unsent(self, conversation_id: str) -> list[dict]:
        """What the conversation's last turn returned as unsent, while that turn is kept."""
        finished = [t for t in self._turns.values() if t.conversation_id == conversation_id and t.done]
        latest = max(finished, key=lambda t: t.finished_at or 0.0, default=None)
        return latest.unsent if latest is not None else []

    @staticmethod
    def _next_waiting(turn: Turn, mode: str) -> FollowUp | None:
        if turn.stopping:
            return None
        return next((m for m in turn.follow_ups if m.mode == mode and m.state == "waiting"), None)

    def _emit(self, turn: Turn, event: dict) -> None:
        if len(turn.events) >= MAX_EVENTS_PER_TURN and event.get("type") == "delta":
            # The final "message" event still carries the full answer, so dropping deltas loses nothing.
            return
        turn.events.append(event)
        previous, turn.changed = turn.changed, asyncio.Event()
        previous.set()

    def _on_loop(self, callback: Callable[..., None], *args: Any) -> None:
        """Runs callback on the turn loop: the SDK and the write tools may call back from another thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            callback(*args)
        else:
            loop.call_soon_threadsafe(callback, *args)

    def _emit_threadsafe(self, turn: Turn, event: dict) -> None:
        self._on_loop(self._emit, turn, event)

    async def _run(
        self, turn: Turn, prompt: str, model: str, attachments: list[dict], display_prompt: str | None = None
    ) -> None:
        conversation = self.conversations.get(turn.conversation_id)
        scope = WriteScope(
            approver=lambda path, diff: self._approve(turn, path, diff),
            on_write=lambda path, diff, approval_id: self._emit_threadsafe(
                turn, {"type": "file_write", "path": path, "diff": diff[:4000], "approval_id": approval_id}
            ),
            is_active=lambda: not turn.done and not turn.stopping,
        )
        try:
            if conversation is None:
                raise LookupError("conversation not found")
            turn.active = await self.manager.open_session(
                turn.conversation_id, model=model, resume=conversation.started, write_scope=scope
            )
            self.conversations.update(turn.conversation_id, started=True, model=model)
            unsubscribe = turn.active.session.on(lambda ev: self._on_event(turn, ev))
            try:
                # Images go as blob attachments; other attached files are already text in the prompt. The typed
                # text goes as the display prompt, so the history knows which part of the message the user wrote.
                extra = {"attachments": attachments} if attachments else {}
                if display_prompt is not None:
                    extra["display_prompt"] = display_prompt
                await self._answer(turn, prompt, **extra)
                # 「あとで送信」: each message gets its own answer once the answers before it are finished. It is shown
                # (and counted as read) only when Copilot's user.message for it arrives, as for 「すぐに送信」.
                while (message := self._next_waiting(turn, "later")) is not None:
                    message.state = "sent"
                    later_prompt, options = message.take_request()
                    await self._answer(turn, later_prompt, message, **options)
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
            # From here on no approval or approved write may go on (the release below awaits); settle the open
            # cards before "end" so every card receives its result on the stream. Nothing is sent any more either:
            # what Copilot has not read goes back to the user.
            turn.stopping = True
            turn.unsent = [self._unsent(m) for m in turn.follow_ups if m.unsent]
            for message in turn.follow_ups:
                message.request = {}  # nothing is sent any more: the finished turn keeps no attached files
            self._cancel_approvals(turn)
            if turn.active is not None:
                await turn.active.release()
            turn.done = True
            policy = turn.active.policy if turn.active is not None else None
            if policy is not None and policy.write_scope is scope:
                # Do not keep this turn (and its events) alive through the cached session's policy.
                policy.write_scope = None
            turn.finished_at = time.monotonic()
            self._busy.pop(turn.conversation_id, None)
            self._emit(turn, {"type": "end", "unsent": turn.unsent} if turn.unsent else {"type": "end"})
            self._schedule_expiry(turn)

    def _message_event(self, kind: str, message: FollowUp) -> dict:
        """queued / user: the message as the chat shows it."""
        event = {"type": kind, "id": message.id, "text": self.masker.mask_text(message.shown), "mode": message.mode}
        return self._with_attachments(event, message)

    def _unsent(self, message: FollowUp) -> dict:
        # The text goes back to the composer as it was typed: none for a message with only attachments.
        return self._with_attachments({"id": message.id, "text": self.masker.mask_text(message.text)}, message)

    def _with_attachments(self, entry: dict, message: FollowUp) -> dict:
        """Adds the message's attached files, their names masked like the text, when it has any."""
        if message.attachments:
            entry["attachments"] = [a | {"name": self.masker.mask_text(a["name"])} for a in message.attachments]
        return entry

    async def _answer(self, turn: Turn, prompt: str, message: FollowUp | None = None, **options: Any) -> None:
        """Sends a request (options: its attachments and display prompt) and waits until Copilot has answered it,
        together with the 「すぐに送信」 messages sent meanwhile: the runtime reads them before its next model request,
        so they join the answer in progress."""
        assert turn.active is not None
        if turn.stopping:
            # 中断 came while the session was opening: send nothing.
            return
        session = turn.active.session
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        idles = turn.idles
        message_id = await session.send(prompt, **options)
        if message is not None:
            message.message_id = message_id
            # Its user.message may be handled before send returns (a runtime that reports ids fills turn.read).
            if message.read_at is None and message_id in turn.read:
                self._mark_read(turn, message, turn.read[message_id])
        steering: list[FollowUp] = []
        while True:
            while (extra := self._next_waiting(turn, "now")) is not None:
                extra.state = "sent"
                steering.append(extra)
                extra_prompt, options = extra.take_request()
                extra.message_id = await session.send(extra_prompt, mode="immediate", **options)
                # Its user.message may be handled before send returns.
                if extra.read_at is None and extra.message_id in turn.read:
                    self._mark_read(turn, extra, turn.read[extra.message_id])
            if turn.error is not None:
                raise RuntimeError(turn.error)
            # A message the runtime took after going idle starts another run, which ends with another idle: an idle
            # only finishes the messages Copilot had read before it. After 中断 the unread ones are dropped.
            if turn.idles > idles and (
                turn.stopping or all(m.read_at is not None and m.read_at < turn.idles for m in steering)
            ):
                return
            turn.wake.clear()
            await asyncio.wait_for(turn.wake.wait(), max(deadline - loop.time(), 0))

    def _on_event(self, turn: Turn, event: Any) -> None:
        self._on_loop(self._handle_event, turn, event)

    def _handle_event(self, turn: Turn, event: Any) -> None:
        data = getattr(event, "data", None)
        sub = subagent_event(event)
        if sub and isinstance(data, SessionErrorData):
            # The session's own agent hears about this as the failed task result and can carry on without it.
            logger.warning("sub-agent error: %s", self.masker.mask_text(data.message or ""))
        # Only the session's own agent drives the turn: a sub-agent's user/idle/error events must not end or fail it.
        match None if sub else data:
            case UserMessageData() as data:
                self._note_read(turn, data)
            case SessionIdleData() as data if data.mode != SessionMode.AUTOPILOT:
                turn.idles += 1
                if data.aborted:
                    # Cancelled (中断, or by the runtime itself): the messages it had not read are dropped.
                    turn.stopping = True
                turn.wake.set()
            case SessionErrorData() as data:
                if turn.error is None:
                    turn.error = f"Session error: {data.message or str(data)}"
                turn.wake.set()
                # _answer() raises turn.error and _run() emits the single user-facing error: do not emit it twice.
                return
        mapped = map_event(event, self.masker)
        if mapped is not None:
            self._emit(turn, mapped)

    def _note_read(self, turn: Turn, data: UserMessageData) -> None:
        """Records a user message Copilot has read and shows it (「すぐに送信」 and 「あとで送信」 alike)."""
        unread = [m for m in turn.follow_ups if m.state == "sent" and m.read_at is None]
        if data.message_id:
            turn.read[data.message_id] = turn.idles
            message = next((m for m in unread if m.message_id == data.message_id), None)
        else:
            # A runtime that does not report message ids: the text tells which message was read. The runtime reports
            # the display prompt, so a message with attached files is recognised by its typed text too.
            message = next((m for m in unread if m.shown == data.content), None)
        if message is not None:
            self._mark_read(turn, message, turn.idles)
        turn.wake.set()

    def _mark_read(self, turn: Turn, message: FollowUp, idles: int) -> None:
        message.read_at = idles
        self._emit(turn, self._message_event("user", message))

    # -- write approvals ---------------------------------------------------------------------------------

    async def _approve(self, turn: Turn, path: str, diff: str) -> Approval:
        """Shows an approval card for a knowledge-base write and waits for the user's decision."""
        if turn.done or turn.stopping:
            return Approval(False, APPROVAL_REASONS["cancelled"])
        if turn.approve_all:
            return Approval(True)
        approval = PendingApproval(id=uuid.uuid4().hex, path=path, future=asyncio.get_running_loop().create_future())
        turn.approvals[approval.id] = approval
        self._emit(
            turn, {"type": "approval_request", "id": approval.id, "path": path, "diff": self.masker.mask_text(diff)}
        )
        try:
            return await asyncio.wait_for(asyncio.shield(approval.future), APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            self._settle(turn, approval, "expired")
            return approval.future.result()
        except asyncio.CancelledError:
            self._settle(turn, approval, "cancelled")
            raise

    def _settle(self, turn: Turn, approval: PendingApproval, status: str) -> None:
        if approval.status != "pending":
            return
        approval.status = status
        self._emit(turn, {"type": "approval_result", "id": approval.id, "status": status})
        if not approval.future.done():
            approved = status == "approved"
            reason = "" if approved else APPROVAL_REASONS[status]
            approval.future.set_result(Approval(approved, reason, approval.id))

    def _cancel_approvals(self, turn: Turn) -> None:
        for approval in list(turn.approvals.values()):
            self._settle(turn, approval, "cancelled")

    def resolve_approval(self, turn_id: str, approval_id: str, decision: str) -> str:
        """Applies the user's decision; ``approve_all`` also approves every other open card of the turn."""
        if decision not in APPROVAL_DECISIONS:
            raise ValueError(f"unknown decision: {decision}")
        turn = self._turns.get(turn_id)
        approval = turn.approvals.get(approval_id) if turn is not None else None
        if turn is None or approval is None:
            raise ApprovalNotFoundError(approval_id)
        if approval.status != "pending":
            raise ApprovalResolvedError(approval.status)
        if decision == "approve_all":
            turn.approve_all = True
            for pending in list(turn.approvals.values()):
                self._settle(turn, pending, "approved")
        else:
            self._settle(turn, approval, "approved" if decision == "approve" else "rejected")
        return approval.status

    async def _abort_quietly(self, turn: Turn) -> None:
        # First of all: an already approved write may be waiting for the write lock and must not save any more.
        turn.stopping = True
        self._cancel_approvals(turn)
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
