from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    ToolExecutionStartData,
    UserMessageData,
)

from life_helper.copilot_integration.manager import ActiveSession, NoTokenError

from .conftest import sign_in


class FakeSession:
    def __init__(self, reply: str = "こんにちは", delay: float = 0.0, fail: Exception | None = None) -> None:
        self.reply = reply
        self.delay = delay
        self.fail = fail
        self.handlers: list = []
        self.prompts: list[str] = []
        self.aborted = False

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    def _fire(self, data):
        for h in list(self.handlers):
            h(SimpleNamespace(data=data))

    async def send_and_wait(self, prompt: str, timeout: float = 60):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        self._fire(ToolExecutionStartData(tool_call_id="t1", tool_name="grep", arguments={"pattern": "NISA"}))
        for ch in self.reply:
            self._fire(AssistantMessageDeltaData(delta_content=ch, message_id="m1"))
            await asyncio.sleep(self.delay)
        self._fire(AssistantMessageData(content=self.reply, message_id="m1"))
        return None

    async def get_events(self):
        return [
            SimpleNamespace(data=UserMessageData(content="質問")),
            SimpleNamespace(data=ToolExecutionStartData(tool_call_id="t1", tool_name="grep", arguments={})),
            SimpleNamespace(data=AssistantMessageData(content="回答", message_id="m1")),
        ]

    async def abort(self):
        self.aborted = True


class FakeManager:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.opened: list = []
        self.deleted: list = []
        self.no_token = False
        self.approver = None
        self.scope = None

    async def open_session(self, session_id, *, model, resume, allow_write=True, extra_tools=None, write_scope=None):
        if self.no_token:
            raise NoTokenError()
        self.opened.append((session_id, model, resume))
        self.approver = write_scope.approver if write_scope else None
        self.scope = write_scope
        if write_scope and write_scope.on_write:
            write_scope.on_write("memories/a.md", "+ fact", None)
        return ActiveSession(session=self.session, policy=None, model=model)  # type: ignore[arg-type]

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


def test_turn_rejects_sensitive_prompt_without_confirmation(client, ctx):
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

    async def send_and_wait(self, prompt: str, timeout: float = 60):
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

    async def send_and_wait(self, prompt: str, timeout: float = 60):
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
