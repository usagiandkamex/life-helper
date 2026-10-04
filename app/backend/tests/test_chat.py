from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    SessionErrorData,
    SessionIdleData,
    ToolExecutionStartData,
    UserMessageData,
)

from life_helper.copilot_integration.manager import ActiveSession, NoTokenError

from .conftest import sign_in


class FakeSession:
    """Works like the runtime: send() queues the message and returns its id; the messages are answered in order
    (user.message, the answer), and the session goes idle once none is left. abort() drops the queued ones."""

    def __init__(self, reply: str = "こんにちは", delay: float = 0.0, fail: Exception | None = None) -> None:
        self.reply = reply
        self.delay = delay
        self.fail = fail
        self.handlers: list = []
        self.prompts: list[str] = []
        self.modes: list[str | None] = []
        self.aborted = False
        self.cancelled = False  # the current run was aborted
        self.pending: list[tuple[str, str]] = []
        self.worker: asyncio.Task | None = None

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    def _fire(self, data):
        for h in list(self.handlers):
            h(SimpleNamespace(data=data))

    async def send(self, prompt: str, mode: str | None = None) -> str:
        self.prompts.append(prompt)
        self.modes.append(mode)
        if self.fail:
            raise self.fail
        message_id = f"u{len(self.prompts)}"
        self.pending.append((message_id, prompt))
        if self.worker is None or self.worker.done():
            self.cancelled = False
            self.worker = asyncio.create_task(self._work())
        return message_id

    async def _work(self) -> None:
        try:
            while self.pending and not self.cancelled:
                message_id, prompt = self.pending.pop(0)
                self._fire(UserMessageData(content=prompt, message_id=message_id))
                await self.answer(prompt)
        except Exception as exc:  # noqa: BLE001
            self._fire(SessionErrorData(error_type="error", message=str(exc)))
        finally:
            self._fire(SessionIdleData(aborted=self.cancelled or None))

    async def answer(self, prompt: str) -> None:
        self._fire(ToolExecutionStartData(tool_call_id="t1", tool_name="grep", arguments={"pattern": "NISA"}))
        for ch in self.reply:
            self._fire(AssistantMessageDeltaData(delta_content=ch, message_id="m1"))
            await asyncio.sleep(self.delay)
        self._fire(AssistantMessageData(content=self.reply, message_id="m1"))

    async def get_events(self):
        return [
            SimpleNamespace(data=UserMessageData(content="質問")),
            SimpleNamespace(data=ToolExecutionStartData(tool_call_id="t1", tool_name="grep", arguments={})),
            SimpleNamespace(data=AssistantMessageData(content="回答", message_id="m1")),
        ]

    async def abort(self):
        self.aborted = True
        self.cancelled = True
        self.pending.clear()


class SubAgentSession(FakeSession):
    """Works like a session that ran a research sub-agent (``task``): the sub-agent's own events share this stream
    and carry an agent_id, which the session's own events never have."""

    def _fire_sub(self, data):
        for h in list(self.handlers):
            h(SimpleNamespace(data=data, agent_id="a1"))

    def _sub_events(self) -> None:
        self._fire_sub(UserMessageData(content="サブへの依頼", message_id="s1"))
        self._fire_sub(ToolExecutionStartData(tool_call_id="s1", tool_name="web_fetch", arguments={"url": "x"}))
        self._fire_sub(AssistantMessageDeltaData(delta_content="サブの途中", message_id="sm1"))
        self._fire_sub(AssistantMessageData(content="サブの報告", message_id="sm1"))
        self._fire_sub(SessionErrorData(error_type="error", message="サブの失敗"))
        self._fire_sub(SessionIdleData())

    async def answer(self, prompt: str) -> None:
        self._fire(ToolExecutionStartData(tool_call_id="t0", tool_name="task", arguments={"agent_type": "x"}))
        self._sub_events()
        await super().answer(prompt)

    async def get_events(self):
        return [
            SimpleNamespace(data=UserMessageData(content="質問")),
            SimpleNamespace(data=ToolExecutionStartData(tool_call_id="t0", tool_name="task", arguments={})),
            SimpleNamespace(data=UserMessageData(content="サブへの依頼"), agent_id="a1"),
            SimpleNamespace(
                data=ToolExecutionStartData(tool_call_id="s1", tool_name="web_fetch", arguments={}), agent_id="a1"
            ),
            SimpleNamespace(data=AssistantMessageData(content="サブの報告", message_id="sm1"), agent_id="a1"),
            SimpleNamespace(data=AssistantMessageData(content="回答", message_id="m1")),
        ]


class FakeManager:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.opened: list = []
        self.deleted: list = []
        self.no_token = False
        self.approver = None
        self.scope = None
        self.answers: list = []

    async def open_session(self, session_id, *, model, resume, allow_write=True, extra_tools=None, write_scope=None):
        if self.no_token:
            raise NoTokenError()
        self.opened.append((session_id, model, resume))
        self.approver = write_scope.approver if write_scope else None
        self.scope = write_scope
        if write_scope and write_scope.on_write:
            write_scope.on_write("memories/a.md", "+ fact", None)
        # The turn only asks the policy for the write scope and for the per-answer sub-agent budget.
        policy = SimpleNamespace(write_scope=write_scope, begin_answer=lambda: self.answers.append(session_id))
        return ActiveSession(session=self.session, policy=policy, model=model)  # type: ignore[arg-type]

    async def delete_session(self, session_id):
        self.deleted.append(session_id)

    async def close_session(self, session_id):
        self.closed = getattr(self, "closed", []) + [session_id]

    async def list_models(self):
        return [{"id": "auto", "name": "auto"}]

    async def reset(self):
        return None

    def mark_sessions_stale(self):
        self.stale = True


def install_fake(ctx, session: FakeSession) -> FakeManager:
    fake = FakeManager(session)
    ctx.copilot = fake
    ctx.turns.manager = fake
    ctx.vault.save("gho_test_token_value", 134019422, "usagiandkamex")
    return fake


def parse_sse(text: str) -> list[tuple[int, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        eid = int(next(line for line in lines if line.startswith("id: "))[4:])
        data = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
        out.append((eid, data))
    return out


def wait_turn_done(ctx, turn_id: str) -> None:
    for _ in range(200):
        turn = ctx.turns.get(turn_id)
        if turn and turn.done:
            return
        import time

        time.sleep(0.01)
    raise AssertionError("turn did not finish")


def without_time(entry: dict) -> dict:
    """The message (or event) without the time the chat shows on it, after checking it is one the browser can read:
    a time zone is needed to show it in JST, and a fresh time to tell it apart from a leftover one."""
    rest = dict(entry)
    at = rest.pop("at", None)
    assert isinstance(at, str), f"no time: {at!r}"
    posted = datetime.fromisoformat(at)
    assert posted.tzinfo is not None, f"time without a zone: {at!r}"
    assert abs(datetime.now(UTC) - posted) < timedelta(minutes=5), f"time is not the one just made: {at!r}"
    return rest


def test_chat_flow_with_sse_replay(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, FakeSession(reply="やあ"))
    conv = client.post("/api/conversations", json={}, headers=h).json()
    assert conv["title"] == "新しい会話"

    turn = client.post(
        f"/api/conversations/{conv['id']}/turns", json={"prompt": "ふるさと納税について"}, headers=h
    ).json()
    wait_turn_done(ctx, turn["turn_id"])
    events = parse_sse(client.get(f"/api/turns/{turn['turn_id']}/events").text)
    types = [e["type"] for _, e in events]
    assert types[0] == "file_write"
    assert "tool_start" in types and "delta" in types and types[-2:] == ["done", "end"]
    assert "".join(e["text"] for _, e in events if e["type"] == "delta") == "やあ"
    assert [eid for eid, _ in events] == list(range(len(events)))

    # Reconnecting with Last-Event-ID resumes after the given event.
    replay = parse_sse(client.get(f"/api/turns/{turn['turn_id']}/events", headers={"Last-Event-ID": "2"}).text)
    assert replay[0][0] == 3

    convs = client.get("/api/conversations").json()
    assert convs[0]["title"] == "ふるさと納税について" and convs[0]["started"] is True
    assert fake.opened[0] == (conv["id"], "auto", False)

    history = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert [m["role"] for m in history] == ["user", "tool", "assistant"]

    assert client.delete(f"/api/conversations/{conv['id']}", headers=h).status_code == 200
    assert fake.deleted == [conv["id"]]


def test_sub_agent_events_do_not_end_or_answer_the_turn(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, SubAgentSession(reply="やあ"))
    conv = client.post("/api/conversations", json={}, headers=h).json()

    turn = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "3 件まとめて"}, headers=h).json()
    wait_turn_done(ctx, turn["turn_id"])
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn['turn_id']}/events").text)]
    types = [e["type"] for e in events]
    # The sub-agent's idle and error neither finished the turn early nor failed it.
    assert "error" not in types and types[-2:] == ["done", "end"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "やあ"
    # Its answer is not the answer, but its look-ups are shown (marked as the sub-agent's).
    assert all("サブ" not in json.dumps(e, ensure_ascii=False) for e in events)
    tools = [e for e in events if e["type"] == "tool_start"]
    assert [(e["name"], e.get("subagent")) for e in tools] == [
        ("task", None),
        ("web_fetch", True),
        ("grep", None),
    ]

    history = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert [m["role"] for m in history] == ["user", "tool", "tool", "assistant"]
    # The replayed tool calls keep their sub-agent marker, so the 「調査 ›」 label survives a reopen.
    assert [(m["name"], m.get("subagent")) for m in history if m["role"] == "tool"] == [
        ("task", None),
        ("web_fetch", True),
    ]
    assert [m["content"] for m in history if m["role"] != "tool"] == ["質問", "回答"]

    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, FakeSession())
    conv = client.post("/api/conversations", json={}, headers=h).json()
    resp = client.post(
        f"/api/conversations/{conv['id']}/turns", json={"prompt": "カード 4111 1111 1111 1111"}, headers=h
    )
    assert resp.status_code == 422
    ok = client.post(
        f"/api/conversations/{conv['id']}/turns",
        json={"prompt": "カード 4111 1111 1111 1111", "confirm_sensitive": True},
        headers=h,
    )
    assert ok.status_code == 200


