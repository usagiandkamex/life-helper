"""Runs one automation as an unattended Copilot session and decides whether to notify."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from copilot import define_tool
from pydantic import BaseModel, Field

from ..connectors.registry import get_connectors
from ..copilot_integration.events import map_event, send_and_wait_own
from ..copilot_integration.manager import CopilotManager, NoTokenError
from ..security import redact_sensitive
from ..tools.registry import ToolSpec
from .locks import FileLock
from .models import Automation, expand_prompt
from .notify import GitHubNotifier, NotifyError
from .store import AutomationStore

if TYPE_CHECKING:
    from ..context import AppContext

logger = logging.getLogger(__name__)
KEPT_EVENT_TYPES = ("message", "tool_start", "tool_end", "file_write", "error")
MAX_EVENTS = 200
TRANSCRIPT_VERSION = 1
REPORT_REMINDER = (
    "\n\n（最後に必ず report_result ツールを呼び、結果の本文（summary）と、利用者に通知すべきかを報告してください。"
    "summary はアプリの「実行履歴」に Markdown で表示されます。上の指示で形式（表など）が指定されていればそのとおりに、"
    "指定がなければ表や箇条書きで、あとから読んでも分かるようにまとめてください。）"
)
REPORT_FOLLOW_UP = (
    "report_result ツールを呼んで、今回の結果の本文（summary。指示どおりの形式の Markdown）と、"
    "利用者に通知すべきか（notify）を報告してください。"
)
REAUTH_MESSAGE = "GitHub への再ログインが必要です。アプリを開いてログインし直してください。"


class ReportParams(BaseModel):
    summary: str = Field(
        max_length=4000,
        description=(
            "利用者が「実行履歴」で読む結果の本文（Markdown、4000 文字以内）。"
            "指示で表などの形式が指定されていればその形式で、完成した結果を書く。"
        ),
    )
    notify: bool = Field(description="利用者に知らせるべき結果か（例: 条件を満たした、要確認の事項がある）")


@dataclass
class RunContext:
    report: dict | None = None
    signals: dict[str, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    requests: int = 0

    def capture_tool_result(self, tool_name: str, result: Any) -> None:
        data = result
        if isinstance(result, dict) and "textResultForLlm" in result:
            data = result.get("textResultForLlm")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return
        if isinstance(data, dict) and isinstance(data.get("signal"), dict):
            for key, value in data["signal"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.signals[key] = float(value)


def trim_events(events: list[dict], limit: int = MAX_EVENTS) -> tuple[list[dict], int]:
    """Keeps the last ``limit`` events and returns how many were dropped. A dropped follow-up marker is kept, so the
    answers after it are never read as part of the first answer."""
    if len(events) <= limit:
        return list(events), 0
    dropped, kept = events[:-limit], events[-limit:]
    markers = [e for e in dropped if e.get("type") == "follow_up"][-limit:]
    trimmed = markers + kept[len(markers) :]
    return trimmed, len(events) - len(trimmed)


def build_notifier(ctx: AppContext) -> GitHubNotifier:
    s = ctx.settings
    return GitHubNotifier(
        app_id=s.github_app_id,
        private_key=s.github_app_private_key.get_secret_value(),
        installation_id=s.github_app_installation_id,
        repo=s.notify_repo,
        mention=s.notify_mention,
        masker=ctx.masker,
        transport=ctx.extras.get("http_transport"),
    )


class AutomationRunner:
    def __init__(self, ctx: AppContext, store: AutomationStore, manager: CopilotManager | None = None) -> None:
        self.ctx = ctx
        self.store = store
        self.manager = manager or CopilotManager(ctx, ctx.settings.copilot_automation_dir, automation=True)
        self.notifier = build_notifier(ctx)
        self._token_checked_at: float | None = None

    def _lock(self, automation_id: str) -> FileLock:
        return FileLock(
            self.store.locks_dir / f"automation-{automation_id}.lock", self.ctx.settings.automation_lock_ttl_seconds
        )

    async def run_due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        results = []
        for automation in self.store.list():
            if automation.enabled and not automation.state.next_run_at:
                self.store.update_state(automation.id, next_run_at=automation.schedule.next_after(now).isoformat())
                continue
            if automation.is_due(now):
                results.append(await self.run(automation.id, now=now, scheduled=True))
        return results

    async def run(self, automation_id: str, *, now: datetime | None = None, scheduled: bool = False) -> dict:
        now = now or datetime.now(UTC)
        lock = self._lock(automation_id)
        if not lock.try_acquire():
            return {"automation_id": automation_id, "status": "skipped_locked"}
        try:
            # Re-read after taking the lock: an overlapping job may have just run it.
            automation = self.store.get(automation_id)
            if automation is None:
                return {"automation_id": automation_id, "status": "not_found"}
            if scheduled and not automation.is_due(now):
                return {"automation_id": automation_id, "status": "skipped_not_due"}
            # Schedule the next run first so a crash cannot cause a tight retry loop.
            self.store.update_state(automation.id, next_run_at=automation.schedule.next_after(now).isoformat())
            return await self._execute(automation, now)
        finally:
            lock.release()

    async def _execute(self, automation: Automation, now: datetime) -> dict:
        run_id = uuid.uuid4().hex[:16]
        record: dict[str, Any] = {
            "id": run_id,
            "automation_id": automation.id,
            "name": automation.name,
            # Due automations run one after another with the same scheduled ``now``, so the record keeps the time this
            # run really started; the history shows how long it took (finished_at - started_at).
            "started_at": datetime.now(UTC).isoformat(),
            "read": False,
            "notified": False,
            # Records carrying these fields are replayed as a conversation in the chat (older ones are not).
            "transcript_version": TRANSCRIPT_VERSION,
            "conversation_mode": automation.conversation_mode,
            "prompt": self._sanitize(expand_prompt(automation.prompt, now)),
            # Overwritten when Copilot runs; kept for runs that stop before it, so every such record has one shape.
            "error": None,
            "final_message": "",
            "report": None,
            "signals": {},
            "events": [],
            "events_omitted": 0,
            "attempts": 0,
            "requests": 0,
        }
        missing = [
            c
            for c in automation.connectors
            if not get_connectors(self.ctx).get(c) or not get_connectors(self.ctx)[c].configured
        ]
        if missing:
            record |= {
                "status": "error",
                "summary": f"使えないコネクタがあります（API キーが未登録か、機能が無効）: {', '.join(missing)}",
            }
            await self._notify_failure(automation, record)
            return self._finish(automation, record, condition_met=None)
        if not await self._token_valid():
            record |= {"status": "reauth", "error": REAUTH_MESSAGE, "summary": REAUTH_MESSAGE}
            await self._notify_reauth(now)
            return self._finish(automation, record, condition_met=None)
        limit = self.ctx.settings.automation_monthly_run_limit
        if not self.store.try_reserve_run(limit, now):
            record |= {
                "status": "skipped_limit",
                "summary": f"今月の実行回数の上限（{limit} 回）に達したため実行しませんでした。",
            }
            return self._finish(automation, record, condition_met=None)

        # One deadline for the whole run, retry included, so a run never outlives its lock.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + automation.max_runtime_minutes * 60
        run_ctx = RunContext()
        status, error = "error", None
        attempts = 0
        for attempt in range(2):
            run_ctx = RunContext()
            attempts = attempt + 1
            try:
                await self._run_session(automation, run_ctx, f"{run_id}-{attempt}", now, deadline)
                status, error = "success", None
                break
            except NoTokenError:
                status, error = "reauth", REAUTH_MESSAGE
                await self._notify_reauth(now)
                break
            except TimeoutError:
                status, error = "timeout", f"{automation.max_runtime_minutes} 分以内に終わらなかったため中断しました。"
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("automation %s failed (attempt %d): %s", automation.id, attempt + 1, type(exc).__name__)
                status, error = "error", self.ctx.masker.mask_text(str(exc)) or "エラーが発生しました"
                side_effects = any(e["type"] in ("tool_start", "file_write") for e in run_ctx.events)
                # Retry only failures that happened before any tool ran, so writes are never repeated.
                if attempt == 0 and not side_effects and deadline - loop.time() > 120:
                    await asyncio.sleep(5)
                    continue
                break

        final_message = next((e["content"] for e in reversed(run_ctx.events) if e["type"] == "message"), "")
        summary = (run_ctx.report or {}).get("summary") or final_message[:2000] or error or ""
        events, omitted = trim_events(run_ctx.events)
        record |= self._sanitize(
            {
                "status": status,
                "error": error,
                "summary": summary,
                "final_message": final_message,
                "report": run_ctx.report,
                "signals": run_ctx.signals,
                "events": events,
                "events_omitted": omitted,
                "attempts": attempts,
                "requests": run_ctx.requests,
            }
        )
        condition_met = None
        if status == "success":
            condition_met = self._condition_met(automation, run_ctx)
            if automation.notify.github and self._should_notify(automation, condition_met):
                await self._send(automation, record, headline="条件を満たしました" if condition_met else "実行結果")
                if not record["notified"] and automation.notify.only_on_change:
                    # Keep the previous baseline so the missed notification is retried on the next run.
                    condition_met = None
        elif status != "reauth":
            await self._notify_failure(automation, record)
        return self._finish(automation, record, condition_met=condition_met)

    def _sanitize(self, value: Any) -> Any:
        """Masks secrets and removes sensitive values from anything written to the run history."""
        if isinstance(value, str):
            return redact_sensitive(self.ctx.masker.mask_text(value))[0]
        if isinstance(value, dict):
            return {k: self._sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize(v) for v in value]
        return value

    async def _token_valid(self) -> bool:
        """Detects revoked/expired tokens up front so they follow the re-login path instead of a generic failure."""
        token = self.ctx.github_token()
        if not token:
            return False
        if self._token_checked_at and time.monotonic() - self._token_checked_at < 600:
            return True
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.ctx.extras.get("http_transport")) as client:
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                )
        except httpx.HTTPError:
            return True  # network trouble is not proof of a bad token; the run itself will report errors
        if resp.status_code == 401:
            return False
        self._token_checked_at = time.monotonic()
        return True

    async def _run_session(
        self, automation: Automation, run_ctx: RunContext, attempt_id: str, now: datetime, deadline: float
    ) -> None:
        @define_tool(
            name="report_result",
            description="オートメーションの最後に必ず呼び、利用者が読む結果の本文（Markdown）と通知すべきかを報告する。",
            skip_permission=True,
            is_terminal=True,
        )
        def report_result(params: ReportParams) -> dict:
            run_ctx.report = {"summary": params.summary, "notify": params.notify}
            return {"ok": True}

        continue_mode = automation.conversation_mode == "continue"
        session_id = f"auto-{automation.id}" if continue_mode else f"auto-{automation.id}-{attempt_id}"
        active = await self.manager.open_session(
            session_id,
            model=automation.model,
            resume=continue_mode,
            allow_write=automation.allow_write,
            extra_tools=[ToolSpec(report_result)],
            connectors=list(automation.connectors),
        )
        active.policy.on_tool_result = run_ctx.capture_tool_result

        def on_event(event: Any) -> None:
            mapped = map_event(event, self.ctx.masker)
            if mapped is None:
                return
            if mapped["type"] == "usage":
                run_ctx.requests += 1
            elif mapped["type"] in KEPT_EVENT_TYPES:
                run_ctx.events.append(mapped)

        unsubscribe = active.session.on(on_event)
        loop = asyncio.get_running_loop()
        prompt = expand_prompt(automation.prompt, now) + REPORT_REMINDER
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            # Each send gets its own budget of research sub-agents, like the chat's per-answer budget.
            active.policy.begin_answer()
            await asyncio.wait_for(send_and_wait_own(active.session, prompt), remaining)
            remaining = deadline - loop.time()
            if run_ctx.report is None and remaining > 0:
                # The report carries the result the user reads in the run history (and the notify decision),
                # so ask once more within the same session. The work itself is already done, so this extra
                # request is best effort: failing it must not turn a finished run into a failed one.
                run_ctx.events.append({"type": "follow_up"})
                follow_up_start = len(run_ctx.events)
                try:
                    active.policy.begin_answer()
                    await asyncio.wait_for(send_and_wait_own(active.session, REPORT_FOLLOW_UP), remaining)
                except Exception:  # noqa: BLE001
                    logger.warning("automation %s did not report after the follow-up request", automation.id)
                    await self._abort(active.session)
                    del run_ctx.events[follow_up_start:]
        except TimeoutError:
            await self._abort(active.session)
            raise
        finally:
            unsubscribe()
            await active.release()
            # Always close: report_result is bound to this run's context, so a cached session would report into a
            # previous run. "continue" mode resumes the stored history from disk next time; "new" mode sessions are
            # deleted so per-run session state does not pile up on the volume.
            await self.manager.close_session(session_id)
            if not continue_mode:
                try:
                    await self.manager.delete_session(session_id)
                except Exception:  # noqa: BLE001
                    logger.warning("could not delete automation session state")

    @staticmethod
    async def _abort(session: Any) -> None:
        try:
            await session.abort()
        except Exception:  # noqa: BLE001
            logger.debug("abort failed")

    @staticmethod
    def _condition_met(automation: Automation, run_ctx: RunContext) -> bool | None:
        match automation.notify.condition:
            case "always":
                return True
            case "report":
                return bool(run_ctx.report and run_ctx.report.get("notify"))
            case "signal":
                return automation.notify.signal_met(run_ctx.signals)
        return None

    @staticmethod
    def _should_notify(automation: Automation, condition_met: bool | None) -> bool:
        if not condition_met:
            return False
        if automation.notify.only_on_change:
            return automation.state.last_condition_met is not True
        return True

    async def _send(self, automation: Automation, record: dict, *, headline: str) -> None:
        base = self.ctx.settings.base_url.rstrip("/")
        link = f"{base}/automations?automation={automation.id}&run={record['id']}"
        lines = [f"@{self.notifier.mention}", "", f"オートメーション「{automation.name}」: {headline}", ""]
        if automation.notify.include_summary and record.get("summary"):
            lines += [record["summary"], ""]
        lines.append(f"詳細はアプリで確認してください: {link}")
        try:
            record["issue_url"] = await self.notifier.create_issue(
                f"[Life Helper] {automation.name}: {headline}", "\n".join(lines)
            )
            record["notified"] = True
        except NotifyError as e:
            record["notify_error"] = str(e)

    async def _notify_failure(self, automation: Automation, record: dict) -> None:
        if automation.notify.github:
            await self._send(automation, record, headline="実行に失敗しました")

    async def _notify_reauth(self, now: datetime) -> None:
        """All automations stop without a token, so this is sent once a day regardless of per-automation settings."""
        day = now.date().isoformat()
        if (
            not self.ctx.settings.notify_reauth
            or self.store.last_reauth_notice() == day
            or not self.notifier.configured
        ):
            return
        base = self.ctx.settings.base_url.rstrip("/")
        body = (
            f"@{self.notifier.mention}\n\nGitHub のトークンが使えないため、オートメーションを実行できません。\n"
            f"{base} を開いてログインし直してください。"
        )
        try:
            await self.notifier.create_issue("[Life Helper] 再ログインが必要です", body)
            self.store.set_reauth_notice(day)
        except NotifyError:
            logger.warning("could not send re-login notice")

    def _finish(self, automation: Automation, record: dict, *, condition_met: bool | None) -> dict:
        record["finished_at"] = datetime.now(UTC).isoformat()
        self.store.save_run(record)
        fields: dict[str, Any] = {"last_run_at": record["started_at"], "last_status": record["status"]}
        if condition_met is not None:
            fields["last_condition_met"] = condition_met
        self.store.update_state(automation.id, **fields)
        return record
