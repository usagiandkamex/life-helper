from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
import yaml
from copilot import ToolInvocation
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantUsageData,
    SessionErrorData,
    SessionIdleData,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from life_helper.automation import chat
from life_helper.automation import runner as runner_module
from life_helper.automation import store as store_module
from life_helper.automation.locks import FileLock
from life_helper.automation.models import Automation, NotifySettings, Schedule, expand_prompt
from life_helper.automation.runner import AutomationRunner, trim_events
from life_helper.automation.store import AutomationStore
from life_helper.copilot_integration.manager import ActiveSession, NoTokenError

from .conftest import sign_in

# -- schedule ---------------------------------------------------------------------------------------------


def test_daily_schedule_uses_japan_time():
    s = Schedule(kind="daily", time="09:00")
    # 2026-09-24 23:30 UTC is 2026-09-25 08:30 JST, so the next run is 09:00 JST = 00:00 UTC.
    assert s.next_after(datetime(2026, 9, 24, 23, 30, tzinfo=UTC)) == datetime(2026, 9, 25, 0, 0, tzinfo=UTC)


def test_weekly_monthly_yearly_expressions():
    assert Schedule(kind="weekly", time="07:15", weekday=0).cron_expression() == "15 7 * * 1"  # Monday
    assert Schedule(kind="weekly", time="07:15", weekday=6).cron_expression() == "15 7 * * 0"  # Sunday
    assert Schedule(kind="monthly", time="08:00", day=1).cron_expression() == "0 8 1 * *"
    assert Schedule(kind="yearly", time="08:00", day=1, month=10).cron_expression() == "0 8 1 10 *"
    assert Schedule(kind="daily").occurrences_within(datetime(2026, 1, 1, tzinfo=UTC), 30) == 30


@pytest.mark.parametrize("cron", ["not a cron", "* * * * *", "*/1 * * * *"])
def test_invalid_or_too_frequent_cron_rejected(cron):
    with pytest.raises(ValidationError):
        Schedule(kind="cron", cron=cron)


def test_expand_prompt():
    now = datetime(2026, 12, 31, 16, 0, tzinfo=UTC)  # 2027-01-01 01:00 JST
    assert (
        expand_prompt("{{today}} {{year}}/{{month}} {{weekday}} {{unknown}}", now) == "2027-01-01 2027/1 金 {{unknown}}"
    )


def test_signal_condition():
    n = NotifySettings(condition="signal", signal_field="vacancy_count", signal_op=">", signal_value=0)
    assert n.signal_met({"vacancy_count": 2}) is True
    assert n.signal_met({"vacancy_count": 0}) is False
    assert n.signal_met({}) is None


def test_max_runtime_is_capped_at_60_minutes():
    with pytest.raises(ValidationError):
        Automation(name="x", prompt="y", max_runtime_minutes=61)


# -- locks and store --------------------------------------------------------------------------------------


def test_file_lock_exclusive_and_expiry(tmp_path):
    path = tmp_path / "a.lock"
    first, second = FileLock(path, ttl_seconds=60), FileLock(path, ttl_seconds=60)
    assert first.try_acquire() and not second.try_acquire()
    second.release()  # releasing a lock you do not hold must not remove it
    assert path.exists()
    first.release()
    assert second.try_acquire()
    data = json.loads(path.read_text())
    data["expires_at"] = time.time() - 1
    path.write_text(json.dumps(data))
    third = FileLock(path, ttl_seconds=60)
    assert third.try_acquire()  # stale lock taken over


def test_store_roundtrip(tmp_path):
    store = AutomationStore(tmp_path)
    a = store.upsert(Automation(name="空室", prompt="確認して"))
    assert store.get(a.id).name == "空室"
    store.update_state(a.id, last_status="success")
    assert store.get(a.id).state.last_status == "success"
    store.save_run({"id": "abcdef12", "automation_id": a.id, "started_at": "2026-01-01", "read": False})
    assert store.unread_count() == 1
    store.mark_read(a.id, "abcdef12")
    assert store.unread_count() == 0
    now = datetime(2026, 9, 1, tzinfo=UTC)
    store.count_run(now)
    store.count_run(now)
    assert store.runs_this_month(now) == 2
    with pytest.raises(ValueError):
        store.list_runs("../../etc")
    assert store.delete(a.id) and store.get(a.id) is None


# -- runner with a fake Copilot session -------------------------------------------------------------------


class FakePolicy:
    on_tool_result = None

    def begin_answer(self):
        return None


class FakeAutoSession:
    def __init__(self, manager: FakeAutoManager) -> None:
        self.manager = manager
        self.handlers: list = []

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    async def send(self, prompt: str) -> None:
        m = self.manager
        m.prompts.append(prompt)
        if m.fail and m.fail_on_call in (None, len(m.prompts)):
            raise m.fail
        if m.signal is not None:
            m.active.policy.on_tool_result(
                "search_rakuten_vacancy", {"textResultForLlm": json.dumps({"signal": m.signal})}
            )
        for h in list(self.handlers):
            h(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
            h(SimpleNamespace(data=AssistantMessageData(content="空室を確認しました", message_id="m")))
        if len(m.prompts) >= m.report_on_call:
            report = next(s.tool for s in m.extra_tools if s.tool.name == "report_result")
            await report.handler(ToolInvocation(arguments={"summary": "要約", "notify": m.report_notify}))
        # The session's own agent going idle is what ends the wait (send_and_wait_own filters sub-agent idles).
        for h in list(self.handlers):
            h(SimpleNamespace(data=SessionIdleData()))

    async def abort(self):
        return None


class FakeAutoManager:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.opened: list = []
        self.closed: list = []
        self.deleted: list = []
        self.fail: Exception | None = None
        self.fail_on_call: int | None = None  # None fails every call
        self.signal: dict | None = None
        self.report_notify = False
        self.report_on_call = 1
        self.extra_tools: list = []
        self.active: ActiveSession | None = None

    async def open_session(
        self, session_id, *, model, resume, allow_write=True, extra_tools=None, connectors=None, on_write=None
    ):
        if isinstance(self.fail, NoTokenError):
            raise self.fail
        self.opened.append({"id": session_id, "resume": resume, "allow_write": allow_write, "connectors": connectors})
        self.extra_tools = extra_tools or []
        self.active = ActiveSession(session=FakeAutoSession(self), policy=FakePolicy(), model=model)  # type: ignore[arg-type]
        return self.active

    async def close_session(self, session_id):
        self.closed.append(session_id)

    async def delete_session(self, session_id):
        self.deleted.append(session_id)

    async def reset(self):
        return None


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def auto_env(ctx, settings):
    from life_helper.bootstrap import init_core

    settings.github_app_id = "12345"
    settings.github_app_private_key = SecretStr(_private_key_pem())
    settings.github_app_installation_id = "999"
    settings.notify_repo = "usagiandkamex/life-helper-notifications"
    init_core(ctx)
    manager = FakeAutoManager()
    runner = AutomationRunner(ctx, ctx.automations, manager=manager)  # type: ignore[arg-type]
    runner._token_checked_at = time.monotonic() + 10_000  # skip the live token check in unit tests
    ctx.vault.save("gho_unit_test_token_1234", 134019422, "usagiandkamex")
    return ctx, runner, manager


def _mock_github():
    respx.post("https://api.github.com/app/installations/999/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_installation_token_1234"})
    )
    return respx.post("https://api.github.com/repos/usagiandkamex/life-helper-notifications/issues").mock(
        return_value=httpx.Response(201, json={"html_url": "https://github.com/x/1"})
    )


async def test_send_and_wait_own_ignores_sub_agent_idle_and_error():
    """A research sub-agent shares the stream; its idle/error must not end the wait — only the root agent's does."""
    from life_helper.copilot_integration.events import send_and_wait_own

    class Session:
        def __init__(self) -> None:
            self.handlers: list = []

        def on(self, handler):
            self.handlers.append(handler)
            return lambda: self.handlers.remove(handler)

        async def send(self, prompt: str) -> None:
            # A sub-agent finishes first (error then idle); the root then answers and goes idle.
            self._fire(SessionErrorData(error_type="error", message="サブの失敗"), agent_id="a1")
            self._fire(SessionIdleData(), agent_id="a1")
            self._fire(AssistantMessageData(content="回答", message_id="m"))
            self._fire(SessionIdleData())

        def _fire(self, data, agent_id: str | None = None) -> None:
            for h in list(self.handlers):
                h(SimpleNamespace(data=data, agent_id=agent_id))

    # Completes on the root idle without raising the sub-agent's error, and unsubscribes afterwards.
    session = Session()
    await send_and_wait_own(session, "調べて")
    assert session.handlers == []


async def test_send_and_wait_own_raises_the_root_agents_error():
    from life_helper.copilot_integration.events import send_and_wait_own

    class Session:
        def __init__(self) -> None:
            self.handlers: list = []

        def on(self, handler):
            self.handlers.append(handler)
            return lambda: self.handlers.remove(handler)

        async def send(self, prompt: str) -> None:
            for h in list(self.handlers):
                h(SimpleNamespace(data=SessionErrorData(error_type="error", message="本体の失敗"), agent_id=None))

    with pytest.raises(RuntimeError, match="本体の失敗"):
        await send_and_wait_own(Session(), "調べて")


@respx.mock
async def test_run_success_without_github_notify_is_default(auto_env):
    ctx, runner, manager = auto_env
    issues = _mock_github()
    a = ctx.automations.upsert(Automation(name="毎朝", prompt="{{today}} の予定"))
    record = await runner.run(a.id)
    assert record["status"] == "success" and record["summary"] == "要約" and record["requests"] == 1
    assert record["notified"] is False and not issues.called
    assert manager.opened[0]["allow_write"] is False  # read-only by default
    assert manager.closed == [manager.opened[0]["id"]]
    assert "{{today}}" not in manager.prompts[0]
    saved = ctx.automations.get_run(a.id, record["id"])
    assert saved["status"] == "success"
    assert ctx.automations.get(a.id).state.next_run_at is not None


@respx.mock
async def test_signal_notify_only_on_change(auto_env):
    ctx, runner, manager = auto_env
    issues = _mock_github()
    notify = NotifySettings(
        github=True, condition="signal", signal_field="vacancy_count", signal_value=0, only_on_change=True
    )
    a = ctx.automations.upsert(Automation(name="楽天空室", prompt="空室確認", connectors=[], notify=notify))

    sequence = [(2, True), (3, False), (0, False), (1, True)]
    for vacancy, expected_notify in sequence:
        manager.signal = {"vacancy_count": vacancy}
        record = await runner.run(a.id)
        assert record["signals"] == {"vacancy_count": float(vacancy)}
        assert record["notified"] is expected_notify, (vacancy, record)
    assert issues.call_count == 2
    body = json.loads(issues.calls.last.request.content)
    assert "@usagiandkamex" in body["body"] and "要約" not in body["body"]  # summary excluded by default
    assert "ghs_installation_token_1234" not in body["body"]


@respx.mock
async def test_report_condition_and_include_summary(auto_env):
    ctx, runner, manager = auto_env
    issues = _mock_github()
    a = ctx.automations.upsert(
        Automation(
            name="NISA", prompt="確認", notify=NotifySettings(github=True, condition="report", include_summary=True)
        )
    )
    manager.report_notify = False
    assert (await runner.run(a.id))["notified"] is False
    manager.report_notify = True
    assert (await runner.run(a.id))["notified"] is True
    assert "要約" in json.loads(issues.calls.last.request.content)["body"]


