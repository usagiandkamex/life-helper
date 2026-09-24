"""Automation definitions (automations.yaml), run history and monthly usage on the shared volume."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..knowledge.store import atomic_write
from .locks import FileLock
from .models import Automation

_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


class AutomationStore:
    def __init__(self, app_state_dir: Path) -> None:
        self.path = app_state_dir / "automations.yaml"
        self.runs_dir = app_state_dir / "automation-runs"
        self.usage_path = app_state_dir / "automation-usage.json"
        self.locks_dir = app_state_dir / "locks"

    # -- definitions -------------------------------------------------------------------------------------

    def _edit_lock(self) -> FileLock:
        return FileLock(self.locks_dir / "automations-edit.lock", ttl_seconds=30)

    def _with_lock(self, fn):
        lock = self._edit_lock()
        for _ in range(50):
            if lock.try_acquire():
                try:
                    return fn()
                finally:
                    lock.release()
            time.sleep(0.1)
        raise TimeoutError("automations file is busy")

    def list(self) -> list[Automation]:
        if not self.path.exists():
            return []
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []
        return [Automation.model_validate(item) for item in raw]

    def get(self, automation_id: str) -> Automation | None:
        return next((a for a in self.list() if a.id == automation_id), None)

    def _save_all(self, items: list[Automation]) -> None:
        data = [a.model_dump(mode="json") for a in items]
        atomic_write(self.path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def upsert(self, automation: Automation) -> Automation:
        def op():
            items = self.list()
            for i, existing in enumerate(items):
                if existing.id == automation.id:
                    items[i] = automation
                    break
            else:
                items.append(automation)
            self._save_all(items)
            return automation

        return self._with_lock(op)

    def update_state(self, automation_id: str, **fields) -> Automation | None:
        def op():
            items = self.list()
            target = next((a for a in items if a.id == automation_id), None)
            if target is None:
                return None
            for key, value in fields.items():
                setattr(target.state, key, value)
            self._save_all(items)
            return target

        return self._with_lock(op)

    def delete(self, automation_id: str) -> bool:
        def op():
            items = self.list()
            kept = [a for a in items if a.id != automation_id]
            if len(kept) == len(items):
                return False
            self._save_all(kept)
            return True

        return self._with_lock(op)

    # -- runs --------------------------------------------------------------------------------------------

    def _run_dir(self, automation_id: str) -> Path:
        if not _ID_RE.match(automation_id):
            raise ValueError("invalid automation id")
        return self.runs_dir / automation_id

    def save_run(self, record: dict) -> None:
        path = self._run_dir(record["automation_id"]) / f"{record['id']}.json"
        atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1))

    def list_runs(self, automation_id: str | None = None, limit: int = 50) -> list[dict]:
        dirs = [self._run_dir(automation_id)] if automation_id else [d for d in self.runs_dir.glob("*") if d.is_dir()]
        records = []
        for d in dirs:
            for p in d.glob("*.json"):
                try:
                    records.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return records[:limit]

    def get_run(self, automation_id: str, run_id: str) -> dict | None:
        if not _ID_RE.match(run_id):
            return None
        path = self._run_dir(automation_id) / f"{run_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def mark_read(self, automation_id: str, run_id: str) -> None:
        record = self.get_run(automation_id, run_id)
        if record and not record.get("read"):
            record["read"] = True
            self.save_run(record)

    def unread_count(self) -> int:
        return sum(1 for r in self.list_runs(limit=500) if not r.get("read"))

    # -- monthly usage -----------------------------------------------------------------------------------

    def _usage(self) -> dict:
        try:
            return json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def month_key(self, now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).strftime("%Y-%m")

    def runs_this_month(self, now: datetime | None = None) -> int:
        return int(self._usage().get(self.month_key(now), 0))

    def try_reserve_run(self, limit: int, now: datetime | None = None) -> bool:
        """Atomically checks the monthly limit and counts the run (app and job share this file)."""

        def op():
            usage = self._usage()
            key = self.month_key(now)
            if int(usage.get(key, 0)) >= limit:
                return False
            usage[key] = int(usage.get(key, 0)) + 1
            atomic_write(self.usage_path, json.dumps(usage))
            return True

        return self._with_lock(op)

    def count_run(self, now: datetime | None = None) -> None:
        def op():
            usage = self._usage()
            key = self.month_key(now)
            usage[key] = int(usage.get(key, 0)) + 1
            atomic_write(self.usage_path, json.dumps(usage))

        self._with_lock(op)

    def last_reauth_notice(self) -> str | None:
        return self._usage().get("last_reauth_notice")

    def set_reauth_notice(self, day: str) -> None:
        def op():
            usage = self._usage()
            usage["last_reauth_notice"] = day
            atomic_write(self.usage_path, json.dumps(usage))

        self._with_lock(op)
