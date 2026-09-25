from __future__ import annotations

import json

import pytest
from copilot import ToolInvocation

from life_helper.automation.locks import FileLock
from life_helper.copilot_integration import knowledge_tools
from life_helper.copilot_integration.knowledge_tools import build_knowledge_tools, save_knowledge_file, unified_diff
from life_helper.copilot_integration.policy import Approval, ToolPolicy, WriteScope
from life_helper.security import SecretMasker


@pytest.fixture
def kb(tmp_path):
    root = tmp_path / "knowledge"
    for d in ("memories", "notes", "plans", "profile", "docs", "money"):
        (root / d).mkdir(parents=True)
    (root / "INDEX.md").write_text("# idx\n", encoding="utf-8")
    (root / "profile" / "about-me.md").write_text("東京都在住\n", encoding="utf-8")
    return root


class Recorder:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str | None]] = []
        self.asked: list[tuple[str, str]] = []

    def on_write(self, path: str, diff: str, approval_id: str | None) -> None:
        self.writes.append((path, diff, approval_id))


@pytest.fixture
def rec():
    return Recorder()


def make_policy(kb, tmp_path, rec: Recorder, *, require_approval: bool = False, allow_write: bool = True):
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    policy = ToolPolicy(
        knowledge_root=kb,
        skills_root=skills,
        fetch_domains=["go.jp"],
        masker=SecretMasker(["SUPERSECRETKEY"]),
        allow_write=allow_write,
        require_approval=require_approval,
        write_lock_path=tmp_path / "locks" / "knowledge-write.lock",
    )
    policy.write_scope = WriteScope(on_write=rec.on_write)
    return policy


def approver(rec: Recorder, answer: Approval):
    async def ask(path: str, diff: str) -> Approval:
        rec.asked.append((path, diff))
        return answer

    return ask


def use(policy: ToolPolicy, rec: Recorder, ask, is_active=lambda: True) -> None:
    policy.write_scope = WriteScope(approver=ask, on_write=rec.on_write, is_active=is_active)


def content(path) -> str:
    return path.read_text(encoding="utf-8")


async def test_write_and_edit_without_approval_for_automations(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec)
    out = await save_knowledge_file(policy, "memories/money.md", lambda before: "- 2026-09-25: NISA は月 3 万円\n")
    assert out == {"saved": True, "path": "memories/money.md", "message": "保存しました"}
    assert content(kb / "memories" / "money.md") == "- 2026-09-25: NISA は月 3 万円\n"
    path, diff, approval_id = rec.writes[0]
    assert path == "memories/money.md" and approval_id is None
    assert diff.startswith("--- /dev/null\n+++ b/memories/money.md") and "+- 2026-09-25: NISA は月 3 万円" in diff

    # Absolute paths inside the knowledge base work the same way.
    out = await save_knowledge_file(policy, str(kb / "INDEX.md"), lambda before: before + "- memories/money.md\n")
    assert out["saved"] and content(kb / "INDEX.md") == "# idx\n- memories/money.md\n"
    assert "--- a/INDEX.md" in rec.writes[1][1]


def test_apply_edit():
    from life_helper.copilot_integration.knowledge_tools import EditError, apply_edit

    assert apply_edit("a\nb\n", "b", "c") == "a\nc\n"
    assert apply_edit("a", "", "b") == "a\nb\n"  # append on a new line
    assert apply_edit("a\n", "", "b\n") == "a\nb\n"
    assert apply_edit("", "", "b") == "b\n"
    with pytest.raises(EditError, match="write_knowledge_file"):
        apply_edit(None, "", "b")
    with pytest.raises(EditError, match="見つかりません"):
        apply_edit("a\n", "x", "y")
    with pytest.raises(EditError, match="2 か所"):
        apply_edit("x\nx\n", "x", "y")


async def test_rejects_locations_outside_the_writable_areas(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec)
    outside = tmp_path / "outside.md"
    for bad in (
        "profile/about-me.md",
        "docs/x.md",
        "money/portfolio.yaml",
        "x.md",
        "notes/run.py",
        "memories/../profile/about-me.md",
        str(outside),
        str(tmp_path / "base" / "session-state" / "abc" / "plan.md"),
    ):
        out = await save_knowledge_file(policy, bad, lambda before: "x")
        assert out["saved"] is False and "memories/" in out["error"], bad
    assert content(kb / "profile" / "about-me.md") == "東京都在住\n" and not outside.exists()
    assert rec.writes == []


async def test_readonly_automation_cannot_write(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, allow_write=False)
    out = await save_knowledge_file(policy, "memories/a.md", lambda before: "x")
    assert out["saved"] is False and "読み取り専用" in out["error"]
    assert not (kb / "memories" / "a.md").exists()


