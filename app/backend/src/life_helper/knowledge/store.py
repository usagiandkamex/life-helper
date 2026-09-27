"""Markdown knowledge base stored on the shared data volume."""

from __future__ import annotations

import io
import os
import re
import shutil
import unicodedata
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

# Top-level areas the user may edit from the UI. ``money/`` is managed through the portfolio API.
USER_WRITABLE = ("profile", "memories", "notes", "plans", "docs")
USER_WRITABLE_FILES = ("INDEX.md",)
EDITABLE_SUFFIXES = (".md", ".txt")
UPLOAD_SUFFIXES = (".md", ".txt", ".pdf")
SEED_DIRS = ("profile", "memories", "notes", "plans", "money", "money/prices", "docs")


class KnowledgePathError(ValueError):
    pass


@dataclass
class FileEntry:
    path: str
    size: int
    modified: str
    writable: bool


def normalize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", PurePosixPath(name.replace("\\", "/")).name).strip()
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    stem = re.sub(r"[^\w\-ぁ-んァ-ヶー一-龠々]+", "_", stem).strip("._") or "file"
    suffix = re.sub(r"[^A-Za-z0-9]", "", suffix).lower()
    return f"{stem[:80]}.{suffix}" if suffix else stem[:80]


class KnowledgeStore:
    def __init__(self, root: Path, seed_dir: Path | None = None) -> None:
        self.root = root
        self.seed_dir = seed_dir

    def ensure_seeded(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for d in SEED_DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if self.seed_dir and self.seed_dir.exists():
            for src in self.seed_dir.rglob("*"):
                if src.is_file():
                    dest = self.root / src.relative_to(self.seed_dir)
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dest)

    # -- path safety -------------------------------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Resolves a user-supplied relative path, rejecting anything that escapes the knowledge root."""
        if not rel or "\x00" in rel:
            raise KnowledgePathError("path is required")
        pure = PurePosixPath(rel.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", rel):
            raise KnowledgePathError("path must be relative to the knowledge base")
        root = self.root.resolve()
        target = (root / Path(*pure.parts)).resolve()
        if target != root and not target.is_relative_to(root):
            raise KnowledgePathError("path escapes the knowledge base")
        return target

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def is_user_writable(self, rel: str) -> bool:
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if not parts or ".." in parts:
            return False
        if len(parts) == 1:
            return parts[0] in USER_WRITABLE_FILES
        return parts[0] in USER_WRITABLE and rel.lower().endswith(EDITABLE_SUFFIXES)

    # -- operations --------------------------------------------------------------------------------------

    def list_files(self) -> list[FileEntry]:
        entries: list[FileEntry] = []
        root = self.root.resolve()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith("money/prices/"):
                continue
            stat = path.stat()
            entries.append(
                FileEntry(
                    path=rel,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    writable=self.is_user_writable(rel),
                )
            )
        return entries

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path.read_text(encoding="utf-8", errors="replace")

    def write_text(self, rel: str, content: str) -> str:
        if not self.is_user_writable(rel):
            raise KnowledgePathError("this location cannot be edited")
        path = self.resolve(rel)
        atomic_write(path, content)
        return self.relative(path)

    def delete(self, rel: str) -> None:
        if not self.is_user_writable(rel):
            raise KnowledgePathError("this location cannot be deleted")
        path = self.resolve(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        path.unlink()

    def save_upload(self, filename: str, markdown: str) -> str:
        stem = Path(normalize_filename(filename)).stem
        rel = f"docs/{stem}.md"
        counter = 2
        while self.resolve(rel).exists():
            rel = f"docs/{stem}-{counter}.md"
            counter += 1
        atomic_write(self.resolve(rel), markdown)
        return rel

    def export_zip(self) -> bytes:
        buf = io.BytesIO()
        root = self.root.resolve()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink() and not path.name.startswith("."):
                    zf.write(path, arcname=path.relative_to(root).as_posix())
        return buf.getvalue()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: the web app and the scheduled job may write the same target concurrently.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def convert_upload(filename: str, data: bytes) -> str:
    """Converts an uploaded md/txt/pdf file to Markdown text (the original file is not kept)."""
    safe = normalize_filename(filename)
    suffix = Path(safe).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise KnowledgePathError("only .md, .txt and .pdf files can be uploaded")
    title = Path(safe).stem
    header = f"# {title}\n\n> アップロード元: {safe}（{datetime.now(UTC).date().isoformat()} 取り込み）\n\n"
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise KnowledgePathError("the file is not a valid PDF")
        return header + _pdf_to_markdown(data)
    text = data.decode("utf-8-sig", errors="replace")
    if suffix == ".md":
        return text if text.lstrip().startswith("#") else header + text
    return header + text


def _pdf_to_markdown(data: bytes) -> str:
    return "\n".join(pdf_pages(data))


def pdf_pages(data: bytes, *, max_pages: int | None = None) -> Iterator[str]:
    """Yields each page of a PDF as a Markdown section; pages are read one at a time, so a caller may stop early.

    Text is extracted only for the pages that are asked for, and never past ``max_pages``.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    if max_pages is not None:
        page_count = min(page_count, max_pages)
    for i in range(page_count):
        page = reader.pages[i]
        text = (page.extract_text() or "").strip()
        yield f"## ページ {i + 1}\n\n{text or '（テキストを抽出できませんでした。スキャン画像の PDF は非対応です）'}\n"
