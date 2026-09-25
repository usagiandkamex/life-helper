"""HTTP API for automations: definitions, run history, run-now and usage estimates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from ..auth import CurrentUser, require_user
from ..connectors.registry import get_connectors
from ..context import AppContext, get_ctx
from ..security import SENSITIVE_LABELS, detect_sensitive
from .models import Automation, AutomationState, NotifySettings, Schedule, normalize_connectors
from .runner import AutomationRunner, build_notifier
from .store import AutomationStore

router = APIRouter(prefix="/api/automations")


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
    max_runtime_minutes: int = Field(default=20, ge=1, le=20)

    @field_validator("connectors")
    @classmethod
    def _connectors(cls, v: list[str]) -> list[str]:
        # An automation opened before a connector was renamed is sent back with the old name.
        return normalize_connectors(v)


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


@router.get("")
def list_automations(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    store = _store(ctx)
    items = [_view(ctx, a) for a in store.list()]
    return {
        "automations": items,
        "usage": {
            "runs_this_month": store.runs_this_month(),
            "monthly_limit": ctx.settings.automation_monthly_run_limit,
            "estimated_runs_per_month": sum(a["estimated_runs_per_month"] for a in items),
        },
        "unread": store.unread_count(),
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
        runs = _store(ctx).list_runs(automation_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return [{k: v for k, v in r.items() if k != "events"} for r in runs]


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
    return record


@router.post("/{automation_id}/runs/{run_id}/read")
def mark_read(
    automation_id: str, run_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    try:
        _store(ctx).mark_read(automation_id, run_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"ok": True}


@router.post("/{automation_id}/run")
async def run_now(
    automation_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    if _store(ctx).get(automation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "automation not found")
    runner: AutomationRunner = ctx.extras.setdefault("automation_runner", AutomationRunner(ctx, _store(ctx)))
    task = asyncio.create_task(runner.run(automation_id))
    ctx.extras.setdefault("automation_tasks", set()).add(task)
    task.add_done_callback(ctx.extras["automation_tasks"].discard)
    return {"started": True}