async def test_rejects_sensitive_data_and_known_secrets(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec)
    out = await save_knowledge_file(policy, "memories/m.md", lambda before: "口座番号: 1234567")
    assert out["saved"] is False and "口座番号" in out["error"]
    # A split edit whose fragment looks harmless is still checked on the resulting file.
    target = kb / "memories" / "m.md"
    target.write_text("口座番号: XXXX\n", encoding="utf-8")
    tools = {spec.tool.name: spec.tool for spec in build_knowledge_tools(policy)}
    result = await tools["edit_knowledge_file"].handler(
        ToolInvocation(arguments={"path": str(target), "old_str": "XXXX", "new_str": "1234567"})
    )
    assert json.loads(result.text_result_for_llm)["saved"] is False
    assert content(target) == "口座番号: XXXX\n"
    # Configured secrets (API keys, tokens) are rejected even though the pattern check does not know them.
    out = await save_knowledge_file(policy, "notes/n.md", lambda before: "key=SUPERSECRETKEY")
    assert out["saved"] is False and "秘密情報" in out["error"]
    assert rec.writes == []


async def test_size_limit_and_no_change(kb, tmp_path, rec, monkeypatch):
    policy = make_policy(kb, tmp_path, rec)
    monkeypatch.setattr(knowledge_tools, "MAX_FILE_CHARS", 10)
    out = await save_knowledge_file(policy, "notes/big.md", lambda before: "x" * 11)
    assert out["saved"] is False and not (kb / "notes" / "big.md").exists()
    out = await save_knowledge_file(policy, "INDEX.md", lambda before: before)
    assert out["saved"] is False and "変更しませんでした" in out["message"] and rec.writes == []


