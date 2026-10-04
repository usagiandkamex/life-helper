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
from ..copilot_integration.system_prompt import MAX_SUMMARY_CHARS
from ..security import redact_sensitive
from ..tools.registry import ToolSpec
from .locks import FileLock
from .models import Automation, expand_prompt
from .notify import GitHubNotifier, NotifyError
from .store import INTERRUPTED_STATUS, RUNNING_STATUS, AutomationStore, parse_timestamp

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..context import AppContext

logger = logging.getLogger(__name__)
KEPT_EVENT_TYPES = ("message", "tool_start", "tool_end", "file_write", "error")
MAX_EVENTS = 200
# While a run is in progress, what it has produced so far is written to its record this often, so a run cut off by
# the app or job stopping (shown as interrupted) still has the text it streamed.
CHECKPOINT_SECONDS = 30
# The record is also written at least this often (with ``heartbeat_at``), so the history can tell a run whose process
# has stopped without saving anything well before its lock expires (see automation/api.py).
HEARTBEAT_SECONDS = 60
# How long a progress write waits for the record's lock; a skipped write is made again at the next checkpoint.
PROGRESS_SAVE_SECONDS = 10
# Copilot calls made while a run ends are bounded, so a CLI that stopped answering cannot keep the run from saving its
# result: the client is restarted instead.
ABORT_TIMEOUT_SECONDS = 10
CLEANUP_TIMEOUT_SECONDS = 30
RESET_TIMEOUT_SECONDS = 45
# A run stopped from outside saves that it was interrupted within this long (the platform kills a container about
# 30 seconds after asking it to stop).
STOP_SAVE_SECONDS = 10
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
APP_STOPPED_MESSAGE = "アプリが停止したため中断しました（しばらく使われないときの自動停止や、更新による再起動など）。"
JOB_STOPPED_MESSAGE = "オートメーションを実行するジョブが停止したため中断しました（制限時間の超過や更新など）。"
JOB_TIME_LIMIT_MESSAGE = "オートメーションを実行するジョブの制限時間が近づいたため中断しました。"


