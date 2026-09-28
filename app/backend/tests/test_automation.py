from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml
from copilot import ToolInvocation
from copilot.session_events import AssistantMessageData, AssistantUsageData, SessionErrorData, SessionIdleData
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from life_helper.automation import chat
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


def test_max_runtime_is_capped_at_20_minutes():
    with pytest.raises(ValidationError):
        Automation(name="x", prompt="y", max_runtime_minutes=21)


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
    assert {r["started_at"] for r in results} == {
        datetime(2026, 1, 1, 15, 1, 1, tzinfo=UTC).isoformat(),
        datetime(2026, 1, 1, 15, 1, 3, tzinfo=UTC).isoformat(),
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

    listing = client.get("/api/automations").json()
    assert listing["usage"]["estimated_runs_per_month"] == 30 and listing["github_notify_configured"] is False

    bad = dict(body, notify={"github": True, "condition": "signal"})
    assert client.post("/api/automations", json=bad, headers=h).status_code == 400
    assert client.post("/api/automations", json=dict(body, connectors=["nope"]), headers=h).status_code == 400
    assert client.post("/api/automations", json=dict(body, max_runtime_minutes=30), headers=h).status_code == 422
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
    record = await runner.run(a.id)

    # While it runs the history shows the run without a result, and there is nothing to read or to replay yet.
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
    record["started_at"] = "2026-09-25 00:00:00"  # a start time without a timezone cannot be compared
    ctx.automations.save_run(record)
    assert client.get("/api/automations/runs").json()[0]["status"] == "running"


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
        store.save_run(record | {"status": "success", "summary": "要約"})

    assert "a000000000000001" not in {r["id"] for r in client.get("/api/automations/runs").json()}
    # The interrupted run is not reported as running, so its automation can be started again.
    assert client.get("/api/automations").json()["running_automation_ids"] == ["aaaaaa000001"]
