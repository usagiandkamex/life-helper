"""The only way Copilot writes to the knowledge base: app-owned tools that check, ask and write the file themselves.

The runtime's built-in writers are not exposed, because a write that bypasses the permission handler (or runs after
a timed-out, fail-open ``pre_tool_use`` hook) could never be stopped. Here every step runs in the app, in order:
location and content checks, the user's approval in chat (without holding any lock), then the shared write lock,
a re-check that the file did not change since the user saw the diff, and an atomic write.
"""

from __future__ import annotations

import asyncio
import difflib
from collections.abc import Callable
from pathlib import Path

from copilot import define_tool
from pydantic import BaseModel, Field

from ..automation.locks import FileLock, wait_acquire
from ..knowledge.store import atomic_write
from ..security import SENSITIVE_LABELS, detect_sensitive, redact_sensitive
from ..tools.registry import ToolSpec
from .policy import ToolPolicy

MAX_FILE_CHARS = 100_000
WRITE_LOCK_TTL_SECONDS = 60
WRITE_LOCK_WAIT_SECONDS = 15
PATH_DESCRIPTION = "知識ベースの中のファイル（絶対パス、または知識ベースからの相対パス。例: memories/money.md）"


class WriteFileParams(BaseModel):
    path: str = Field(description=PATH_DESCRIPTION)
    content: str = Field(description="ファイル全体の新しい内容（Markdown）")


class EditFileParams(BaseModel):
    path: str = Field(description=PATH_DESCRIPTION)
    old_str: str = Field(
        default="",
        description=(
            "置き換える部分（ファイルの中で 1 か所だけ一致する文字列）。空にすると new_str をファイルの末尾に追記する"
        ),
    )
    new_str: str = Field(description="新しい内容")


class EditError(ValueError):
    pass


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


NO_NEWLINE = "\\ No newline at end of file"


def unified_diff(path: str, before: str | None, after: str) -> str:
    """A unified diff that also shows line-ending changes, so no change is invisible to the user."""
    out: list[str] = []
    for line in difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile=f"b/{path}",
    ):
        if line.endswith("\n"):
            out.append(line[:-1])
        else:
            out += [line, NO_NEWLINE]
    return "\n".join(out)


def _fail(message: str) -> dict:
    return {"saved": False, "error": message}


def apply_edit(before: str | None, old_str: str, new_str: str) -> str:
    if before is None:
        raise EditError("ファイルがありません。新しく作るときは write_knowledge_file を使ってください")
    if not old_str:
        separator = "" if not before or before.endswith("\n") else "\n"
        return before + separator + (new_str if new_str.endswith("\n") else new_str + "\n")
    count = before.count(old_str)
    if count == 0:
        raise EditError("old_str がファイルの中に見つかりませんでした。view で最新の内容を確認してください")
    if count > 1:
        raise EditError(f"old_str がファイルの中に {count} か所あります。前後の行を含めて 1 か所に絞ってください")
    return before.replace(old_str, new_str, 1)


async def save_knowledge_file(policy: ToolPolicy, raw_path: str, build: Callable[[str | None], str]) -> dict:
    """Checks, asks for approval (chat), then writes ``build(current content)`` to ``raw_path``."""
    # Taken before any await: this call belongs to the turn that is running now (see WriteScope).
    scope = policy.write_scope
    if not policy.allow_write:
        return _fail("このオートメーションは読み取り専用のため、ファイルを変更できません")
    if not policy.writable(raw_path):
        kb = policy.knowledge_root.as_posix()
        return _fail(f"書き込みは {kb} の memories/・notes/・plans/（.md / .txt）と INDEX.md だけ許可されています")
    target, display = policy.resolve_path(raw_path), policy.display_path(raw_path)
    try:
        before = await asyncio.to_thread(_read, target)
        after = build(before)
    except EditError as e:
        return _fail(str(e))
    except OSError:
        return _fail("ファイルを読み込めませんでした")
    if len(after) > MAX_FILE_CHARS:
        return _fail(f"1 ファイルは {MAX_FILE_CHARS:,} 文字までです。トピックごとにファイルを分けてください")
    kinds = detect_sensitive(after)
    if kinds:
        return _fail(f"機微情報（{'、'.join(SENSITIVE_LABELS[k] for k in kinds)}）はファイルに保存できません")
    if policy.masker.contains_secret(after):
        return _fail("API キーなどの秘密情報はファイルに保存できません")
    if after == before:
        return {"saved": False, "path": display, "message": "内容が同じなので変更しませんでした"}

    # Removed lines may still hold old sensitive values: never show them (the approval is bound to before/after).
    diff = policy.masker.mask_text(redact_sensitive(unified_diff(display, before, after))[0])
    if not diff:
        return _fail("変更内容を表示できないため、書き込みませんでした")
    approval_id = None
    if policy.require_approval:
        approval = await policy.request_approval(scope, display, diff)
        if not approval.approved:
            return _fail(approval.reason or "利用者が書き込みを承認しませんでした")
        approval_id = approval.approval_id

    lock = FileLock(policy.write_lock_path, WRITE_LOCK_TTL_SECONDS) if policy.write_lock_path else None
    if lock is not None and not await wait_acquire(lock, WRITE_LOCK_WAIT_SECONDS):
        return _fail("ほかの処理が知識ベースを更新中です。少し待ってからもう一度試してください")
    try:
        # The user approved this exact diff: refuse if the file changed meanwhile (UI edit, another conversation).
        if await asyncio.to_thread(_read, target) != before:
            return _fail(
                "確認のあいだにファイルが変更されたため、書き込みませんでした。読み直してからもう一度変更してください"
            )
        # Checked last, with no await before the write, so an answer stopped meanwhile never writes afterwards.
        if scope is not None and not scope.is_active():
            return _fail("回答が中断・終了したため、書き込みませんでした")
        await asyncio.to_thread(atomic_write, target, after)
    except OSError:
        return _fail("ファイルに書き込めませんでした")
    finally:
        if lock is not None:
            lock.release()
    if scope is not None and scope.on_write is not None:
        scope.on_write(display, diff, approval_id)
    return {"saved": True, "path": display, "message": "保存しました"}


def build_knowledge_tools(policy: ToolPolicy) -> list[ToolSpec]:
    @define_tool(
        name="write_knowledge_file",
        description=(
            "知識ベースのファイルを新しく作る、または全体を書き換える。書き込めるのは memories/・notes/・plans/"
            "（.md / .txt）と INDEX.md だけ。チャットでは利用者が承認したときだけ保存される。"
        ),
    )
    async def write_knowledge_file(params: WriteFileParams) -> dict:
        return await save_knowledge_file(policy, params.path, lambda _before: params.content)

    @define_tool(
        name="edit_knowledge_file",
        description=(
            "知識ベースの既存ファイルの一部を置き換える（old_str がファイルの中で 1 か所だけ一致すること）。"
            "old_str を空にすると new_str を末尾に追記する。書き込める場所と承認は write_knowledge_file と同じ。"
        ),
    )
    async def edit_knowledge_file(params: EditFileParams) -> dict:
        return await save_knowledge_file(
            policy, params.path, lambda before: apply_edit(before, params.old_str, params.new_str)
        )

    return [ToolSpec(write_knowledge_file, writes=True), ToolSpec(edit_knowledge_file, writes=True)]
