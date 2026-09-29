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
    p = ToolPolicy(
        knowledge_root=kb,
        skills_root=skills,
        masker=SecretMasker(["SUPERSECRETKEY"]),
        custom_tools={"calculate", "update_holding"},
        write_custom_tools={"update_holding"},
    )
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


def test_runtime_write_requests_are_always_denied(policy, kb):
    # Built-in writers are not exposed; even allowed locations must go through the app's knowledge tools.
    for target in (kb / "memories" / "money.md", kb / "INDEX.md", kb / "profile" / "about-me.md"):
        decision = policy.handle_permission(req("write", file_name=str(target)), {})
        assert isinstance(decision, PermissionDecisionReject), target
        assert "write_knowledge_file" in decision.feedback


def test_writable_locations(policy, kb, tmp_path):
    assert policy.writable(str(kb / "memories" / "money.md"))
    assert policy.writable("notes/sub/a.txt") and policy.writable("INDEX.md")
    for bad in (
        kb / "profile" / "about-me.md",
        kb / "docs" / "x.md",
        kb / "money" / "portfolio.yaml",
        kb / "x.md",
        kb / "notes" / "run.py",
        tmp_path / "memories" / "x.md",
    ):
        assert not policy.writable(str(bad)), bad
    assert not policy.writable("memories/../profile/about-me.md")
    assert not policy.writable("") and not policy.writable("memories/a\x00.md")
    # Control/formatting characters would forge the path and the diff headers shown on the approval card.
    assert policy.writable("memories/家計.md")
    for hidden in ("\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\u2028", "\u2029", "\u202e", "\u200b"):
        assert not policy.writable(f"notes/safe{hidden}+++ b-forged.md"), repr(hidden)
    policy.allow_write = False
    assert not policy.writable("memories/money.md")


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


async def test_pre_tool_use_blocks_builtin_writers(policy, kb):
    # GPT-family models get apply_patch, others create/edit: none of them may run.
    for tool, args in (
        ("create", {"path": str(kb / "notes" / "a.md"), "file_text": "hi"}),
        ("edit", {"path": str(kb / "memories" / "m.md"), "old_str": "a", "new_str": "b"}),
        ("apply_patch", "*** Begin Patch\n*** Add File: notes/a.md\n+hi\n*** End Patch"),
        ("str_replace_editor", {"command": "create", "path": str(kb / "notes" / "a.md")}),
        ("powershell", {}),
    ):
        out = await policy.pre_tool_use({"toolName": tool, "toolArgs": args}, {})
        assert out["permissionDecision"] == "deny", tool


def test_available_builtins_exclude_writers():
    from life_helper.copilot_integration.manager import available_toolset
    from life_helper.copilot_integration.policy import ALLOWED_BUILTINS

    assert not {"create", "edit", "apply_patch", "str_replace_editor", "bash", "powershell"} & set(ALLOWED_BUILTINS)
    chat = [f"builtin:{t}" for t in ALLOWED_BUILTINS] + ["custom:*"]
    assert available_toolset(True, True).to_list() == chat
    # Without the research agent enabled, task is not offered; skill needs skills present.
    assert available_toolset(True).to_list() == [t for t in chat if t != "builtin:task"]
    assert available_toolset(False).to_list() == [t for t in chat if t not in ("builtin:task", "builtin:skill")]


async def test_task_tool_only_starts_the_research_agent(policy):
    from life_helper.copilot_integration.agents import MAX_TASKS_PER_ANSWER, RESEARCH_AGENT

    async def task(**args):
        return await policy.pre_tool_use({"toolName": "task", "toolArgs": args}, {})

    # A session that was not given the research agent (allow_subagents=False, the default) cannot start sub-agents.
    assert (await task(agent_type=RESEARCH_AGENT, prompt="x"))["permissionDecision"] == "deny"
    policy.allow_subagents = True

    assert await task(agent_type=RESEARCH_AGENT, prompt="ふるさと納税の上限") is None
    assert policy.tasks_started == 1
    # The runtime's own agents, a missing agent and disagreeing aliases are all refused.
    for args in (
        {"prompt": "x"},
        {"agent_type": "general-purpose", "prompt": "x"},
        {"subagent_type": "explore", "prompt": "x"},
        {"agent_type": RESEARCH_AGENT, "agentType": "general-purpose", "prompt": "x"},
    ):
        assert (await task(**args))["permissionDecision"] == "deny", args
    # A background agent would outlive the answer (中断 and timeouts included); anything but sync is refused.
    for args in (
        {"mode": "background"},
        {"mode": "fleet"},
        {"mode": 0},  # not a string: the runtime would read something we did not check
        {"mode": ""},
        {"background": True},
        {"detach": True},
        {"run_in_background": True},
        {"agentMode": "background"},  # a mode under a name we do not know
        {"agentMode": "fleet"},  # any non-sync value under an alias is refused, not just "background"
        {"agentMode": 0},
        {"agentMode": ""},
        {"mode": "sync", "agentMode": "fleet"},  # every mode-like key must agree on sync
    ):
        out = await task(agent_type=RESEARCH_AGENT, prompt="x", **args)
        assert out["permissionDecision"] == "deny", args
    assert await task(agentType=RESEARCH_AGENT, prompt="x", mode="sync") is None
    # The sub-agent follows the session's model: overrides are dropped instead of refusing the call.
    out = await task(agent_type=RESEARCH_AGENT, prompt="x", model="gpt-5", reasoningEffort="high")
    assert out["modifiedArgs"] == {"agent_type": RESEARCH_AGENT, "prompt": "x"}
    assert policy.tasks_started == 3

    policy.tasks_started = MAX_TASKS_PER_ANSWER
    assert (await task(agent_type=RESEARCH_AGENT, prompt="x"))["permissionDecision"] == "deny"
    # The cap counts per answer: the next answer may delegate again.
    policy.begin_answer()
    assert await task(agent_type=RESEARCH_AGENT, prompt="x") is None


