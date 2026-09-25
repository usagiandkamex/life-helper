from __future__ import annotations

from types import SimpleNamespace

import pytest
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from life_helper.copilot_integration.policy import ToolPolicy
from life_helper.copilot_integration.system_prompt import build_system_message
from life_helper.netguard import host_matches
from life_helper.security import SecretMasker


class Req(SimpleNamespace):
    """Stand-in for the runtime's permission request objects (kind is a class attribute there)."""


def req(kind: str, **fields):
    cls = type(f"Req_{kind}", (Req,), {"kind": kind})
    base = {
        "path": None,
        "resolved_path": None,
        "file_name": None,
        "diff": "",
        "new_file_contents": None,
        "url": None,
        "tool_name": None,
    }
    return cls(**(base | fields))


@pytest.fixture
def kb(tmp_path):
    root = tmp_path / "knowledge"
    for d in ("memories", "notes", "plans", "profile", "docs", "money"):
        (root / d).mkdir(parents=True)
    (root / "INDEX.md").write_text("# idx", encoding="utf-8")
    (root / "profile" / "about-me.md").write_text("東京都在住", encoding="utf-8")
    return root


@pytest.fixture
def policy(kb, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    writes: list = []
    p = ToolPolicy(
        knowledge_root=kb,
        skills_root=skills,
        masker=SecretMasker(["SUPERSECRETKEY"]),
        custom_tools={"calculate", "update_holding"},
        write_custom_tools={"update_holding"},
        on_write=lambda path, diff: writes.append(path),
    )
    p.writes = writes  # type: ignore[attr-defined]
    return p


def approved(decision) -> bool:
    return isinstance(decision, PermissionDecisionApproveOnce)


def test_read_permissions(policy, kb, tmp_path):
    assert approved(policy.handle_permission(req("read", path=str(kb / "INDEX.md")), {}))
    assert approved(policy.handle_permission(req("read", path=str(policy.skills_root / "x" / "SKILL.md")), {}))
    assert isinstance(
        policy.handle_permission(req("read", path=str(tmp_path / "other.txt")), {}), PermissionDecisionReject
    )
    assert isinstance(policy.handle_permission(req("read", path=str(kb / ".." / "x")), {}), PermissionDecisionReject)


def test_write_permissions(policy, kb):
    assert approved(policy.handle_permission(req("write", file_name=str(kb / "memories" / "money.md")), {}))
    assert approved(policy.handle_permission(req("write", file_name=str(kb / "INDEX.md")), {}))
    assert policy.writes == ["memories/money.md", "INDEX.md"]
    for bad in (kb / "profile" / "about-me.md", kb / "docs" / "x.md", kb / "money" / "portfolio.yaml", kb / "x.md"):
        assert not approved(policy.handle_permission(req("write", file_name=str(bad)), {})), bad
    assert not approved(policy.handle_permission(req("write", file_name=str(kb / "notes" / "run.py")), {}))


def test_url_permissions(policy):
    for ok in (
        "https://www.soumu.go.jp/main_sosiki/",
        "http://www.soumu.go.jp/",
        "https://example.com/",
        "https://query1.finance.yahoo.com/v8/finance/chart/7203.T",
        "https://93.184.215.14/",
    ):
        assert approved(policy.handle_permission(req("url", url=ok), {})), ok
    for bad in (
        "https://openapi.rakuten.co.jp/engine/api",
        "https://api.github.com/user",
        "https://api.github.com。/user",
        "https://ａｐｐ.rakuten.co.jp/",
        "http://127.0.0.1:8000/healthz",
        "http://localhost:8000/",
        "http://169.254.169.254/metadata/instance",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://2130706433/",
        "file:///etc/passwd",
        "https://example.com/?card=4111111111111111",
    ):
        assert not approved(policy.handle_permission(req("url", url=bad), {})), bad
    assert all("4111111111111111" not in d for d in policy.denials)


def test_custom_tool_and_other_kinds(policy):
    assert approved(policy.handle_permission(req("custom-tool", tool_name="calculate"), {}))
    assert not approved(policy.handle_permission(req("custom-tool", tool_name="unknown"), {}))
    for kind in ("shell", "mcp", "memory", "hook"):
        assert not approved(policy.handle_permission(req(kind), {}))


def test_readonly_mode_blocks_all_writes(policy, kb):
    policy.allow_write = False
    assert not approved(policy.handle_permission(req("write", file_name=str(kb / "notes" / "a.md")), {}))
    assert not approved(policy.handle_permission(req("custom-tool", tool_name="update_holding"), {}))
    assert approved(policy.handle_permission(req("custom-tool", tool_name="calculate"), {}))


async def test_pre_tool_use_blocks_writes_outside_allowed_dirs(policy, kb, tmp_path):
    session_ws = tmp_path / "base" / "session-state" / "abc" / "secret.md"
    out = await policy.pre_tool_use({"toolName": "create", "toolArgs": {"path": str(session_ws), "file_text": "x"}}, {})
    assert out["permissionDecision"] == "deny"
    ok = await policy.pre_tool_use(
        {"toolName": "create", "toolArgs": {"path": str(kb / "notes" / "a.md"), "file_text": "hi"}}, {}
    )
    assert ok is None


async def test_pre_tool_use_blocks_sensitive_content(policy, kb):
    out = await policy.pre_tool_use(
        {
            "toolName": "edit",
            "toolArgs": {"path": str(kb / "memories" / "m.md"), "old_str": "a", "new_str": "口座番号: 1234567"},
        },
        {},
    )
    assert out["permissionDecision"] == "deny"


async def test_split_edit_cannot_assemble_sensitive_data(policy, kb):
    target = kb / "memories" / "m.md"
    target.write_text("口座番号: XXXX\n", encoding="utf-8")
    # The fragment alone looks harmless, but the resulting file would contain an account number.
    out = await policy.pre_tool_use(
        {"toolName": "edit", "toolArgs": {"path": str(target), "old_str": "XXXX", "new_str": "1234567"}}, {}
    )
    assert out["permissionDecision"] == "deny"


def test_write_permission_checks_full_post_edit_content(policy, kb):
    request = req(
        "write", file_name=str(kb / "memories" / "m.md"), new_file_contents="口座番号: 1234567", diff="+1234567"
    )
    assert not approved(policy.handle_permission(request, {}))
    diff_only = req("write", file_name=str(kb / "memories" / "m.md"), diff="-口座番号: 1234567\n+(削除)")
    assert approved(policy.handle_permission(diff_only, {}))  # removing old sensitive data is allowed


def test_denial_reasons_are_masked(policy):
    policy.handle_permission(req("url", url="https://example.com/?k=SUPERSECRETKEY"), {})
    assert "SUPERSECRETKEY" not in policy.denials[-1]


async def test_pre_tool_use_defaults_search_path_to_knowledge(policy, kb, tmp_path):
    out = await policy.pre_tool_use({"toolName": "grep", "toolArgs": {"pattern": "NISA"}}, {})
    assert out["modifiedArgs"]["path"] == str(kb.resolve())
    out = await policy.pre_tool_use({"toolName": "glob", "toolArgs": {"pattern": "**/*.md", "paths": []}}, {})
    assert out["modifiedArgs"]["paths"] == [str(kb.resolve())]
    out = await policy.pre_tool_use({"toolName": "view", "toolArgs": {"path": "INDEX.md"}}, {})
    assert out["modifiedArgs"]["path"] == str((kb / "INDEX.md").resolve())
    out = await policy.pre_tool_use({"toolName": "view", "toolArgs": {"path": str(tmp_path / "x")}}, {})
    assert out["permissionDecision"] == "deny"
    out = await policy.pre_tool_use({"toolName": "glob", "toolArgs": {"pattern": "*", "paths": [str(tmp_path)]}}, {})
    assert out["permissionDecision"] == "deny"


async def test_pre_tool_use_checks_string_paths_and_both_keys(policy, kb, tmp_path):
    # Observed live: grep/glob may receive ``paths`` as a plain string.
    out = await policy.pre_tool_use({"toolName": "grep", "toolArgs": {"pattern": "x", "paths": str(tmp_path)}}, {})
    assert out["permissionDecision"] == "deny"
    out = await policy.pre_tool_use({"toolName": "grep", "toolArgs": {"pattern": "x", "paths": str(kb)}}, {})
    assert out["modifiedArgs"]["paths"] == str(kb.resolve())
    out = await policy.pre_tool_use(
        {"toolName": "grep", "toolArgs": {"pattern": "x", "paths": [str(kb)], "path": str(tmp_path)}}, {}
    )
    assert out["permissionDecision"] == "deny"
    out = await policy.pre_tool_use({"toolName": "grep", "toolArgs": {"pattern": "x", "paths": 5}}, {})
    assert out["permissionDecision"] == "deny"


async def test_pre_tool_use_rejects_unknown_tools_and_bad_urls(policy):
    assert (await policy.pre_tool_use({"toolName": "powershell", "toolArgs": {}}, {}))["permissionDecision"] == "deny"
    out = await policy.pre_tool_use(
        {"toolName": "web_fetch", "toolArgs": {"url": "https://openapi.rakuten.co.jp/?applicationId=x"}}, {}
    )
    assert out["permissionDecision"] == "deny"
    assert (
        await policy.pre_tool_use({"toolName": "web_fetch", "toolArgs": {"url": "https://www.nta.go.jp/"}}, {}) is None
    )
    assert await policy.pre_tool_use({"toolName": "calculate", "toolArgs": {"expression": "1+1"}}, {}) is None


async def test_pre_tool_use_web_fetch_checks_dns(policy, fake_dns):
    fake_dns["rebind.example.com"] = ["93.184.215.14", "127.0.0.1"]
    fake_dns["missing.example.com"] = []
    for url in ("https://rebind.example.com/", "https://missing.example.com/", "http://127.0.0.1/"):
        out = await policy.pre_tool_use({"toolName": "web_fetch", "toolArgs": {"url": url}}, {})
        assert out["permissionDecision"] == "deny", url
    out = await policy.pre_tool_use({"toolName": "web_fetch", "toolArgs": {"url": "https://news.example.com/"}}, {})
    assert out is None


async def test_post_tool_use_masks_secrets(policy):
    out = await policy.post_tool_use({"toolResult": {"text": "key=SUPERSECRETKEY"}}, {})
    assert out == {"modifiedResult": {"text": "key=***"}}
    assert await policy.post_tool_use({"toolResult": "clean"}, {}) is None


def test_host_matches():
    assert host_matches("www.nta.go.jp", ["go.jp"])
    assert not host_matches("nta-go.jp", ["go.jp"])


def test_system_message_contains_kb_profile_and_rules(kb):
    msg = build_system_message(kb)
    assert kb.resolve().as_posix() in msg
    assert "東京都在住" in msg
    # INDEX.md is model-writable, so its content must not be promoted into the system message.
    assert "# idx" not in msg and "INDEX.md" in msg
    assert "目安" in msg and "個別銘柄" in msg
    assert "report_result" not in msg
    auto = build_system_message(kb, automation=True, allow_write=False)
    assert "report_result" in auto and "読み取り専用" in auto


async def test_write_lock_is_shared_across_sessions_and_reentrant(policy, kb, tmp_path):
    from life_helper.automation.locks import FileLock

    lock_path = tmp_path / "locks" / "knowledge-write.lock"
    policy.write_lock_path = lock_path
    args = {"toolName": "create", "toolArgs": {"path": str(kb / "notes" / "a.md"), "file_text": "x"}}
    assert await policy.pre_tool_use(args, {}) is None
    assert await policy.pre_tool_use(args, {}) is None  # parallel write in the same session: reentrant
    other = FileLock(lock_path, ttl_seconds=60)
    assert not other.try_acquire()  # another session/process is excluded
    await policy.post_tool_use({"toolName": "create", "toolResult": "ok"}, {})
    assert not other.try_acquire()
    await policy.post_tool_use_failure({"toolName": "create"}, {})
    assert other.try_acquire()
    other.release()
    await policy.pre_tool_use(args, {})
    policy.release_all()
    assert other.try_acquire()


def test_connector_filter(ctx, settings):
    from pydantic import SecretStr

    from life_helper.tools.registry import build_tools

    settings.rakuten_application_id = SecretStr("app-id-123456")
    settings.rakuten_access_key = SecretStr("access-key-123456")
    ctx.extras.pop("connectors", None)
    names_all = {s.tool.name for s in build_tools(ctx)}
    assert {"search_rakuten_vacancy", "get_stock_price"} <= names_all
    names_none = {s.tool.name for s in build_tools(ctx, connectors=[])}
    assert "search_rakuten_vacancy" not in names_none and "get_stock_price" not in names_none
    assert "calculate" in names_none
    names_rakuten = {s.tool.name for s in build_tools(ctx, connectors=["rakuten_travel"])}
    assert "search_rakuten_vacancy" in names_rakuten and "get_stock_price" not in names_rakuten
