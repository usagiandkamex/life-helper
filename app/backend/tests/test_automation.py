from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from copilot import ToolInvocation
from copilot.session_events import AssistantMessageData, AssistantUsageData
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from life_helper.automation.locks import FileLock
from life_helper.automation.models import Automation, NotifySettings, Schedule, expand_prompt
from life_helper.automation.runner import AutomationRunner
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


class FakeAutoSession:
    def __init__(self, manager: FakeAutoManager) -> None:
        self.manager = manager
        self.handlers: list = []

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    async def send_and_wait(self, prompt: str, timeout: float = 60):
        m = self.manager
        m.prompts.append(prompt)
        if m.fail:
            raise m.fail
        if m.signal is not None:
            m.active.policy.on_tool_result(
                "search_rakuten_vacancy", {"textResultForLlm": json.dumps({"signal": m.signal})}
            )
        for h in list(self.handlers):
            h(SimpleNamespace(data=AssistantUsageData(model="gpt-5-mini")))
            h(SimpleNamespace(data=AssistantMessageData(content="空室を確認しました", message_id="m")))
        if len(m.prompts) < m.report_on_call:
            return  # the model "forgets" to report on this call
        report = next(s.tool for s in m.extra_tools if s.tool.name == "report_result")
        await report.handler(ToolInvocation(arguments={"summary": "要約", "notify": m.report_notify}))

    async def abort(self):
        return None


class FakeAutoManager:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.opened: list = []
        self.closed: list = []
        self.deleted: list = []
        self.fail: Exception | None = None
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
async def test_missing_report_triggers_one_follow_up_when_notify_depends_on_it(auto_env):
    ctx, runner, manager = auto_env
    _mock_github()
    manager.report_on_call = 2
    manager.report_notify = True
    a = ctx.automations.upsert(Automation(name="x", prompt="y", notify=NotifySettings(github=True, condition="report")))
    record = await runner.run(a.id)
    assert len(manager.prompts) == 2 and "report_result" in manager.prompts[1]
    assert "report_result" in manager.prompts[0]  # the reminder is appended to the unattended prompt
    assert record["report"]["notify"] is True and record["notified"] is True

    # Without GitHub notify there is no need to spend an extra request on a follow-up.
    manager.prompts.clear()
    b = ctx.automations.upsert(Automation(name="z", prompt="y"))
    await runner.run(b.id)
    assert len(manager.prompts) == 1


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
    b = ctx.automations.upsert(Automation(name="楽天", prompt="y", connectors=["rakuten_travel"]))
    record = await runner.run(b.id)
    assert record["status"] == "error" and "rakuten_travel" in record["summary"]


async def test_run_due_only_runs_due_automations(auto_env):
    ctx, runner, manager = auto_env
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    assert await runner.run_due(datetime(2026, 1, 1, tzinfo=UTC)) == []  # first pass only schedules
    next_run = datetime.fromisoformat(ctx.automations.get(a.id).state.next_run_at)
    assert await runner.run_due(next_run - timedelta(minutes=1)) == []
    results = await runner.run_due(next_run)
    assert [r["status"] for r in results] == ["success"]
    assert datetime.fromisoformat(ctx.automations.get(a.id).state.next_run_at) > next_run


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

    original = FakeAutoSession.send_and_wait

    async def leaky(self, prompt, timeout=60):
        report = next(s.tool for s in self.manager.extra_tools if s.tool.name == "report_result")
        await report.handler(
            ToolInvocation(arguments={"summary": "口座番号: 1234567 key SECRET-API-KEY-123", "notify": False})
        )

    FakeAutoSession.send_and_wait = leaky
    try:
        record = await runner.run(a.id)
    finally:
        FakeAutoSession.send_and_wait = original
    stored = json.dumps(ctx.automations.get_run(a.id, record["id"]), ensure_ascii=False)
    assert "1234567" not in stored and "SECRET-API-KEY-123" not in stored


async def test_no_retry_after_side_effects(auto_env, monkeypatch):
    ctx, runner, manager = auto_env
    monkeypatch.setattr("life_helper.automation.runner.asyncio.sleep", _no_sleep)
    a = ctx.automations.upsert(Automation(name="x", prompt="y"))
    original = FakeAutoSession.send_and_wait

    async def fails_after_tool(self, prompt, timeout=60):
        self.manager.prompts.append(prompt)
        from copilot.session_events import ToolExecutionStartData

        for h in list(self.handlers):
            h(SimpleNamespace(data=ToolExecutionStartData(tool_call_id="t", tool_name="edit", arguments={})))
        raise RuntimeError("boom after writing")

    FakeAutoSession.send_and_wait = fails_after_tool
    try:
        record = await runner.run(a.id)
    finally:
        FakeAutoSession.send_and_wait = original
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
