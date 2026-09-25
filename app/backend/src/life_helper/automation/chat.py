"""Read-only chat view of finished automation runs.

A run follows its automation's conversation setting: "new" runs are one conversation each, "continue" runs of the same
automation form one conversation (they share one Copilot session). Only records written with a transcript are shown.
"""

from __future__ import annotations

import re
from typing import Any

from .store import AutomationStore

THREAD_ID_RE = re.compile(
    r"^(?:c-(?P<continue_id>[0-9a-f]{6,32})|r-(?P<new_id>[0-9a-f]{6,32})-(?P<run_id>[0-9a-f]{6,32}))$"
)
RUN_ID_PATTERN = r"^[0-9a-f]{6,32}$"
PAGE_SIZE = 20
ANCHOR_LIMIT = 100


def thread_id_for(record: dict) -> str | None:
    """The chat conversation a run belongs to, or None for runs recorded before transcripts existed."""
    if not isinstance(record.get("transcript_version"), int):
        return None
    automation_id, run_id = record.get("automation_id"), record.get("id")
    if not automation_id or not run_id:
        return None
    if record.get("conversation_mode") == "continue":
        return f"c-{automation_id}"
    return f"r-{automation_id}-{run_id}"


def parse_thread_id(thread_id: str) -> tuple[str, str | None]:
    """Returns (automation_id, run_id); run_id is None for a "continue" conversation."""
    m = THREAD_ID_RE.fullmatch(thread_id)
    if not m:
        raise ValueError("invalid thread id")
    if m.group("continue_id"):
        return m.group("continue_id"), None
    return m.group("new_id"), m.group("run_id")


def _summary(thread_id: str, runs: list[dict], names: dict[str, str]) -> dict[str, Any]:
    """``runs`` is run metadata, newest first."""
    latest = runs[0]
    continued = thread_id.startswith("c-")
    title = (names.get(latest["automation_id"]) if continued else None) or latest.get("name") or "オートメーション"
    return {
        "id": thread_id,
        "mode": "continue" if continued else "new",
        "automation_id": latest["automation_id"],
        "title": title,
        "updated_at": latest.get("finished_at") or latest.get("started_at", ""),
        "latest_run_id": latest["id"],
        "latest_started_at": latest.get("started_at", ""),
        "latest_status": latest.get("status"),
        "unread": any(not r.get("read") for r in runs),
        "run_count": len(runs),
    }


def _chronological(record: dict) -> tuple[str, str]:
    # The id breaks ties, so "latest run" (which decides hiding) never flips between two equal timestamps.
    return record.get("started_at", ""), record.get("id", "")


def _thread_meta(store: AutomationStore, thread_id: str) -> list[dict]:
    """The metadata of one conversation's runs, oldest first (transcripts are read only for the page shown)."""
    automation_id, _ = parse_thread_id(thread_id)
    runs = [r for r in store.list_run_meta(automation_id) if thread_id_for(r) == thread_id]
    return sorted(runs, key=_chronological)


def list_threads(store: AutomationStore) -> list[dict]:
    """Visible conversations, most recently updated first (without events)."""
    names = {a.id: a.name for a in store.list()}
    hidden = store.chat_hidden()
    groups: dict[str, list[dict]] = {}
    for record in store.list_run_meta():
        thread_id = thread_id_for(record)
        if thread_id:
            groups.setdefault(thread_id, []).append(record)
    threads = []
    for thread_id, runs in groups.items():
        runs.sort(key=_chronological, reverse=True)
        # Hidden until a run arrives that the user has not seen yet.
        if hidden.get(thread_id) != runs[0]["id"]:
            threads.append(_summary(thread_id, runs, names))
    threads.sort(key=lambda t: (t["updated_at"], t["latest_started_at"], t["id"]), reverse=True)
    return threads


def get_thread(
    store: AutomationStore, thread_id: str, *, before: str | None = None, anchor: str | None = None
) -> dict | None:
    """One page of a conversation, oldest first. ``before`` pages back; ``anchor`` starts the page at that run."""
    if before is not None and anchor is not None:
        raise ValueError("before and anchor cannot be combined")
    metas = _thread_meta(store, thread_id)
    if not metas:
        return None
    ids = [r["id"] for r in metas]
    for run_id in (before, anchor):
        if run_id is not None and run_id not in ids:
            raise ValueError("run is not part of this conversation")
    if anchor is not None:
        start = ids.index(anchor)
        end = min(len(metas), start + ANCHOR_LIMIT)
    else:
        end = ids.index(before) if before is not None else len(metas)
        start = max(0, end - PAGE_SIZE)
    automation_id, _ = parse_thread_id(thread_id)
    page = (store.get_run(automation_id, run_id) for run_id in ids[start:end])
    names = {a.id: a.name for a in store.list()}
    return {
        "thread": _summary(thread_id, list(reversed(metas)), names),
        "runs": [record for record in page if record is not None],
        "has_more": start > 0,
        "has_newer": end < len(metas),
    }


def mark_thread_read(store: AutomationStore, thread_id: str, run_ids: list[str]) -> bool:
    """Marks only the given runs read, so a run that arrived after the page was loaded stays unread.
    Returns False when the conversation does not exist."""
    automation_id, _ = parse_thread_id(thread_id)
    runs = {r["id"]: r for r in _thread_meta(store, thread_id)}
    if not runs:
        return False
    store.mark_runs_read(
        automation_id, [run_id for run_id in run_ids if run_id in runs and not runs[run_id].get("read")]
    )
    return True


def hide_thread(store: AutomationStore, thread_id: str, through_run_id: str) -> bool:
    """Hides the conversation from the chat list until a run newer than ``through_run_id`` arrives."""
    if not any(r["id"] == through_run_id for r in _thread_meta(store, thread_id)):
        return False
    store.hide_chat_thread(thread_id, through_run_id)
    return True
