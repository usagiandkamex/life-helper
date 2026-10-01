"""Deleting history and internal data that has not been used for the retention period (issue #67)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from life_helper import jobs, retention
from life_helper.automation import chat
from life_helper.automation import store as store_module
from life_helper.automation.locks import FileLock
from life_helper.automation.models import Automation
from life_helper.bootstrap import init_chat, init_core
from life_helper.copilot_integration.manager import NoTokenError, SessionStateError

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=10)


def _run(automation_id: str, run_id: str, started: datetime | str, **extra) -> dict:
    return {
        "id": run_id,
        "automation_id": automation_id,
        "name": "定期チェック",
        "started_at": started.isoformat() if isinstance(started, datetime) else started,
        "finished_at": started.isoformat() if isinstance(started, datetime) else started,
        "status": "success",
        "read": True,
        "transcript_version": 1,
        "conversation_mode": "new",
        "prompt": "確認して",
        "summary": "要約",
        "events": [],
    } | extra


def _age(path: Path, when: datetime) -> None:
    """Sets the modification time of ``path`` and everything in it."""
    stamp = when.timestamp()
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (stamp, stamp))


def _session(base_dir: Path, name: str, last_used: datetime) -> Path:
    path = base_dir / "session-state" / name
    (path / "files").mkdir(parents=True)
    (path / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (path / "workspace.yaml").write_text("id: x\n", encoding="utf-8")
    _age(path, last_used)
    return path


class FakeSessions:
    """Stands in for CopilotManager: deletes the session folder the way Copilot does."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.deleted: list[str] = []
        self.fail: Exception | None = None

    async def delete_session(self, session_id: str) -> None:
        if self.fail is not None:
            raise self.fail
        shutil.rmtree(self.base_dir / "session-state" / session_id)
        self.deleted.append(session_id)


@pytest.fixture
def env(ctx):
    init_core(ctx)
    ctx.vault.save("gho_unit_test_token_1234", 134019422, "usagiandkamex")
    return ctx


@pytest.fixture
def chat_env(env):
    init_chat(env)
    env.copilot = FakeSessions(env.settings.copilot_chat_dir)  # type: ignore[assignment]
    return env


def _stop() -> bool:
    return False


# -- automation runs -----------------------------------------------------------------------------------------


async def test_expired_runs_and_their_index_entries_are_deleted(env):
    store = env.automations
    live = store.upsert(Automation(id="aaaaaa000001", name="毎朝", prompt="予定"))
    store.save_run(_run(live.id, "a000000000000001", OLD))
    store.save_run(_run(live.id, "a000000000000002", RECENT))
    # Left behind as running by a job that stopped long ago (the history shows it as interrupted).
    store.save_run(_run(live.id, "a000000000000003", OLD, status="running"))
    store.save_run(_run(live.id, "a000000000000004", "not a time"))  # unreadable start: kept, not guessed
    store.save_run(_run("dddddd000001", "d000000000000001", OLD))  # an automation deleted since
    store.list_run_meta()  # fills the index

    result = await retention.prune_automation_data(env, None, NOW, _stop)

    assert result.complete and result.deleted == {"runs": 3}
    assert sorted(m["id"] for m in store.list_run_meta()) == ["a000000000000002", "a000000000000004"]
    index = json.loads(store.run_index_path.read_text(encoding="utf-8"))["runs"]
    assert sorted(index) == [f"{live.id}/a000000000000002", f"{live.id}/a000000000000004"]
    # The deleted automation's emptied folder goes; a live automation keeps its own.
    assert not (store.runs_dir / "dddddd000001").exists() and (store.runs_dir / live.id).is_dir()


async def test_naive_start_times_are_read_as_utc(env):
    store = env.automations
    store.save_run(_run("aaaaaa000001", "a000000000000001", OLD.replace(tzinfo=None).isoformat()))
    await retention.prune_automation_data(env, None, NOW, _stop)
    assert store.list_run_meta() == []


async def test_a_run_used_meanwhile_is_kept(env, monkeypatch):
    """Records are checked again under their lock, so one rewritten since the scan is not deleted."""
    store = env.automations
    store.save_run(_run("aaaaaa000001", "a000000000000001", OLD))
    real = store.get_run

    def rewritten(automation_id, run_id):
        record = real(automation_id, run_id)
        return record and record | {"started_at": RECENT.isoformat()}

    monkeypatch.setattr(store, "get_run", rewritten)
    assert store.prune_runs(NOW - timedelta(days=180)) == []
    assert store.list_run_meta() != []


