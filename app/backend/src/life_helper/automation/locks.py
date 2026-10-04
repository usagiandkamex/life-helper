"""File-based lock with expiry, safe to use from the web app and the scheduled job on the shared volume."""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path


class FileLock:
    def __init__(self, path: Path, ttl_seconds: int, data: dict | None = None) -> None:
        self.path = path
        self.ttl = ttl_seconds
        self.token = uuid.uuid4().hex
        self.acquired = False
        # Extra fields written into the lock file (for locks that also carry a small payload).
        self.data = data or {}

    def _write_new(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            lock = {"token": self.token, "host": socket.gethostname(), "expires_at": time.time() + self.ttl}
            json.dump(self.data | lock, f)
        return True

    def _expired(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return float(data.get("expires_at", 0)) < time.time()
        except (OSError, ValueError):
            # A lock file that cannot be read is treated as stale only after the TTL based on its mtime.
            try:
                return self.path.stat().st_mtime + self.ttl < time.time()
            except OSError:
                return True

    def try_acquire(self) -> bool:
        if self._write_new():
            self.acquired = True
            return True
        if self._expired():
            # Move the stale lock aside atomically so only one contender can take it over.
            stale = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
            try:
                os.replace(self.path, stale)
                stale.unlink(missing_ok=True)
            except OSError:
                return False
            if self._write_new():
                self.acquired = True
                return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self.acquired = False

    def __enter__(self) -> FileLock:
        if not self.try_acquire():
            raise TimeoutError("lock is held by another process")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


async def wait_acquire(lock: FileLock, seconds: float) -> bool:
    """Tries to take ``lock`` for up to ``seconds`` without blocking the event loop."""
    import asyncio

    for _ in range(max(1, int(seconds * 4))):
        if lock.try_acquire():
            return True
        await asyncio.sleep(0.25)
    return False
