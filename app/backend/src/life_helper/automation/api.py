"""HTTP API for automations: definitions, run history, run-now and usage estimates."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StringConstraints, field_validator

from ..auth import CurrentUser, require_user
from ..connectors.registry import get_connectors
from ..context import AppContext, get_ctx
from ..security import SENSITIVE_LABELS, detect_sensitive
from . import chat
from .dispatch import JobStartError, job_starter
from .models import (
    DEFAULT_RUNTIME_MINUTES,
    MAX_RUNTIME_MINUTES,
    Automation,
    AutomationState,
    NotifySettings,
    Schedule,
    normalize_connectors,
)
from .runner import AutomationRunner, build_notifier
from .store import INTERRUPTED_STATUS, RUNNING_STATUS, AutomationStore, parse_timestamp

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/automations")

# A run still recorded as running after its lock could have expired, or whose record has not been written for this
# long (a run in progress writes it every minute, see runner.HEARTBEAT_SECONDS), was left behind by an app or job that
# stopped without saving anything, so the history shows it as interrupted instead of running for ever.
HEARTBEAT_STALE_SECONDS = 5 * 60
# The run list shows only names, times and statuses. The result and transcript are read from the run itself, so the
# list (up to 50 runs) is answered from the run metadata index without opening the records, however long the results
# get.
RUN_LIST_FIELDS = ("id", "automation_id", "name", "started_at", "finished_at", "status", "read", "notified")
RUN_LIST_LIMIT = 50


class AutomationBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    prompt: str = Field(min_length=1, max_length=8000)
    schedule: Schedule
    conversation_mode: Literal["new", "continue"] = "new"
    model: str = "auto"
    allow_write: bool = False
    connectors: list[str] = Field(default_factory=list)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    max_runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, ge=1, le=MAX_RUNTIME_MINUTES)

    @field_validator("connectors")
    @classmethod
    def _connectors(cls, v: list[str]) -> list[str]:
        # An automation opened before a connector was renamed is sent back with the old name.
        return normalize_connectors(v)


RunId = Annotated[str, StringConstraints(pattern=chat.RUN_ID_PATTERN)]


class ReadBody(BaseModel):
    run_ids: list[RunId] = Field(max_length=chat.ANCHOR_LIMIT)


class HideBody(BaseModel):
    run_id: RunId


def _store(ctx: AppContext) -> AutomationStore:
    assert ctx.automations is not None
    return ctx.automations


def _validate(ctx: AppContext, body: AutomationBody) -> None:
    # Prompts are stored in automations.yaml and resent on every run, so sensitive data is never accepted here.
    kinds = detect_sensitive(body.prompt)
    if kinds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "sensitive_data",
                "message": "機微情報は指示に含められません: " + "、".join(SENSITIVE_LABELS[k] for k in kinds),
            },
        )
    if ctx.masker.contains_secret(body.prompt) or ctx.masker.contains_secret(body.name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "secret", "message": "API キーなどの秘密情報は指示に含められません（コネクタを使ってください）"},
        )
    known = get_connectors(ctx)
    unknown = [c for c in body.connectors if c not in known]
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown connectors: {', '.join(unknown)}")
    if body.notify.condition == "signal" and not body.notify.signal_field:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signal_field is required when condition is 'signal'")


def _view(ctx: AppContext, automation: Automation) -> dict:
    now = datetime.now(UTC)
    return automation.model_dump(mode="json") | {
        "estimated_runs_per_month": automation.schedule.occurrences_within(now, 30) if automation.enabled else 0,
        "cron": automation.schedule.cron_expression(),
    }


def _run_view(ctx: AppContext, record: dict) -> dict:
    """The run as the history shows it. The stored record is left alone (the run may still be finishing in the
    job process), so both the list and the detail decide the same way, from the same clock."""
    if record.get("status") != RUNNING_STATUS:
        return record
    now = datetime.now(UTC)
    started = parse_timestamp(record.get("started_at"))
    if started is not None and (now - started).total_seconds() > ctx.settings.automation_lock_ttl_seconds:
        return record | {"status": INTERRUPTED_STATUS}
    # Records written before the heartbeat existed have none and are decided by their start alone.
    heartbeat = parse_timestamp(record.get("heartbeat_at"))
    if heartbeat is not None and (now - heartbeat).total_seconds() > HEARTBEAT_STALE_SECONDS:
        return record | {"status": INTERRUPTED_STATUS}
    return record


def _requested_ids(store: AutomationStore) -> set[str]:
    """Automations with a 「今すぐ実行」 waiting for the job to start it."""
    return {r["automation_id"] for r in store.run_requests() if not r["expired"]}


@router.get("")
def list_automations(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    store = _store(ctx)
    items = [_view(ctx, a) for a in store.list()]
    metas = store.list_run_meta()
    # Taken from every run record (the history is capped), so an automation stays marked as running however many
    # newer runs there are; it cannot be started again until then.
    running = {m["automation_id"] for m in metas if _run_view(ctx, m).get("status") == RUNNING_STATUS}
    running |= _requested_ids(store)
    running &= {a["id"] for a in items}  # the history of a deleted automation is kept
    return {
        "automations": items,
        "running_automation_ids": sorted(running),
        "usage": {
            "runs_this_month": store.runs_this_month(),
            "monthly_limit": ctx.settings.automation_monthly_run_limit,
            "estimated_runs_per_month": sum(a["estimated_runs_per_month"] for a in items),
        },
        "unread": store.unread_count(metas),
        "github_notify_configured": build_notifier(ctx).configured,
        "connectors": [
            {"name": c.info.name, "label": c.info.label, "configured": c.configured}
            for c in get_connectors(ctx).values()
        ],
    }


@router.post("")
def create_automation(
    body: AutomationBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    _validate(ctx, body)
    automation = Automation.model_validate(body.model_dump())
    automation.schedule_next(datetime.now(UTC))
    return _view(ctx, _store(ctx).upsert(automation))


# -- chat view of finished runs (declared before the /{automation_id} routes) ----------------------------


def _parse_thread(thread_id: str) -> None:
    try:
        chat.parse_thread_id(thread_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/chat")
def list_chat_threads(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return chat.list_threads(_store(ctx))


@router.get("/chat/{thread_id}")
def get_chat_thread(
    thread_id: str,
    before: Annotated[str | None, Query(pattern=chat.RUN_ID_PATTERN)] = None,
    anchor: Annotated[str | None, Query(pattern=chat.RUN_ID_PATTERN)] = None,
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    _parse_thread(thread_id)
    try:
        thread = chat.get_thread(_store(ctx), thread_id, before=before, anchor=anchor)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return thread


@router.post("/chat/{thread_id}/read")
def read_chat_thread(
    thread_id: str, body: ReadBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    _parse_thread(thread_id)
    store = _store(ctx)
    if not chat.mark_thread_read(store, thread_id, body.run_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return {"ok": True, "unread": store.unread_count()}


@router.post("/chat/{thread_id}/hide")
def hide_chat_thread(
    thread_id: str, body: HideBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    _parse_thread(thread_id)
    if not chat.hide_thread(_store(ctx), thread_id, body.run_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found in this conversation")
    return {"ok": True}


@router.put("/{automation_id}")
def update_automation(
    automation_id: str,
    body: AutomationBody,
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    _validate(ctx, body)
    existing = _store(ctx).get(automation_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "automation not found")
    updated = Automation.model_validate(body.model_dump() | {"id": automation_id})
    updated.state = AutomationState(**existing.state.model_dump())
    updated.schedule_next(datetime.now(UTC))
    return _view(ctx, _store(ctx).upsert(updated))


@router.delete("/{automation_id}")
def delete_automation(
    automation_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    if not _store(ctx).delete(automation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "automation not found")
    return {"ok": True}


@router.get("/runs")
def list_runs(
    automation_id: str | None = None, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> list[dict]:
    try:
        metas = _store(ctx).list_run_meta(automation_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    views = (_run_view(ctx, m) for m in metas[:RUN_LIST_LIMIT])
    return [{k: v[k] for k in RUN_LIST_FIELDS if k in v} for v in views]


@router.get("/{automation_id}/runs/{run_id}")
def get_run(
    automation_id: str, run_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    try:
        record = _store(ctx).get_run(automation_id, run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return _run_view(ctx, record) | {"chat_thread_id": chat.thread_id_for(record)}


@router.post("/{automation_id}/runs/{run_id}/read")
def mark_read(
    automation_id: str, run_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    try:
        _store(ctx).mark_read(automation_id, run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"ok": True}


@router.delete("/{automation_id}/runs/{run_id}")
def delete_run(
    automation_id: str, run_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    """Deletes one run from the history (and from the chat). A run that may still be running cannot be deleted."""
    store = _store(ctx)
    try:
        outcome, record = store.delete_run(
            automation_id, run_id, in_progress=lambda r: _run_view(ctx, r).get("status") == RUNNING_STATUS
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except TimeoutError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "実行の記録を更新中です。もう一度お試しください") from e
    if outcome == "missing":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    if outcome == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "実行中の記録は削除できません。終わってから削除してください")
    assert record is not None
    chat.forget_run(store, record)
    return {"ok": True, "unread": store.unread_count()}


@router.post("/{automation_id}/run")
async def run_now(
    automation_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    store = _store(ctx)
    automation = store.get(automation_id)
    if automation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "automation not found")
    # The screen's list can be older than the saved settings (edited elsewhere), so the reply says what runs.
    accepted = {"name": automation.name, "max_runtime_minutes": automation.max_runtime_minutes}
    busy = any(_run_view(ctx, m).get("status") == RUNNING_STATUS for m in store.list_run_meta(automation_id))
    if busy or automation_id in _requested_ids(store):
        raise HTTPException(status.HTTP_409_CONFLICT, "実行中です。終わってから実行してください")
    run_id = uuid.uuid4().hex[:16]
    starter = job_starter(ctx)
    if starter.configured:
        # Run in the job: the web app scales in to zero (and would stop the run) once nobody uses it for a while.
        if not store.add_run_request(automation_id, run_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "実行中です。終わってから実行してください")
        try:
            await starter.start()
        except JobStartError as e:
            # The request stays: the next scheduled job execution (within 15 minutes) runs it.
            logger.warning("could not start the automation job: %s", e)
            return {"started": True, "run_id": run_id, "runner": "job", "job_started": False} | accepted
        return {"started": True, "run_id": run_id, "runner": "job", "job_started": True} | accepted
    # Local development: run in this process.
    runner: AutomationRunner = ctx.extras.setdefault("automation_runner", AutomationRunner(ctx, store))
    task = asyncio.create_task(runner.run(automation_id, run_id=run_id))
    ctx.extras.setdefault("automation_tasks", set()).add(task)
    task.add_done_callback(ctx.extras["automation_tasks"].discard)
    return {"started": True, "run_id": run_id, "runner": "app"} | accepted