async def test_hidden_conversations_and_month_counts_are_pruned(env):
    store = env.automations
    store.save_run(_run("aaaaaa000001", "a000000000000001", OLD, conversation_mode="continue"))
    store.save_run(_run("aaaaaa000001", "a000000000000002", RECENT, conversation_mode="continue"))
    store.save_run(_run("bbbbbb000001", "b000000000000001", OLD))
    store.save_run(_run("cccccc000001", "c000000000000001", RECENT))
    store.hide_chat_thread("c-aaaaaa000001", "a000000000000002")
    store.hide_chat_thread("r-bbbbbb000001-b000000000000001", "b000000000000001")
    store.hide_chat_thread("r-cccccc000001-c000000000000001", "c000000000000001")
    store.usage_path.write_text(
        json.dumps({"2026-02": 10, "2026-03": 5, "2026-04": 7, "2026-10": 1, "last_reauth_notice": "2026-01-01"})
    )

    result = await retention.prune_automation_data(env, None, NOW, _stop)

    # Only the conversation whose run is gone is forgotten; the others stay hidden.
    assert store.chat_hidden() == {
        "c-aaaaaa000001": "a000000000000002",
        "r-cccccc000001-c000000000000001": "c000000000000001",
    }
    # Months that ended before the cutoff (2026-04-04) go; the month it falls in and later ones stay.
    assert json.loads(store.usage_path.read_text()) == {"2026-04": 7, "2026-10": 1, "last_reauth_notice": "2026-01-01"}
    assert result.deleted == {"runs": 2, "hidden": 1, "usage_months": 2}
    assert [t["id"] for t in chat.list_threads(store)] == []


async def test_month_counts_are_pruned_under_the_edit_lock(env, monkeypatch):
    store = env.automations
    store.usage_path.write_text(json.dumps({"2026-01": 3}))
    monkeypatch.setattr(store_module.time, "sleep", lambda _seconds: None)  # do not wait out the retries
    with FileLock(store.locks_dir / "automations-edit.lock", ttl_seconds=60):
        with pytest.raises(TimeoutError):
            store.prune_usage(NOW - timedelta(days=180))
    assert json.loads(store.usage_path.read_text()) == {"2026-01": 3}


# -- automation Copilot sessions ---------------------------------------------------------------------------


async def test_automation_sessions_are_deleted_when_unused(env):
    store = env.automations
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    store.upsert(Automation(id="aaaaaa000001", name="続ける", prompt="x", conversation_mode="continue"))
    store.upsert(Automation(id="bbbbbb000001", name="続ける", prompt="x", conversation_mode="continue"))
    # A "continue" automation that keeps running but stops before Copilot (e.g. the monthly limit): kept.
    _session(base, "auto-aaaaaa000001", OLD)
    store.save_run(_run("aaaaaa000001", "a000000000000001", RECENT, conversation_mode="continue", status="error"))
    # One that has not run for the whole period: its memory goes.
    _session(base, "auto-bbbbbb000001", OLD)
    store.save_run(_run("bbbbbb000001", "b000000000000001", OLD + timedelta(days=1), conversation_mode="continue"))
    # Left behind by a "new" run whose clean-up failed: old goes, recent stays.
    _session(base, "auto-cccccc000001-c000000000000001-0", OLD)
    _session(base, "auto-cccccc000001-c000000000000002-0", RECENT)
    # Not a session id: left alone.
    _session(base, ".odd", OLD)

    result = await retention.prune_automation_data(env, sessions, NOW, _stop)

    assert sorted(sessions.deleted) == ["auto-bbbbbb000001", "auto-cccccc000001-c000000000000001-0"]
    assert result.complete and result.deleted["automation_sessions"] == 2
    left = sorted(p.name for p in (base / "session-state").iterdir())
    assert left == [".odd", "auto-aaaaaa000001", "auto-cccccc000001-c000000000000002-0"]


async def test_a_continue_session_switched_to_new_mode_is_not_kept_by_new_runs(env):
    store = env.automations
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    automation = store.upsert(Automation(id="aaaaaa000001", name="x", prompt="x", conversation_mode="new"))
    store.update_state(automation.id, last_run_at=RECENT.isoformat())
    store.save_run(_run(automation.id, "a000000000000001", RECENT))  # runs in "new" mode now
    _session(base, "auto-aaaaaa000001", OLD)
    await retention.prune_automation_data(env, sessions, NOW, _stop)
    assert sessions.deleted == ["auto-aaaaaa000001"]