@respx.mock
async def test_missing_report_triggers_one_follow_up(auto_env):
    ctx, runner, manager = auto_env
    _mock_github()
    manager.report_on_call = 2
    manager.report_notify = True
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True, condition="report")))
    record = await runner.run(a.id)
    assert len(manager.prompts) == 2 and "report_result" in manager.prompts[1]
    assert "report_result" in manager.prompts[0]  # the reminder is appended to the unattended prompt
    assert record["report"]["notify"] is True and record["notified"] is True

    # The report is also the result shown in the app, so it is asked for again without GitHub notify too (issue #41).
    manager.prompts.clear()
    b = ctx.automations.upsert(Automation(name="z", prompt="y"))
    record = await runner.run(b.id)
    assert len(manager.prompts) == 2 and record["report"]["summary"] == "要約"

    # A run that reported on the first call does not spend an extra request.
    manager.prompts.clear()
    manager.report_on_call = 1
    await runner.run(b.id)
    assert len(manager.prompts) == 1


async def test_follow_up_failure_keeps_the_finished_run_successful(auto_env):
    ctx, runner, manager = auto_env
    manager.report_on_call = 2  # the model does not report on the first call
    manager.fail, manager.fail_on_call = TimeoutError(), 2  # and the extra request runs out of time
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)
    # The work was already done when the report was asked for again, so the answer is kept instead of failing the run.
    assert len(manager.prompts) == 2 and record["status"] == "success" and record["report"] is None
    assert record["summary"] == "空室を確認しました"


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("follow-up failed")])
async def test_failed_follow_up_messages_do_not_replace_the_finished_answer(auto_env, monkeypatch, failure):
    ctx, runner, manager = auto_env
    manager.report_on_call = 2
    manager.fail, manager.fail_on_call = failure, 2
    send = FakeAutoSession.send

    async def send_with_follow_up_message(session, prompt):
        if manager.prompts:
            for handler in list(session.handlers):
                handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
                handler(SimpleNamespace(data=AssistantMessageData(content="報告を試みます", message_id="follow-up")))
        return await send(session, prompt)

    async def abort_with_message(session):
        for handler in list(session.handlers):
            handler(SimpleNamespace(data=AssistantMessageData(content="中断しました", message_id="abort")))

    monkeypatch.setattr(FakeAutoSession, "send", send_with_follow_up_message)
    monkeypatch.setattr(FakeAutoSession, "abort", abort_with_message)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)

    assert record["status"] == "success" and record["error"] is None and record["report"] is None
    assert record["summary"] == record["final_message"] == "空室を確認しました"
    assert [event["type"] for event in record["events"]] == ["message", "follow_up"]
    assert record["events"][0]["content"] == "空室を確認しました"
    assert record["requests"] == 2 and record["attempts"] == 1
    assert len(manager.prompts) == 2


@respx.mock
async def test_failure_retries_once_and_notifies_when_enabled(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    issues = _mock_github()
    monkeypatch.setattr("life_helper.automation.runner.asyncio.sleep", _no_sleep)
    manager.fail = RuntimeError("boom")
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True)))
    record = await runner.run(a.id)
    assert record["status"] == "error" and len(manager.prompts) == 2
    assert record["notified"] is True and issues.called


async def _no_sleep(_seconds):
    return None


async def test_timeout_is_recorded(auto_env):
    ctx, runner, manager = auto_env
    manager.fail = TimeoutError()
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)
    assert record["status"] == "timeout" and "20 分" in record["error"]


async def test_timeout_keeps_the_partial_answer(auto_env, monkeypatch):
    ctx, runner, manager = auto_env

    async def stream_then_timeout(session, prompt):
        manager.prompts.append(prompt)
        for handler in list(session.handlers):
            handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
            for chunk in ("途中まで", "書きました"):
                handler(
                    SimpleNamespace(
                        data=AssistantMessageDeltaData(delta_content=chunk, message_id="m", parent_tool_call_id=None)
                    )
                )
        raise TimeoutError

    monkeypatch.setattr(FakeAutoSession, "send", stream_then_timeout)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)

    # The run still timed out, but the text produced before it was cut off is kept as the result.
    assert record["status"] == "timeout"
    assert record["final_message"] == "途中まで書きました" and record["summary"] == "途中まで書きました"
    assert record["events"][-1] == {"type": "message", "content": "途中まで書きました", "partial": True}


async def test_successful_follow_up_stream_is_not_kept_as_partial(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    manager.report_on_call = 2  # the model reports only on the follow-up request

    async def send(session, prompt):
        manager.prompts.append(prompt)
        if len(manager.prompts) == 1:
            # The main answer finishes normally.
            for handler in list(session.handlers):
                handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
                handler(SimpleNamespace(data=AssistantMessageData(content="空室を確認しました", message_id="m")))
        else:
            # The follow-up streams chatter as deltas and then calls the terminal report tool without ever
            # finishing a message, so that leftover text must not become the run's result.
            for handler in list(session.handlers):
                handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
                handler(
                    SimpleNamespace(
                        data=AssistantMessageDeltaData(
                            delta_content="報告します", message_id="f", parent_tool_call_id=None
                        )
                    )
                )
            report = next(s.tool for s in manager.extra_tools if s.tool.name == "report_result")
            await report.handler(ToolInvocation(arguments={"summary": "要約", "notify": False}))
        for handler in list(session.handlers):
            handler(SimpleNamespace(data=SessionIdleData()))

    monkeypatch.setattr(FakeAutoSession, "send", send)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)

    assert record["status"] == "success" and record["report"]["summary"] == "要約"
    assert record["final_message"] == "空室を確認しました" and "報告します" not in record["final_message"]
    assert not any(e.get("partial") for e in record["events"])


async def test_progress_is_kept_when_the_run_is_cut_off(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CHECKPOINT_SECONDS", 0.01)
    streamed = asyncio.Event()

    async def stream_then_hang(session, prompt):
        manager.prompts.append(prompt)
        for handler in list(session.handlers):
            handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
            handler(
                SimpleNamespace(
                    data=AssistantMessageDeltaData(delta_content="途中まで", message_id="m", parent_tool_call_id=None)
                )
            )
        streamed.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(FakeAutoSession, "send", stream_then_hang)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await streamed.wait()

    # While it runs, the text streamed so far is written to the record, which stays marked as running.
    for _ in range(200):
        stored = ctx.automations.get_run(a.id, "abcdef0123456789")
        if stored and stored["final_message"]:
            break
        await asyncio.sleep(0.01)
    assert stored["status"] == "running" and stored["final_message"] == "途中まで"
    assert stored["events"][-1] == {"type": "message", "content": "途中まで", "partial": True}

    # Stopped from outside (the app shutting down): saved as interrupted, with the reason and the progress so far.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "interrupted" and stored["final_message"] == "途中まで"
    assert stored["summary"] == "途中まで" and stored["error"] == runner_module.APP_STOPPED_MESSAGE
    assert stored["finished_at"] and stored["events"][-1]["partial"] is True
    assert ctx.automations.get(a.id).state.last_status == "interrupted"


async def test_progress_leaves_out_an_unfinished_follow_up(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CHECKPOINT_SECONDS", 0.01)
    manager.report_on_call = 2
    in_follow_up = asyncio.Event()
    send = FakeAutoSession.send

    async def hang_in_follow_up(session, prompt):
        if not manager.prompts:
            return await send(session, prompt)
        manager.prompts.append(prompt)
        for handler in list(session.handlers):
            handler(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
            handler(SimpleNamespace(data=AssistantMessageData(content="報告を試みます", message_id="f1")))
            handler(
                SimpleNamespace(
                    data=AssistantMessageDeltaData(delta_content="報告しま", message_id="f2", parent_tool_call_id=None)
                )
            )
        in_follow_up.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(FakeAutoSession, "send", hang_in_follow_up)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await in_follow_up.wait()
    for _ in range(200):
        stored = ctx.automations.get_run(a.id, "abcdef0123456789")
        if stored and stored["final_message"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # more checkpoints while the follow-up is in progress

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Interrupted during the follow-up: the finished main answer is shown, never the follow-up's text.
    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "interrupted"
    assert stored["final_message"] == stored["summary"] == "空室を確認しました"
    assert [e["type"] for e in stored["events"]] == ["message", "follow_up"]
    assert not any(e.get("partial") for e in stored["events"])


async def test_finished_result_replaces_the_progress(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CHECKPOINT_SECONDS", 0.01)
    original_send = FakeAutoSession.send

    async def slow_send(session, prompt):
        for handler in list(session.handlers):
            handler(
                SimpleNamespace(
                    data=AssistantMessageDeltaData(delta_content="途中", message_id="m", parent_tool_call_id=None)
                )
            )
        await asyncio.sleep(0.1)  # long enough for checkpoints to be written
        await original_send(session, prompt)

    monkeypatch.setattr(FakeAutoSession, "send", slow_send)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)

    stored = ctx.automations.get_run(a.id, record["id"])
    assert stored["status"] == "success" and stored["final_message"] == "空室を確認しました"
    assert not any(e.get("partial") for e in stored["events"])


async def test_progress_is_kept_when_cut_off_while_notifying(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    notifying = asyncio.Event()

    async def hang(*args, **kwargs):
        notifying.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_send", hang)
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True, condition="always")))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await notifying.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The result was decided before the notification, so it is saved as it is (only the notification is missing).
    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "success" and stored["final_message"] == "空室を確認しました"
    assert stored["report"] == {"summary": "要約", "notify": False} and stored["summary"] == "要約"
    assert stored["finished_at"] and stored["notified"] is False
    # The baseline for "only on change" is left alone, so a missed notification is not lost.
    assert ctx.automations.get(a.id).state.last_condition_met is None


async def test_failure_reason_is_kept_when_cut_off_while_notifying(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    manager.fail = TimeoutError()
    notifying = asyncio.Event()

    async def hang(*args, **kwargs):
        notifying.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_send", hang)
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True)))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await notifying.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "timeout" and "20 分" in stored["error"] and "20 分" in stored["summary"]
    assert stored["finished_at"]


async def test_retry_clears_the_progress_of_the_failed_attempt(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CHECKPOINT_SECONDS", 0.01)
    retrying = asyncio.Event()

    async def send(session, prompt):
        manager.prompts.append(prompt)
        if len(manager.prompts) == 1:
            for handler in list(session.handlers):
                handler(
                    SimpleNamespace(
                        data=AssistantMessageDeltaData(delta_content="失敗前", message_id="m", parent_tool_call_id=None)
                    )
                )
            await real_sleep(0.1)  # checkpointed before the failure
            raise RuntimeError("boom")
        retrying.set()
        await real_sleep(3600)

    real_sleep = asyncio.sleep

    async def short_sleep(seconds):
        await real_sleep(min(seconds, 0.1))  # shortens the retry delay; still long enough for a checkpoint

    monkeypatch.setattr("life_helper.automation.runner.asyncio.sleep", short_sleep)
    monkeypatch.setattr(FakeAutoSession, "send", send)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await retrying.wait()
    await real_sleep(0.1)
    # The checkpoints of the new attempt replace the failed attempt's text while the run continues.
    assert ctx.automations.get_run(a.id, "abcdef0123456789")["final_message"] == ""
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the attempt that was running is kept; the failed attempt's text is not shown as the result.
    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "interrupted" and stored["final_message"] == "" and stored["events"] == []
    assert stored["summary"] == stored["error"] == runner_module.APP_STOPPED_MESSAGE


@respx.mock
async def test_reauth_notice_sent_once_per_day(auto_env):
    ctx, runner, manager = auto_env
    issues = _mock_github()
    manager.fail = NoTokenError()
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))  # GitHub notify is off for this automation
    assert (await runner.run(a.id))["status"] == "reauth"
    assert (await runner.run(a.id))["status"] == "reauth"
    assert issues.call_count == 1


