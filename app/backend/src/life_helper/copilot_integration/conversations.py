"""Conversation metadata (titles, model). The conversation content itself lives in Copilot's session state."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..knowledge.store import atomic_write


@dataclass
class Conversation:
    id: str
    title: str
    model: str
    created_at: str
    updated_at: str
    started: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ConversationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Conversation]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {c["id"]: Conversation(**c) for c in raw}

    def _save(self, items: dict[str, Conversation]) -> None:
        atomic_write(self.path, json.dumps([asdict(c) for c in items.values()], ensure_ascii=False, indent=1))

    def list(self) -> list[Conversation]:
        with self._lock:
            return sorted(self._load().values(), key=lambda c: c.updated_at, reverse=True)

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._load().get(conversation_id)

    def create(self, title: str, model: str) -> Conversation:
        with self._lock:
            items = self._load()
            now = _now()
            conv = Conversation(
                id=uuid.uuid4().hex, title=title or "新しい会話", model=model, created_at=now, updated_at=now
            )
            items[conv.id] = conv
            self._save(items)
            return conv

    def update(self, conversation_id: str, **fields) -> Conversation | None:
        with self._lock:
            items = self._load()
            conv = items.get(conversation_id)
            if conv is None:
                return None
            for key, value in fields.items():
                if key in {"title", "model", "started"} and value is not None:
                    setattr(conv, key, value)
            conv.updated_at = _now()
            self._save(items)
            return conv

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            items = self._load()
            if items.pop(conversation_id, None) is None:
                return False
            self._save(items)
            return True

    def delete_if(self, conversation_id: str, predicate: Callable[[Conversation], bool]) -> Conversation | None:
        """Deletes the conversation only while ``predicate`` holds for it, checked under the same lock as updates
        (so one renamed in the meantime is kept). Returns the deleted conversation."""
        with self._lock:
            items = self._load()
            conv = items.get(conversation_id)
            if conv is None or not predicate(conv):
                return None
            del items[conversation_id]
            self._save(items)
            return conv

    def ids_if_readable(self) -> set[str] | None:
        """Every conversation id, or None when the list exists but cannot be read (unlike ``list``, which shows an
        unreadable list as empty): nothing may be treated as having no conversation then."""
        with self._lock:
            if not self.path.exists():
                return set()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return {c["id"] for c in raw}
            except (OSError, ValueError, TypeError, KeyError):
                return None
