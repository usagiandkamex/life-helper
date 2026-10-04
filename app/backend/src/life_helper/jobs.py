"""Entrypoint for the scheduled ACA job: ``python -m life_helper.jobs run-due``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time

from .automation.runner import AutomationRunner
from .bootstrap import init_core
from .browser.service import shutdown_browser
from .config import get_settings
from .context import build_context
from .retention import PASS_BUDGET_SECONDS, run_scope
from .security import install_log_masking

logger = logging.getLogger("life_helper.jobs")

# Runs must end this long before the job's time limit, leaving time to stop Copilot, save and exit.
RUN_RESERVE_SECONDS = 5 * 60
# The data retention pass must end this long before the job's time limit.
RETENTION_RESERVE_SECONDS = 60
# A retention pass is not started with less time than this.
RETENTION_MIN_SECONDS = 10


async def run_due() -> int:
    job_started = time.monotonic()
    ctx = build_context(get_settings())
    install_log_masking(ctx.masker)
    init_core(ctx)
    assert ctx.automations is not None
    job_ends = job_started + ctx.settings.automation_job_timeout_seconds
    runner = AutomationRunner(ctx, ctx.automations, job_deadline=job_ends - RUN_RESERVE_SECONDS)
    try:
        # 「今すぐ実行」 first: the user is waiting for it.
        results = await runner.run_requested()
        results += await runner.run_due()
        # Once a day, after the due runs so they are not delayed (the app does the same while it is running).
        budget = min(PASS_BUDGET_SECONDS, job_ends - RETENTION_RESERVE_SECONDS - time.monotonic())
        if budget < RETENTION_MIN_SECONDS:
            logger.info("data retention skipped: no time left in this job execution")
        else:
            try:
                await run_scope(ctx, "automation", automation_manager=runner.manager, budget_seconds=budget)
            except Exception as exc:  # noqa: BLE001 - never fails the job; retried an hour later
                logger.error("data retention failed: %s", type(exc).__name__)
    finally:
        await runner.manager.reset()
        await shutdown_browser(ctx)
    summary = [{k: r.get(k) for k in ("automation_id", "id", "status", "notified")} for r in results]
    logger.info("automation job finished: %s", json.dumps(summary, ensure_ascii=False))
    return 0


async def run_until_stopped() -> int:
    """Runs the job; when the platform stops it (SIGTERM: the job's time limit, a manual stop or an update), the run
    in progress is cancelled so it records that it was interrupted before the process is killed."""
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except NotImplementedError:  # Windows (local runs): stopped with Ctrl+C instead
        return await run_due()
    try:
        return await run_due()
    except asyncio.CancelledError:
        logger.warning("automation job stopped before it finished")
        return 1
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="life_helper.jobs")
    parser.add_argument("command", choices=["run-due"])
    args = parser.parse_args(argv)
    if args.command == "run-due":
        return asyncio.run(run_until_stopped())
    return 1


def cli() -> None:
    """Console script ``life-helper-job`` used by the scheduled ACA job (single token, no CLI flag parsing issues)."""
    sys.exit(main(["run-due"]))


if __name__ == "__main__":
    sys.exit(main())