async def test_monthly_limit_and_lock_and_missing_connector(auto_env, settings):
    ctx, runner, manager = auto_env
    settings.automation_monthly_run_limit = 1
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    assert (await runner.run(a.id))["status"] == "success"
    assert (await runner.run(a.id))["status"] == "skipped_limit"

    lock = runner._lock(a.id)
    assert lock.try_acquire()
    assert (await runner.run(a.id))["status"] == "skipped_locked"
    lock.release()

    settings.automation_monthly_run_limit = 100
    b = ctx.automations.upsert(Automation(name="楽天", prompt="y", connectors=["rakuten"]))
    record = await runner.run(b.id)
    assert record["status"] == "error" and "rakuten" in record["summary"]


async def test_run_due_only_runs_due_automations(auto_env):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    assert await runner.run_due(datetime(2026, 1, 1, tzinfo=UTC)) == []  # first pass only schedules
    next_run = datetime.fromisoformat(ctx.automations.get(a.id).state.next_run_at)
    assert await runner.run_due(next_run - timedelta(minutes=1)) == []
    results = await runner.run_due(next_run)
    assert [r["status"] for r in results] == ["success"]
    assert datetime.fromisoformat(ctx.automations.get(a.id).state.next_run_at) > next_run


async def test_run_due_records_real_start_time_but_shares_now(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    schedule = Schedule(kind="daily", time="00:00")  # 00:00 JST
    a = ctx.automations.upsert(Automation(name="A", prompt="{{today}} A", schedule=schedule))
    b = ctx.automations.upsert(Automation(name="B", prompt="{{today}} B", schedule=schedule))
    # 2026-01-01 23:59 JST: the shared scheduled tick expands {{today}} to 2026-01-01 and schedules 00:00 JST next day.
    scheduled = datetime(2026, 1, 1, 14, 59, tzinfo=UTC)
    for auto in (a, b):
        ctx.automations.update_state(auto.id, next_run_at=(scheduled - timedelta(minutes=1)).isoformat())

    # Real wall clock is 2026-01-02 00:01 JST and advances 1s per call, so it lands on a different JST day than the
    # scheduled tick. This makes a regression to the shared `now` visible in both the start time and the JST date.
    ticks = (datetime(2026, 1, 1, 15, 1, s, tzinfo=UTC) for s in range(1, 60))
    monkeypatch.setattr("life_helper.automation.runner.datetime", SimpleNamespace(now=lambda tz=None: next(ticks)))

    results = await runner.run_due(scheduled)

    assert [r["status"] for r in results] == ["success", "success"]
    # Each record keeps its own real start time; a shared `now` would make them identical (both == scheduled).
    # (A's start, its last progress and its finish come first.)
    assert {r["started_at"] for r in results} == {
        datetime(2026, 1, 1, 15, 1, 1, tzinfo=UTC).isoformat(),
        datetime(2026, 1, 1, 15, 1, 4, tzinfo=UTC).isoformat(),
    }
    # Prompt expansion (both the stored prompt and the sent prompt) uses the shared scheduled `now`, not the clock.
    assert all("2026-01-01" in p and "2026-01-02" not in p for p in manager.prompts)
    assert all("2026-01-01" in r["prompt"] and "2026-01-02" not in r["prompt"] for r in results)
    # Scheduling the next run also uses the shared `now`: 00:00 JST on 2026-01-02 (= 2026-01-01 15:00 UTC).
    expected_next = schedule.next_after(scheduled).isoformat()
    assert ctx.automations.get(a.id).state.next_run_at == expected_next
    assert ctx.automations.get(b.id).state.next_run_at == expected_next


# -- API --------------------------------------------------------------------------------------------------


def test_automation_api(client, ctx):
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    body = {
        "name": "楽天トラベル空室チェック",
        "prompt": "12/30-31 大人2名で空室を確認",
        "schedule": {"kind": "daily", "time": "09:00"},
        "notify": {"github": True, "condition": "signal", "signal_field": "vacancy_count", "only_on_change": True},
    }
    created = client.post("/api/automations", json=body, headers=h).json()
    assert created["notify"]["github"] is True and created["allow_write"] is False
    assert created["estimated_runs_per_month"] == 30 and created["state"]["next_run_at"]
    assert created["max_runtime_minutes"] == 20

    listing = client.get("/api/automations").json()
    assert listing["usage"]["estimated_runs_per_month"] == 30 and listing["github_notify_configured"] is False

    bad = dict(body, notify={"github": True, "condition": "signal"})
    assert client.post("/api/automations", json=bad, headers=h).status_code == 400
    assert client.post("/api/automations", json=dict(body, connectors=["nope"]), headers=h).status_code == 400
    longest = client.post("/api/automations", json=dict(body, max_runtime_minutes=60), headers=h)
    assert longest.status_code == 200 and longest.json()["max_runtime_minutes"] == 60
    assert client.delete(f"/api/automations/{longest.json()['id']}", headers=h).status_code == 200
    assert client.post("/api/automations", json=dict(body, max_runtime_minutes=61), headers=h).status_code == 422
    assert client.post("/api/automations", json=dict(body, prompt="口座番号: 1234567"), headers=h).status_code == 422

    updated = client.put(f"/api/automations/{created['id']}", json=dict(body, name="改名"), headers=h).json()
    assert updated["name"] == "改名" and updated["id"] == created["id"]
    assert client.get("/api/automations/runs").json() == []
    assert client.delete(f"/api/automations/{created['id']}", headers=h).status_code == 200
    assert client.get("/api/automations/x/runs/y").status_code in (400, 404)


def test_automations_saved_with_stooq_keep_the_stock_tools(client, ctx):
    """Stooq was replaced by Yahoo Finance; automations that selected it keep getting the stock-price tools."""
    saved = Automation.model_validate({"name": "株価", "prompt": "更新", "connectors": ["stooq", "yahoo_finance"]})
    assert saved.connectors == ["yahoo_finance"]
    csrf = sign_in(client, ctx)
    body = {
        "name": "株価",
        "prompt": "株価を更新",
        "schedule": {"kind": "daily", "time": "09:00"},
        "connectors": ["stooq"],
    }
    created = client.post("/api/automations", json=body, headers={"x-csrf-token": csrf})
    assert created.status_code == 200 and created.json()["connectors"] == ["yahoo_finance"]


def test_automations_saved_with_rakuten_travel_keep_the_rakuten_tools(client, ctx, settings):
    """The Rakuten Travel connector became the Rakuten Web Service connector; saved automations follow the rename."""
    saved = Automation.model_validate({"name": "空室", "prompt": "確認", "connectors": ["rakuten_travel", "rakuten"]})
    assert saved.connectors == ["rakuten"]
    legacy = Automation(name="空室", prompt="確認").model_dump(mode="json") | {"connectors": ["rakuten_travel"]}
    ctx.automations.path.parent.mkdir(parents=True, exist_ok=True)
    ctx.automations.path.write_text(yaml.safe_dump([legacy], allow_unicode=True), encoding="utf-8")
    assert ctx.automations.get(legacy["id"]).connectors == ["rakuten"]
    settings.rakuten_application_id = SecretStr("app-id-123456")
    settings.rakuten_access_key = SecretStr("access-key-123456")
    ctx.extras.pop("connectors", None)
    csrf = sign_in(client, ctx)
    body = {
        "name": "空室",
        "prompt": "空室を確認",
        "schedule": {"kind": "daily", "time": "09:00"},
        "connectors": ["rakuten_travel"],
    }
    created = client.post("/api/automations", json=body, headers={"x-csrf-token": csrf})
    assert created.status_code == 200 and created.json()["connectors"] == ["rakuten"]
    options = client.get("/api/automations").json()["connectors"]
    assert [c["name"] for c in options if c["name"].startswith("rakuten")] == ["rakuten_csv", "rakuten"]


# -- regression tests for the second review round -----------------------------------------------------------


async def test_run_restricts_connectors_and_cleans_up_new_mode_sessions(auto_env):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    await runner.run(a.id)
    assert manager.opened[0]["connectors"] == []  # only the selected connectors are exposed
    assert manager.opened[0]["id"].endswith("-0")  # attempt-specific session id
    assert manager.deleted == [manager.opened[0]["id"]]  # new-mode session state is removed

    manager.opened.clear()
    manager.deleted.clear()
    b = ctx.automations.upsert(Automation(name="c", prompt="y", conversation_mode="continue"))
    await runner.run(b.id)
    assert manager.opened[0]["id"] == f"auto-{b.id}" and manager.opened[0]["resume"] is True
    assert manager.deleted == []  # continue-mode history is kept


async def test_run_record_is_sanitized(auto_env):
    ctx, runner, manager = auto_env
    ctx.masker.add("SECRET-API-KEY-123")
    manager.report_notify = False
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))

    original = FakeAutoSession.send

    async def leaky(self, prompt):
        report = next(s.tool for s in self.manager.extra_tools if s.tool.name == "report_result")
        await report.handler(
            ToolInvocation(arguments={"summary": "口座番号: 1234567 key SECRET-API-KEY-123", "notify": False})
        )
        for h in list(self.handlers):
            h(SimpleNamespace(data=SessionIdleData()))

    FakeAutoSession.send = leaky
    try:
        record = await runner.run(a.id)
    finally:
        FakeAutoSession.send = original
    stored = json.dumps(ctx.automations.get_run(a.id, record["id"]), ensure_ascii=False)
    assert "1234567" not in stored and "SECRET-API-KEY-123" not in stored


async def test_report_result_accepts_summaries_up_to_the_limit(auto_env):
    """A result may be up to MAX_SUMMARY_CHARS long. A longer one fails the tool call (so the model can shorten it and
    call again) and is never recorded (issue #68)."""
    from life_helper.copilot_integration.system_prompt import MAX_SUMMARY_CHARS

    assert MAX_SUMMARY_CHARS == 40_000  # as long as a result can be written in one go (issue #77)
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    full = "🏠" + "あ" * (MAX_SUMMARY_CHARS - 1)  # counted in code points, so the emoji is one character
    calls: list[list[str]] = []
    results: list = []
    original = FakeAutoSession.send

    async def reports(self, prompt):
        self.manager.prompts.append(prompt)
        tool = next(s.tool for s in self.manager.extra_tools if s.tool.name == "report_result")
        for summary in calls.pop(0):
            results.append(await tool.handler(ToolInvocation(arguments={"summary": summary, "notify": False})))
        for h in list(self.handlers):
            h(SimpleNamespace(data=SessionIdleData()))

    FakeAutoSession.send = reports
    try:
        calls[:] = [[full + "い", full]]
        record = await runner.run(a.id)
        tool = next(s.tool for s in manager.extra_tools if s.tool.name == "report_result")
        assert tool.parameters["properties"]["summary"]["maxLength"] == MAX_SUMMARY_CHARS
        assert results[0].result_type == "failure" and "summary" in results[0].text_result_for_llm
        assert results[1].result_type == "success"
        assert record["report"]["summary"] == full and record["summary"] == full

        # Only too-long reports: nothing is recorded, even after the follow-up request.
        results.clear()
        calls[:] = [[full + "い"], [full + "い"]]
        record = await runner.run(a.id)
        assert [r.result_type for r in results] == ["failure", "failure"]
        assert record["status"] == "success" and record["report"] is None and record["summary"] == ""
    finally:
        FakeAutoSession.send = original