def test_long_pasted_text_is_sent_up_to_the_limit(client, ctx):
    from life_helper.copilot_integration.api import MAX_PROMPT_CHARS

    assert MAX_PROMPT_CHARS == 50_000  # the composer says this number; both are changed together

    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, FakeSession())
    conv = client.post("/api/conversations", json={}, headers=h).json()
    # A pasted error log: it reaches Copilot whole, not clipped.
    log = "エラーが出ました\n" + "a" * (MAX_PROMPT_CHARS - len("エラーが出ました\n"))
    resp = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": log}, headers=h)
    assert resp.status_code == 200
    wait_turn_done(ctx, resp.json()["turn_id"])
    assert fake.session.prompts == [log]

    too_long = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": f"{log}a"}, headers=h)
    assert too_long.status_code == 422


def test_turn_requires_token(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, FakeSession())
    ctx.vault.clear()
    conv = client.post("/api/conversations", json={}, headers=h).json()
    resp = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "hi"}, headers=h)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "reauth"


def test_turn_errors_are_masked(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, FakeSession(fail=RuntimeError("boom gho_test_token_value")))
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "hi"}, headers=h).json()
    wait_turn_done(ctx, turn["turn_id"])
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn['turn_id']}/events").text)]
    error = next(e for e in events if e["type"] == "error")
    assert "gho_test_token_value" not in error["message"] and "***" in error["message"]


async def test_busy_conversation_is_rejected(ctx):
    from life_helper.bootstrap import init_chat, init_core
    from life_helper.copilot_integration.turns import TurnBusyError

    init_core(ctx)
    init_chat(ctx)
    install_fake(ctx, FakeSession(reply="abc", delay=0.05))
    conv = ctx.extras["conversations"].create("t", "auto")
    await ctx.turns.start(conv.id, "one", "auto")
    try:
        await ctx.turns.start(conv.id, "two", "auto")
        raise AssertionError("expected busy")
    except TurnBusyError:
        pass
    # Deletion cannot reserve a conversation that is answering.
    try:
        async with ctx.turns.reserve(conv.id):
            raise AssertionError("expected busy")
    except TurnBusyError:
        pass
    turn_id = ctx.turns.active_turn_id(conv.id)
    chunks = [c async for c in ctx.turns.stream(ctx.turns.get(turn_id))]
    assert chunks[-1].startswith("id:") and '"end"' in chunks[-1]
    assert not ctx.turns.busy(conv.id)


def test_history_while_busy_does_not_touch_session(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, FakeSession(reply="x" * 40, delay=0.02))
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "hi"}, headers=h).json()
    history = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert history["busy"] is True and history["turn_id"] == turn["turn_id"]
    assert client.get(f"/api/conversations/{conv['id']}/attachments/a").status_code == 409
    assert len(fake.opened) == 1  # only the turn opened the session
    assert client.delete(f"/api/conversations/{conv['id']}", headers=h).status_code == 409
    wait_turn_done(ctx, turn["turn_id"])


class FakeClient:
    def __init__(self, base_dir, fail_resume=False):
        self.base_dir = base_dir
        self.fail_resume = fail_resume
        self.created: list[str] = []
        self.resumed: list[str] = []
        self.deleted: list[str] = []

    async def create_session(self, session_id, **options):
        self.created.append(session_id)
        (self.base_dir / "session-state" / session_id).mkdir(parents=True, exist_ok=True)
        return FakeSession()

    async def resume_session(self, session_id, **options):
        if self.fail_resume:
            raise RuntimeError("corrupted")
        self.resumed.append(session_id)
        return FakeSession()

    async def delete_session(self, session_id):
        import shutil

        self.deleted.append(session_id)
        shutil.rmtree(self.base_dir / "session-state" / session_id, ignore_errors=True)


async def test_manager_resume_only_when_state_exists_and_delete_is_verified(ctx, tmp_path):
    import pytest

    from life_helper.copilot_integration.manager import CopilotManager, SessionStateError

    base = tmp_path / "copilot"
    manager = CopilotManager(ctx, base)
    fake_client = FakeClient(base)

    async def fake_client_factory():
        return fake_client, manager._generation

    manager.client = fake_client_factory  # type: ignore[method-assign]
    ctx.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)

    # No stored state: resume=True falls back to creating the session.
    await manager.open_session("c1", model="auto", resume=True)
    assert fake_client.created == ["c1"] and fake_client.resumed == []
    await manager.close_session("c1")
    # Stored state exists: resume is used.
    await manager.open_session("c1", model="auto", resume=True)
    assert fake_client.resumed == ["c1"]
    await manager.close_session("c1")
    # Stored state exists but resume fails: never silently create a new, empty session.
    fake_client.fail_resume = True
    with pytest.raises(SessionStateError):
        await manager.open_session("c1", model="auto", resume=True)
    assert fake_client.created == ["c1"]
    await manager.delete_session("c1")
    assert fake_client.deleted == ["c1"] and not manager.state_exists("c1")


async def test_manager_discards_session_opened_across_client_restart(ctx, tmp_path):
    import pytest

    from life_helper.copilot_integration.manager import CopilotManager, SessionStateError

    base = tmp_path / "copilot"
    manager = CopilotManager(ctx, base)
    fake_client = FakeClient(base)
    ctx.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)

    async def stale_client():
        generation = manager._generation
        manager._generation += 1  # simulate a token change/restart while the session is being opened
        return fake_client, generation

    manager.client = stale_client  # type: ignore[method-assign]
    with pytest.raises(SessionStateError):
        await manager.open_session("c2", model="auto", resume=False)
    assert "c2" not in manager._sessions


# -- write approvals ------------------------------------------------------------------------------------------


class WritingSession(FakeSession):
    """Asks the turn's approver the way the knowledge write tools do, then answers."""

    def __init__(self, paths: list[str], parallel: bool = False, then: list[str] | None = None) -> None:
        super().__init__()
        self.paths = paths
        self.parallel = parallel
        self.then = then or []
        self.manager: FakeManager | None = None
        self.results: list = []

    async def answer(self, prompt: str) -> None:
        assert self.manager is not None
        approver = self.manager.approver
        if self.parallel:
            self.results = list(await asyncio.gather(*(approver(p, f"+++ b/{p}\n+fact") for p in self.paths)))
        else:
            self.results = [await approver(p, f"+++ b/{p}\n+fact") for p in self.paths]
        # Writes asked after an "approve all" decision do not show a card.
        self.results += [await approver(p, "+later") for p in self.then]
        self._fire(AssistantMessageData(content="完了", message_id="m1"))