async def test_a_session_in_use_by_a_run_is_not_deleted(env):
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    _session(base, "auto-aaaaaa000001", OLD)
    lock = FileLock(env.automations.locks_dir / "automation-aaaaaa000001.lock", ttl_seconds=60)
    assert lock.try_acquire()
    try:
        result = await retention.prune_automation_data(env, sessions, NOW, _stop)
    finally:
        lock.release()
    assert sessions.deleted == [] and not result.complete


async def test_a_session_used_after_the_scan_is_not_deleted(env, monkeypatch):
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    path = _session(base, "auto-aaaaaa000001", OLD)
    real = retention._recheck_automation_session

    def used_meanwhile(ctx, name, session_path):
        _age(path, NOW)  # a run used it between the scan and the lock
        return real(ctx, name, session_path)

    monkeypatch.setattr(retention, "_recheck_automation_session", used_meanwhile)
    await retention.prune_automation_data(env, sessions, NOW, _stop)
    assert sessions.deleted == [] and path.exists()


async def test_sessions_are_kept_while_a_run_record_cannot_be_read(env):
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    _session(base, "auto-aaaaaa000001", OLD)
    # A run that started recently could be the one that used the session; it must be readable to decide.
    broken = env.automations.runs_dir / "aaaaaa000001" / "a000000000000001.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{broken", encoding="utf-8")

    result = await retention.prune_automation_data(env, sessions, NOW, _stop)

    assert sessions.deleted == [] and not result.complete and broken.exists()


async def test_sessions_wait_for_a_token(env, monkeypatch):
    base = env.settings.copilot_automation_dir
    sessions = FakeSessions(base)
    _session(base, "auto-aaaaaa000001", OLD)
    monkeypatch.setattr(env, "github_token", lambda: None)
    result = await retention.prune_automation_data(env, sessions, NOW, _stop)
    assert sessions.deleted == [] and not result.complete

    # A token that Copilot does not accept (the CLI does not start) stops the session part the same way.
    monkeypatch.setattr(env, "github_token", lambda: "gho_revoked")
    sessions.fail = RuntimeError("failed to start")
    result = await retention.prune_automation_data(env, sessions, NOW, _stop)
    assert not result.complete and (base / "session-state" / "auto-aaaaaa000001").exists()