async def test_no_retry_after_side_effects(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr("life_helper.automation.runner.asyncio.sleep", _no_sleep)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    original = FakeAutoSession.send

    async def fails_after_tool(self, prompt):
        self.manager.prompts.append(prompt)
        from copilot.session_events import ToolExecutionStartData

        for h in list(self.handlers):
            h(SimpleNamespace(data=ToolExecutionStartData(tool_call_id="t", tool_name="edit", arguments={})))
        raise RuntimeError("boom after writing")

    FakeAutoSession.send = fails_after_tool
    try:
        record = await runner.run(a.id)
    finally:
        FakeAutoSession.send = original
    assert record["status"] == "error" and len(manager.prompts) == 1


@respx.mock
async def test_only_on_change_retries_when_delivery_failed(auto_env):
    ctx, runner, manager = auto_env
    respx.post("https://api.github.com/app/installations/999/access_tokens").mock(return_value=httpx.Response(500))
    notify = NotifySettings(github=True, condition="signal", signal_field="vacancy_count", only_on_change=True)
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=notify))
    manager.signal = {"vacancy_count": 1}
    first = await runner.run(a.id)
    assert first["notified"] is False and first["notify_error"]
    assert ctx.automations.get(a.id).state.last_condition_met is not True  # baseline not advanced

    _mock_github()
    second = await runner.run(a.id)
    assert second["notified"] is True


@respx.mock
async def test_revoked_token_follows_reauth_path(auto_env):
    ctx, runner, manager = auto_env
    runner._token_checked_at = None
    ctx.vault.save("gho_revoked_token_value", 134019422, "usagiandkamex")
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(401))
    issues = _mock_github()
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    record = await runner.run(a.id)
    assert record["status"] == "reauth" and manager.prompts == []
    assert issues.call_count == 1  # once-a-day re-login notice


def test_monthly_reservation_is_atomic(tmp_path):
    store = AutomationStore(tmp_path)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    assert store.try_reserve_run(2, now) and store.try_reserve_run(2, now)
    assert not store.try_reserve_run(2, now)
    assert store.runs_this_month(now) == 2


def test_automation_api_rejects_secrets(client, ctx):
    csrf = sign_in(client, ctx)
    ctx.masker.add("rakuten-access-key-987654")
    body = {"name": "x", "prompt": "キーは rakuten-access-key-987654", "schedule": {"kind": "daily", "time": "09:00"}}
    resp = client.post("/api/automations", json=body, headers={"x-csrf-token": csrf})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "secret"


# -- chat view of finished runs (issue #31) ----------------------------------------------------------------


def _chat_record(automation_id: str, run_id: str, *, mode: str = "new", minute: int = 0, **extra) -> dict:
    started = datetime(2026, 9, 25, 0, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return {
        "id": run_id,
        "automation_id": automation_id,
        "name": "定期チェック",
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(seconds=30)).isoformat(),
        "read": False,
        "notified": False,
        "transcript_version": 1,
        "conversation_mode": mode,
        "prompt": "確認して",
        "status": "success",
        "summary": "要約",
        "events": [{"type": "message", "content": "確認しました"}],
    } | extra


def test_run_list_leaves_out_results_and_transcripts(client, ctx, monkeypatch):
    """The list only names the runs and the result is read from the run itself, so long results (up to 40,000
    characters each, issues #68 and #77) do not make the list heavy."""
    sign_in(client, ctx)
    store = ctx.automations
    store.upsert(Automation(id="aaaaaa000001", name="定期チェック", prompt="確認して", schedule=Schedule(kind="daily")))
    store.save_run(
        _chat_record(
            "aaaaaa000001",
            "a000000000000001",
            report={"summary": "要約", "notify": True},
            final_message="確認しました",
            error="途中で一部のページを読めませんでした",
            notified=True,
        )
    )

    # An index written before it cached "notified" is not trusted, so the list still says the run was notified.
    index = json.loads(store.run_index_path.read_text(encoding="utf-8"))
    for entry in index["runs"].values():
        entry["meta"].pop("notified")
    store.run_index_path.write_text(json.dumps({"runs": index["runs"]}), encoding="utf-8")
    assert client.get("/api/automations/runs").json()[0]["notified"] is True

    reads = _watch_run_reads(monkeypatch, store)
    [listed] = client.get("/api/automations/runs").json()
    assert reads == []  # answered from the metadata index without opening the record
    # Only what the list shows: no result, error, instruction or transcript.
    assert listed == {
        "id": "a000000000000001",
        "automation_id": "aaaaaa000001",
        "name": "定期チェック",
        "started_at": "2026-09-25T00:00:00+00:00",
        "finished_at": "2026-09-25T00:00:30+00:00",
        "status": "success",
        "read": False,
        "notified": True,
    }

    detail = client.get("/api/automations/aaaaaa000001/runs/a000000000000001").json()
    assert detail["summary"] == "要約" and detail["report"]["summary"] == "要約"
    assert detail["final_message"] == "確認しました" and detail["prompt"] == "確認して" and detail["events"]
    assert detail["error"] == "途中で一部のページを読めませんでした"


async def test_run_record_carries_a_transcript(auto_env):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="毎朝", prompt="{{today}} の予定", conversation_mode="continue"))
    record = await runner.run(a.id, now=datetime(2026, 9, 25, 0, 0, tzinfo=UTC))
    assert record["transcript_version"] == 1 and record["conversation_mode"] == "continue"
    assert record["prompt"] == "2026-09-25 の予定"  # expanded, without the report reminder sent to Copilot
    # The reminder says where the result is shown, so it is written for the run history (issue #41).
    assert "report_result" in manager.prompts[0] and "実行履歴" in manager.prompts[0]
    assert record["attempts"] == 1 and record["events_omitted"] == 0
    assert [e["type"] for e in record["events"]] == ["message"]


async def test_runs_that_did_not_start_are_still_part_of_the_conversation(auto_env, settings):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="楽天", prompt="空室は？", connectors=["rakuten_travel"]))
    missing = await runner.run(a.id)
    assert missing["status"] == "error" and missing["prompt"] == "空室は？" and missing["transcript_version"] == 1
    settings.automation_monthly_run_limit = 0
    b = ctx.automations.upsert(Automation(name="x", prompt="y"))
    skipped = await runner.run(b.id)
    assert skipped["status"] == "skipped_limit" and skipped["prompt"] == "y"
    ctx.vault.clear()
    c = ctx.automations.upsert(Automation(name="z", prompt="z"))
    reauth = await runner.run(c.id)
    assert reauth["status"] == "reauth" and manager.prompts == []
    shape = {"events": [], "events_omitted": 0, "attempts": 0, "requests": 0, "report": None, "final_message": ""}
    for record in (missing, skipped, reauth):
        assert {k: record[k] for k in shape} == shape  # one shape for every transcript-bearing record
    ids = {t["id"] for t in chat.list_threads(ctx.automations)}
    assert {f"r-{a.id}-{missing['id']}", f"r-{b.id}-{skipped['id']}", f"r-{c.id}-{reauth['id']}"} <= ids


async def test_run_prompt_is_sanitized(auto_env):
    ctx, runner, manager = auto_env
    # The API refuses such prompts; one that still reaches the file is masked in the record.
    a = ctx.automations.upsert(Automation(name="x", prompt="口座番号: 1234567 を確認"))
    record = await runner.run(a.id)
    assert "1234567" not in record["prompt"]


@respx.mock
async def test_follow_up_request_is_marked_in_the_transcript(auto_env):
    ctx, runner, manager = auto_env
    _mock_github()
    manager.report_on_call = 2
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True, condition="report")))
    record = await runner.run(a.id)
    assert [e["type"] for e in record["events"]] == ["message", "follow_up", "message"]


def test_trim_events_keeps_the_follow_up_marker():
    events = [{"type": "message", "content": str(i)} for i in range(5)] + [{"type": "follow_up"}]
    events += [{"type": "tool_start", "id": str(i)} for i in range(300)]
    trimmed, omitted = trim_events(events, 200)
    assert len(trimmed) == 200 and omitted == 106
    assert trimmed[0] == {"type": "follow_up"} and trimmed[-1] == {"type": "tool_start", "id": "299"}
    assert trim_events(events[:10], 200) == (events[:10], 0)


async def test_chat_threads_follow_the_conversation_setting(auto_env):
    ctx, runner, manager = auto_env
    new = ctx.automations.upsert(Automation(name="毎回", prompt="y"))
    cont = ctx.automations.upsert(Automation(name="続ける", prompt="y", conversation_mode="continue"))
    first = await runner.run(new.id, now=datetime(2026, 9, 25, 0, 0, tzinfo=UTC))
    second = await runner.run(new.id, now=datetime(2026, 9, 25, 1, 0, tzinfo=UTC))
    c1 = await runner.run(cont.id, now=datetime(2026, 9, 25, 2, 0, tzinfo=UTC))
    c2 = await runner.run(cont.id, now=datetime(2026, 9, 25, 3, 0, tzinfo=UTC))
    # Records from before transcripts existed are left to the automations page.
    ctx.automations.save_run({"id": "0dd0000000000001", "automation_id": new.id, "started_at": "2026-09-01"})
    ctx.automations.save_run(_chat_record(new.id, "0ab0000000000001") | {"transcript_version": None})

    threads = chat.list_threads(ctx.automations)
    by_id = {t["id"]: t for t in threads}
    assert set(by_id) == {f"r-{new.id}-{first['id']}", f"r-{new.id}-{second['id']}", f"c-{cont.id}"}
    assert by_id[f"c-{cont.id}"]["run_count"] == 2 and by_id[f"c-{cont.id}"]["latest_run_id"] == c2["id"]
    assert by_id[f"c-{cont.id}"]["title"] == "続ける" and by_id[f"c-{cont.id}"]["unread"] is True
    assert threads[0]["id"] == f"c-{cont.id}"  # most recently updated first

    detail = chat.get_thread(ctx.automations, f"c-{cont.id}")
    assert [r["id"] for r in detail["runs"]] == [c1["id"], c2["id"]]  # oldest first
    assert detail["has_more"] is False and detail["has_newer"] is False

    # Renaming follows the automation; a deleted automation keeps its conversations under the recorded name.
    ctx.automations.upsert(cont.model_copy(update={"name": "改名"}))
    assert chat.get_thread(ctx.automations, f"c-{cont.id}")["thread"]["title"] == "改名"
    ctx.automations.delete(cont.id)
    assert chat.get_thread(ctx.automations, f"c-{cont.id}")["thread"]["title"] == "続ける"


async def test_switching_the_conversation_setting_keeps_each_run_where_it_ran(auto_env):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="x", prompt="y", conversation_mode="continue"))
    c1 = await runner.run(a.id, now=datetime(2026, 9, 25, 0, 0, tzinfo=UTC))
    ctx.automations.upsert(ctx.automations.get(a.id).model_copy(update={"conversation_mode": "new"}))
    n1 = await runner.run(a.id, now=datetime(2026, 9, 25, 1, 0, tzinfo=UTC))
    ids = {t["id"] for t in chat.list_threads(ctx.automations)}
    assert ids == {f"c-{a.id}", f"r-{a.id}-{n1['id']}"}
    assert [r["id"] for r in chat.get_thread(ctx.automations, f"c-{a.id}")["runs"]] == [c1["id"]]


def test_hidden_continue_thread_comes_back_with_its_whole_history(tmp_path):
    store = AutomationStore(tmp_path)
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001", mode="continue", minute=0))
    store.save_run(_chat_record("aaaaaa000001", "a000000000000002", mode="continue", minute=1))
    assert chat.hide_thread(store, "c-aaaaaa000001", "a000000000000002")
    assert chat.list_threads(store) == []

    store.save_run(_chat_record("aaaaaa000001", "a000000000000003", mode="continue", minute=2))
    [thread] = chat.list_threads(store)
    assert thread["id"] == "c-aaaaaa000001" and thread["run_count"] == 3
    runs = chat.get_thread(store, "c-aaaaaa000001")["runs"]
    assert [r["id"] for r in runs] == ["a000000000000001", "a000000000000002", "a000000000000003"]

    # Hiding with the run the user saw, while a newer one arrived meanwhile, does not hide the newer one.
    store.save_run(_chat_record("aaaaaa000001", "a000000000000004", mode="continue", minute=3))
    assert chat.hide_thread(store, "c-aaaaaa000001", "a000000000000003")
    assert [t["id"] for t in chat.list_threads(store)] == ["c-aaaaaa000001"]
    assert not chat.hide_thread(store, "c-aaaaaa000001", "b000000000000009")  # not part of the conversation
    # Hiding never rewrites the run records.
    assert "hidden" not in json.dumps(store.list_runs(limit=None))


