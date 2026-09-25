"""Deny-by-default tool policy: the Copilot permission handler plus pre/post tool-use hooks.

Two layers are applied on purpose (verified against the runtime):
* the permission handler decides read/write/url/custom-tool requests raised by the runtime, and
* the ``pre_tool_use`` hook checks tool arguments before execution, because writes into Copilot's own
  session workspace are auto-approved by the runtime without raising a permission request.

The runtime's built-in file writers (create/edit/apply_patch) are not exposed at all. Knowledge-base writes go
only through the app's own tools (``knowledge_tools``), which check the location and content, ask the user in chat,
take the shared write lock and write the file themselves, so no path depends on the fail-open hook.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from ..security import SecretMasker

logger = logging.getLogger(__name__)

# The runtime names some tools per model: GPT-family models search with ``rg`` (paths only) instead of ``grep``.
ALLOWED_BUILTINS = ("view", "grep", "rg", "glob", "web_fetch", "skill")
READ_TOOLS = ("view", "grep", "rg", "glob")
COPILOT_WRITABLE_DIRS = ("memories", "notes", "plans")
COPILOT_WRITABLE_FILES = ("INDEX.md",)
WRITABLE_SUFFIXES = (".md", ".txt")
# Hosts that carry API keys in the URL: only connectors may call them, never web_fetch.
CONNECTOR_HOSTS = ("openapi.rakuten.co.jp", "app.rakuten.co.jp", "api.github.com")


def has_hidden_chars(raw: str) -> bool:
    """Control, formatting (bidi overrides included) and line/paragraph separators.

    The approval card shows the path and the unified diff, so such a character could inject extra lines into the
    diff headers or hide part of the path: the user would approve a write to something else than what is shown.
    """
    return any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in raw)


def host_matches(host: str, domains: list[str] | tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def knowledge_write_lock_path(settings: Any) -> Path:
    """Lock shared by chat turns, automation runs and UI edits (all processes on the same volume)."""
    return settings.app_state_dir / "locks" / "knowledge-write.lock"


@dataclass(frozen=True)
class Approval:
    approved: bool
    reason: str = ""
    # Set when the user decided on an approval card, so the UI can attach the write result to that card.
    approval_id: str | None = None


Approver = Callable[[str, str], Awaitable[Approval]]
"""Asks the user to approve a knowledge-base write: ``(display path, unified diff) -> Approval``."""


def _always_active() -> bool:
    return True


@dataclass(frozen=True)
class WriteScope:
    """Callbacks of one chat turn. A tool call takes the scope that is current when it starts and keeps it, so a
    delayed call from a finished turn can never show a card in, or report a write to, the next turn."""

    approver: Approver | None = None
    # Called after a knowledge-base file was written: (display path, diff, approval id or None).
    on_write: Callable[[str, str, str | None], None] | None = None
    is_active: Callable[[], bool] = _always_active


@dataclass
class ToolPolicy:
    knowledge_root: Path
    skills_root: Path
    fetch_domains: list[str]
    masker: SecretMasker
    custom_tools: set[str] = field(default_factory=set)
    write_custom_tools: set[str] = field(default_factory=set)
    allow_write: bool = True
    # Chat: every knowledge-base write waits for the user's approval, and is refused without an approver.
    require_approval: bool = False
    write_scope: WriteScope | None = None
    on_tool_result: Callable[[str, Any], None] | None = None
    write_lock_path: Path | None = None
    denials: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.knowledge_root = self.knowledge_root.resolve()
        self.skills_root = self.skills_root.resolve()

    # -- path helpers ------------------------------------------------------------------------------------

    def resolve_path(self, raw: str) -> Path:
        """Absolute, symlink-resolved path; relative paths are taken from the knowledge root."""
        p = Path(raw)
        if not p.is_absolute():
            p = self.knowledge_root / p
        return p.resolve()

    def _readable(self, raw: str) -> bool:
        p = self.resolve_path(raw)
        return p.is_relative_to(self.knowledge_root) or p.is_relative_to(self.skills_root)

    def writable(self, raw: str) -> bool:
        if not self.allow_write or not raw or has_hidden_chars(raw):
            return False
        p = self.resolve_path(raw)
        if not p.is_relative_to(self.knowledge_root):
            return False
        rel = p.relative_to(self.knowledge_root)
        if len(rel.parts) == 1:
            return rel.parts[0] in COPILOT_WRITABLE_FILES
        return rel.parts[0] in COPILOT_WRITABLE_DIRS and p.suffix.lower() in WRITABLE_SUFFIXES

    def display_path(self, raw: str) -> str:
        p = self.resolve_path(raw)
        return p.relative_to(self.knowledge_root).as_posix() if p.is_relative_to(self.knowledge_root) else str(p)

    def _url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not host:
            return False
        if host_matches(host, CONNECTOR_HOSTS):
            return False
        return host_matches(host, self.fetch_domains)

    def _deny(self, reason: str) -> PermissionDecisionReject:
        reason = self.masker.mask_text(reason)
        self.denials.append(reason)
        logger.info("tool policy denied: %s", reason)
        return PermissionDecisionReject(feedback=reason)

    # -- permission handler ------------------------------------------------------------------------------

    def handle_permission(self, request: Any, invocation: Any) -> Any:
        kind = getattr(type(request), "kind", None) or getattr(request, "kind", None)
        if kind == "read":
            path = request.resolved_path or request.path
            if path and self._readable(path):
                return PermissionDecisionApproveOnce()
            return self._deny(f"読み取りは知識ベースの中だけ許可されています: {path}")
        if kind == "write":
            # Built-in file writers are not exposed; this only catches anything the runtime might still raise.
            return self._deny(
                "ファイルへの書き込みは write_knowledge_file / edit_knowledge_file ツールだけで行えます: "
                f"{request.resolved_path or request.file_name}"
            )
        if kind == "url":
            if self._url_allowed(request.url):
                return PermissionDecisionApproveOnce()
            return self._deny(f"このサイトは参照が許可されていません: {request.url}")
        if kind == "custom-tool":
            name = request.tool_name
            if name not in self.custom_tools:
                return self._deny(f"未登録のツールです: {name}")
            if name in self.write_custom_tools and not self.allow_write:
                return self._deny(f"このオートメーションでは書き込み系ツールは使えません: {name}")
            return PermissionDecisionApproveOnce()
        return self._deny(f"この操作は許可されていません: {kind}")

    # -- approval ----------------------------------------------------------------------------------------

    async def request_approval(self, scope: WriteScope | None, path: str, diff: str) -> Approval:
        """Asks the scope's approver; anything other than an explicit approval refuses the write."""
        approver = scope.approver if scope is not None else None
        if approver is None or not scope.is_active():
            return Approval(False, "この会話では書き込みを承認できないため、書き込みませんでした")
        try:
            approval = await approver(path, diff)
        except Exception:  # noqa: BLE001
            logger.warning("write approval failed", exc_info=True)
            return Approval(False, "書き込みの承認を確認できなかったため、書き込みませんでした")
        return approval if isinstance(approval, Approval) else Approval(False, "書き込みは承認されませんでした")

    # -- hooks -------------------------------------------------------------------------------------------

    async def pre_tool_use(self, hook_input: dict, _ctx: dict) -> dict | None:
        tool = hook_input.get("toolName", "")
        if tool in self.custom_tools:
            if tool in self.write_custom_tools and not self.allow_write:
                return self._hook_deny(f"このオートメーションでは書き込み系ツールは使えません: {tool}")
            return None
        if tool not in ALLOWED_BUILTINS:
            return self._hook_deny(f"このツールは許可されていません: {tool}")
        if tool == "skill":
            return None
        raw_args = hook_input.get("toolArgs")
        if not isinstance(raw_args, dict | None):
            return self._hook_deny(f"ツールの引数を確認できませんでした: {tool}")
        args = dict(raw_args or {})
        if tool == "web_fetch":
            url = str(args.get("url", ""))
            return None if self._url_allowed(url) else self._hook_deny(f"このサイトは参照が許可されていません: {url}")
        if tool in READ_TOOLS:
            return self._check_read_args(tool, args)
        return None

    def _check_read_args(self, tool: str, args: dict) -> dict | None:
        deny = f"読み取りは {self.knowledge_root.as_posix()} の中だけ許可されています"
        modified = False
        if "paths" in args:
            raw = args["paths"]
            # The runtime accepts both a single string and a list here (observed live), so check both shapes.
            if isinstance(raw, str):
                paths = [raw] if raw else []
            elif isinstance(raw, list):
                paths = [str(p) for p in raw]
            else:
                return self._hook_deny(deny)
            paths = paths or [str(self.knowledge_root)]
            if not all(self._readable(p) for p in paths):
                return self._hook_deny(deny)
            absolute = [str(self.resolve_path(p)) for p in paths]
            args["paths"] = absolute[0] if isinstance(raw, str) and len(absolute) == 1 else absolute
            modified = True
        path = args.get("path")
        if path is not None or "paths" not in args:
            if not path:
                # Without a path the runtime would fall back to its own session workspace.
                args["paths" if tool == "rg" else "path"] = str(self.knowledge_root)
                modified = True
            elif not self._readable(str(path)):
                return self._hook_deny(deny)
            elif not Path(str(path)).is_absolute():
                args["path"] = str(self.resolve_path(str(path)))
                modified = True
        return {"modifiedArgs": args} if modified else None

    def _hook_deny(self, reason: str) -> dict:
        reason = self.masker.mask_text(reason)
        self.denials.append(reason)
        logger.info("tool hook denied: %s", reason)
        return {"permissionDecision": "deny", "permissionDecisionReason": reason}

    async def post_tool_use(self, hook_input: dict, _ctx: dict) -> dict | None:
        result = hook_input.get("toolResult")
        masked = self.masker.mask(result)
        if self.on_tool_result is not None:
            self.on_tool_result(hook_input.get("toolName", ""), masked)
        if masked != result:
            return {"modifiedResult": masked}
        return None

    def hooks(self) -> dict:
        return {
            "on_pre_tool_use": self.pre_tool_use,
            "on_post_tool_use": self.post_tool_use,
        }
