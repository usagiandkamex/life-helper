"""Automation definitions (automations.yaml), run history and monthly usage on the shared volume."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from ..knowledge.store import atomic_write
from .locks import FileLock
from .models import Automation

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")


class UnreadableRunError(OSError):
    """A run record exists but cannot be read (see ``list_run_meta(strict=True)``)."""


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


def _valid_id(value: str) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def parse_timestamp(value: object) -> datetime | None:
    """An ISO timestamp as an aware datetime (records are written in UTC), or None when it cannot be read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_link(path: Path) -> bool:
    """Symlinks and junctions are never followed or deleted by the cleanup."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


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

    def _with_lock(self, fn, lock: FileLock | None = None, attempts: int = 50):
        lock = lock or self._edit_lock()
        for _ in range(attempts):
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
        if not _valid_id(automation_id):
            raise ValueError("invalid automation id")
        return self.runs_dir / automation_id

    def _run_path(self, automation_id: str, run_id: str) -> Path:
        if not _valid_id(run_id):
            raise ValueError("invalid run id")
        return self._run_dir(automation_id) / f"{run_id}.json"

    def _run_lock(self, automation_id: str, run_id: str) -> FileLock:
        # Taken by everything that rewrites or deletes an existing record, so a deleted run is never written back.
        return FileLock(self.locks_dir / f"run-{automation_id}-{run_id}.lock", ttl_seconds=30)

    def save_run(self, record: dict, *, replace_only: bool = False) -> bool:
        """Writes a run record. With ``replace_only`` it is written only while the record still exists, so a run
        deleted from the history in the meantime is not brought back. Returns whether it was written."""
        if not replace_only:
            self._save_runs([record])
            return True
        automation_id, run_id = record["automation_id"], record["id"]
        path = self._run_path(automation_id, run_id)

        def op():
            if not path.exists():
                return False
            self._save_runs([record])
            return True

        # Never written without the lock (a deletion could slip in between); waiting past the lock's 30-second expiry
        # lets a lock left by a stopped process be taken over.
        try:
            return self._with_lock(op, lock=self._run_lock(automation_id, run_id), attempts=400)
        except TimeoutError:
            logger.error("could not save the result of an automation run: its record stayed locked")
            return False

    def _write_run(self, record: dict) -> tuple[str, dict] | None:
        """Writes one record and returns its index entry (None when it cannot be cached)."""
        path = self._run_path(record["automation_id"], record["id"])
        atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1))
        read = self._read_run_meta(path)
        if read is not None and read[1] is not None:
            return f"{record['automation_id']}/{record['id']}", read[1]
        return None

    def _save_runs(self, records: list[dict]) -> None:
        entries = dict(filter(None, (self._write_run(record) for record in records)))
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

    def list_run_meta(self, automation_id: str | None = None, *, strict: bool = False) -> list[dict]:
        """Run metadata without transcripts, newest first.

        Records are opened only when the index has no fresh entry for them, so listing stays cheap as runs pile up.
        A record that cannot be read is left out, or raises ``UnreadableRunError`` with ``strict`` (for decisions
        that must see every run, such as whether a Copilot session is still in use).
        """
        dirs = [self._run_dir(automation_id)] if automation_id else [d for d in self.runs_dir.glob("*") if d.is_dir()]
        index = self._read_run_index()
        metas: list[dict] = []
        refreshed: dict[str, dict] = {}
        seen: set[str] = set()
        for d in dirs:
            if not _valid_id(d.name):
                continue
            for p in d.glob("*.json"):
                if not _valid_id(p.stem):
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
                        if strict:
                            raise UnreadableRunError(key)
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
        if not _valid_id(run_id):
            return None
        path = self._run_path(automation_id, run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def mark_read(self, automation_id: str, run_id: str) -> None:
        self.mark_runs_read(automation_id, [run_id])

    def mark_runs_read(self, automation_id: str, run_ids: list[str]) -> None:
        entries: dict[str, dict] = {}
        for run_id in dict.fromkeys(run_ids):
            # A run in progress has nothing to read, and writing it back would replace the result the runner is
            # about to save with this (older) copy of the same record.
            record = self.get_run(automation_id, run_id)
            if not record or record.get("read") or record.get("status") == RUNNING_STATUS:
                continue

            def op(run_id: str = run_id) -> tuple[str, dict] | None:
                # Read again under the lock: the run may have been deleted (or replaced) since.
                current = self.get_run(automation_id, run_id)
                if not current or current.get("read") or current.get("status") == RUNNING_STATUS:
                    return None
                current["read"] = True
                return self._write_run(current)

            try:
                entry = self._with_lock(op, lock=self._run_lock(automation_id, run_id))
            except TimeoutError:
                logger.warning("run record is busy; it stays unread")
                continue
            if entry is not None:
                entries[entry[0]] = entry[1]
        if entries:
            self._update_run_index(entries)

    def delete_run(
        self, automation_id: str, run_id: str, *, in_progress: Callable[[dict], bool]
    ) -> tuple[Literal["deleted", "missing", "running"], dict | None]:
        """Deletes one run record unless ``in_progress`` says it may still be running. Returns the outcome and the
        record that was found."""
        path = self._run_path(automation_id, run_id)

        def op():
            record = self.get_run(automation_id, run_id)
            if record is None:
                return "missing", None
            if in_progress(record):
                return "running", record
            path.unlink(missing_ok=True)
            return "deleted", record

        outcome, record = self._with_lock(op, lock=self._run_lock(automation_id, run_id))
        if outcome == "deleted":
            self._update_run_index({}, drop={f"{automation_id}/{run_id}"})
        return outcome, record

    def prune_runs(self, cutoff: datetime, should_stop: Callable[[], bool] = lambda: False) -> list[dict]:
        """Deletes the records of runs that started before ``cutoff`` and returns their metadata.

        The cutoff is at least a day ago while a run lasts at most 20 minutes, so a record still marked as running
        that started before it was left behind by a stopped app or job (the history shows it as interrupted).
        Records whose start time cannot be read are kept.
        """
        deleted: list[dict] = []
        for meta in self.list_run_meta():
            if should_stop():
                break
            started = parse_timestamp(meta.get("started_at"))
            if started is None or started >= cutoff:
                continue
            automation_id, run_id = meta["automation_id"], meta["id"]
            path = self._run_path(automation_id, run_id)
            if is_link(path.parent) or is_link(path):
                continue

            def op(automation_id: str = automation_id, run_id: str = run_id, path: Path = path) -> bool:
                current = self.get_run(automation_id, run_id)
                started = parse_timestamp((current or {}).get("started_at"))
                if started is None or started >= cutoff:
                    return False
                path.unlink(missing_ok=True)
                return True

            try:
                if self._with_lock(op, lock=self._run_lock(automation_id, run_id)):
                    deleted.append(meta)
            except (TimeoutError, OSError):
                logger.warning("could not delete an expired run record")
        if deleted:
            self._update_run_index({}, drop={f"{m['automation_id']}/{m['id']}" for m in deleted})
        return deleted

    def remove_empty_run_dirs(self) -> None:
        """Removes the emptied history folders of deleted automations (a live automation may be writing to its own)."""
        live = {a.id for a in self.list()}
        for d in self.runs_dir.glob("*"):
            if d.name in live or not _valid_id(d.name) or is_link(d) or not d.is_dir():
                continue
            try:
                d.rmdir()
            except OSError:
                pass  # not empty

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

    def _write_hidden(self, edit: Callable[[dict[str, str]], bool]) -> bool:
        """Applies ``edit`` to the hidden conversations under the edit lock; it returns whether anything changed."""

        def op():
            state = self._chat_state()
            hidden = state.get("hidden") if isinstance(state.get("hidden"), dict) else {}
            if not edit(hidden):
                return False
            state["hidden"] = hidden
            atomic_write(self.chat_state_path, json.dumps(state, ensure_ascii=False))
            return True

        return self._with_lock(op)

    def repoint_hidden(self, thread_id: str, deleted_run_id: str, new_run_id: str | None) -> None:
        """A conversation hidden through a run that was deleted stays hidden through ``new_run_id`` (or is forgotten
        when that is None)."""

        def edit(hidden: dict[str, str]) -> bool:
            if hidden.get(thread_id) != deleted_run_id:
                return False
            if new_run_id:
                hidden[thread_id] = new_run_id
            else:
                hidden.pop(thread_id)
            return True

        self._write_hidden(edit)

    def prune_hidden(self, runs_by_thread: dict[str, set[str]]) -> int:
        """Forgets hidden conversations whose run is gone (such a conversation is shown anyway). Returns how many."""
        dropped: list[str] = []

        def edit(hidden: dict[str, str]) -> bool:
            dropped[:] = [t for t, run_id in hidden.items() if run_id not in runs_by_thread.get(t, ())]
            for thread_id in dropped:
                hidden.pop(thread_id)
            return bool(dropped)

        self._write_hidden(edit)
        return len(dropped)

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

    def prune_usage(self, cutoff: datetime) -> int:
        """Drops the run counts of months that ended before ``cutoff`` (never the current month). Returns how many."""
        keep_from = self.month_key(cutoff)

        def op():
            usage = self._usage()
            stale = [k for k in usage if _MONTH_RE.fullmatch(k) and k < keep_from]
            if stale:
                for key in stale:
                    usage.pop(key)
                atomic_write(self.usage_path, json.dumps(usage))
            return len(stale)

        return self._with_lock(op)