async def test_chat_write_requires_approval(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    # No approver attached: refused (fail closed).
    out = await save_knowledge_file(policy, "memories/a.md", lambda before: "fact\n")
    assert out["saved"] is False and not (kb / "memories" / "a.md").exists()

    use(policy, rec, approver(rec, Approval(False, "利用者が書き込みを却下しました", "id1")))
    out = await save_knowledge_file(policy, "memories/a.md", lambda before: "fact\n")
    assert out == {"saved": False, "error": "利用者が書き込みを却下しました"}
    assert not (kb / "memories" / "a.md").exists() and rec.writes == []

    use(policy, rec, approver(rec, Approval(True, approval_id="id2")))
    long_text = "".join(f"- line {i}\n" for i in range(800))
    out = await save_knowledge_file(policy, "memories/a.md", lambda before: long_text)
    assert out["saved"] is True and content(kb / "memories" / "a.md") == long_text
    # The user sees the complete diff (not a truncated preview) before anything is written.
    asked_path, asked_diff = rec.asked[-1]
    assert asked_path == "memories/a.md" and asked_diff.count("\n+- line ") == 800
    assert rec.writes == [("memories/a.md", asked_diff, "id2")]


async def test_write_tools_parse_arguments(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    use(policy, rec, approver(rec, Approval(True, approval_id="a")))
    tools = {spec.tool.name: spec for spec in build_knowledge_tools(policy)}
    assert all(spec.writes for spec in tools.values())
    result = await tools["write_knowledge_file"].tool.handler(
        ToolInvocation(arguments={"path": "notes/a.md", "content": "hello\n"})
    )
    assert json.loads(result.text_result_for_llm)["saved"] is True
    result = await tools["edit_knowledge_file"].tool.handler(
        ToolInvocation(arguments={"path": "notes/a.md", "new_str": "world"})
    )
    assert json.loads(result.text_result_for_llm)["saved"] is True
    assert content(kb / "notes" / "a.md") == "hello\nworld\n"
    result = await tools["write_knowledge_file"].tool.handler(ToolInvocation(arguments={"path": "notes/a.md"}))
    assert result.result_type == "failure"  # missing content


async def test_file_changed_while_waiting_for_approval_is_not_overwritten(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    target = kb / "memories" / "a.md"
    target.write_text("old\n", encoding="utf-8")

    async def edit_then_approve(path, diff):
        target.write_text("edited in the UI\n", encoding="utf-8")
        return Approval(True, approval_id="x")

    use(policy, rec, edit_then_approve)
    out = await save_knowledge_file(policy, "memories/a.md", lambda before: "new\n")
    assert out["saved"] is False and "変更された" in out["error"]
    assert content(target) == "edited in the UI\n" and rec.writes == []


async def test_lock_is_not_held_while_waiting_and_is_respected_when_writing(kb, tmp_path, rec, monkeypatch):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    other = FileLock(policy.write_lock_path, ttl_seconds=60)

    async def check_lock_free(path, diff):
        # Other writers (UI edits, automations) are not blocked while the user is deciding.
        assert other.try_acquire()
        other.release()
        return Approval(True, approval_id="x")

    use(policy, rec, check_lock_free)
    assert (await save_knowledge_file(policy, "notes/a.md", lambda before: "a\n"))["saved"]
    assert other.try_acquire()  # released after the write

    use(policy, rec, approver(rec, Approval(True, approval_id="y")))
    monkeypatch.setattr(knowledge_tools, "WRITE_LOCK_WAIT_SECONDS", 0.5)
    out = await save_knowledge_file(policy, "notes/b.md", lambda before: "b\n")
    assert out["saved"] is False and "更新中" in out["error"]
    assert not (kb / "notes" / "b.md").exists()
    other.release()


def test_diff_shows_line_ending_changes():
    added = unified_diff("a.md", "a", "a\n")
    removed = unified_diff("a.md", "a\n", "a")
    assert added and removed
    assert "\\ No newline at end of file" in added and "\\ No newline at end of file" in removed
    assert unified_diff("a.md", None, "x\n").splitlines() == ["--- /dev/null", "+++ b/a.md", "@@ -0,0 +1 @@", "+x"]


async def test_trailing_newline_change_is_shown_before_approval(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    use(policy, rec, approver(rec, Approval(True, approval_id="n")))
    (kb / "notes" / "a.md").write_text("a", encoding="utf-8")
    out = await save_knowledge_file(policy, "notes/a.md", lambda before: "a\n")
    assert out["saved"] is True and rec.asked[-1][1].strip()


async def test_removed_sensitive_values_are_not_shown_in_the_card(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    use(policy, rec, approver(rec, Approval(True, approval_id="r")))
    target = kb / "memories" / "m.md"
    target.write_text("口座番号: 1234567\nkey=SUPERSECRETKEY\n", encoding="utf-8")
    out = await save_knowledge_file(policy, "memories/m.md", lambda before: "（削除しました）\n")
    assert out["saved"] is True and content(target) == "（削除しました）\n"
    shown = rec.asked[-1][1]
    assert "1234567" not in shown and "SUPERSECRETKEY" not in shown and "-" in shown
    assert "1234567" not in rec.writes[-1][1]


async def test_scope_is_taken_when_the_call_starts(kb, tmp_path, rec, monkeypatch):
    import asyncio
    import time

    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    first, second = Recorder(), Recorder()
    use(policy, first, approver(first, Approval(True, approval_id="old")))
    real_read = knowledge_tools._read

    def slow_read(path):
        time.sleep(0.3)
        return real_read(path)

    monkeypatch.setattr(knowledge_tools, "_read", slow_read)
    task = asyncio.create_task(save_knowledge_file(policy, "notes/a.md", lambda before: "a\n"))
    await asyncio.sleep(0.05)
    use(policy, second, approver(second, Approval(True, approval_id="new")))  # the next turn starts meanwhile
    assert (await task)["saved"] is True
    assert len(first.asked) == 1 and first.writes[0][2] == "old"
    assert second.asked == [] and second.writes == []


async def test_write_is_refused_when_the_turn_ends_after_approval(kb, tmp_path, rec):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    active = {"value": True}

    async def approve_then_abort(path, diff):
        active["value"] = False  # e.g. the user pressed 中断 right after approving
        return Approval(True, approval_id="z")

    use(policy, rec, approve_then_abort, is_active=lambda: active["value"])
    out = await save_knowledge_file(policy, "notes/a.md", lambda before: "a\n")
    assert out["saved"] is False and "中断" in out["error"]
    assert not (kb / "notes" / "a.md").exists() and rec.writes == []


async def test_write_is_refused_when_the_turn_ends_during_the_final_check(kb, tmp_path, rec, monkeypatch):
    policy = make_policy(kb, tmp_path, rec, require_approval=True)
    active = {"value": True}
    reads = {"count": 0}
    real_read = knowledge_tools._read

    def read_then_abort(path):
        reads["count"] += 1
        if reads["count"] == 2:
            active["value"] = False  # the answer is stopped while the file is re-read after approval
        return real_read(path)

    monkeypatch.setattr(knowledge_tools, "_read", read_then_abort)
    use(policy, rec, approver(rec, Approval(True, approval_id="w")), is_active=lambda: active["value"])
    out = await save_knowledge_file(policy, "notes/a.md", lambda before: "a\n")
    assert reads["count"] == 2 and out["saved"] is False and "中断" in out["error"]
    assert not (kb / "notes" / "a.md").exists() and rec.writes == []
