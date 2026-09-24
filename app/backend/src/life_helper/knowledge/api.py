"""HTTP API for browsing, editing, uploading and exporting the knowledge base."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import CurrentUser, require_user
from ..automation.locks import FileLock, wait_acquire
from ..context import AppContext, get_ctx
from ..copilot_integration.policy import knowledge_write_lock_path
from ..security import SENSITIVE_LABELS, detect_sensitive, redact_sensitive
from .store import KnowledgePathError, KnowledgeStore, convert_upload

router = APIRouter(prefix="/api")


def _store(ctx: AppContext) -> KnowledgeStore:
    assert ctx.knowledge is not None
    return ctx.knowledge


def _reject_sensitive(text: str) -> None:
    # Knowledge files are readable by the model, so sensitive values are never stored there (no override).
    kinds = detect_sensitive(text)
    if kinds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "sensitive_data",
                "kinds": kinds,
                "message": "機微情報はファイルに保存できません: " + "、".join(SENSITIVE_LABELS[k] for k in kinds),
            },
        )


def _refresh_system_message_if_needed(ctx: AppContext, path: str) -> None:
    normalized = path.replace("\\", "/")
    if normalized.startswith("profile/") and ctx.copilot is not None:
        ctx.copilot.mark_sessions_stale()


class WriteBody(BaseModel):
    path: str
    content: str


@router.get("/files")
def list_files(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [asdict(e) for e in _store(ctx).list_files()]


@router.get("/files/content")
def read_file(path: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    store = _store(ctx)
    try:
        return {"path": path, "content": store.read_text(path), "writable": store.is_user_writable(path)}
    except KnowledgePathError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from e


@router.put("/files/content")
async def write_file(
    body: WriteBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    _reject_sensitive(body.content)
    async with _knowledge_write_lock(ctx):
        try:
            saved = await asyncio.to_thread(_store(ctx).write_text, body.path, body.content)
        except KnowledgePathError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    # Runs on the event loop, which owns the Copilot manager state.
    _refresh_system_message_if_needed(ctx, saved)
    return {"path": saved}


@router.delete("/files")
async def delete_file(path: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    async with _knowledge_write_lock(ctx):
        try:
            await asyncio.to_thread(_store(ctx).delete, path)
        except KnowledgePathError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from e
    _refresh_system_message_if_needed(ctx, path)
    return {"ok": True}


@contextlib.asynccontextmanager
async def _knowledge_write_lock(ctx: AppContext):
    """Same lock that Copilot's create/edit take, so UI edits never interleave with chat/automation writes."""
    lock = FileLock(knowledge_write_lock_path(ctx.settings), ttl_seconds=60)
    if not await wait_acquire(lock, 10):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "ほかの処理が知識ベースを更新中です。少し待ってから保存してください"
        )
    try:
        yield
    finally:
        lock.release()


@router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    limit = ctx.settings.upload_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"file is larger than {limit // (1024 * 1024)} MB")
    try:
        markdown = await asyncio.to_thread(convert_upload, file.filename or "upload.txt", data)
    except KnowledgePathError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except Exception as e:  # noqa: BLE001 - pypdf raises many exception types for broken files
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the file could not be read") from e
    # Documents such as statements often contain account numbers: store them with those values removed.
    markdown, kinds = redact_sensitive(markdown)
    path = await asyncio.to_thread(_store(ctx).save_upload, file.filename or "upload.txt", markdown)
    return {"path": path, "redacted": [SENSITIVE_LABELS[k] for k in kinds]}


@router.get("/export.zip")
def export_zip(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> Response:
    return Response(
        _store(ctx).export_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="life-helper-knowledge.zip"'},
    )