def start_writing_turn(client, ctx, session: WritingSession) -> tuple[dict, str]:
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, session)
    session.manager = fake
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "メモリにして"}, headers=h).json()
    return h, turn["turn_id"]


def wait_approvals(ctx, turn_id: str, count: int) -> list[dict]:
    import time

    for _ in range(300):
        turn = ctx.turns.get(turn_id)
        cards = [e for e in turn.events if e["type"] == "approval_request"] if turn else []
        if len(cards) >= count:
            return cards
        time.sleep(0.01)
    raise AssertionError("approval card did not appear")


def decide(client, h, turn_id: str, approval_id: str, decision: str):
    return client.post(f"/api/turns/{turn_id}/approvals/{approval_id}", json={"decision": decision}, headers=h)


def test_write_waits_for_approval_and_reports_the_decision(client, ctx):
    session = WritingSession(["memories/a.md", "memories/b.md"])
    h, turn_id = start_writing_turn(client, ctx, session)
    card = wait_approvals(ctx, turn_id, 1)[0]
    assert card["path"] == "memories/a.md" and "+fact" in card["diff"]
    assert not ctx.turns.get(turn_id).done  # the answer waits for the user

    assert client.post(f"/api/turns/{turn_id}/approvals/{card['id']}", json={"decision": "approve"}).status_code == 403
    assert decide(client, h, turn_id, card["id"], "maybe").status_code == 422
    assert decide(client, h, turn_id, "nope", "approve").status_code == 404
    assert decide(client, h, "no-turn", card["id"], "approve").status_code == 404
    assert decide(client, h, turn_id, card["id"], "approve").json() == {"status": "approved"}
    again = decide(client, h, turn_id, card["id"], "reject")
    assert again.status_code == 409 and again.json()["detail"]["status"] == "approved"

    second = wait_approvals(ctx, turn_id, 2)[1]
    assert decide(client, h, turn_id, second["id"], "reject").json() == {"status": "rejected"}
    wait_turn_done(ctx, turn_id)
    first, rejected = session.results
    assert first.approved and first.approval_id == card["id"]
    assert not rejected.approved and "却下" in rejected.reason

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    results = [(e["id"], e["status"]) for e in events if e["type"] == "approval_result"]
    assert results == [(card["id"], "approved"), (second["id"], "rejected")]
    assert [e["type"] for e in events][-2:] == ["done", "end"]


def test_approve_all_covers_open_and_later_writes(client, ctx):
    session = WritingSession(["memories/a.md", "memories/b.md"], parallel=True, then=["memories/c.md"])
    h, turn_id = start_writing_turn(client, ctx, session)
    cards = wait_approvals(ctx, turn_id, 2)
    assert decide(client, h, turn_id, cards[0]["id"], "approve_all").json() == {"status": "approved"}
    wait_turn_done(ctx, turn_id)
    assert all(r.approved for r in session.results) and len(session.results) == 3
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    assert len([e for e in events if e["type"] == "approval_request"]) == 2  # no card for the later write
    assert {e["id"] for e in events if e["type"] == "approval_result"} == {c["id"] for c in cards}


def test_abort_rejects_pending_approvals(client, ctx):
    session = WritingSession(["memories/a.md"])
    h, turn_id = start_writing_turn(client, ctx, session)
    card = wait_approvals(ctx, turn_id, 1)[0]
    assert client.post(f"/api/turns/{turn_id}/abort", headers=h).json() == {"aborted": True}
    wait_turn_done(ctx, turn_id)
    assert not session.results[0].approved and "中断" in session.results[0].reason
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    types = [e["type"] for e in events]
    assert types.index("approval_result") < types.index("end")
    assert next(e for e in events if e["type"] == "approval_result") == {
        "type": "approval_result",
        "id": card["id"],
        "status": "cancelled",
    }
    assert decide(client, h, turn_id, card["id"], "approve").status_code == 409


class AbortAfterApprovalSession(FakeSession):
    """Approves a write, then keeps it waiting (as the write lock would) until 中断 is pressed."""

    def __init__(self) -> None:
        super().__init__()
        self.manager: FakeManager | None = None
        self.approval = None
        self.active_after_abort: bool | None = None
        self.aborted_gate = asyncio.Event()

    async def abort(self):
        self.aborted = True
        self.aborted_gate.set()

    async def answer(self, prompt: str) -> None:
        assert self.manager is not None
        self.approval = await self.manager.approver("memories/a.md", "+++ b/memories/a.md\n+fact")
        await self.aborted_gate.wait()
        # save_knowledge_file checks the scope again just before writing the file.
        self.active_after_abort = self.manager.scope.is_active()
        self._fire(AssistantMessageData(content="完了", message_id="m1"))


def test_abort_stops_an_already_approved_write(client, ctx):
    session = AbortAfterApprovalSession()
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, session)
    session.manager = fake
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn_id = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "メモリにして"}, headers=h).json()[
        "turn_id"
    ]
    card = wait_approvals(ctx, turn_id, 1)[0]
    assert decide(client, h, turn_id, card["id"], "approve").json() == {"status": "approved"}
    assert client.post(f"/api/turns/{turn_id}/abort", headers=h).json() == {"aborted": True}
    wait_turn_done(ctx, turn_id)
    assert session.approval.approved  # the write was approved, but must not be saved after 中断
    assert session.active_after_abort is False


def test_unanswered_approval_expires(client, ctx, monkeypatch):
    from life_helper.copilot_integration import turns

    monkeypatch.setattr(turns, "APPROVAL_TIMEOUT_SECONDS", 0.2)
    session = WritingSession(["memories/a.md"])
    h, turn_id = start_writing_turn(client, ctx, session)
    wait_turn_done(ctx, turn_id)
    assert not session.results[0].approved
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    assert next(e["status"] for e in events if e["type"] == "approval_result") == "expired"


async def test_approver_refuses_after_the_turn_finished(ctx):
    from life_helper.bootstrap import init_chat, init_core
    from life_helper.copilot_integration.turns import Turn

    init_core(ctx)
    init_chat(ctx)
    turn = Turn(id="t", conversation_id="c", done=True)
    approval = await ctx.turns._approve(turn, "memories/a.md", "+x")
    assert not approval.approved and turn.events == []


class ToolCallingSession(FakeSession):
    """Calls the real write_knowledge_file handler the way the runtime does."""

    def __init__(self) -> None:
        super().__init__()
        self.tools: dict = {}
        self.result = None

    async def answer(self, prompt: str) -> None:
        from copilot import ToolInvocation

        self.result = await self.tools["write_knowledge_file"].handler(
            ToolInvocation(arguments={"path": "memories/money.md", "content": "- NISA は月 3 万円\n"})
        )
        self._fire(AssistantMessageData(content="保存しました", message_id="m1"))


class RealToolsManager(FakeManager):
    def __init__(self, session: ToolCallingSession, ctx) -> None:
        super().__init__(session)
        self.ctx = ctx
        self.policy = None

    async def open_session(self, session_id, *, model, resume, allow_write=True, extra_tools=None, write_scope=None):
        from life_helper.copilot_integration.manager import CopilotManager

        specs, policy = CopilotManager(self.ctx, self.ctx.settings.copilot_chat_dir).build_session_tools(
            allow_write=allow_write
        )
        policy.write_scope = write_scope
        self.session.tools = {spec.tool.name: spec.tool for spec in specs}
        self.policy = policy
        return ActiveSession(session=self.session, policy=policy, model=model)


def test_real_write_tool_saves_only_after_the_card_is_approved(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    session = ToolCallingSession()
    fake = RealToolsManager(session, ctx)
    ctx.copilot = fake
    ctx.turns.manager = fake
    ctx.vault.save("gho_test_token_value", 134019422, "usagiandkamex")
    target = ctx.settings.knowledge_dir / "memories" / "money.md"
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn_id = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "保存して"}, headers=h).json()[
        "turn_id"
    ]
    card = wait_approvals(ctx, turn_id, 1)[0]
    assert card["path"] == "memories/money.md" and "+- NISA は月 3 万円" in card["diff"]
    assert not target.exists()  # nothing is written while the user decides

    assert decide(client, h, turn_id, card["id"], "approve").status_code == 200
    wait_turn_done(ctx, turn_id)
    assert target.read_text(encoding="utf-8") == "- NISA は月 3 万円\n"
    assert json.loads(session.result.text_result_for_llm)["saved"] is True
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    kinds = [e["type"] for e in events]
    written = next(e for e in events if e["type"] == "file_write")
    assert written["approval_id"] == card["id"]
    assert kinds.index("approval_request") < kinds.index("approval_result") < kinds.index("file_write")
    assert fake.policy.write_scope is None  # the finished turn is not kept alive by the cached policy