async def test_sub_agent_calls_are_held_to_the_research_tools(policy, kb):
    """When the runtime tells the hook that a sub-agent is calling, it keeps only its read-only tools."""
    from life_helper.copilot_integration.agents import RESEARCH_AGENT_TOOLS

    policy.allow_subagents = True
    ctx = {"session_id": "s1"}
    calls = [
        ("task", {"agent_type": "researcher", "prompt": "x"}),  # no sub-agents of its own
        ("update_holding", {}),  # no custom tools, writing ones least of all
        ("calculate", {}),
        ("browser_open", {"url": "https://example.com/"}),
        ("write_knowledge_file", {"path": "memories/a.md"}),
    ]
    # The runtime marks such a call with the sub-agent's id, or delivers it under the sub-agent's own session id.
    for sub in ({"agentId": "a1"}, {"sessionId": "s1.sub"}):
        for tool, args in calls:
            out = await policy.pre_tool_use(sub | {"toolName": tool, "toolArgs": args}, ctx)
            assert out["permissionDecision"] == "deny", (sub, tool)
    assert "browser_open" in policy.denials[-2]
    # Its own tools are still checked as usual: reads stay inside the knowledge base.
    out = await policy.pre_tool_use({"agentId": "a1", "toolName": "view", "toolArgs": {"path": "INDEX.md"}}, ctx)
    assert out["modifiedArgs"]["path"] == str((kb / "INDEX.md").resolve())
    assert set(RESEARCH_AGENT_TOOLS) == {"view", "grep", "rg", "glob", "web_fetch"}
    # The session's own calls are not mistaken for a sub-agent's: same session id, no agent id.
    assert await policy.pre_tool_use({"sessionId": "s1", "toolName": "calculate", "toolArgs": {}}, ctx) is None


async def test_request_approval_fails_closed(policy):
    from life_helper.copilot_integration.policy import Approval, WriteScope

    assert not (await policy.request_approval(None, "memories/a.md", "+x")).approved  # no scope attached
    assert not (await policy.request_approval(WriteScope(), "memories/a.md", "+x")).approved  # no approver

    async def boom(path, diff):
        raise RuntimeError("ui gone")

    assert not (await policy.request_approval(WriteScope(approver=boom), "memories/a.md", "+x")).approved

    async def not_an_approval(path, diff):
        return True

    assert not (await policy.request_approval(WriteScope(approver=not_an_approval), "memories/a.md", "+x")).approved

    async def yes(path, diff):
        return Approval(True, approval_id="a1")

    assert (await policy.request_approval(WriteScope(approver=yes), "memories/a.md", "+x")) == Approval(
        True, approval_id="a1"
    )
    # A scope whose turn has finished cannot ask anymore.
    finished = WriteScope(approver=yes, is_active=lambda: False)
    assert not (await policy.request_approval(finished, "memories/a.md", "+x")).approved


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
    # GPT-family models search with rg, which only takes ``paths``.
    out = await policy.pre_tool_use({"toolName": "rg", "toolArgs": {"pattern": "NISA"}}, {})
    assert out["modifiedArgs"] == {"pattern": "NISA", "paths": str(kb.resolve())}
    out = await policy.pre_tool_use({"toolName": "rg", "toolArgs": {"pattern": "x", "paths": "memories"}}, {})
    assert out["modifiedArgs"]["paths"] == str((kb / "memories").resolve())
    out = await policy.pre_tool_use({"toolName": "rg", "toolArgs": {"pattern": "x", "paths": str(tmp_path)}}, {})
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
    # The browser tools answer with JSON text, so a secret in a string result is masked as well.
    text = await policy.post_tool_use({"toolResult": '{"text": "key=SUPERSECRETKEY"}'}, {})
    assert text == {"modifiedResult": '{"text": "key=***"}'}
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
    # web_fetch は先頭で切れることがあるので、続きの読み方を伝える（issue #65）。
    assert "max_length" in msg and "start_index" in msg
    assert "write_knowledge_file" in msg and "edit_knowledge_file" in msg
    assert "report_result" not in msg and "承認" not in msg
    chat = build_system_message(kb, approval=True)
    assert "承認" in chat and "繰り返さない" in chat
    # A message sent while the chat answers must not replace the original request (issue #46).
    assert "両方に対応" in chat
    auto = build_system_message(kb, automation=True, allow_write=False, approval=True)
    assert "report_result" in auto and "読み取り専用" in auto
    assert "両方に対応" not in auto
    # The result is read later in the run history, so the rules say how to write it (issue #41).
    assert "実行履歴" in auto and "Markdown" in auto and "表" in auto
    assert "承認ボタン" not in auto  # unattended runs never wait for a user
    # Batching independent look-ups into one answer helps everywhere; the sub-agent rules are added only when the
    # session is given the research agent (issue #56).
    assert "まとめて出して" in msg and "まとめて出して" in auto
    assert "task" not in msg and "task" not in auto
    research = build_system_message(kb, subagents=True)
    assert "task" in research and "researcher" in research and "最大 6 件" in research
    # Automations may also run the research sub-agent, so its rules appear when subagents is on.
    auto_research = build_system_message(kb, automation=True, allow_write=False, subagents=True)
    assert "task" in auto_research and "researcher" in auto_research and "report_result" in auto_research


