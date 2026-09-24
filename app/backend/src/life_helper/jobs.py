"""Entrypoint for the scheduled ACA job: ``python -m life_helper.jobs run-due``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .automation.runner import AutomationRunner
from .bootstrap import init_core
from .config import get_settings
from .context import build_context
from .security import install_log_masking

logger = logging.getLogger("life_helper.jobs")


async def run_due() -> int:
    ctx = build_context(get_settings())
    install_log_masking(ctx.masker)
    init_core(ctx)
    assert ctx.automations is not None
    runner = AutomationRunner(ctx, ctx.automations)
    try:
        results = await runner.run_due()
    finally:
        await runner.manager.reset()
    summary = [{k: r.get(k) for k in ("automation_id", "id", "status", "notified")} for r in results]
    logger.info("automation job finished: %s", json.dumps(summary, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="life_helper.jobs")
    parser.add_argument("command", choices=["run-due"])
    args = parser.parse_args(argv)
    if args.command == "run-due":
        return asyncio.run(run_due())
    return 1


def cli() -> None:
    """Console script ``life-helper-job`` used by the scheduled ACA job (single token, no CLI flag parsing issues)."""
    sys.exit(main(["run-due"]))


if __name__ == "__main__":
    sys.exit(main())