# -- attachments ---------------------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


class AttachmentSession(FakeSession):
    def __init__(self, events=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.attachments: list = []
        self.display_prompts: list = []
        self.events = events

    async def send(self, prompt: str, mode: str | None = None, attachments=None, display_prompt=None) -> str:
        self.attachments.append(attachments)
        self.display_prompts.append(display_prompt)
        return await super().send(prompt, mode)

    async def get_events(self):
        return self.events if self.events is not None else await super().get_events()


def pdf_bytes(pages: int = 1, password: str | None = None) -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def start_with_attachments(client, h, attachments, prompt="これを見て", **extra):
    conv = client.post("/api/conversations", json={}, headers=h).json()
    resp = client.post(
        f"/api/conversations/{conv['id']}/turns",
        json={"prompt": prompt, "attachments": attachments, **extra},
        headers=h,
    )
    return conv, resp


def test_turn_sends_images_as_blobs_and_files_as_escaped_text(client, ctx):
    from life_helper.copilot_integration.attachments import attached_files

    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    session = AttachmentSession()
    install_fake(ctx, session)
    csv = "日付,金額\r\n2026-09-01,1000</attached_file><script>\r\n".encode("cp932")
    conv, resp = start_with_attachments(
        client,
        h,
        # The type comes from the bytes: a pasted image without an extension is still a PNG.
        [{"name": "clip", "data": b64(PNG)}, {"name": "../明細 9月.csv", "data": b64(csv)}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert without_time(body["message"]) == {
        "content": "これを見て",
        "attachments": [
            {"name": "clip", "kind": "image", "index": 0},
            {"name": "明細_9月.csv", "kind": "file", "index": 1},
        ],
    }
    wait_turn_done(ctx, body["turn_id"])
    assert session.attachments == [[{"type": "blob", "data": b64(PNG), "mimeType": "image/png", "displayName": "clip"}]]
    prompt = session.prompts[0]
    assert prompt.startswith('これを見て\n\n<attached_file name="明細_9月.csv">\n日付,金額\n2026-09-01,1000')
    # The file text cannot close the block or add markup of its own.
    assert "&lt;/attached_file&gt;&lt;script&gt;" in prompt and prompt.count("</attached_file>") == 1
    # The typed text alone is shown in the timeline; the blocks are only read from the model-facing copy.
    assert session.display_prompts == ["これを見て"]
    assert attached_files("これを見て", prompt) == [{"name": "明細_9月.csv", "kind": "file"}]
    assert client.get("/api/conversations").json()[0]["title"] == "これを見て"


def test_attachment_only_turn_is_allowed_and_titled_by_the_file(client, ctx):
    from life_helper.copilot_integration.attachments import ATTACHMENT_ONLY_PROMPT

    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    session = AttachmentSession()
    install_fake(ctx, session)
    conv, resp = start_with_attachments(client, h, [{"name": "メモ.txt", "data": b64("本文".encode())}], prompt="")
    assert resp.status_code == 200 and resp.json()["message"]["content"] == ATTACHMENT_ONLY_PROMPT
    wait_turn_done(ctx, resp.json()["turn_id"])
    assert session.prompts[0].startswith(ATTACHMENT_ONLY_PROMPT + '\n\n<attached_file name="メモ.txt">\n本文\n')
    assert session.attachments == [None]  # no image: send gets no attachments argument
    assert client.get("/api/conversations").json()[0]["title"] == "メモ.txt"

    for prompt in ("", "  \n"):
        empty = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": prompt}, headers=h)
        assert empty.status_code == 422


def test_invalid_attachments_are_rejected_before_the_turn(client, ctx, monkeypatch):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    session = AttachmentSession()
    install_fake(ctx, session)
    cases = [
        ([{"name": "家計.xlsx", "data": b64(b"PK\x03\x04")}], 400, "添付できない形式"),
        ([{"name": "fake.png", "data": b64(b"not an image")}], 400, "画像として読み込めません"),
        ([{"name": "fake.pdf", "data": b64(b"not a pdf")}], 400, "PDF として読み込めません"),
        ([{"name": "locked.pdf", "data": b64(pdf_bytes(1, "lock"))}], 400, "パスワード付き"),
        ([{"name": "binary.txt", "data": b64(b"a\x00b")}], 400, "テキストとして読み込めません"),
        ([{"name": "a.txt", "data": "!!!not base64"}], 400, "読み込めません"),
        ([{"name": "a.txt", "data": b64(b"x")}] * 6, 422, None),
    ]
    for attachments, code, message in cases:
        _, resp = start_with_attachments(client, h, attachments)
        assert resp.status_code == code, attachments[0]["name"]
        if message:
            assert message in resp.json()["detail"]

    monkeypatch.setattr(ctx.settings, "upload_max_bytes", 100)
    _, resp = start_with_attachments(client, h, [{"name": "a.txt", "data": b64(b"x" * 60)}] * 2)
    assert resp.status_code == 413
    _, resp = start_with_attachments(client, h, [{"name": "a.txt", "data": b64(b"x" * 101)}])
    assert resp.status_code == 413
    assert session.prompts == []


def test_sensitive_data_in_an_attachment_needs_confirmation(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, AttachmentSession())
    files = [{"name": "card.txt", "data": b64("カード 4111 1111 1111 1111".encode())}]
    _, resp = start_with_attachments(client, h, files, prompt="確認して")
    detail = resp.json()["detail"]
    assert resp.status_code == 422 and detail["code"] == "sensitive_data" and "添付ファイル" in detail["message"]
    _, ok = start_with_attachments(client, h, files, prompt="確認して", confirm_sensitive=True)
    assert ok.status_code == 200
    # The name is sent to Copilot as well.
    named = [{"name": "4111 1111 1111 1111.png", "data": b64(PNG)}]
    _, resp = start_with_attachments(client, h, named, prompt="見て")
    assert resp.status_code == 422 and "添付ファイル" in resp.json()["detail"]["message"]
    # A number typed at the end of the message does not run into the attached text.
    tail = [{"name": "a.txt", "data": b64(b"1111 1111")}]
    _, resp = start_with_attachments(client, h, tail, prompt="4111 1111")
    assert resp.status_code == 200


def test_attachment_items_keep_their_position_in_the_request():
    from life_helper.copilot_integration.attachments import prepare_attachments

    # The bytes decide what is an image, so the browser cannot tell the order by itself.
    prepared = prepare_attachments(
        [("memo.txt", b64(PNG)), ("a.csv", b64(b"x,y")), ("b.png", b64(PNG))], max_bytes=10**6
    )
    assert [(i["name"], i["kind"], i["index"]) for i in prepared.items] == [
        ("memo.txt", "image", 0),
        ("b.png", "image", 2),
        ("a.csv", "file", 1),
    ]


def test_images_are_rejected_for_a_model_without_vision(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    fake = install_fake(ctx, AttachmentSession())

    async def list_models():
        return [{"id": "text-only", "name": "Text", "vision": False}, {"id": "seeing", "name": "S", "vision": True}]

    fake.list_models = list_models
    image = [{"name": "clip.png", "data": b64(PNG)}]
    _, resp = start_with_attachments(client, h, image, model="text-only")
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "no_vision"
    # Without a model in the request, the conversation's model is the one checked.
    conv = client.post("/api/conversations", json={"model": "text-only"}, headers=h).json()
    resp = client.post(f"/api/conversations/{conv['id']}/turns", json={"attachments": image}, headers=h)
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "no_vision"
    # Text files work with any model; an unknown model (such as auto) is left to the runtime.
    for model, files in (("text-only", [{"name": "a.txt", "data": b64(b"x")}]), ("seeing", image), ("auto", image)):
        _, resp = start_with_attachments(client, h, files, model=model)
        assert resp.status_code == 200
        wait_turn_done(ctx, resp.json()["turn_id"])


def test_attached_text_is_limited(monkeypatch):
    from life_helper.copilot_integration import attachments as mod

    monkeypatch.setattr(mod, "MAX_TEXT_CHARS", 30)
    monkeypatch.setattr(mod, "MAX_PDF_PAGES", 2)
    prepared = mod.prepare_attachments(
        [("a.txt", b64(b"a" * 20)), ("b.md", b64(b"b" * 20)), ("c.json", b64(b"{}")), ("d.pdf", b64(pdf_bytes(3)))],
        max_bytes=10**6,
    )
    assert [i.get("truncated", False) for i in prepared.items] == [False, True, True, True]
    assert "a" * 20 in prepared.text and "b" * 10 + "\n" + mod.TRUNCATED_NOTE in prepared.text
    assert "b" * 11 not in prepared.text
    files = mod.attached_files("見て", "見て" + prepared.text)
    assert files == [{k: v for k, v in item.items() if k != "index"} for item in prepared.items]

    monkeypatch.setattr(mod, "MAX_TEXT_CHARS", 10_000)
    pdf = mod.prepare_attachments([("d.pdf", b64(pdf_bytes(3)))], max_bytes=10**6)
    # Page by page up to the page limit; a scanned page says it has no text.
    assert pdf.items == [{"name": "d.pdf", "kind": "file", "index": 0, "truncated": True}]
    assert "## ページ 2" in pdf.text and "## ページ 3" not in pdf.text and "テキストを抽出できませんでした" in pdf.text


def test_pdf_pages_never_loads_pages_past_the_limit(monkeypatch):
    from life_helper.knowledge.store import pdf_pages

    class Page:
        def __init__(self, index: int) -> None:
            self.index = index

        def extract_text(self):
            extracted.append(self.index)
            return f"text {self.index}"

    class Pages:
        def __len__(self):
            return 5

        def __getitem__(self, index: int):
            loaded.append(index)
            return Page(index)

    loaded: list[int] = []
    extracted: list[int] = []
    pages = Pages()

    class Reader:
        def __init__(self, _stream) -> None:
            self.pages = pages

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    assert list(pdf_pages(b"%PDF", max_pages=2)) == ["## ページ 1\n\ntext 0\n", "## ページ 2\n\ntext 1\n"]
    assert loaded == [0, 1]
    assert extracted == [0, 1]


def test_attached_text_encodings():
    from life_helper.copilot_integration.attachments import prepare_attachments

    for data in ("家計簿".encode(), "家計簿".encode("utf-8-sig"), "家計簿".encode("cp932"), "家計簿".encode("utf-16")):
        prepared = prepare_attachments([("a.tsv", b64(data))], max_bytes=10**6)
        assert (
            prepared.raw_text.endswith("\n\n家計簿")
            and '<attached_file name="a.tsv">\n家計簿\n</attached_file>' in prepared.text
        )


def test_history_lists_attachments_without_the_file_text(client, ctx):
    from copilot.session_events import AttachmentBlob, BinaryAssetType, SessionBinaryAssetData

    from life_helper.copilot_integration.attachments import prepare_attachments

    files = prepare_attachments([("明細.csv", b64(b"a,b")), ("長い.txt", b64(b"x" * 40_000))], max_bytes=10**6)
    # Text that itself ends with the block syntax: the typed message is stored as it was written.
    typed = '見て\n\n<attached_file name="x">\n本文\n</attached_file>'
    events = [
        SimpleNamespace(
            data=SessionBinaryAssetData(
                asset_id="a1",
                byte_length=len(PNG),
                data=b64(PNG),
                mime_type="image/png",
                type=BinaryAssetType.IMAGE,
            )
        ),
        SimpleNamespace(
            data=SessionBinaryAssetData(
                asset_id="orphan",
                byte_length=len(PNG),
                data=b64(PNG),
                mime_type="image/png",
                type=BinaryAssetType.IMAGE,
            )
        ),
        SimpleNamespace(
            data=UserMessageData(
                content=typed,
                transformed_content=typed + files.text,
                attachments=[AttachmentBlob(mime_type="image/png", display_name="clip.png", asset_id="a1")],
            )
        ),
        # A message without attachments keeps its text and gets no file chip, however it ends.
        SimpleNamespace(data=UserMessageData(content=typed)),
        SimpleNamespace(data=AssistantMessageData(content="回答", message_id="m1")),
    ]
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, AttachmentSession(events=events))
    conv = client.post("/api/conversations", json={}, headers=h).json()
    ctx.extras["conversations"].update(conv["id"], started=True)
    history = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert history[0] == {
        "role": "user",
        "content": typed,
        "attachments": [
            {
                "name": "clip.png",
                "kind": "image",
                "url": f"/api/conversations/{conv['id']}/attachments/a1",
            },
            {"name": "明細.csv", "kind": "file"},
            {"name": "長い.txt", "kind": "file", "truncated": True},
        ],
    }
    image = client.get(history[0]["attachments"][0]["url"])
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert client.get(f"/api/conversations/{conv['id']}/attachments/missing").status_code == 404
    assert client.get(f"/api/conversations/{conv['id']}/attachments/orphan").status_code == 404
    assert history[1] == {"role": "user", "content": typed}
    assert history[2] == {"role": "assistant", "content": "回答"}


def image_message(*assets) -> list:
    """A history whose one message references every given asset, as an image."""
    from copilot.session_events import AttachmentBlob, BinaryAssetType, SessionBinaryAssetData

    events = [
        SimpleNamespace(
            data=SessionBinaryAssetData(
                asset_id=asset_id, byte_length=length, data=data, mime_type=mime, type=BinaryAssetType.IMAGE
            )
        )
        for asset_id, length, data, mime in assets
    ]
    blobs = [AttachmentBlob(mime_type=mime, display_name=f"{a}.png", asset_id=a) for a, _, _, mime in assets]
    return [*events, SimpleNamespace(data=UserMessageData(content="見て", attachments=blobs))]


def test_history_images_reject_invalid_assets(client, ctx, settings):
    settings.upload_max_bytes = 64
    limit = PNG + b"\x00" * (64 - len(PNG))
    over = limit + b"\x00"
    events = image_message(
        ("limit", len(limit), b64(limit), "image/png"),
        ("over", len(over), b64(over), "image/png"),
        # The base64 text is longer than the stated size can be.
        ("long", len(PNG), b64(PNG) + "AAAA", "image/png"),
        # Of the right length, but not base64.
        ("garbled", len(PNG), "!" * len(b64(PNG)), "image/png"),
        # Padded base64 of the right length that decodes to one byte less than stated.
        ("short", len(PNG), b64(PNG[:-1]), "image/png"),
        ("text", 24, b64(b"x" * 24), "image/png"),
        ("mislabelled", len(PNG), b64(PNG), "image/jpeg"),
    )
    assert len(b64(PNG[:-1])) == len(b64(PNG))
    csrf = sign_in(client, ctx)
    install_fake(ctx, AttachmentSession(events=events))
    conv = client.post("/api/conversations", json={}, headers={"x-csrf-token": csrf}).json()
    ctx.extras["conversations"].update(conv["id"], started=True)

    image = client.get(f"/api/conversations/{conv['id']}/attachments/limit")
    assert image.status_code == 200 and image.content == limit
    for asset_id in ("over", "long", "garbled", "short", "text", "mislabelled"):
        assert client.get(f"/api/conversations/{conv['id']}/attachments/{asset_id}").status_code == 404, asset_id


@pytest.mark.parametrize("requests", [("a", "b"), ("a", "messages"), ("messages", "a"), ("messages", "messages")])
@pytest.mark.parametrize("blocked_at", ["open_session", "get_events"])
async def test_overlapping_history_image_requests_share_one_read(ctx, monkeypatch, requests, blocked_at):
    from life_helper.bootstrap import init_chat, init_core
    from life_helper.copilot_integration.api import conversation_attachment, conversation_messages
    from life_helper.copilot_integration.turns import TurnBusyError

    jpeg = b"\xff\xd8\xff" + b"\x00" * 16
    started = asyncio.Event()
    release = asyncio.Event()

    async def pause(stage):
        if blocked_at == stage:
            started.set()
            await release.wait()

    class SlowSession(AttachmentSession):
        reads = 0

        async def get_events(self):
            self.reads += 1
            await pause("get_events")
            return await super().get_events()

    init_core(ctx)
    init_chat(ctx)
    session = SlowSession(
        events=image_message(("a", len(PNG), b64(PNG), "image/png"), ("b", len(jpeg), b64(jpeg), "image/jpeg"))
    )
    fake = install_fake(ctx, session)
    open_session = fake.open_session

    async def slow_open(*args, **kwargs):
        await pause("open_session")
        return await open_session(*args, **kwargs)

    monkeypatch.setattr(fake, "open_session", slow_open)
    conv = ctx.extras["conversations"].create("", "auto")
    ctx.extras["conversations"].update(conv.id, started=True)

    async def request(kind):
        if kind == "messages":
            return await conversation_messages(conv.id, user=None, ctx=ctx)
        return await conversation_attachment(conv.id, kind, user=None, ctx=ctx)

    first = asyncio.create_task(request(requests[0]))
    await asyncio.wait_for(started.wait(), timeout=5)
    second = asyncio.create_task(request(requests[1]))
    await asyncio.sleep(0)
    # The read still keeps turns and deletion out of the conversation.
    with pytest.raises(TurnBusyError):
        async with ctx.turns.reserve(conv.id):
            pass
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    for kind, result in zip(requests, results, strict=True):
        assert not isinstance(result, Exception), result
        if kind == "messages":
            assert result == {
                "messages": [
                    {
                        "role": "user",
                        "content": "見て",
                        "attachments": [
                            {
                                "name": f"{a}.png",
                                "kind": "image",
                                "url": f"/api/conversations/{conv.id}/attachments/{a}",
                            }
                            for a in ("a", "b")
                        ],
                    }
                ],
                "busy": False,
                "unsent": [],
            }
        else:
            content, mime = (PNG, "image/png") if kind == "a" else (jpeg, "image/jpeg")
            assert (result.status_code, result.body, result.media_type) == (200, content, mime)
    assert fake.opened == [(conv.id, "auto", True)]
    assert session.reads == 1
    assert not ctx.turns.busy(conv.id)


@pytest.mark.parametrize("kind", ["image", "messages"])
async def test_history_read_does_not_reopen_a_deleted_conversation(ctx, kind):
    from fastapi import HTTPException

    from life_helper.bootstrap import init_chat, init_core
    from life_helper.copilot_integration.api import conversation_attachment, conversation_messages, delete_conversation

    init_core(ctx)
    init_chat(ctx)
    fake = install_fake(ctx, AttachmentSession(events=image_message(("a", len(PNG), b64(PNG), "image/png"))))
    conv = ctx.extras["conversations"].create("", "auto")
    ctx.extras["conversations"].update(conv.id, started=True)

    read = asyncio.create_task(
        conversation_attachment(conv.id, "a", user=None, ctx=ctx)
        if kind == "image"
        else conversation_messages(conv.id, user=None, ctx=ctx)
    )
    await asyncio.sleep(0)
    # Deleted after the request looked the conversation up, but before its read reserved it.
    assert await delete_conversation(conv.id, user=None, ctx=ctx) == {"ok": True}
    with pytest.raises(HTTPException) as e:
        await read
    assert e.value.status_code == 404
    assert fake.opened == [] and fake.deleted == [conv.id]


def test_message_times_come_from_the_session_events(client, ctx):
    from life_helper.copilot_integration.events import map_event
    from life_helper.security import SecretMasker

    posted = datetime.fromisoformat("2026-09-28T20:15:00+09:00")
    # A runtime that stamps in naive UTC, and one that does not stamp the event at all.
    answered = datetime.fromisoformat("2026-09-28T11:16:00")
    events = [
        SimpleNamespace(data=UserMessageData(content="質問"), timestamp=posted),
        SimpleNamespace(
            data=ToolExecutionStartData(tool_call_id="t1", tool_name="grep", arguments={}), timestamp=posted
        ),
        SimpleNamespace(data=AssistantMessageData(content="回答", message_id="m1"), timestamp=answered),
        SimpleNamespace(data=AssistantMessageData(content="続き", message_id="m2")),
    ]
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, AttachmentSession(events=events))
    conv = client.post("/api/conversations", json={}, headers=h).json()
    ctx.extras["conversations"].update(conv["id"], started=True)
    history = client.get(f"/api/conversations/{conv['id']}/messages").json()["messages"]
    assert history[0] == {"role": "user", "content": "質問", "at": "2026-09-28T20:15:00+09:00"}
    assert history[1] == {"role": "tool", "name": "grep", "args": "{}"}  # a look-up is not a message
    assert history[2] == {"role": "assistant", "content": "回答", "at": "2026-09-28T11:16:00+00:00"}
    assert history[3] == {"role": "assistant", "content": "続き"}

    masker = SecretMasker([])
    # The answer is stamped when it is finished: a time on every delta would about double the stream.
    assert map_event(events[2], masker) == {"type": "message", "content": "回答", "at": "2026-09-28T11:16:00+00:00"}
    delta = SimpleNamespace(data=AssistantMessageDeltaData(delta_content="回", message_id="m1"), timestamp=posted)
    assert map_event(delta, masker) == {"type": "delta", "text": "回"}


async def test_model_list_reports_vision_support(ctx, tmp_path):
    from copilot.client import ModelCapabilities, ModelInfo, ModelLimits, ModelSupports

    from life_helper.copilot_integration.manager import CopilotManager

    manager = CopilotManager(ctx, tmp_path / "copilot")

    def info(model_id: str, vision: bool) -> ModelInfo:
        return ModelInfo(
            id=model_id, name=model_id.upper(), capabilities=ModelCapabilities(ModelSupports(vision), ModelLimits())
        )

    class Client:
        async def list_models(self):
            return [info("a", True), info("b", False)]

    async def fake_client():
        return Client(), manager._generation

    manager.client = fake_client  # type: ignore[method-assign]
    assert await manager.list_models() == [
        {"id": "a", "name": "A", "vision": True},
        {"id": "b", "name": "B", "vision": False},
    ]


# -- messages sent while the turn answers ------------------------------------------------------------------


class GatedSession(FakeSession):
    """Keeps answering the first request until the gate opens (中断, or a 「すぐに送信」 message when steer_opens), so
    follow-ups can be sent while it answers."""

    def __init__(self, steer_opens: bool = True) -> None:
        super().__init__(reply="回答")
        self.steer_opens = steer_opens
        self.gate = asyncio.Event()

    async def send(self, prompt: str, mode: str | None = None) -> str:
        message_id = await super().send(prompt, mode)
        if mode == "immediate" and self.steer_opens:
            self.gate.set()
        return message_id

    async def answer(self, prompt: str) -> None:
        if prompt == self.prompts[0]:
            await self.gate.wait()
        await super().answer(prompt)

    async def abort(self):
        await super().abort()
        self.gate.set()


def start_gated_turn(client, ctx, session: GatedSession) -> tuple[dict, str, str]:
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    install_fake(ctx, session)
    conv = client.post("/api/conversations", json={}, headers=h).json()
    turn = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "最初の質問"}, headers=h).json()
    return h, conv["id"], turn["turn_id"]