def test_research_agent_is_read_only(kb):
    from life_helper.copilot_integration.agents import RESEARCH_AGENT_TOOLS, build_research_agent

    agent = build_research_agent(kb.resolve().as_posix())
    assert agent["name"] == "researcher"
    # No writers, no sub-agents of its own, no browser and no connector tools.
    assert agent["tools"] == list(RESEARCH_AGENT_TOOLS)
    assert not {"task", "create", "edit", "apply_patch", "write_knowledge_file"} & set(agent["tools"])
    # Not auto-selected from the prompt: an inferred start would skip the per-answer cap, so only explicit task calls
    # (which go through pre_tool_use) may start it.
    assert agent["infer"] is False
    assert kb.resolve().as_posix() in agent["prompt"]
    assert "外部の信頼できないデータ" in agent["prompt"]
    # Its only way to the internet is web_fetch, so it is told how to read past a cut-off result (issue #65).
    assert "max_length" in agent["prompt"] and "start_index" in agent["prompt"]


def test_chat_sessions_require_approval_but_automations_do_not(ctx):
    from life_helper.copilot_integration.manager import CopilotManager

    chat_specs, chat_policy = CopilotManager(ctx, ctx.settings.copilot_chat_dir).build_session_tools(allow_write=True)
    assert chat_policy.require_approval
    names = {s.tool.name for s in chat_specs}
    assert {"write_knowledge_file", "edit_knowledge_file"} <= names
    assert {"write_knowledge_file", "edit_knowledge_file"} <= chat_policy.write_custom_tools
    assert {"write_knowledge_file", "edit_knowledge_file"} <= chat_policy.custom_tools
    _, auto_policy = CopilotManager(ctx, ctx.settings.copilot_automation_dir, automation=True).build_session_tools(
        allow_write=False
    )
    assert not auto_policy.require_approval and not auto_policy.allow_write
    # Chat and automation alike may run the read-only research sub-agent (``task``).
    assert chat_policy.allow_subagents and auto_policy.allow_subagents


def test_chat_and_automation_sessions_get_the_research_agent(ctx):
    from life_helper.copilot_integration.manager import CopilotManager

    def options(manager, allow_write):
        specs, policy = manager.build_session_tools(allow_write=allow_write)
        return manager.session_options(model="auto", policy=policy, specs=specs, allow_write=allow_write)

    chat = options(CopilotManager(ctx, ctx.settings.copilot_chat_dir), True)
    assert [a["name"] for a in chat["custom_agents"]] == ["researcher"]
    # The sub-agents' own tokens stay out of the answer; their tool calls still arrive.
    assert chat["include_sub_agent_streaming_events"] is False
    assert "builtin:task" in chat["available_tools"].to_list()

    # Automations run the same read-only research agent; the runner waits for its own agent's idle.
    auto = options(CopilotManager(ctx, ctx.settings.copilot_automation_dir, automation=True), False)
    assert [a["name"] for a in auto["custom_agents"]] == ["researcher"]
    assert auto["include_sub_agent_streaming_events"] is False
    assert "builtin:task" in auto["available_tools"].to_list()


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
    names_rakuten = {s.tool.name for s in build_tools(ctx, connectors=["rakuten"])}
    assert {"search_rakuten_vacancy", "search_rakuten_items", "get_rakuten_recipe_ranking"} <= names_rakuten
    assert "get_stock_price" not in names_rakuten