def test_hidden_new_thread_can_still_be_opened(tmp_path):
    store = AutomationStore(tmp_path)
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001"))
    assert chat.hide_thread(store, "r-aaaaaa000001-a000000000000001", "a000000000000001")
    assert chat.list_threads(store) == []
    assert chat.get_thread(store, "r-aaaaaa000001-a000000000000001")["runs"][0]["id"] == "a000000000000001"
    assert len(store.list_runs()) == 1  # the automations page keeps the run


def test_mark_thread_read_only_marks_the_runs_that_were_shown(tmp_path):
    store = AutomationStore(tmp_path)
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001", mode="continue", minute=0))
    store.save_run(_chat_record("aaaaaa000001", "a000000000000002", mode="continue", minute=1))
    store.save_run(_chat_record("bbbbbb000001", "b000000000000001", minute=2))
    chat.mark_thread_read(store, "c-aaaaaa000001", ["a000000000000001", "b000000000000001", "../x"])
    assert store.get_run("aaaaaa000001", "a000000000000001")["read"] is True
    assert store.get_run("aaaaaa000001", "a000000000000002")["read"] is False
    assert store.get_run("bbbbbb000001", "b000000000000001")["read"] is False  # other conversation


@pytest.mark.parametrize("anchored", [False, True])
def test_mark_thread_read_batches_index_updates(tmp_path, anchored):
    store = AutomationStore(tmp_path)
    automation_id = "aaaaaa000001"
    thread_id = f"c-{automation_id}"
    ids = [f"a{i:015x}" for i in range(chat.ANCHOR_LIMIT + 2)]
    for i, run_id in enumerate(ids):
        store.save_run(_chat_record(automation_id, run_id, mode="continue", minute=i))
    page = chat.get_thread(store, thread_id, anchor=ids[1] if anchored else None)
    shown_ids = [r["id"] for r in page["runs"]]
    assert len(shown_ids) == (chat.ANCHOR_LIMIT if anchored else chat.PAGE_SIZE)
    store.mark_read(automation_id, shown_ids[0])
    store.save_run(_chat_record(automation_id, "ffffffffffffffff", mode="continue", minute=len(ids)))
    store.save_run(_chat_record(automation_id, "eeeeeeeeeeeeeeee"))
    with (
        patch.object(store, "_read_run_index", wraps=store._read_run_index) as index_reads,
        patch.object(store_module, "atomic_write", wraps=store_module.atomic_write) as writes,
    ):
        assert chat.mark_thread_read(
            store, thread_id, shown_ids + [shown_ids[-1], "eeeeeeeeeeeeeeee", "dddddddddddddddd", "../x"]
        )
        assert index_reads.call_count == 2  # thread metadata lookup and the single batched update
        paths = [call.args[0] for call in writes.call_args_list]
        assert paths.count(store.run_index_path) == 1
        assert len(paths) == len(shown_ids)  # already-read and duplicate records are not rewritten

        writes.reset_mock()
        assert chat.mark_thread_read(store, thread_id, shown_ids)
        assert chat.mark_thread_read(store, thread_id, [])
        assert not chat.mark_thread_read(store, "c-bbbbbb000001", shown_ids)
        writes.assert_not_called()

    for meta in store.list_run_meta():
        record = store.get_run(automation_id, meta["id"])
        assert meta["read"] is (meta["id"] in shown_ids)
        assert record["read"] == meta["read"]
        assert record["events"] == [{"type": "message", "content": "確認しました"}]
    assert store.unread_count() == len(ids) + 2 - len(shown_ids)


def test_thread_paging_and_anchor(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path)
    ids = [f"a{i:015x}" for i in range(25)]
    for i, run_id in enumerate(ids):
        store.save_run(_chat_record("aaaaaa000001", run_id, mode="continue", minute=i))
    latest = chat.get_thread(store, "c-aaaaaa000001")
    assert [r["id"] for r in latest["runs"]] == ids[5:] and latest["has_more"] is True
    older = chat.get_thread(store, "c-aaaaaa000001", before=ids[5])
    assert [r["id"] for r in older["runs"]] == ids[:5] and older["has_more"] is False
    anchored = chat.get_thread(store, "c-aaaaaa000001", anchor=ids[2])
    assert anchored["runs"][0]["id"] == ids[2] and anchored["has_more"] is True and anchored["has_newer"] is False
    monkeypatch.setattr(chat, "ANCHOR_LIMIT", 5)
    anchored = chat.get_thread(store, "c-aaaaaa000001", anchor=ids[2])
    assert [r["id"] for r in anchored["runs"]] == ids[2:7] and anchored["has_newer"] is True
    with pytest.raises(ValueError):
        chat.get_thread(store, "c-aaaaaa000001", before="ffffffffffffffff")
    with pytest.raises(ValueError):
        chat.get_thread(store, "c-aaaaaa000001", anchor="ffffffffffffffff")  # a stale link must not show another run
    with pytest.raises(ValueError):
        chat.get_thread(store, "c-aaaaaa000001", before=ids[5], anchor=ids[2])
    assert chat.get_thread(store, "c-cccccc000001") is None


def test_runs_with_equal_start_times_have_a_stable_order(tmp_path):
    store = AutomationStore(tmp_path)
    # Written in reverse id order with identical timestamps: the latest run is still decided by id.
    for run_id in ("a000000000000003", "a000000000000001", "a000000000000002"):
        store.save_run(_chat_record("aaaaaa000001", run_id, mode="continue"))
    [thread] = chat.list_threads(store)
    assert thread["latest_run_id"] == "a000000000000003"
    runs = chat.get_thread(store, "c-aaaaaa000001")["runs"]
    assert [r["id"] for r in runs] == ["a000000000000001", "a000000000000002", "a000000000000003"]
    assert chat.hide_thread(store, "c-aaaaaa000001", "a000000000000003")
    assert chat.list_threads(store) == []


def test_every_conversation_is_listed_beyond_the_run_history_limit(tmp_path):
    store = AutomationStore(tmp_path)
    for i in range(60):
        store.save_run(_chat_record("aaaaaa000001", f"a{i:015x}", minute=i))
    assert len(store.list_runs()) == 50  # the automations page shows the latest 50
    assert len(chat.list_threads(store)) == 60


def _watch_run_reads(monkeypatch, store: AutomationStore) -> list[str]:
    """Collects the ids of the run records that are opened from here on."""
    reads: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args, **kwargs):
        if self.is_relative_to(store.runs_dir):
            reads.append(self.stem)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    return reads


def test_listing_conversations_does_not_open_the_transcripts(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path)
    for i in range(3):
        store.save_run(_chat_record("aaaaaa000001", f"a{i:015x}", mode="continue", minute=i))
    reads = _watch_run_reads(monkeypatch, store)
    [thread] = chat.list_threads(store)
    assert thread["run_count"] == 3 and thread["unread"] is True
    assert reads == []  # the metadata index answers the list

    # A record written before the index existed is read once and remembered.
    legacy = _chat_record("aaaaaa000001", "a00000000000009", mode="continue", minute=9)
    (store.runs_dir / "aaaaaa000001" / "a00000000000009.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert chat.list_threads(store)[0]["run_count"] == 4
    assert reads == ["a00000000000009"]
    reads.clear()
    assert chat.list_threads(store)[0]["run_count"] == 4
    assert reads == []


def test_stale_and_lost_index_entries_are_repaired_from_the_records(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path)
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001", mode="continue", minute=0))
    store.save_run(_chat_record("aaaaaa000001", "a000000000000002", mode="continue", minute=1))

    # A record changed behind the index's back (or saved while the index could not be written) is picked up again.
    monkeypatch.setattr(AutomationStore, "_with_lock", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("busy")))
    store.save_run(_chat_record("aaaaaa000001", "a000000000000002", mode="continue", minute=1, read=True))
    monkeypatch.undo()
    assert store.unread_count() == 1
    assert chat.list_threads(store)[0]["unread"] is True

    store.run_index_path.write_text("{ broken", encoding="utf-8")
    assert [t["run_count"] for t in chat.list_threads(store)] == [2]

    # Entries of removed records do not pile up in the index, and a scan of one automation keeps the others.
    store.save_run(_chat_record("bbbbbb000001", "b000000000000001", minute=2))
    (store.runs_dir / "aaaaaa000001" / "a000000000000001.json").unlink()
    assert [m["id"] for m in store.list_run_meta("aaaaaa000001")] == ["a000000000000002"]
    assert sorted(json.loads(store.run_index_path.read_text(encoding="utf-8"))["runs"]) == [
        "aaaaaa000001/a000000000000002",
        "bbbbbb000001/b000000000000001",
    ]


def test_a_conversation_page_only_opens_the_runs_it_shows(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path)
    ids = [f"a{i:015x}" for i in range(25)]
    for i, run_id in enumerate(ids):
        store.save_run(_chat_record("aaaaaa000001", run_id, mode="continue", minute=i))
    reads = _watch_run_reads(monkeypatch, store)
    page = chat.get_thread(store, "c-aaaaaa000001")
    assert [r["id"] for r in page["runs"]] == ids[5:] and page["thread"]["run_count"] == 25
    assert reads == ids[5:]
    reads.clear()
    assert [r["id"] for r in store.list_runs(limit=3)] == ids[:-4:-1]  # the run history loads only what it returns
    assert reads == ids[:-4:-1]


@pytest.mark.parametrize(
    "thread_id",
    ["x", "c-", "c-../etc", "r-aaaaaa000001", "c-aaaaaa000001-a000000000000001", "C-AAAAAA000001", "c-aaaaaa000001\n"],
)
def test_invalid_thread_ids_are_rejected(thread_id):
    with pytest.raises(ValueError):
        chat.parse_thread_id(thread_id)