def _link_dir(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no extra rights needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


async def test_linked_folders_are_not_followed(env, tmp_path):
    outside = tmp_path / "outside"
    _session(outside, "auto-aaaaaa000001", OLD)
    base = env.settings.copilot_automation_dir
    (base / "session-state").mkdir(parents=True)
    _link_dir(base / "session-state" / "auto-bbbbbb000001", outside / "session-state" / "auto-aaaaaa000001")
    runs_outside = tmp_path / "runs-outside"
    runs_outside.mkdir()
    (runs_outside / "a000000000000001.json").write_text(json.dumps(_run("aaaaaa000001", "a000000000000001", OLD)))
    env.automations.runs_dir.mkdir(parents=True, exist_ok=True)
    _link_dir(env.automations.runs_dir / "aaaaaa000001", runs_outside)

    sessions = FakeSessions(base)
    await retention.prune_automation_data(env, sessions, NOW, _stop)
    assert sessions.deleted == []
    assert (runs_outside / "a000000000000001.json").exists()
    assert (outside / "session-state" / "auto-aaaaaa000001").exists()


# -- chat --------------------------------------------------------------------------------------------------


def _conversation(ctx, updated: datetime, *, started: bool = True) -> str:
    store = ctx.extras["conversations"]
    conv = store.create("会話", "auto")
    items = json.loads(store.path.read_text(encoding="utf-8"))
    for item in items:
        if item["id"] == conv.id:
            item["updated_at"] = updated.isoformat()
            item["started"] = started
    store.path.write_text(json.dumps(items), encoding="utf-8")
    if started:
        _session(ctx.settings.copilot_chat_dir, conv.id, updated)
    return conv.id


async def test_expired_conversations_and_their_sessions_are_deleted(chat_env):
    ctx = chat_env
    old = _conversation(ctx, OLD)
    never_sent = _conversation(ctx, OLD, started=False)
    recent = _conversation(ctx, RECENT)
    orphan_old = _session(ctx.settings.copilot_chat_dir, "f" * 32, OLD)
    orphan_recent = _session(ctx.settings.copilot_chat_dir, "e" * 32, RECENT)

    result = await retention.prune_chat_data(ctx, NOW, _stop)

    assert [c.id for c in ctx.extras["conversations"].list()] == [recent]
    assert sorted(ctx.copilot.deleted) == sorted([old, "f" * 32])
    assert never_sent not in ctx.copilot.deleted
    assert not orphan_old.exists() and orphan_recent.exists()
    assert result.complete and result.deleted == {"conversations": 2, "chat_sessions": 2}


async def test_a_conversation_that_is_answering_or_renamed_is_kept(chat_env, monkeypatch):
    ctx = chat_env
    store = ctx.extras["conversations"]
    answering = _conversation(ctx, OLD)
    renamed = _conversation(ctx, OLD)
    real_reserve = ctx.turns.reserve

    def reserve(conversation_id):
        if conversation_id == renamed:
            store.update(renamed, title="新しい名前")  # renamed after the list was read
        return real_reserve(conversation_id)

    monkeypatch.setattr(ctx.turns, "reserve", reserve)
    async with real_reserve(answering):
        result = await retention.prune_chat_data(ctx, NOW, _stop)

    assert sorted(c.id for c in store.list()) == sorted([answering, renamed])
    assert ctx.copilot.deleted == [] and not result.complete


async def test_a_session_that_could_not_be_deleted_is_retried_without_its_conversation(chat_env):
    ctx = chat_env
    conv = _conversation(ctx, OLD)
    ctx.copilot.fail = SessionStateError("busy")
    first = await retention.prune_chat_data(ctx, NOW, _stop)
    assert ctx.extras["conversations"].list() == [] and not first.complete
    assert (ctx.settings.copilot_chat_dir / "session-state" / conv).exists()

    ctx.copilot.fail = None
    second = await retention.prune_chat_data(ctx, NOW + timedelta(hours=1), _stop)
    assert ctx.copilot.deleted == [conv] and second.complete


async def test_chat_sessions_wait_for_a_token(chat_env, monkeypatch):
    ctx = chat_env
    conv = _conversation(ctx, OLD)
    monkeypatch.setattr(ctx, "github_token", lambda: None)
    result = await retention.prune_chat_data(ctx, NOW, _stop)
    assert not result.complete and ctx.copilot.deleted == []
    assert (ctx.settings.copilot_chat_dir / "session-state" / conv).exists()

    ctx.copilot.fail = NoTokenError("signed out")
    monkeypatch.setattr(ctx, "github_token", lambda: "gho_unit_test_token_1234")
    result = await retention.prune_chat_data(ctx, NOW, _stop)
    assert not result.complete


# -- what is never touched ---------------------------------------------------------------------------------


async def test_sessions_are_kept_while_the_conversation_list_cannot_be_read(chat_env):
    ctx = chat_env
    conv = _conversation(ctx, RECENT)
    session = ctx.settings.copilot_chat_dir / "session-state" / conv
    _age(session, OLD)  # renamed recently, last answered long ago
    ctx.extras["conversations"].path.write_text("{broken", encoding="utf-8")

    result = await retention.prune_chat_data(ctx, NOW, _stop)

    # An unreadable list must not make every session look like one without a conversation.
    assert ctx.copilot.deleted == [] and session.exists() and not result.complete


def test_a_result_is_never_written_without_the_record_lock(env, monkeypatch):
    store = env.automations
    record = _run("aaaaaa000001", "a000000000000001", RECENT, status="running")
    store.save_run(record)
    monkeypatch.setattr(store_module.time, "sleep", lambda _seconds: None)
    with FileLock(store.locks_dir / "run-aaaaaa000001-a000000000000001.lock", ttl_seconds=60):
        assert store.save_run(record | {"status": "success"}, replace_only=True) is False
    assert store.get_run("aaaaaa000001", "a000000000000001")["status"] == "running"


async def test_knowledge_definitions_and_secrets_are_kept(chat_env):
    ctx = chat_env
    s = ctx.settings
    ctx.automations.upsert(Automation(id="aaaaaa000001", name="毎朝", prompt="予定"))
    kept = [
        s.knowledge_dir / "notes" / "old.md",
        s.app_state_dir / "secrets" / "github_token.enc",
        s.app_state_dir / "connectors.json",
        s.app_state_dir / "session-generation.txt",
        ctx.automations.path,
        s.copilot_chat_dir / "installed-plugins" / "x.json",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("x", encoding="utf-8")
        _age(path, OLD)
    _age(s.knowledge_dir, OLD)

    assert await retention.run_scope(ctx, "chat", now=NOW) is not None
    assert await retention.run_scope(
        ctx, "automation", automation_manager=FakeSessions(s.copilot_automation_dir), now=NOW
    )
    assert all(path.exists() for path in kept)


# -- when it runs ------------------------------------------------------------------------------------------


async def test_each_scope_runs_once_a_day_and_retries_an_unfinished_pass(env, monkeypatch):
    sessions = FakeSessions(env.settings.copilot_automation_dir)
    assert await retention.run_scope(env, "automation", automation_manager=sessions, now=NOW) is not None
    assert (
        await retention.run_scope(env, "automation", automation_manager=sessions, now=NOW + timedelta(hours=23)) is None
    )
    assert await retention.run_scope(env, "automation", automation_manager=sessions, now=NOW + timedelta(hours=24))

    # A pass that could not finish (here: no token for an expired session) is tried again an hour later.
    _session(env.settings.copilot_automation_dir, "auto-aaaaaa000001", OLD)
    monkeypatch.setattr(env, "github_token", lambda: None)
    later = NOW + timedelta(days=2)
    result = await retention.run_scope(env, "automation", automation_manager=sessions, now=later)
    assert result is not None and not result.complete
    assert (
        await retention.run_scope(env, "automation", automation_manager=sessions, now=later + timedelta(minutes=30))
        is None
    )
    monkeypatch.setattr(env, "github_token", lambda: "gho_unit_test_token_1234")
    result = await retention.run_scope(env, "automation", automation_manager=sessions, now=later + timedelta(hours=1))
    assert result is not None and result.complete and sessions.deleted == ["auto-aaaaaa000001"]


async def test_a_scope_runs_in_one_process_at_a_time(env):
    lock = FileLock(env.settings.app_state_dir / "locks" / "retention-automation.lock", ttl_seconds=60)
    assert lock.try_acquire()
    try:
        assert await retention.run_scope(env, "automation", now=NOW) is None
    finally:
        lock.release()
    assert await retention.run_scope(env, "automation", now=NOW) is not None


async def test_a_pass_that_runs_out_of_time_is_retried(env):
    store = env.automations
    for i in range(3):
        store.save_run(_run("aaaaaa000001", f"a{i:015x}", OLD))
    result = await retention.run_scope(env, "automation", now=NOW, budget_seconds=-1)
    assert result is not None and not result.complete
    assert len(store.list_run_meta()) == 3
    assert await retention.run_scope(env, "automation", now=NOW + timedelta(hours=1))
    assert store.list_run_meta() == []


async def test_a_failing_pass_is_recorded_and_retried_later(env, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("volume unavailable")

    monkeypatch.setattr(env.automations, "prune_runs", broken)
    with pytest.raises(OSError):
        await retention.run_scope(env, "automation", now=NOW)
    assert await retention.run_scope(env, "automation", now=NOW + timedelta(minutes=30)) is None
    assert not retention.RetentionGate(env, "automation").lock.path.exists()


async def test_the_job_runs_the_automation_scope_after_the_due_runs(monkeypatch, settings):
    calls: list[str] = []

    async def run_due(self, now=None):
        calls.append("run_due")
        return []

    async def run_scope(ctx, scope, *, automation_manager=None, **kwargs):
        calls.append(scope)
        assert automation_manager is not None
        raise RuntimeError("must not fail the job")

    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs.AutomationRunner, "run_due", run_due)
    monkeypatch.setattr(jobs, "run_scope", run_scope)
    assert await jobs.run_due() == 0
    assert calls == ["run_due", "automation"]


def test_the_app_schedules_and_stops_the_retention(app, ctx):
    with TestClient(app, base_url="http://testserver"):
        scheduler = ctx.extras["retention"]
        assert isinstance(scheduler, retention.RetentionScheduler)
        task = scheduler.task
        assert task is not None and not task.done()
    # Stopped with the app, before the Copilot clients it may use are shut down.
    assert task.done() and "retention" not in ctx.extras


async def test_the_scheduler_runs_both_scopes(chat_env, monkeypatch):
    scopes: list[str] = []

    async def run_scope(ctx, scope, *, automation_manager=None, **kwargs):
        scopes.append(scope)
        if scope == "chat":
            raise RuntimeError("one scope failing does not stop the other")

    monkeypatch.setattr(retention, "run_scope", run_scope)
    scheduler = retention.RetentionScheduler(chat_env)
    await scheduler.run_once()
    assert scopes == ["chat", "automation"]
    assert chat_env.extras["automation_runner"].manager.base_dir == chat_env.settings.copilot_automation_dir

    scheduler.start()
    await asyncio.sleep(0)
    await scheduler.shutdown()
    assert scheduler.task.done()
