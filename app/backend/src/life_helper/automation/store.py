"""Automation definitions (automations.yaml), run history and monthly usage on the shared volume."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..knowledge.store import atomic_write
from .locks import FileLock
from .models import Automation

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")

# A run is recorded as running when it starts and the same record is replaced with the result when it finishes, so
# the history can show a run that is still in progress (its result and transcript are only there once it is done).
RUNNING_STATUS = "running"

# Everything the run history and the chat view need before opening a record (which also holds the transcript).
RUN_META_FIELDS = (
    "name",
    "started_at",
    "finished_at",
    "status",
    "read",
    "notified",
    "transcript_version",
    "conversation_mode",
)


def _run_meta(record: dict) -> dict:
    return {k: record[k] for k in RUN_META_FIELDS if k in record}


class AutomationStore:
    def __init__(self, app_state_dir: Path) -> None:
        self.path = app_state_dir / "automations.yaml"
        self.runs_dir = app_state_dir / "automation-runs"
        self.usage_path = app_state_dir / "automation-usage.json"
        self.chat_state_path = app_state_dir / "automation-chat.json"
        self.run_index_path = app_state_dir / "automation-run-index.json"
        self.locks_dir = app_state_dir / "locks"

    # -- definitions -------------------------------------------------------------------------------------

    def _edit_lock(self) -> FileLock:
        return FileLock(self.locks_dir / "automations-edit.lock", ttl_seconds=30)

    def _with_lock(self, fn, lock: FileLock | None = None):
        lock = lock or self._edit_lock()
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
        self._save_runs([record])

    def _save_runs(self, records: list[dict]) -> None:
        entries = {}
        for record in records:
            path = self._run_dir(record["automation_id"]) / f"{record['id']}.json"
            atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1))
            read = self._read_run_meta(path)
            if read is not None and read[1] is not None:
                entries[f"{record['automation_id']}/{record['id']}"] = read[1]
        if entries:
            self._update_run_index(entries)

    # The index only caches what list_run_meta() would otherwise read from every record, so a failed or outdated
    # update never loses anything: the next read repairs the entry from the record itself. It also names the fields
    # it caches, so an index written before RUN_META_FIELDS changed is read again from the records instead of used.

    def _index_lock(self) -> FileLock:
        return FileLock(self.locks_dir / "automation-run-index.lock", ttl_seconds=30)

    def _fingerprint(self, path: Path) -> str | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return f"{st.st_mtime_ns}-{st.st_size}"

    def _read_run_meta(self, path: Path) -> tuple[dict, dict | None] | None:
        """The metadata of a record and the index entry for it, or None when the record cannot be read.

        The entry is None when the file was written again while it was read, so cached metadata and the fingerprint
        it is checked against always come from the same version of the file.
        """
        before = self._fingerprint(path)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        meta = _run_meta(record)
        if before is None or before != self._fingerprint(path):
            return meta, None
        return meta, {"fingerprint": before, "meta": meta}

    def _read_run_index(self) -> dict[str, dict]:
        try:
            data = json.loads(self.run_index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("fields") != list(RUN_META_FIELDS):
            return {}
        runs = data.get("runs")
        return runs if isinstance(runs, dict) else {}

    def _update_run_index(self, entries: dict[str, dict], drop: set[str] = frozenset()) -> None:
        def op():
            runs = self._read_run_index()
            runs.update(entries)
            for key in drop:
                runs.pop(key, None)
            atomic_write(
                self.run_index_path, json.dumps({"fields": list(RUN_META_FIELDS), "runs": runs}, ensure_ascii=False)
            )

        try:
            self._with_lock(op, lock=self._index_lock())
        except (TimeoutError, OSError):
            logger.warning("could not update the automation run index")

    def list_run_meta(self, automation_id: str | None = None) -> list[dict]:
        """Run metadata without transcripts, newest first.

        Records are opened only when the index has no fresh entry for them, so listing stays cheap as runs pile up.
        """
        dirs = [self._run_dir(automation_id)] if automation_id else [d for d in self.runs_dir.glob("*") if d.is_dir()]
        index = self._read_run_index()
        metas: list[dict] = []
        refreshed: dict[str, dict] = {}
        seen: set[str] = set()
        for d in dirs:
            if not _ID_RE.match(d.name):
                continue
            for p in d.glob("*.json"):
                if not _ID_RE.match(p.stem):
                    continue
                key = f"{d.name}/{p.stem}"
                seen.add(key)
                cached = index.get(key) if isinstance(index.get(key), dict) else {}
                fingerprint = self._fingerprint(p)
                if isinstance(cached.get("meta"), dict) and cached.get("fingerprint") == fingerprint:
                    meta = dict(cached["meta"])
                else:
                    read = self._read_run_meta(p)
                    if read is None:
                        continue
                    meta, entry = read
                    if entry is not None:
                        refreshed[key] = entry
                # The path decides the identity, so metadata always names a record that can be opened again.
                metas.append(meta | {"id": p.stem, "automation_id": d.name})
        stale = {k for k in index if k.startswith(f"{automation_id}/")} - seen if automation_id else set(index) - seen
        if refreshed or stale:
            self._update_run_index(refreshed, stale)
        metas.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return metas

    def list_runs(self, automation_id: str | None = None, limit: int | None = 50) -> list[dict]:
        metas = self.list_run_meta(automation_id)
        records = []
        for meta in metas[:limit] if limit is not None else metas:
            record = self.get_run(meta["automation_id"], meta["id"])
            if record is not None:
                records.append(record)
        return records

    def get_run(self, automation_id: str, run_id: str) -> dict | None:
        if not _ID_RE.match(run_id):
            return None
        path = self._run_dir(automation_id) / f"{run_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def mark_read(self, automation_id: str, run_id: str) -> None:
        self.mark_runs_read(automation_id, [run_id])

    def mark_runs_read(self, automation_id: str, run_ids: list[str]) -> None:
        records = []
        for run_id in dict.fromkeys(run_ids):
            record = self.get_run(automation_id, run_id)
            # A run in progress has nothing to read, and writing it back would replace the result the runner is
            # about to save with this (older) copy of the same record.
            if record and not record.get("read") and record.get("status") != RUNNING_STATUS:
                record["read"] = True
                records.append(record)
        self._save_runs(records)

    def unread_count(self, metas: list[dict] | None = None) -> int:
        # A run that is still in progress has nothing to read yet.
        if metas is None:
            metas = self.list_run_meta()
        return sum(1 for r in metas if not r.get("read") and r.get("status") != RUNNING_STATUS)

    # -- chat view state ---------------------------------------------------------------------------------
    # Kept apart from the run records so hiding a conversation never rewrites a result (or races with "read").

    def _chat_state(self) -> dict:
        try:
            data = json.loads(self.chat_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def chat_hidden(self) -> dict[str, str]:
        """Thread id -> id of the latest run the user saw when hiding it."""
        hidden = self._chat_state().get("hidden")
        return hidden if isinstance(hidden, dict) else {}

    def hide_chat_thread(self, thread_id: str, through_run_id: str) -> None:
        def op():
            state = self._chat_state()
            hidden = state.get("hidden") if isinstance(state.get("hidden"), dict) else {}
            hidden[thread_id] = through_run_id
            state["hidden"] = hidden
            atomic_write(self.chat_state_path, json.dumps(state, ensure_ascii=False))

        self._with_lock(op)

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
