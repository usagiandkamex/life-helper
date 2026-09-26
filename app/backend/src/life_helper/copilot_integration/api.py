"""HTTP API for conversations, turns (SSE), models and memory organisation."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..auth import CurrentUser, require_user
from ..context import AppContext, get_ctx
from ..security import SENSITIVE_LABELS, detect_sensitive
from .attachments import (
    ATTACHMENT_ONLY_PROMPT,
    MAX_ATTACHMENTS,
    AttachmentError,
    PreparedAttachments,
    prepare_attachments,
)
from .events import history_from_events
from .manager import NoTokenError, SessionStateError
from .turns import ApprovalNotFoundError, ApprovalResolvedError, TurnBusyError

router = APIRouter(prefix="/api")

ORGANIZE_PROMPT = (
    "memory-keeper スキルに従って、知識ベースの memories/ を見直してください。"
    "重複している内容は統合し、古くなった情報は更新し、INDEX.md の目次を整えてください。"
    "最後に、変更したファイルと変更内容を一覧で報告してください。"
)


class CreateConversation(BaseModel):
    title: str = ""
    model: str | None = None


class UpdateConversation(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model: str | None = None


class AttachmentBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(min_length=1)  # base64


class TurnBody(BaseModel):
    prompt: str = Field(default="", max_length=20000)
    model: str | None = None
    confirm_sensitive: bool = False
    attachments: list[AttachmentBody] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @model_validator(mode="after")
    def _not_empty(self) -> TurnBody:
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("a prompt or an attachment is required")
        return self


class ApprovalBody(BaseModel):
    decision: Literal["approve", "approve_all", "reject"]


def _reauth() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {"code": "reauth", "message": "GitHub への再ログインが必要です"})


@router.get("/models")
async def models(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        items = await ctx.copilot.list_models()
    except NoTokenError as e:
        raise _reauth() from e
    return {"default": ctx.settings.default_model, "models": items}


@router.get("/conversations")
def list_conversations(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [asdict(c) | {"busy": ctx.turns.busy(c.id)} for c in ctx.extras["conversations"].list()]


@router.post("/conversations")
def create_conversation(
    body: CreateConversation, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    conv = ctx.extras["conversations"].create(body.title.strip()[:120], body.model or ctx.settings.default_model)
    return asdict(conv)


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    body: UpdateConversation,
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    conv = ctx.extras["conversations"].update(conversation_id, title=body.title, model=body.model)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return asdict(conv)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    conv = ctx.extras["conversations"].get(conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    try:
        async with ctx.turns.reserve(conversation_id):
            if conv.started:
                await ctx.copilot.delete_session(conversation_id)
            ctx.extras["conversations"].delete(conversation_id)
    except TurnBusyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "the conversation is answering; wait until it finishes") from e
    except NoTokenError as e:
        raise _reauth() from e
    except SessionStateError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e)) from e
    return {"ok": True}


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    conv = ctx.extras["conversations"].get(conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    if ctx.turns.busy(conversation_id):
        # Do not touch the live session while it answers; the client re-attaches to the running turn instead.
        return {"messages": [], "busy": True, "turn_id": ctx.turns.active_turn_id(conversation_id)}
    if not conv.started:
        return {"messages": [], "busy": False}
    try:
        # Reserve so a turn cannot start while the history is being read from the session.
        async with ctx.turns.reserve(conversation_id):
            active = await ctx.copilot.open_session(conversation_id, model=conv.model, resume=True)
            events = await active.session.get_events()
    except TurnBusyError:
        return {"messages": [], "busy": True, "turn_id": ctx.turns.active_turn_id(conversation_id)}
    except NoTokenError as e:
        raise _reauth() from e
    except SessionStateError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e)) from e
    return {"messages": history_from_events(events, ctx.masker), "busy": False}


async def _start_turn(
    ctx: AppContext,
    conversation_id: str,
    prompt: str,
    model: str | None,
    attachments: PreparedAttachments | None = None,
) -> dict:
    conv = ctx.extras["conversations"].get(conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    if not ctx.github_token():
        raise _reauth()
    attachments = attachments or PreparedAttachments()
    lines = prompt.strip().splitlines()
    if conv.title == "新しい会話":
        title = lines[0] if lines else attachments.items[0]["name"]
        ctx.extras["conversations"].update(conversation_id, title=title[:40])
    shown = prompt if lines else ATTACHMENT_ONLY_PROMPT
    try:
        turn = await ctx.turns.start(
            conversation_id, shown + attachments.text, model or conv.model, attachments=attachments.blobs
        )
    except TurnBusyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "the conversation is already answering") from e
    return {
        "turn_id": turn.id,
        "conversation_id": conversation_id,
        "message": {"content": shown, "attachments": attachments.items},
    }


async def _reject_images_without_vision(ctx: AppContext, model: str) -> None:
    # The runtime would drop the image with an English error after the turn started; say so before sending.
    try:
        models = await ctx.copilot.list_models()
    except Exception:  # noqa: BLE001 - the turn itself reports connection and sign-in problems
        return
    info = next((m for m in models if m["id"] == model), None)
    if info is not None and info.get("vision") is False:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "code": "no_vision",
                "message": "選択中のモデルは画像を読み取れません。画像に対応したモデルを選んでください",
            },
        )


@router.post("/conversations/{conversation_id}/turns")
async def start_turn(
    conversation_id: str, body: TurnBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    conv = ctx.extras["conversations"].get(conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    try:
        prepared = await asyncio.to_thread(
            prepare_attachments, [(a.name, a.data) for a in body.attachments], max_bytes=ctx.settings.upload_max_bytes
        )
    except AttachmentError as e:
        raise HTTPException(e.status_code, str(e)) from e
    kinds = detect_sensitive(f"{body.prompt}\n{prepared.raw_text}")
    if kinds and not body.confirm_sensitive:
        where = "（添付ファイルを含む）" if prepared.raw_text else ""
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "sensitive_data",
                "kinds": kinds,
                "message": f"機微情報の可能性があります{where}: " + "、".join(SENSITIVE_LABELS[k] for k in kinds),
            },
        )
    if prepared.blobs:
        await _reject_images_without_vision(ctx, body.model or conv.model)
    return await _start_turn(ctx, conversation_id, body.prompt, body.model, prepared)


@router.get("/turns/{turn_id}/events")
async def turn_events(
    turn_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> StreamingResponse:
    turn = ctx.turns.get(turn_id)
    if turn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found (it may have expired)")
    try:
        last = int(last_event_id) if last_event_id is not None else -1
    except ValueError:
        last = -1
    return StreamingResponse(
        ctx.turns.stream(turn, last),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/turns/{turn_id}/abort")
async def abort_turn(
    turn_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    return {"aborted": await ctx.turns.abort(turn_id)}


@router.post("/turns/{turn_id}/approvals/{approval_id}")
async def decide_approval(
    turn_id: str,
    approval_id: str,
    body: ApprovalBody,
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    try:
        result = ctx.turns.resolve_approval(turn_id, approval_id, body.decision)
    except ApprovalNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found (it may have expired)") from e
    except ApprovalResolvedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "resolved", "status": str(e)}) from e
    return {"status": result}


@router.post("/memories/organize")
async def organize_memories(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    conv = ctx.extras["conversations"].create("メモリの整理", ctx.settings.default_model)
    return await _start_turn(ctx, conv.id, ORGANIZE_PROMPT, None)