def test_chat_api(client, ctx):
    assert client.get("/api/automations/chat").status_code == 401
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    store = ctx.automations
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001", mode="continue", minute=0))
    store.save_run(_chat_record("aaaaaa000001", "a000000000000002", mode="continue", minute=1))
    store.save_run(_chat_record("bbbbbb000001", "b000000000000001", minute=2))

    threads = client.get("/api/automations/chat").json()
    assert [t["id"] for t in threads] == ["r-bbbbbb000001-b000000000000001", "c-aaaaaa000001"]
    assert all("events" not in t for t in threads)
    detail = client.get("/api/automations/chat/c-aaaaaa000001").json()
    assert [r["id"] for r in detail["runs"]] == ["a000000000000001", "a000000000000002"]
    assert detail["runs"][0]["events"][0]["content"] == "確認しました" and detail["runs"][0]["prompt"] == "確認して"
    assert client.get("/api/automations/chat/c-aaaaaa000001?anchor=a000000000000002").json()["runs"][0]["id"] == (
        "a000000000000002"
    )
    assert client.get("/api/automations/chat/c-aaaaaa000001?before=ffffffffffffffff").status_code == 400
    assert client.get("/api/automations/chat/c-aaaaaa000001?anchor=ffffffffffffffff").status_code == 400
    assert client.get(f"/api/automations/chat/c-aaaaaa000001?anchor={'a' * 500}").status_code == 422
    assert client.get("/api/automations/chat/bad..id").status_code == 400
    assert client.get("/api/automations/chat/c-cccccc000001").status_code == 404

    body = {"run_ids": ["a000000000000001", "a000000000000002"]}
    assert client.post("/api/automations/chat/c-aaaaaa000001/read", json=body).status_code == 403  # no CSRF
    assert client.post("/api/automations/chat/c-cccccc000001/read", json=body, headers=h).status_code == 404
    too_long = {"run_ids": ["a" * 500]}
    assert client.post("/api/automations/chat/c-aaaaaa000001/read", json=too_long, headers=h).status_code == 422
    too_many = {"run_ids": ["a000000000000001"] * 101}
    assert client.post("/api/automations/chat/c-aaaaaa000001/read", json=too_many, headers=h).status_code == 422
    read = client.post("/api/automations/chat/c-aaaaaa000001/read", json=body, headers=h).json()
    assert read["unread"] == 1 and client.get("/api/automations").json()["unread"] == 1

    hide = {"run_id": "b000000000000001"}
    assert client.post("/api/automations/chat/r-bbbbbb000001-b000000000000001/hide", json=hide).status_code == 403
    assert client.post("/api/automations/chat/c-aaaaaa000001/hide", json=hide, headers=h).status_code == 404
    bad_hide = {"run_id": "../../x"}
    assert client.post("/api/automations/chat/c-aaaaaa000001/hide", json=bad_hide, headers=h).status_code == 422
    assert (
        client.post("/api/automations/chat/r-bbbbbb000001-b000000000000001/hide", json=hide, headers=h).status_code
        == 200
    )
    assert [t["id"] for t in client.get("/api/automations/chat").json()] == ["c-aaaaaa000001"]
    assert len(client.get("/api/automations/runs").json()) == 3  # the run history keeps hidden runs

    run = client.get("/api/automations/bbbbbb000001/runs/b000000000000001").json()
    assert run["chat_thread_id"] == "r-bbbbbb000001-b000000000000001"
    store.save_run({"id": "0dd0000000000001", "automation_id": "bbbbbb000001", "started_at": "2026-09-01"})
    assert client.get("/api/automations/bbbbbb000001/runs/0dd0000000000001").json()["chat_thread_id"] is None


# -- runs in progress (issue #61) ---------------------------------------------------------------------------


def _running_record(automation_id: str, run_id: str, *, started: datetime | None = None) -> dict:
    """A record as the runner writes it when a run starts: no result and no transcript yet."""
    return {
        "id": run_id,
        "automation_id": automation_id,
        "name": "定期チェック",
        "started_at": (started or datetime(2026, 9, 25, 0, 0, tzinfo=UTC)).isoformat(),
        "status": "running",
        "read": False,
        "transcript_version": 1,
        "conversation_mode": "new",
        "prompt": "確認して",
        "events": [],
    }


async def test_a_run_is_in_the_history_while_it_runs(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="毎朝", prompt="予定"))
    during: dict = {}
    session = runner._run_session

    async def spy(*args, **kwargs):
        during["runs"] = ctx.automations.list_runs(a.id)
        during["unread"] = ctx.automations.unread_count()
        during["threads"] = chat.list_threads(ctx.automations)
        await session(*args, **kwargs)

    monkeypatch.setattr(runner, "_run_session", spy)
    record = await runner.run(a.id, run_id="a000000000000001")

    # While it runs the history shows the run without a result, and there is nothing to read or to replay yet.
    assert record["id"] == "a000000000000001"
    assert [(r["id"], r["status"]) for r in during["runs"]] == [(record["id"], "running")]
    assert "summary" not in during["runs"][0] and during["runs"][0]["events"] == []
    assert during["unread"] == 0 and during["threads"] == []
    # The same record is replaced by the result, so a run never appears twice.
    assert [(r["id"], r["status"]) for r in ctx.automations.list_runs(a.id)] == [(record["id"], "success")]
    assert ctx.automations.unread_count() == 1
    assert [t["id"] for t in chat.list_threads(ctx.automations)] == [f"r-{a.id}-{record['id']}"]


def test_marking_a_run_in_progress_read_does_not_replace_the_result(tmp_path):
    store = AutomationStore(tmp_path)
    aid, rid = "aaaaaa000001", "a000000000000001"
    store.save_run(_running_record(aid, rid))
    store.mark_read(aid, rid)
    assert store.get_run(aid, rid)["read"] is False  # the record the runner is about to replace is left alone
    store.save_run(_chat_record(aid, rid))  # the run finishes
    store.mark_read(aid, rid)
    saved = store.get_run(aid, rid)
    assert saved["read"] is True and saved["summary"] == "要約"


def test_runs_api_shows_a_run_in_progress_and_an_abandoned_one(client, ctx, settings):
    csrf = sign_in(client, ctx)
    store = ctx.automations
    now = datetime.now(UTC)
    store.save_run(_running_record("aaaaaa000001", "a000000000000001", started=now))
    left_behind = now - timedelta(seconds=settings.automation_lock_ttl_seconds + 60)
    store.save_run(_running_record("aaaaaa000001", "a000000000000002", started=left_behind))

    runs = {r["id"]: r for r in client.get("/api/automations/runs").json()}
    assert runs["a000000000000001"]["status"] == "running"
    # A run that is still recorded as running long after it could be is shown as interrupted, not as running.
    assert runs["a000000000000002"]["status"] == "interrupted"
    detail = client.get("/api/automations/aaaaaa000001/runs/a000000000000002").json()
    assert detail["status"] == "interrupted" and detail["chat_thread_id"] is None
    assert store.get_run("aaaaaa000001", "a000000000000002")["status"] == "running"  # the record is left alone

    assert client.get("/api/automations").json()["unread"] == 0  # a run without a result is not unread
    for rid in ("a000000000000001", "a000000000000002"):
        read = client.post(f"/api/automations/aaaaaa000001/runs/{rid}/read", headers={"x-csrf-token": csrf})
        assert read.status_code == 200 and store.get_run("aaaaaa000001", rid)["read"] is False


def test_a_run_in_progress_without_a_usable_start_time_stays_running(client, ctx):
    sign_in(client, ctx)
    record = _running_record("aaaaaa000001", "a000000000000001")
    record["started_at"] = "not a time"
    ctx.automations.save_run(record)
    assert client.get("/api/automations/runs").json()[0]["status"] == "running"


def test_a_start_time_without_a_timezone_is_read_as_utc(client, ctx):
    sign_in(client, ctx)
    record = _running_record("aaaaaa000001", "a000000000000001")
    record["started_at"] = "2026-09-25 00:00:00"  # written by an older version
    ctx.automations.save_run(record)
    assert client.get("/api/automations/runs").json()[0]["status"] == "interrupted"


def test_automation_list_reports_runs_in_progress_beyond_the_history(client, ctx, settings):
    sign_in(client, ctx)
    store = ctx.automations
    store.upsert(Automation(id="aaaaaa000001", name="定期チェック", prompt="確認して", schedule=Schedule(kind="daily")))
    store.upsert(Automation(id="aaaaaa000002", name="定期チェック", prompt="確認して", schedule=Schedule(kind="daily")))
    now = datetime.now(UTC)
    store.save_run(_running_record("aaaaaa000001", "a000000000000001", started=now - timedelta(minutes=5)))
    left_behind = now - timedelta(seconds=settings.automation_lock_ttl_seconds + 60)
    store.save_run(_running_record("aaaaaa000002", "a000000000000002", started=left_behind))
    store.save_run(_running_record("aaaaaa000009", "a000000000000009", started=now))  # a deleted automation
    for i in range(50):  # newer runs push the one in progress out of the (capped) history
        record = _running_record("aaaaaa000003", f"b{i:015d}", started=now - timedelta(seconds=i))
        store.save_run(record | {"status": "success" if i % 2 else "error", "read": i < 10, "summary": "要約"})

    assert "a000000000000001" not in {r["id"] for r in client.get("/api/automations/runs").json()}
    # The interrupted run is not reported as running, so its automation can be started again.
    with patch.object(store, "list_run_meta", wraps=store.list_run_meta) as scan:
        listing = client.get("/api/automations").json()
    scan.assert_called_once_with()
    assert listing["running_automation_ids"] == ["aaaaaa000001"]
    assert listing["unread"] == 40


def test_automation_list_scans_empty_history_once(client, ctx):
    sign_in(client, ctx)
    with patch.object(ctx.automations, "list_run_meta", wraps=ctx.automations.list_run_meta) as scan:
        listing = client.get("/api/automations").json()
    scan.assert_called_once_with()
    assert listing["running_automation_ids"] == [] and listing["unread"] == 0


def test_run_now_returns_the_id_reserved_for_its_task(client, ctx):
    csrf = sign_in(client, ctx)
    automation = ctx.automations.upsert(Automation(name="定期チェック", prompt="確認して", max_runtime_minutes=40))
    # Saved from another screen after this one listed it: the reply tells the settings the run uses (issue #77).
    ctx.automations.upsert(automation.model_copy(update={"name": "家探し", "max_runtime_minutes": 60}))
    runner = SimpleNamespace(
        run=AsyncMock(return_value={"status": "success"}), manager=SimpleNamespace(reset=AsyncMock())
    )
    ctx.extras["automation_runner"] = runner

    response = client.post(f"/api/automations/{automation.id}/run", headers={"x-csrf-token": csrf})

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert response.json() == {
        "started": True,
        "run_id": run_id,
        "runner": "app",
        "name": "家探し",
        "max_runtime_minutes": 60,
    }
    assert len(run_id) == 16
    runner.run.assert_called_once_with(automation.id, run_id=run_id)


# -- deleting runs from the history (issue #67) -------------------------------------------------------------


def test_delete_run_api(client, ctx, settings):
    store = ctx.automations
    aid = "aaaaaa000001"
    now = datetime.now(UTC)
    store.save_run(_chat_record(aid, "a000000000000001"))
    store.save_run(_chat_record(aid, "a000000000000002", minute=1))
    store.save_run(_running_record(aid, "a000000000000003", started=now))
    left_behind = now - timedelta(seconds=settings.automation_lock_ttl_seconds + 60)
    store.save_run(_running_record(aid, "a000000000000004", started=left_behind))
    # Written by an older version without a time zone: read as UTC, like the rest of the history.
    store.save_run(_running_record(aid, "a000000000000005", started=left_behind.replace(tzinfo=None)))
    url = f"/api/automations/{aid}/runs"

    assert client.delete(f"{url}/a000000000000001").status_code == 401
    csrf = sign_in(client, ctx)
    h = {"x-csrf-token": csrf}
    assert client.delete(f"{url}/a000000000000001").status_code == 403  # no CSRF
    assert client.delete(f"{url}/not-a-run", headers=h).status_code == 400
    assert client.delete("/api/automations/BAD/runs/a000000000000001", headers=h).status_code == 400
    assert client.delete(f"{url}/ffffffffffffffff", headers=h).status_code == 404
    # A run in progress is replaced by its result when it finishes, so it cannot be deleted yet.
    running = client.delete(f"{url}/a000000000000003", headers=h)
    assert running.status_code == 409 and "実行中" in running.json()["detail"]
    assert store.get_run(aid, "a000000000000003") is not None

    deleted = client.delete(f"{url}/a000000000000001", headers=h)
    assert deleted.status_code == 200 and deleted.json() == {"ok": True, "unread": 1}
    # One that stopped while running (shown as interrupted) can be deleted.
    assert client.delete(f"{url}/a000000000000004", headers=h).status_code == 200
    assert client.delete(f"{url}/a000000000000005", headers=h).status_code == 200
    assert client.delete(f"{url}/a000000000000001", headers=h).status_code == 404

    assert [r["id"] for r in client.get("/api/automations/runs").json()] == ["a000000000000003", "a000000000000002"]
    assert client.get(f"{url}/a000000000000001").status_code == 404
    assert [t["id"] for t in client.get("/api/automations/chat").json()] == [f"r-{aid}-a000000000000002"]
    assert client.get(f"/api/automations/chat/r-{aid}-a000000000000001").status_code == 404
    index = json.loads(store.run_index_path.read_text(encoding="utf-8"))["runs"]
    assert f"{aid}/a000000000000001" not in index and f"{aid}/a000000000000004" not in index