def follow_up(client, h, conversation_id: str, text: str, mode: str):
    return client.post(f"/api/conversations/{conversation_id}/turns", json={"prompt": text, "mode": mode}, headers=h)


def test_turn_status_requires_sign_in(client):
    assert client.get("/api/turns/nope").status_code == 401


def test_turn_status_preserves_unread_messages_until_expiry(client, ctx, monkeypatch):
    from life_helper.copilot_integration import turns

    session = GatedSession(steer_opens=False)
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    later = follow_up(client, h, conversation_id, "あとで聞くこと", "later").json()["message_id"]
    now = follow_up(client, h, conversation_id, "すぐ伝えること", "now").json()["message_id"]
    cancelled = follow_up(client, h, conversation_id, "取り消すこと", "later").json()["message_id"]
    opened = list(ctx.copilot.opened)
    endpoint = f"/api/turns/{turn_id}"
    assert client.get(endpoint).json() == {"done": False, "unread_ids": [later, now, cancelled]}
    client.delete(f"{endpoint}/queue/{cancelled}", headers=h)
    assert client.get(endpoint).json() == {"done": False, "unread_ids": [later, now]}

    monkeypatch.setattr(turns, "TURN_RETENTION_SECONDS", 0.2)
    client.post(f"{endpoint}/abort", headers=h)
    wait_turn_done(ctx, turn_id)
    assert client.get(endpoint).json() == {"done": True, "unread_ids": [later, now]}
    assert ctx.copilot.opened == opened  # polling neither opens the runtime nor reads conversation history

    import time

    for _ in range(200):
        if client.get(endpoint).status_code == 404:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("turn status did not expire")


