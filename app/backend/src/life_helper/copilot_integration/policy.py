"""Deny-by-default tool policy: the Copilot permission handler plus pre/post tool-use hooks.

Two layers are applied on purpose (verified against the runtime):
* the permission handler decides read/write/url/custom-tool requests raised by the runtime, and
* the ``pre_tool_use`` hook checks tool arguments before execution, because writes into Copilot's own
  session workspace are auto-approved by the runtime without raising a permission request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from ..automation.locks import FileLock
from ..security import SENSITIVE_LABELS, SecretMasker, detect_sensitive

logger = logging.getLogger(__name__)

ALLOWED_BUILTINS = ("view", "grep", "glob", "create", "edit", "web_fetch", "skill")
READ_TOOLS = ("view", "grep", "glob")
WRITE_TOOLS = ("create", "edit")
COPILOT_WRITABLE_DIRS = ("memories", "notes", "plans")
COPILOT_WRITABLE_FILES = ("INDEX.md",)
WRITABLE_SUFFIXES = (".md", ".txt")
# Hosts that carry API keys in the URL: only connectors may call them, never web_fetch.
CONNECTOR_HOSTS = ("openapi.rakuten.co.jp", "app.rakuten.co.jp", "api.github.com")


def host_matches(host: str, domains: list[str] | tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def knowledge_write_lock_path(settings: Any) -> Path:
    """Lock shared by chat turns, automation runs and UI edits (all processes on the same volume)."""
    return settings.app_state_dir / "locks" / "knowledge-write.lock"


WRITE_LOCK_TTL_SECONDS = 120
WRITE_LOCK_WAIT_SECONDS = 15


@dataclass
class ToolPolicy:
    knowledge_root: Path
    skills_root: Path
    fetch_domains: list[str]
    masker: SecretMasker
    custom_tools: set[str] = field(default_factory=set)
    write_custom_tools: set[str] = field(default_factory=set)
    allow_write: bool = True
    on_write: Callable[[str, str], None] | None = None
    on_tool_result: Callable[[str, Any], None] | None = None
    write_lock_path: Path | None = None
    denials: list[str] = field(default_factory=list)
    _write_lock: FileLock | None = field(default=None, init=False, repr=False)
    _write_holds: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.knowledge_root = self.knowledge_root.resolve()
        self.skills_root = self.skills_root.resolve()

    # -- path helpers ------------------------------------------------------------------------------------

    def _abs(self, raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = self.knowledge_root / p
        return p.resolve()

    def _readable(self, raw: str) -> bool:
        p = self._abs(raw)
        return p.is_relative_to(self.knowledge_root) or p.is_relative_to(self.skills_root)

    def _writable(self, raw: str) -> bool:
        if not self.allow_write:
            return False
        p = self._abs(raw)
        if not p.is_relative_to(self.knowledge_root):
            return False
        rel = p.relative_to(self.knowledge_root)
        if len(rel.parts) == 1:
            return rel.parts[0] in COPILOT_WRITABLE_FILES
        return rel.parts[0] in COPILOT_WRITABLE_DIRS and p.suffix.lower() in WRITABLE_SUFFIXES

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
            path = request.resolved_path or request.file_name
            if not path or not self._writable(path):
                return self._deny(f"この場所への書き込みは許可されていません: {path}")
            # Check the complete post-edit file, not only the inserted fragment (split edits must not bypass this).
            full = getattr(request, "new_file_contents", None)
            if full is None:
                diff_lines = (getattr(request, "diff", "") or "").splitlines()
                full = "\n".join(line[1:] for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
            kinds = detect_sensitive(full)
            if kinds:
                labels = "、".join(SENSITIVE_LABELS[k] for k in kinds)
                return self._deny(f"機微情報（{labels}）はファイルに保存できません")
            if self.on_write:
                self.on_write(self._display_path(path), request.diff or "")
            return PermissionDecisionApproveOnce()
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

    def _display_path(self, raw: str) -> str:
        p = self._abs(raw)
        return p.relative_to(self.knowledge_root).as_posix() if p.is_relative_to(self.knowledge_root) else str(p)

    # -- hooks -------------------------------------------------------------------------------------------

    async def pre_tool_use(self, hook_input: dict, _ctx: dict) -> dict | None:
        tool = hook_input.get("toolName", "")
        args = dict(hook_input.get("toolArgs") or {})
        if tool in self.custom_tools:
            if tool in self.write_custom_tools and not self.allow_write:
                return self._hook_deny(f"このオートメーションでは書き込み系ツールは使えません: {tool}")
            return None
        if tool not in ALLOWED_BUILTINS:
            return self._hook_deny(f"このツールは許可されていません: {tool}")
        if tool == "skill":
            return None
        if tool == "web_fetch":
            url = str(args.get("url", ""))
            return None if self._url_allowed(url) else self._hook_deny(f"このサイトは参照が許可されていません: {url}")
        if tool in READ_TOOLS:
            return self._check_read_args(tool, args)
        if tool in WRITE_TOOLS:
            path = str(args.get("path", ""))
            if not path or not self._writable(path):
                kb = self.knowledge_root.as_posix()
                return self._hook_deny(f"書き込みは {kb} の memories/・notes/・plans/ と INDEX.md だけ許可されています")
            text = "\n".join(str(args.get(k, "")) for k in ("file_text", "new_str"))
            simulated = self._simulate_edit(path, args) if tool == "edit" else ""
            kinds = detect_sensitive(text) or detect_sensitive(simulated)
            if kinds:
                labels = "、".join(SENSITIVE_LABELS[k] for k in kinds)
                return self._hook_deny(f"機微情報（{labels}）はファイルに保存できません")
            if not await self._acquire_write():
                return self._hook_deny("ほかの処理が知識ベースを更新中です。少し待ってからもう一度試してください")
            if not Path(path).is_absolute():
                args["path"] = str(self._abs(path))
                return {"modifiedArgs": args}
        return None

    async def _acquire_write(self) -> bool:
        """Takes the shared knowledge-write lock. Reentrant within this session (the model may issue parallel
        writes), exclusive across sessions and processes."""
        if self.write_lock_path is None:
            return True
        if self._write_holds > 0:
            self._write_holds += 1
            return True
        lock = FileLock(self.write_lock_path, WRITE_LOCK_TTL_SECONDS)
        for _ in range(WRITE_LOCK_WAIT_SECONDS * 4):
            if lock.try_acquire():
                self._write_lock, self._write_holds = lock, 1
                return True
            await asyncio.sleep(0.25)
        return False

    def _release_write(self) -> None:
        if self._write_holds <= 0:
            return
        self._write_holds -= 1
        if self._write_holds == 0 and self._write_lock is not None:
            self._write_lock.release()
            self._write_lock = None

    def _simulate_edit(self, raw_path: str, args: dict) -> str:
        """Returns the file content after applying an ``edit`` so split edits cannot assemble sensitive data."""
        try:
            current = self._abs(raw_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        old, new = str(args.get("old_str", "")), str(args.get("new_str", ""))
        return current.replace(old, new, 1) if old else current + new

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
            absolute = [str(self._abs(p)) for p in paths]
            args["paths"] = absolute[0] if isinstance(raw, str) and len(absolute) == 1 else absolute
            modified = True
        path = args.get("path")
        if path is not None or "paths" not in args:
            if not path:
                # Without a path the runtime would fall back to its own session workspace.
                args["path"] = str(self.knowledge_root)
                modified = True
            elif not self._readable(str(path)):
                return self._hook_deny(deny)
            elif not Path(str(path)).is_absolute():
                args["path"] = str(self._abs(str(path)))
                modified = True
        return {"modifiedArgs": args} if modified else None

    def _hook_deny(self, reason: str) -> dict:
        reason = self.masker.mask_text(reason)
        self.denials.append(reason)
        logger.info("tool hook denied: %s", reason)
        return {"permissionDecision": "deny", "permissionDecisionReason": reason}

    async def post_tool_use(self, hook_input: dict, _ctx: dict) -> dict | None:
        if hook_input.get("toolName") in WRITE_TOOLS:
            self._release_write()
        result = hook_input.get("toolResult")
        masked = self.masker.mask(result)
        if self.on_tool_result is not None:
            self.on_tool_result(hook_input.get("toolName", ""), masked)
        if masked != result:
            return {"modifiedResult": masked}
        return None

    async def post_tool_use_failure(self, hook_input: dict, _ctx: dict) -> dict | None:
        if hook_input.get("toolName") in WRITE_TOOLS:
            self._release_write()
        return None

    def release_all(self) -> None:
        """Called when a turn/run ends so a write lock can never outlive its session."""
        self._write_holds = min(self._write_holds, 1)
        self._release_write()

    def hooks(self) -> dict:
        return {
            "on_pre_tool_use": self.pre_tool_use,
            "on_post_tool_use": self.post_tool_use,
            "on_post_tool_use_failure": self.post_tool_use_failure,
        }