def test_deleting_the_latest_run_of_a_hidden_conversation_keeps_it_hidden(tmp_path):
    store = AutomationStore(tmp_path)
    aid, thread = "aaaaaa000001", "c-aaaaaa000001"
    for i in range(2):
        store.save_run(_chat_record(aid, f"a00000000000000{i + 1}", mode="continue", minute=i))
    store.hide_chat_thread(thread, "a000000000000002")

    outcome, record = store.delete_run(aid, "a000000000000002", in_progress=lambda r: False)
    assert outcome == "deleted"
    chat.forget_run(store, record)

    # The user had seen the older run as well, so the conversation stays hidden until a new run arrives.
    assert store.chat_hidden() == {thread: "a000000000000001"} and chat.list_threads(store) == []
    store.save_run(_chat_record(aid, "a000000000000003", mode="continue", minute=2))
    assert [t["id"] for t in chat.list_threads(store)] == [thread]


def test_deleting_an_older_hidden_run_keeps_the_conversation_shown(tmp_path):
    store = AutomationStore(tmp_path)
    aid, thread = "aaaaaa000001", "c-aaaaaa000001"
    store.save_run(_chat_record(aid, "a000000000000001", mode="continue", minute=0))
    store.hide_chat_thread(thread, "a000000000000001")
    store.save_run(_chat_record(aid, "a000000000000002", mode="continue", minute=1))  # an unseen run: shown again

    _, record = store.delete_run(aid, "a000000000000001", in_progress=lambda r: False)
    chat.forget_run(store, record)

    assert store.chat_hidden() == {}
    assert [t["id"] for t in chat.list_threads(store)] == [thread]


def test_deleting_a_hidden_single_run_conversation_forgets_it(tmp_path):
    store = AutomationStore(tmp_path)
    store.save_run(_chat_record("aaaaaa000001", "a000000000000001"))
    store.hide_chat_thread("r-aaaaaa000001-a000000000000001", "a000000000000001")
    _, record = store.delete_run("aaaaaa000001", "a000000000000001", in_progress=lambda r: False)
    chat.forget_run(store, record)
    assert store.chat_hidden() == {}


async def test_a_run_deleted_while_it_runs_is_not_written_back(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="毎朝", prompt="予定"))
    session = runner._run_session

    async def deleted_meanwhile(*args, **kwargs):
        await session(*args, **kwargs)
        # Deleted from the history after the app stopped showing it as running (interrupted).
        assert ctx.automations.delete_run(a.id, "a000000000000001", in_progress=lambda r: False)[0] == "deleted"

    monkeypatch.setattr(runner, "_run_session", deleted_meanwhile)
    record = await runner.run(a.id, run_id="a000000000000001")

    assert record["status"] == "success"
    assert ctx.automations.list_runs(a.id) == []
    assert ctx.automations.get(a.id).state.last_status == "success"


def test_marking_a_run_read_does_not_bring_back_a_deleted_one(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path)
    aid, rid = "aaaaaa000001", "a000000000000001"
    store.save_run(_chat_record(aid, rid))
    real = store.get_run
    calls = 0

    def deleted_after_the_first_read(automation_id, run_id):
        nonlocal calls
        calls += 1
        record = real(automation_id, run_id)
        if calls == 1:
            store.delete_run(automation_id, run_id, in_progress=lambda r: False)
        return record

    monkeypatch.setattr(store, "get_run", deleted_after_the_first_read)
    store.mark_read(aid, rid)
    assert not (store.runs_dir / aid / f"{rid}.json").exists()


def test_writing_a_result_back_only_replaces_an_existing_record(tmp_path):
    store = AutomationStore(tmp_path)
    record = _running_record("aaaaaa000001", "a000000000000001")
    assert store.save_run(record | {"status": "success"}, replace_only=True) is False
    assert store.get_run("aaaaaa000001", "a000000000000001") is None
    store.save_run(record)
    assert store.save_run(record | {"status": "success"}, replace_only=True) is True
    assert store.get_run("aaaaaa000001", "a000000000000001")["status"] == "success"


# -- interrupted runs (issue #75) ---------------------------------------------------------------------------

JOB_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-life-helper"
    "/providers/Microsoft.App/jobs/caj-lifehelper-abc"
)
IDENTITY_ENDPOINT = "http://localhost:42356/msi/token"


def _hang_after_start(monkeypatch) -> asyncio.Event:
    """Makes the fake Copilot session work forever; the event is set once the prompt was sent."""
    started = asyncio.Event()

    async def hang(session, prompt):
        session.manager.prompts.append(prompt)
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(FakeAutoSession, "send", hang)
    return started


def _due(ctx, now: datetime, *automations: tuple[Automation, int]) -> None:
    for automation, minutes_late in automations:
        ctx.automations.update_state(automation.id, next_run_at=(now - timedelta(minutes=minutes_late)).isoformat())


async def test_a_run_stopped_in_the_job_records_why(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    runner.job_deadline = time.monotonic() + 3600
    started = _hang_after_start(monkeypatch)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = ctx.automations.get_run(a.id, "abcdef0123456789")
    assert stored["status"] == "interrupted" and stored["error"] == runner_module.JOB_STOPPED_MESSAGE
    assert stored["summary"] == runner_module.JOB_STOPPED_MESSAGE and stored["finished_at"]
    # No time is spent on the Copilot session while the process stops (its client is stopped on the way out).
    assert manager.closed == [] and manager.deleted == []
    # The automation can run again right away.
    assert runner._lock(a.id).try_acquire()


async def test_a_session_that_does_not_close_restarts_the_client(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    resets: list[bool] = []

    async def hang(session_id):
        await asyncio.sleep(3600)

    async def reset():
        resets.append(True)

    monkeypatch.setattr(manager, "close_session", hang)
    monkeypatch.setattr(manager, "reset", reset)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))

    record = await asyncio.wait_for(runner.run(a.id), 5)

    # The result is still recorded; the client that stopped answering is restarted.
    assert record["status"] == "success" and resets == [True]
    assert ctx.automations.get_run(a.id, record["id"])["status"] == "success"


async def test_a_run_cut_short_by_the_jobs_limit_does_not_wait_for_copilot(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "ABORT_TIMEOUT_SECONDS", 0.05)
    _hang_after_start(monkeypatch)

    async def hang_abort(session):
        await asyncio.sleep(3600)

    monkeypatch.setattr(FakeAutoSession, "abort", hang_abort)
    a = ctx.automations.upsert(Automation(name="x", prompt="y", max_runtime_minutes=20))
    # Less time left in the job than the automation's limit: the run ends with the job's, not the platform's, limit.
    runner.job_deadline = time.monotonic() + 0.2

    record = await asyncio.wait_for(runner.run(a.id), 5)

    assert record["status"] == "timeout" and record["error"] == runner_module.JOB_TIME_LIMIT_MESSAGE
    assert ctx.automations.get_run(a.id, record["id"])["status"] == "timeout"


async def test_a_client_that_does_not_stop_is_stopped_forcibly(ctx, monkeypatch):
    from life_helper.copilot_integration import manager as manager_module

    monkeypatch.setattr(manager_module, "STOP_TIMEOUT_SECONDS", 0.05)
    copilot = manager_module.CopilotManager(ctx, ctx.settings.copilot_automation_dir, automation=True)
    forced: list[bool] = []

    class HungClient:
        async def stop(self):
            await asyncio.sleep(3600)

        async def force_stop(self):
            forced.append(True)

    copilot._client, copilot._token = HungClient(), "token"  # type: ignore[assignment]
    await asyncio.wait_for(copilot.reset(), 5)
    assert forced == [True] and copilot._client is None and copilot._token is None