class ReportParams(BaseModel):
    summary: str = Field(
        max_length=MAX_SUMMARY_CHARS,
        description=(
            f"利用者が「実行履歴」で読む結果の本文（Markdown、{MAX_SUMMARY_CHARS:,} 文字以内）。"
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
    # Text streamed for the answer currently being written, before it is finalized into a "message" event. It is
    # kept so a run that is cut off (e.g. a timeout) can still show the partial result in its history.
    partial: str = ""
    # While the report follow-up is in progress, the index of its first event (after its marker). Until it finishes,
    # its events and streamed text are left out of the progress, as they would be if it failed.
    follow_up_start: int | None = None

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
    def __init__(
        self,
        ctx: AppContext,
        store: AutomationStore,
        manager: CopilotManager | None = None,
        *,
        job_deadline: float | None = None,
    ) -> None:
        self.ctx = ctx
        self.store = store
        self.manager = manager or CopilotManager(ctx, ctx.settings.copilot_automation_dir, automation=True)
        self.notifier = build_notifier(ctx)
        self._token_checked_at: float | None = None
        # In the job: when (time.monotonic()) every run must have ended, ahead of the job's own time limit, after
        # which the platform stops the job and any run still in progress with it.
        self.job_deadline = job_deadline
        self._runs_started = 0

    def _lock(self, automation_id: str) -> FileLock:
        return FileLock(
            self.store.locks_dir / f"automation-{automation_id}.lock", self.ctx.settings.automation_lock_ttl_seconds
        )

    async def run_due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        results = []
        due = []
        for automation in self.store.list():
            if automation.enabled and not automation.state.next_run_at:
                self.store.update_state(automation.id, next_run_at=automation.schedule.next_after(now).isoformat())
                continue
            if automation.is_due(now):
                due.append(automation)
        # Due runs share one job execution, one after another. The longest-waiting run goes first, so a run put off
        # for lack of time (below) is not overtaken again by the same runs at the next job execution.
        due.sort(key=lambda a: parse_timestamp(a.state.next_run_at) or now)
        for automation in due:
            if not self._fits(automation):
                # Still due (next_run_at is left as it is): a later job execution, one every 15 minutes, runs it.
                logger.info("automation %s put off to a later job execution: not enough time left", automation.id)
                results.append({"automation_id": automation.id, "status": "deferred"})
                continue
            results.append(await self.run(automation.id, now=now, scheduled=True))
        return results

    async def run_requested(self) -> list[dict]:
        """Runs the 「今すぐ実行」 requests the web app handed to the job (see AutomationStore.add_run_request)."""
        results = []
        for request in self.store.run_requests():
            automation_id, run_id = request["automation_id"], request["run_id"]
            if request["expired"]:
                if self.store.take_run_request(automation_id, run_id):
                    logger.warning("automation %s: dropped a run request no job execution took in time", automation_id)
                continue
            automation = self.store.get(automation_id)
            if automation is not None and not self._fits(automation):
                # Left waiting: a later job execution runs it first.
                results.append({"automation_id": automation_id, "status": "deferred"})
                continue
            results.append(await self.run(automation_id, run_id=run_id, request=True))
        return results

    def _fits(self, automation: Automation) -> bool:
        """Whether a run can still use its whole time limit before the job's. The first run of a job execution always
        starts (cut short at the job's limit if need be), so no run is put off for good."""
        if self.job_deadline is None or self._runs_started == 0:
            return True
        return time.monotonic() + automation.max_runtime_minutes * 60 <= self.job_deadline

    async def run(
        self,
        automation_id: str,
        *,
        now: datetime | None = None,
        scheduled: bool = False,
        run_id: str | None = None,
        request: bool = False,
    ) -> dict:
        """Runs one automation unless it is already running. With ``request`` it runs the waiting 「今すぐ実行」
        request for ``run_id``, which is kept while the automation is running (a later job execution takes it)."""
        now = now or datetime.now(UTC)
        lock = self._lock(automation_id)
        if not lock.try_acquire():
            return {"automation_id": automation_id, "status": "skipped_locked"}
        try:
            # Re-read after taking the lock: an overlapping job may have just run it.
            automation = self.store.get(automation_id)
            # Taken under the lock, so a request is run once even when two job executions see it.
            if request and not self.store.take_run_request(automation_id, run_id or ""):
                return {"automation_id": automation_id, "status": "skipped_taken"}
            if automation is None:
                return {"automation_id": automation_id, "status": "not_found"}
            if scheduled and not automation.is_due(now):
                return {"automation_id": automation_id, "status": "skipped_not_due"}
            self._runs_started += 1
            # Schedule the next run first so a crash cannot cause a tight retry loop.
            self.store.update_state(automation.id, next_run_at=automation.schedule.next_after(now).isoformat())
            return await self._execute(automation, now, run_id=run_id)
        finally:
            lock.release()

    async def _execute(self, automation: Automation, now: datetime, *, run_id: str | None = None) -> dict:
        run_id = run_id or uuid.uuid4().hex[:16]
        started_at = datetime.now(UTC).isoformat()
        record: dict[str, Any] = {
            "id": run_id,
            "automation_id": automation.id,
            "name": automation.name,
            # Due automations run one after another with the same scheduled ``now``, so the record keeps the time this
            # run really started; the history shows how long it took (finished_at - started_at).
            "started_at": started_at,
            "heartbeat_at": started_at,
            # Recorded as running before any work starts, so the run history shows that the run is in progress
            # (scheduled runs happen in the job process, so the shared volume is the only place the app can see it).
            "status": RUNNING_STATUS,
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
        # Every later write replaces this record, so the result never appears twice in the history.
        self.store.save_run(record)
        try:
            return await self._run_recorded(automation, record, now)
        except asyncio.CancelledError:
            # The process is stopping (the app scaling in or restarting, the job being stopped): record why before it
            # goes, so the history explains the interruption instead of showing a run that seems to go on.
            await self._record_stop(automation, record)
            raise

    async def _run_recorded(self, automation: Automation, record: dict[str, Any], now: datetime) -> dict:
        run_id = record["id"]
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
            return await self._finish(automation, record, condition_met=None)
        if not await self._token_valid():
            record |= {"status": "reauth", "error": REAUTH_MESSAGE, "summary": REAUTH_MESSAGE}
            await self._notify_reauth(now)
            return await self._finish(automation, record, condition_met=None)
        limit = self.ctx.settings.automation_monthly_run_limit
        if not self.store.try_reserve_run(limit, now):
            record |= {
                "status": "skipped_limit",
                "summary": f"今月の実行回数の上限（{limit} 回）に達したため実行しませんでした。",
            }
            return await self._finish(automation, record, condition_met=None)

        # One deadline for the whole run, retry included, so a run never outlives its lock (nor the job's time limit).
        loop = asyncio.get_running_loop()
        limit = float(automation.max_runtime_minutes * 60)
        job_limited = self.job_deadline is not None and self.job_deadline - time.monotonic() < limit
        if job_limited:
            limit = max(0.0, self.job_deadline - time.monotonic())
        deadline = loop.time() + limit
        run_ctx = RunContext()
        status, error = "error", None
        attempts = 0
        stop_checkpoints = asyncio.Event()
        checkpoints = asyncio.create_task(self._checkpoint(record, lambda: run_ctx, stop_checkpoints))
        cut_off, stopped = True, False
        try:
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
                    status, error = (
                        "timeout",
                        JOB_TIME_LIMIT_MESSAGE
                        if job_limited
                        else f"{automation.max_runtime_minutes} 分以内に終わらなかったため中断しました。",
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "automation %s failed (attempt %d): %s", automation.id, attempt + 1, type(exc).__name__
                    )
                    status, error = "error", self.ctx.masker.mask_text(str(exc)) or "エラーが発生しました"
                    side_effects = any(e["type"] in ("tool_start", "file_write") for e in run_ctx.events)
                    # Retry only failures that happened before any tool ran, so writes are never repeated.
                    if attempt == 0 and not side_effects and deadline - loop.time() > 120:
                        await asyncio.sleep(5)
                        continue
                    break
            cut_off = False
        except asyncio.CancelledError:
            stopped = True
            raise
        finally:
            # Waited for (not cancelled), so a checkpoint still being written cannot land after the final result.
            stop_checkpoints.set()
            await asyncio.shield(checkpoints)
            # The latest progress, in case the result is never written (the run is stopped while it notifies below).
            # A finished answer leaves out any unfinished leftover.
            progress = self._progress(
                record, run_ctx, include_partial=cut_off or status != "success", error=None if cut_off else error
            )
            if stopped:
                # Saved by _execute, with the reason the run stopped.
                record |= progress
            else:
                await self._save_progress(progress)

        # A run cut off before it finished (timeout/error) leaves its streamed text here; keep it as the run's
        # result so the history shows what was produced instead of only the failure reason. Successful runs already
        # have their finished answer (and report), and a success's follow-up may stream text without finishing a
        # message, so that leftover must not be surfaced.
        all_events, final_message = self._answer(run_ctx, include_partial=status != "success")
        summary = (run_ctx.report or {}).get("summary") or final_message[:2000] or error or ""
        events, omitted = trim_events(all_events)
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
        return await self._finish(automation, record, condition_met=condition_met)

    @staticmethod
    def _answer(run_ctx: RunContext, *, include_partial: bool) -> tuple[list[dict], str]:
        """The run's events and the answer it shows; with ``include_partial`` text streamed but never finished into
        a message is added as the last (partial) message."""
        events = list(run_ctx.events)
        if run_ctx.follow_up_start is not None:
            del events[run_ctx.follow_up_start :]
            include_partial = False
        if include_partial and run_ctx.partial.strip():
            events.append({"type": "message", "content": run_ctx.partial, "partial": True})
        final_message = next((e["content"] for e in reversed(events) if e["type"] == "message"), "")
        return events, final_message

    async def _checkpoint(self, record: dict, current: Callable[[], RunContext], stop: asyncio.Event) -> None:
        """Writes what the run has produced so far to its record every ``CHECKPOINT_SECONDS`` until ``stop``, and at
        least every ``HEARTBEAT_SECONDS`` so the history can tell that the run is still going on."""
        written: tuple | None = None
        last_write = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), CHECKPOINT_SECONDS)
                return
            except TimeoutError:
                pass
            run_ctx = current()
            state = (
                id(run_ctx),
                len(run_ctx.events),
                len(run_ctx.partial),
                run_ctx.requests,
                run_ctx.report,
                run_ctx.follow_up_start,
            )
            # A retry starts from an empty context, which is written too so the discarded attempt does not remain.
            changed = state != written and (written is not None or run_ctx.events or run_ctx.partial or run_ctx.report)
            if changed or time.monotonic() - last_write >= HEARTBEAT_SECONDS:
                written, last_write = state, time.monotonic()
                await self._save_progress(self._progress(record, run_ctx))

    def _progress(
        self, record: dict, run_ctx: RunContext, *, include_partial: bool = True, error: str | None = None
    ) -> dict:
        """The run's record with its progress so far. It keeps its running status: a finished run replaces it with
        its result, and a run stopped from outside saves it as interrupted."""
        events, final_message = self._answer(run_ctx, include_partial=include_partial)
        trimmed, omitted = trim_events(events)
        snapshot = record | self._sanitize(
            {
                "error": error,
                "summary": (run_ctx.report or {}).get("summary") or final_message[:2000] or error or "",
                "final_message": final_message,
                "report": run_ctx.report,
                "signals": run_ctx.signals,
                "events": trimmed,
                "events_omitted": omitted,
                "requests": run_ctx.requests,
            }
        )
        snapshot["heartbeat_at"] = datetime.now(UTC).isoformat()
        return snapshot

    async def _save_progress(self, snapshot: dict) -> None:
        try:
            await asyncio.to_thread(
                self.store.save_run, snapshot, replace_only=True, wait_seconds=PROGRESS_SAVE_SECONDS
            )
        except Exception:  # noqa: BLE001
            logger.warning("could not save the progress of an automation run")

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
            elif mapped["type"] == "delta":
                # One per token; not stored as events (too many), but kept so a cut-off answer still has its text.
                # The follow-up's text is never kept: the main answer is already finished.
                if run_ctx.follow_up_start is None:
                    run_ctx.partial += mapped["text"]
            elif mapped["type"] in KEPT_EVENT_TYPES:
                if mapped["type"] == "message":
                    run_ctx.partial = ""  # the finished answer supersedes the text streamed so far
                run_ctx.events.append(mapped)

        unsubscribe = active.session.on(on_event)
        loop = asyncio.get_running_loop()
        prompt = expand_prompt(automation.prompt, now) + REPORT_REMINDER
        stopping = False
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
                run_ctx.partial = ""  # leftover of the finished main answer, never shown for it
                run_ctx.follow_up_start = len(run_ctx.events)
                try:
                    active.policy.begin_answer()
                    await asyncio.wait_for(send_and_wait_own(active.session, REPORT_FOLLOW_UP), remaining)
                except Exception:  # noqa: BLE001
                    logger.warning("automation %s did not report after the follow-up request", automation.id)
                    await self._abort(active.session)
                    del run_ctx.events[run_ctx.follow_up_start :]
                run_ctx.follow_up_start = None
        except TimeoutError:
            await self._abort(active.session)
            raise
        except asyncio.CancelledError:
            # The process is stopping: its Copilot client is stopped on the way out (and session state left behind
            # is removed by the data retention), so no time is spent here before the run records the interruption.
            stopping = True
            raise
        finally:
            unsubscribe()
            if not stopping:
                await self._close_session(active, session_id, delete=not continue_mode)

    async def _close_session(self, active: Any, session_id: str, *, delete: bool) -> None:
        """Ends the run's session. Bounded: when Copilot stops answering, its client is restarted instead of waited
        on, so the run still records its result."""
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                await active.release()
                # Always close: report_result is bound to this run's context, so a cached session would report into
                # a previous run. "continue" mode resumes the stored history from disk next time; "new" mode sessions
                # are deleted so per-run session state does not pile up on the volume.
                await self.manager.close_session(session_id)
                if delete:
                    try:
                        await self.manager.delete_session(session_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("could not delete automation session state")
        except TimeoutError:
            logger.warning("Copilot did not answer while an automation session was closed; restarting the client")
            try:
                await asyncio.wait_for(self.manager.reset(), RESET_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001
                logger.warning("could not restart the Copilot client")

    @staticmethod
    async def _abort(session: Any) -> None:
        try:
            await asyncio.wait_for(session.abort(), ABORT_TIMEOUT_SECONDS)
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

    async def _record_stop(self, automation: Automation, record: dict) -> None:
        """Saves a run stopped from outside as interrupted, with what it produced so far and the reason (a run that
        had already decided its result, and was stopped while notifying, keeps that result)."""
        if record.get("status") == RUNNING_STATUS:
            reason = JOB_STOPPED_MESSAGE if self.job_deadline is not None else APP_STOPPED_MESSAGE
            record |= {"status": INTERRUPTED_STATUS, "error": reason, "summary": record.get("summary") or reason}
        try:
            # The baseline for "only on change" is left as it is: the run may not have notified.
            await self._finish(automation, record, condition_met=None, wait_seconds=STOP_SAVE_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("automation %s: could not record that the run was interrupted", automation.id)

    async def _finish(
        self, automation: Automation, record: dict, *, condition_met: bool | None, wait_seconds: float = 40
    ) -> dict:
        record["finished_at"] = datetime.now(UTC).isoformat()
        # The record was written when the run started; if it was deleted from the history since, it stays deleted.
        # Waiting for the record's lock happens off the event loop, so the app keeps serving meanwhile.
        if not await asyncio.to_thread(self.store.save_run, record, replace_only=True, wait_seconds=wait_seconds):
            logger.info(
                "automation %s: the run result was not saved (deleted from the history meanwhile)", automation.id
            )
        fields: dict[str, Any] = {"last_run_at": record["started_at"], "last_status": record["status"]}
        if condition_met is not None:
            fields["last_condition_met"] = condition_met
        self.store.update_state(automation.id, **fields)
        return record