def test_message_sent_now_joins_the_answer(client, ctx):
    session = GatedSession()
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    sent = follow_up(client, h, conversation_id, "追加の依頼", "now").json()
    assert sent["turn_id"] == turn_id and sent["message_id"]
    wait_turn_done(ctx, turn_id)
    assert session.prompts == ["最初の質問", "追加の依頼"] and session.modes == [None, "immediate"]
    assert client.get(f"/api/turns/{turn_id}").json() == {"done": True, "unread_ids": []}

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    kinds = [e["type"] for e in events]
    queued = {"type": "queued", "id": sent["message_id"], "text": "追加の依頼", "mode": "now"}
    assert events[kinds.index("queued")] == queued
    # Shown once Copilot reads it: after the answer so far, in the same turn, with the time it was taken.
    user = kinds.index("user")
    assert without_time(events[user]) == {"type": "user", "id": sent["message_id"], "text": "追加の依頼", "mode": "now"}
    assert kinds.index("queued") < kinds.index("message") < user and kinds[user:].count("message") == 1
    assert kinds.count("done") == 1 and kinds[-2:] == ["done", "end"] and events[-1] == {"type": "end"}


def test_later_messages_wait_for_the_answer_and_can_be_cancelled(client, ctx, monkeypatch):
    from life_helper.copilot_integration import turns

    session = GatedSession(steer_opens=False)
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    first = follow_up(client, h, conversation_id, "次の質問", "later").json()
    second = follow_up(client, h, conversation_id, "取り消す質問", "later").json()
    assert first["turn_id"] == second["turn_id"] == turn_id
    assert follow_up(client, h, conversation_id, "カード 4111 1111 1111 1111", "later").status_code == 422
    monkeypatch.setattr(turns, "MAX_WAITING_MESSAGES", 2)
    full = follow_up(client, h, conversation_id, "多すぎる質問", "later")
    assert full.status_code == 409 and full.json()["detail"]["code"] == "waiting_limit"

    queue = f"/api/turns/{turn_id}/queue"
    assert client.delete(f"{queue}/{second['message_id']}").status_code == 403
    assert client.delete(f"{queue}/{second['message_id']}", headers=h).json() == {"removed": True}
    assert client.delete(f"{queue}/{second['message_id']}", headers=h).json() == {"removed": False}
    assert client.delete(f"/api/turns/nope/queue/{first['message_id']}", headers=h).status_code == 404
    ctx.turns._loop.call_soon_threadsafe(session.gate.set)
    wait_turn_done(ctx, turn_id)
    assert session.prompts == ["最初の質問", "次の質問"] and session.modes == [None, None]
    # Every answer starts with its own budget of research sub-agents, the 「あとで送信」 one included.
    assert len(ctx.turns.manager.answers) == 2
    assert client.delete(f"{queue}/{first['message_id']}", headers=h).json() == {"removed": False}

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    kinds = [e["type"] for e in events]
    assert {"type": "unqueued", "id": second["message_id"]} in events
    # Sent once the first answer is finished, and answered in the same turn.
    user = kinds.index("user")
    assert without_time(events[user]) == {
        "type": "user",
        "id": first["message_id"],
        "text": "次の質問",
        "mode": "later",
    }
    assert kinds.index("message") < user and kinds[user:].count("message") == 1
    assert kinds.count("done") == 1 and events[-1] == {"type": "end"}