async def test_a_run_in_progress_keeps_its_record_fresh(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr(runner_module, "CHECKPOINT_SECONDS", 0.01)
    monkeypatch.setattr(runner_module, "HEARTBEAT_SECONDS", 0)
    started = _hang_after_start(monkeypatch)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    task = asyncio.create_task(runner.run(a.id, run_id="abcdef0123456789"))
    await started.wait()
    first = ctx.automations.get_run(a.id, "abcdef0123456789")["heartbeat_at"]

    for _ in range(200):
        await asyncio.sleep(0.01)
        stored = ctx.automations.get_run(a.id, "abcdef0123456789")
        if stored["heartbeat_at"] != first:
            break
    # Written again although the run has produced nothing yet, so the history can tell it is still going on.
    assert stored["heartbeat_at"] > first
    assert stored["status"] == "running" and stored["events"] == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_a_run_whose_record_stopped_being_written_is_shown_as_interrupted(client, ctx):
    csrf = sign_in(client, ctx)
    store = ctx.automations
    for aid in ("aaaaaa000001", "aaaaaa000002"):
        store.upsert(Automation(id=aid, name="定期チェック", prompt="確認して", schedule=Schedule(kind="daily")))
    now = datetime.now(UTC)
    fresh = _running_record("aaaaaa000001", "a000000000000001", started=now - timedelta(minutes=30))
    store.save_run(fresh | {"heartbeat_at": (now - timedelta(minutes=1)).isoformat()})
    stale = _running_record("aaaaaa000002", "a000000000000002", started=now - timedelta(minutes=10))
    store.save_run(stale | {"heartbeat_at": (now - timedelta(minutes=6)).isoformat()})

    runs = client.get("/api/automations/runs").json()
    assert {r["id"]: r["status"] for r in runs} == {"a000000000000001": "running", "a000000000000002": "interrupted"}
    assert all("heartbeat_at" not in r for r in runs)
    detail = client.get("/api/automations/aaaaaa000002/runs/a000000000000002").json()
    assert detail["status"] == "interrupted"
    # Long before its lock would expire, the automation can be started again and the run deleted.
    assert client.get("/api/automations").json()["running_automation_ids"] == ["aaaaaa000001"]
    h = {"x-csrf-token": csrf}
    assert client.delete("/api/automations/aaaaaa000001/runs/a000000000000001", headers=h).status_code == 409
    assert client.delete("/api/automations/aaaaaa000002/runs/a000000000000002", headers=h).status_code == 200


async def test_due_runs_that_no_longer_fit_in_the_job_wait_for_the_next_one(auto_env):
    ctx, runner, manager = auto_env
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    newest = ctx.automations.upsert(Automation(name="C", prompt="c", max_runtime_minutes=10))
    longest = ctx.automations.upsert(Automation(name="B", prompt="b", max_runtime_minutes=30))
    oldest = ctx.automations.upsert(Automation(name="A", prompt="a", max_runtime_minutes=20))
    _due(ctx, now, (oldest, 3), (longest, 2), (newest, 1))
    runner.job_deadline = time.monotonic() + 25 * 60

    results = await runner.run_due(now)

    # The longest-waiting run goes first; after it, only runs that can still use their whole time limit start.
    assert [(r["automation_id"], r["status"]) for r in results] == [
        (oldest.id, "success"),
        (longest.id, "deferred"),
        (newest.id, "success"),
    ]
    assert [p.split("\n")[0] for p in manager.prompts] == ["a", "c"]
    # The run put off is still due, so the next job execution runs it.
    assert ctx.automations.get(longest.id).state.next_run_at == (now - timedelta(minutes=2)).isoformat()
    assert ctx.automations.list_runs(longest.id) == []


async def test_the_first_run_of_a_job_execution_always_starts(auto_env):
    ctx, runner, manager = auto_env
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    first = ctx.automations.upsert(Automation(name="A", prompt="a", max_runtime_minutes=20))
    second = ctx.automations.upsert(Automation(name="B", prompt="b", max_runtime_minutes=20))
    _due(ctx, now, (first, 2), (second, 1))
    runner.job_deadline = time.monotonic() + 60  # less than either limit

    results = await runner.run_due(now)

    assert [(r["automation_id"], r["status"]) for r in results] == [(first.id, "success"), (second.id, "deferred")]


def _expire_request(store: AutomationStore, automation_id: str) -> None:
    path = store.requests_dir / f"{automation_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(data | {"expires_at": time.time() - 1}), encoding="utf-8")


def test_run_requests_wait_once_per_automation(tmp_path):
    store = AutomationStore(tmp_path)
    aid = "aaaaaa000001"
    assert store.add_run_request(aid, "a000000000000001")
    assert not store.add_run_request(aid, "a000000000000002")  # already waiting for the job
    assert [(r["automation_id"], r["run_id"], r["expired"]) for r in store.run_requests()] == [
        (aid, "a000000000000001", False)
    ]
    # Only the request of that run is taken, and only once.
    assert not store.take_run_request(aid, "a000000000000002")
    assert [r["run_id"] for r in store.run_requests()] == ["a000000000000001"]
    assert store.take_run_request(aid, "a000000000000001")
    assert not store.take_run_request(aid, "a000000000000001")
    assert store.run_requests() == []
    # A request no job execution took in time gives way to a new one.
    assert store.add_run_request(aid, "a000000000000003")
    _expire_request(store, aid)
    assert store.run_requests()[0]["expired"] is True
    assert store.add_run_request(aid, "a000000000000004")
    assert [r["run_id"] for r in store.run_requests()] == ["a000000000000004"]
    for automation_id, run_id in (("../x", "a000000000000005"), (aid, "../x")):
        with pytest.raises(ValueError):
            store.add_run_request(automation_id, run_id)


async def test_the_job_runs_the_requested_runs(auto_env):
    ctx, runner, manager = auto_env
    store = ctx.automations
    ready = store.upsert(Automation(name="A", prompt="a"))
    busy = store.upsert(Automation(name="B", prompt="b"))
    late = store.upsert(Automation(name="C", prompt="c"))
    gone = "bbbbbb000009"  # deleted after the request was made
    for aid, rid in ((ready.id, "a000000000000001"), (busy.id, "b000000000000001"), (gone, "c000000000000001")):
        assert store.add_run_request(aid, rid)
    assert store.add_run_request(late.id, "d000000000000001")
    _expire_request(store, late.id)
    lock = runner._lock(busy.id)
    assert lock.try_acquire()  # running in another job execution

    results = await runner.run_requested()

    statuses = {r["automation_id"]: r["status"] for r in results}
    assert statuses == {ready.id: "success", busy.id: "skipped_locked", gone: "not_found"}
    # The run has the id the app returned to the browser.
    assert store.get_run(ready.id, "a000000000000001")["status"] == "success"
    assert [p.split("\n")[0] for p in manager.prompts] == ["a"]
    # The busy automation's request waits for a later job execution; the others are gone.
    assert [r["automation_id"] for r in store.run_requests()] == [busy.id]
    lock.release()


@pytest.mark.parametrize(
    "job_id",
    [
        "https://example.com/jobs/x",
        JOB_ID + "/../../x",
        JOB_ID.replace("Microsoft.App/jobs", "Microsoft.App/containerApps"),
        JOB_ID.replace("rg-life-helper", ".."),
    ],
)
def test_the_job_to_start_must_be_a_container_apps_job(tmp_path, job_id):
    from life_helper.config import Settings

    assert Settings(environment="development", data_dir=tmp_path, automation_job_id=f" {JOB_ID} ").automation_job_id
    with pytest.raises(ValidationError):
        Settings(environment="development", data_dir=tmp_path, automation_job_id=job_id)


def _job_settings(settings, monkeypatch) -> None:
    settings.automation_job_id = JOB_ID
    settings.managed_identity_client_id = "identity-client-id"
    monkeypatch.setenv("IDENTITY_ENDPOINT", IDENTITY_ENDPOINT)
    monkeypatch.setenv("IDENTITY_HEADER", "identity-header-value")


def test_run_now_hands_the_run_to_the_job(client, ctx, settings, monkeypatch):
    csrf = sign_in(client, ctx)
    _job_settings(settings, monkeypatch)
    automation = ctx.automations.upsert(Automation(name="定期チェック", prompt="確認して"))
    h = {"x-csrf-token": csrf}

    with respx.mock(assert_all_called=True) as mock:
        token = mock.get(IDENTITY_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"access_token": "arm-token", "expires_on": str(time.time() + 3600)})
        )
        start = mock.post(f"https://management.azure.com{JOB_ID}/start").mock(
            return_value=httpx.Response(202, json={"name": "execution-1"})
        )
        response = client.post(f"/api/automations/{automation.id}/run", headers=h)

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert response.json() == {
        "started": True,
        "run_id": run_id,
        "runner": "job",
        "job_started": True,
        "name": "定期チェック",
        "max_runtime_minutes": 20,
    }
    assert "automation_runner" not in ctx.extras  # nothing runs in the app
    # The app's managed identity signs in to Azure Resource Manager and starts the job.
    request = token.calls.last.request
    assert request.headers["X-IDENTITY-HEADER"] == "identity-header-value"
    assert request.url.params["resource"] == "https://management.azure.com/"
    assert request.url.params["client_id"] == "identity-client-id"
    assert start.calls.last.request.headers["Authorization"].split() == ["Bearer", "arm-token"]
    assert start.calls.last.request.url.params["api-version"] == "2024-03-01"
    # The job finds the request; until it is done the automation is shown as running and cannot be started twice.
    assert [(r["automation_id"], r["run_id"]) for r in ctx.automations.run_requests()] == [(automation.id, run_id)]
    assert client.get("/api/automations").json()["running_automation_ids"] == [automation.id]
    assert client.post(f"/api/automations/{automation.id}/run", headers=h).status_code == 409


def test_run_now_leaves_the_request_to_the_scheduled_job_when_the_job_cannot_be_started(
    client, ctx, settings, monkeypatch, caplog
):
    csrf = sign_in(client, ctx)
    _job_settings(settings, monkeypatch)
    automation = ctx.automations.upsert(Automation(name="定期チェック", prompt="確認して"))

    with respx.mock(assert_all_called=True) as mock:
        mock.get(IDENTITY_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"access_token": "arm-token", "expires_on": str(time.time() + 3600)})
        )
        mock.post(f"https://management.azure.com{JOB_ID}/start").mock(
            return_value=httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
        )
        response = client.post(f"/api/automations/{automation.id}/run", headers={"x-csrf-token": csrf})

    run_id = response.json()["run_id"]
    assert response.json() == {
        "started": True,
        "run_id": run_id,
        "runner": "job",
        "job_started": False,
        "name": "定期チェック",
        "max_runtime_minutes": 20,
    }
    assert [r["run_id"] for r in ctx.automations.run_requests()] == [run_id]
    assert "HTTP 403" in caplog.text and "arm-token" not in caplog.text


async def test_the_job_starter_reuses_its_token():
    from life_helper.automation.dispatch import JobStarter

    starter = JobStarter(JOB_ID, identity_endpoint=IDENTITY_ENDPOINT, identity_header="identity-header-value")
    with respx.mock(assert_all_called=True) as mock:
        token = mock.get(IDENTITY_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"access_token": "arm-token", "expires_on": str(time.time() + 3600)})
        )
        start = mock.post(f"https://management.azure.com{JOB_ID}/start").mock(return_value=httpx.Response(202))
        await starter.start()
        await starter.start()
    assert token.call_count == 1 and start.call_count == 2
    assert "client_id" not in token.calls.last.request.url.params  # the system identity when none is given
    assert not JobStarter("").configured  # local development: no job


def test_run_now_leaves_the_request_to_the_job_when_the_managed_identity_is_missing(
    client, ctx, settings, monkeypatch, caplog
):
    csrf = sign_in(client, ctx)
    settings.automation_job_id = JOB_ID
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    automation = ctx.automations.upsert(Automation(name="定期チェック", prompt="確認して"))

    response = client.post(f"/api/automations/{automation.id}/run", headers={"x-csrf-token": csrf})

    # With a job, the run never falls back to the app (which scales in to zero and would stop it).
    run_id = response.json()["run_id"]
    assert response.json() == {
        "started": True,
        "run_id": run_id,
        "runner": "job",
        "job_started": False,
        "name": "定期チェック",
        "max_runtime_minutes": 20,
    }
    assert "automation_runner" not in ctx.extras
    assert [r["run_id"] for r in ctx.automations.run_requests()] == [run_id]
    assert "managed identity endpoint is not available" in caplog.text


def test_run_now_refuses_an_automation_that_is_running(client, ctx):
    csrf = sign_in(client, ctx)
    automation = ctx.automations.upsert(Automation(name="定期チェック", prompt="確認して"))
    ctx.automations.save_run(_running_record(automation.id, "a000000000000001", started=datetime.now(UTC)))

    response = client.post(f"/api/automations/{automation.id}/run", headers={"x-csrf-token": csrf})

    assert response.status_code == 409 and "automation_runner" not in ctx.extras


async def test_the_job_runs_the_requests_first_and_keeps_to_its_time_limit(monkeypatch, settings):
    from life_helper import jobs

    calls: list = []

    async def run_requested(self):
        calls.append(("run_requested", self.job_deadline - time.monotonic()))
        return []

    async def run_due(self, now=None):
        calls.append("run_due")
        return []

    async def run_scope(ctx, scope, *, automation_manager=None, budget_seconds=None):
        calls.append(("retention", budget_seconds))

    settings.automation_job_timeout_seconds = 3600
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs.AutomationRunner, "run_requested", run_requested)
    monkeypatch.setattr(jobs.AutomationRunner, "run_due", run_due)
    monkeypatch.setattr(jobs, "run_scope", run_scope)
    assert await jobs.run_due() == 0
    (name, left), *rest = calls
    # Runs end 5 minutes before the job's limit, so the platform never stops one half-way.
    assert name == "run_requested" and 3600 - 300 - 5 < left <= 3600 - 300
    assert rest == ["run_due", ("retention", jobs.PASS_BUDGET_SECONDS)]

    # Without time left for it, the data retention waits for a later job execution.
    calls.clear()
    monkeypatch.setattr(jobs, "RETENTION_RESERVE_SECONDS", 3600 - 5)
    assert await jobs.run_due() == 0
    assert [c if isinstance(c, str) else c[0] for c in calls] == ["run_requested", "run_due"]


async def test_the_job_stopped_by_the_platform_lets_the_run_record_it(monkeypatch):
    from life_helper import jobs

    stopped: list[bool] = []

    async def run_due():
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.append(True)
            raise
        return 0

    monkeypatch.setattr(jobs, "run_due", run_due)
    assert await asyncio.wait_for(jobs.run_until_stopped(), 5) == 1
    assert stopped == [True]


async def test_the_app_waits_for_stopped_runs_before_it_exits(ctx):
    from life_helper.bootstrap import shutdown_services

    recorded: list[bool] = []

    async def run():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # saving that the run was interrupted
            recorded.append(True)
            raise

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    ctx.extras["automation_tasks"] = {task}
    await shutdown_services(ctx)
    assert recorded == [True] and task.cancelled()