def test_abort_returns_the_messages_copilot_has_not_read(client, ctx, monkeypatch):
    import time

    from life_helper.copilot_integration import turns

    session = GatedSession(steer_opens=False)
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    later = follow_up(client, h, conversation_id, "あとで聞くこと", "later").json()
    now = follow_up(client, h, conversation_id, "すぐ伝えること", "now").json()
    for _ in range(200):
        if "immediate" in session.modes:  # handed to Copilot, which has not read it yet
            break
        time.sleep(0.01)
    # A message handed to Copilot but not read yet still counts toward the waiting limit.
    monkeypatch.setattr(turns, "MAX_WAITING_MESSAGES", 2)
    full = follow_up(client, h, conversation_id, "多すぎる", "now")
    assert full.status_code == 409 and full.json()["detail"]["code"] == "waiting_limit"
    assert client.post(f"/api/turns/{turn_id}/abort", headers=h).json() == {"aborted": True}
    wait_turn_done(ctx, turn_id)
    assert session.prompts == ["最初の質問", "すぐ伝えること"]  # the later one was never sent

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    unsent = [
        {"id": later["message_id"], "text": "あとで聞くこと"},
        {"id": now["message_id"], "text": "すぐ伝えること"},
    ]
    assert events[-1] == {"type": "end", "unsent": unsent}
    assert "user" not in [e["type"] for e in events]
    # A client that was not following the turn gets them with the conversation.
    assert client.get(f"/api/conversations/{conversation_id}/messages").json()["unsent"] == unsent

    # Once the turn is over, a message starts a new turn as usual.
    again = follow_up(client, h, conversation_id, "もう一度", "now").json()
    assert again["turn_id"] != turn_id and "message_id" not in again
    wait_turn_done(ctx, again["turn_id"])
    assert client.get(f"/api/conversations/{conversation_id}/messages").json()["unsent"] == []


class GatedAttachmentSession(GatedSession):
    def __init__(self, steer_opens: bool = True) -> None:
        super().__init__(steer_opens)
        self.attachments: list = []
        self.display_prompts: list = []

    async def send(self, prompt: str, mode: str | None = None, attachments=None, display_prompt=None) -> str:
        self.attachments.append(attachments)
        self.display_prompts.append(display_prompt)
        return await super().send(prompt, mode)


def follow_up_with_files(client, h, conversation_id: str, mode: str, attachments, prompt: str = "", **extra):
    body = {"prompt": prompt, "mode": mode, "attachments": attachments, **extra}
    return client.post(f"/api/conversations/{conversation_id}/turns", json=body, headers=h)


def test_messages_sent_while_answering_carry_their_attachments(client, ctx):
    session = GatedAttachmentSession()
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    files = [{"name": "メモ.txt", "data": b64("本文".encode())}, {"name": "clip.png", "data": b64(PNG)}]
    sent = follow_up_with_files(client, h, conversation_id, "now", files, prompt="これも見て").json()
    assert sent["turn_id"] == turn_id
    wait_turn_done(ctx, turn_id)
    # As with the first message of a turn: the image as a blob, the file's text in the prompt, the typed text shown.
    assert session.modes == [None, "immediate"]
    blob = {"type": "blob", "data": b64(PNG), "mimeType": "image/png", "displayName": "clip.png"}
    assert session.attachments == [None, [blob]]
    assert session.prompts[1] == 'これも見て\n\n<attached_file name="メモ.txt">\n本文\n</attached_file>'
    assert session.display_prompts == [None, "これも見て"]

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    shown = {
        "id": sent["message_id"],
        "text": "これも見て",
        "mode": "now",
        # Images first, each with its position in the request: the browser shows its own copy as the thumbnail.
        "attachments": [
            {"name": "clip.png", "kind": "image", "index": 1},
            {"name": "メモ.txt", "kind": "file", "index": 0},
        ],
    }
    shown_events = [e for e in events if e["type"] in ("queued", "user")]
    assert [shown_events[0], *map(without_time, shown_events[1:])] == [
        {"type": "queued", **shown},
        {"type": "user", **shown},
    ]
    assert events[-1] == {"type": "end"}
    # Copilot has the files now: the turn keeps no copy.
    assert [m.request for m in ctx.turns.get(turn_id).follow_ups] == [{}]


def test_later_message_with_only_attachments(client, ctx):
    from life_helper.copilot_integration.attachments import ATTACHMENT_ONLY_PROMPT

    session = GatedAttachmentSession(steer_opens=False)
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    card = [{"name": "card.txt", "data": b64("カード 4111 1111 1111 1111".encode())}]
    refused = follow_up_with_files(client, h, conversation_id, "later", card)
    assert refused.status_code == 422 and "添付ファイル" in refused.json()["detail"]["message"]
    sent = follow_up_with_files(client, h, conversation_id, "later", [{"name": "clip.png", "data": b64(PNG)}]).json()
    ctx.turns._loop.call_soon_threadsafe(session.gate.set)
    wait_turn_done(ctx, turn_id)
    # Sent once the answer is finished. Without text or file blocks, the prompt is what the history shows.
    assert session.prompts == ["最初の質問", ATTACHMENT_ONLY_PROMPT] and session.modes == [None, None]
    blob = {"type": "blob", "data": b64(PNG), "mimeType": "image/png", "displayName": "clip.png"}
    assert session.attachments == [None, [blob]] and session.display_prompts == [None, None]

    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    assert without_time(next(e for e in events if e["type"] == "user")) == {
        "type": "user",
        "id": sent["message_id"],
        "text": ATTACHMENT_ONLY_PROMPT,
        "mode": "later",
        "attachments": [{"name": "clip.png", "kind": "image", "index": 0}],
    }


def test_unsent_messages_come_back_with_their_attachments(client, ctx):
    session = GatedAttachmentSession(steer_opens=False)
    h, conversation_id, turn_id = start_gated_turn(client, ctx, session)
    files = [{"name": "明細.csv", "data": b64(b"a,b")}]
    later = follow_up_with_files(client, h, conversation_id, "later", files).json()
    assert client.post(f"/api/turns/{turn_id}/abort", headers=h).json() == {"aborted": True}
    wait_turn_done(ctx, turn_id)
    # The text as typed (none here) and the files' names: the browser puts back its own copy of the files.
    unsent = [
        {"id": later["message_id"], "text": "", "attachments": [{"name": "明細.csv", "kind": "file", "index": 0}]}
    ]
    events = [e for _, e in parse_sse(client.get(f"/api/turns/{turn_id}/events").text)]
    assert events[-1] == {"type": "end", "unsent": unsent}
    assert client.get(f"/api/conversations/{conversation_id}/messages").json()["unsent"] == unsent
    assert [m.request for m in ctx.turns.get(turn_id).follow_ups] == [{}]


def test_images_sent_while_answering_need_the_answering_model_to_see(client, ctx):
    from life_helper.copilot_integration.api import NO_VISION_WHILE_ANSWERING

    session = GatedAttachmentSession(steer_opens=False)
    h = {"x-csrf-token": sign_in(client, ctx)}
    fake = install_fake(ctx, session)

    async def list_models():
        return [{"id": "text-only", "name": "Text", "vision": False}, {"id": "seeing", "name": "S", "vision": True}]

    fake.list_models = list_models
    conv = client.post("/api/conversations", json={"model": "text-only"}, headers=h).json()
    turn_id = client.post(f"/api/conversations/{conv['id']}/turns", json={"prompt": "質問"}, headers=h).json()[
        "turn_id"
    ]
    image = [{"name": "clip.png", "data": b64(PNG)}]
    # The model chosen in the composer does not change the answer in progress, which would read the image.
    refused = follow_up_with_files(client, h, conv["id"], "now", image, model="seeing")
    assert refused.status_code == 400
    assert refused.json()["detail"] == {"code": "no_vision", "message": NO_VISION_WHILE_ANSWERING}
    text = follow_up_with_files(client, h, conv["id"], "later", [{"name": "a.txt", "data": b64(b"x")}])
    assert text.status_code == 200 and text.json()["turn_id"] == turn_id
    ctx.turns._loop.call_soon_threadsafe(session.gate.set)
    wait_turn_done(ctx, turn_id)
    # Once the answer is over, the image starts a turn with the chosen model.
    again = follow_up_with_files(client, h, conv["id"], "now", image, model="seeing")
    assert again.status_code == 200 and "message_id" not in again.json()
    wait_turn_done(ctx, again.json()["turn_id"])
    assert fake.opened[-1][1] == "seeing" and session.attachments[-1][0]["displayName"] == "clip.png"


async def start_turn_directly(ctx, session: FakeSession, timeout: float = 5):
    from life_helper.bootstrap import init_chat, init_core

    init_core(ctx)
    init_chat(ctx)
    ctx.turns.timeout_seconds = timeout  # a turn that waits for the wrong thing fails fast
    install_fake(ctx, session)
    conv = ctx.extras["conversations"].create("t", "auto")
    return conv.id, await ctx.turns.start(conv.id, "最初の質問", "auto")


class LateSteeringSession(FakeSession):
    """The first answer is finished (the session goes idle) before a 「すぐに送信」 message reaches the runtime, so
    the message starts another run."""

    async def send(self, prompt: str, mode: str | None = None) -> str:
        if mode == "immediate":
            await self.worker
        return await super().send(prompt, mode)


async def test_message_sent_now_after_the_answer_gets_its_own_answer(ctx):
    session = LateSteeringSession(reply="abc")
    conversation_id, turn = await start_turn_directly(ctx, session)
    ctx.turns.add_message(conversation_id, "追加の依頼", "now")
    [chunk async for chunk in ctx.turns.stream(turn)]
    kinds = [e["type"] for e in turn.events]
    assert session.prompts == ["最初の質問", "追加の依頼"]
    # The turn waits for the second run instead of ending at the first idle.
    assert kinds.count("message") == 2 and kinds.index("message") < kinds.index("user")
    assert kinds[-2:] == ["done", "end"] and turn.events[-1] == {"type": "end"}


class EagerSession(FakeSession):
    """Reads a 「すぐに送信」 message at once: its user.message comes before send returns (with or without an id).
    Like the runtime, it reports the display prompt as the message's content when there is one."""

    def __init__(self, ids: bool) -> None:
        super().__init__(reply="abc")
        self.ids = ids
        self.gate = asyncio.Event()

    async def send(self, prompt: str, mode: str | None = None, attachments=None, display_prompt=None) -> str:
        if mode != "immediate":
            return await super().send(prompt, mode)
        self.prompts.append(prompt)
        self.modes.append(mode)
        message_id = f"u{len(self.prompts)}"
        self._fire(UserMessageData(content=display_prompt or prompt, message_id=message_id if self.ids else None))
        self.gate.set()
        return message_id

    async def answer(self, prompt: str) -> None:
        await self.gate.wait()
        await super().answer(prompt)


@pytest.mark.parametrize("ids", [True, False])
@pytest.mark.parametrize("only_file", [False, True])
async def test_message_read_before_send_returns_is_recognised(ctx, ids, only_file):
    from life_helper.copilot_integration.attachments import ATTACHMENT_ONLY_PROMPT, prepare_attachments

    session = EagerSession(ids)
    conversation_id, turn = await start_turn_directly(ctx, session)
    if only_file:
        # Without text of its own, the message is recognised by the text shown for it (its display prompt).
        files = prepare_attachments([("メモ.txt", b64(b"x"))], max_bytes=10**6)
        _, message = ctx.turns.add_message(conversation_id, "", "now", files)
        shown = {"text": ATTACHMENT_ONLY_PROMPT, "attachments": [{"name": "メモ.txt", "kind": "file", "index": 0}]}
    else:
        _, message = ctx.turns.add_message(conversation_id, "追加の依頼", "now")
        shown = {"text": "追加の依頼"}
    [chunk async for chunk in ctx.turns.stream(turn)]
    kinds = [e["type"] for e in turn.events]
    assert "error" not in kinds and kinds[-2:] == ["done", "end"] and turn.events[-1] == {"type": "end"}
    user = [e for e in turn.events if e["type"] == "user"]
    assert [without_time(e) for e in user] == [{"type": "user", "id": message.id, "mode": "now", **shown}]


async def test_stopping_turn_takes_no_more_messages(ctx):
    session = FakeSession()
    conversation_id, turn = await start_turn_directly(ctx, session)
    _, waiting = ctx.turns.add_message(conversation_id, "あとで聞くこと", "later")
    assert await ctx.turns.abort(turn.id)
    assert ctx.turns.add_message(conversation_id, "もうひとつ", "later") is None
    assert not ctx.turns.unqueue(turn.id, waiting.id)
    [chunk async for chunk in ctx.turns.stream(turn)]
    assert session.prompts == []  # 中断 came before the session was open: nothing was sent
    assert turn.events[-1] == {"type": "end", "unsent": [{"id": waiting.id, "text": "あとで聞くこと"}]}
