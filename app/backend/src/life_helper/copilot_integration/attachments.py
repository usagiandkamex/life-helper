"""Files and images attached to a chat message: short-term input for that conversation only.

Images go to Copilot as blob attachments; the runtime keeps them in the conversation's session state, so they are
deleted with the conversation. Text and PDF files are read here and their text is appended to the prompt, so every
model can use them. Nothing is written to the knowledge base.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..knowledge.store import normalize_filename, pdf_pages

MAX_ATTACHMENTS = 5
# All attached text together; the model is told where it was cut. It stays in the conversation's context.
MAX_TEXT_CHARS = 30_000
MAX_PDF_PAGES = 100
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".tsv", ".json")
SUPPORTED = "画像（PNG・JPEG・GIF・WebP）・テキスト（.txt・.md・.csv・.tsv・.json）・PDF"
# Sent (and shown in the history) when a message has attachments but no text.
ATTACHMENT_ONLY_PROMPT = "添付したファイルを見てください。"
TRUNCATED_NOTE = "…（文字数の上限を超えたため、ここから先は省略しました）"

# The file text is HTML-escaped, so a block body never contains "<" and cannot close the tag early.
_BLOCK = r'\n\n<attached_file name="([^"<>]*)"( truncated="true")?>\n([^<]*)\n</attached_file>'
BLOCK_RE = re.compile(_BLOCK)
BLOCKS_RE = re.compile(rf"(?:{_BLOCK})+")


class AttachmentError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PreparedAttachments:
    blobs: list[dict] = field(default_factory=list)  # SDK blob attachments (images)
    text: str = ""  # <attached_file> blocks appended to the prompt
    raw_text: str = ""  # the file names and extracted text before escaping, for the sensitive-data check
    # {name, kind, index, truncated?} shown in the chat, images first; index: the position in the request
    items: list[dict] = field(default_factory=list)


def image_type(data: bytes) -> str | None:
    """The MIME type from the file's first bytes; the name or the browser's type is not trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare_attachments(items: list[tuple[str, str]], *, max_bytes: int) -> PreparedAttachments:
    """Checks and converts ``(file name, base64 data)`` pairs. Raises AttachmentError with an HTTP status."""
    if len(items) > MAX_ATTACHMENTS:
        raise AttachmentError(400, f"添付できるのは {MAX_ATTACHMENTS} 件までです")
    too_large = AttachmentError(413, f"添付ファイルは合計 {max_bytes // (1024 * 1024)} MB までです")
    # Base64 is 4/3 of the data (up to 2 padding bytes less): reject before decoding anything.
    if sum(len(data) * 3 // 4 - 2 for _, data in items) > max_bytes:
        raise too_large
    result = PreparedAttachments()
    files: list[dict] = []
    raw: list[str] = []
    blocks: list[str] = []
    budget, total = MAX_TEXT_CHARS, 0
    for index, (raw_name, encoded) in enumerate(items):
        name = normalize_filename(raw_name)
        # The name goes to Copilot too (as the image's name or the block's attribute).
        raw += [raw_name, name]
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as e:
            raise AttachmentError(400, f"{name} を読み込めませんでした") from e
        total += len(data)
        if total > max_bytes:
            raise too_large
        if not data:
            raise AttachmentError(400, f"{name} は空のファイルです")
        mime = image_type(data)
        suffix = Path(name).suffix
        if mime:
            result.blobs.append({"type": "blob", "data": encoded, "mimeType": mime, "displayName": name})
            result.items.append({"name": name, "kind": "image", "index": index})
            continue
        if suffix in IMAGE_SUFFIXES:
            raise AttachmentError(400, f"{name} は画像として読み込めません（PNG・JPEG・GIF・WebP に対応しています）")
        if suffix == ".pdf":
            if not data.startswith(b"%PDF"):
                raise AttachmentError(400, f"{name} は PDF として読み込めません")
            text, cut = _pdf_text(data, name, budget)
        elif suffix in TEXT_SUFFIXES:
            text, cut = _decode_text(data, name), False
        else:
            raise AttachmentError(400, f"{name} は添付できない形式です。{SUPPORTED}に対応しています")
        text = text.replace("\r\n", "\n").strip()
        if len(text) > budget:
            text, cut = text[:budget], True
        budget -= len(text)
        raw.append(text)
        body = "\n".join(part for part in (html.escape(text, quote=False), TRUNCATED_NOTE if cut else "") if part)
        truncated = ' truncated="true"' if cut else ""
        blocks.append(f'\n\n<attached_file name="{html.escape(name)}"{truncated}>\n{body}\n</attached_file>')
        files.append({"name": name, "kind": "file", "index": index, **({"truncated": True} if cut else {})})
    result.text = "".join(blocks)
    # Blank lines between the parts: a number split across two of them is not taken as one.
    result.raw_text = "\n\n".join(raw)
    result.items += files
    return result


def attached_files(content: str, transformed: str | None) -> list[dict]:
    """The file blocks ``prepare_attachments`` appended, read from the model-facing copy of the message.

    The message is sent with the typed text as its display prompt, so the runtime stores that text as ``content``
    and the prompt the model saw as ``transformed_content``: the blocks are only read from the part that follows
    the typed text, and the text itself is never parsed or cut. A message that merely ends with the same syntax
    is therefore kept as it was typed. When that relation cannot be shown (an older message, or text that occurs
    more than once in the transformed copy), no files are reported instead of guessing.
    """
    if not content or not transformed:
        return []
    start = transformed.find(content)
    if start < 0 or transformed.find(content, start + 1) >= 0:
        return []
    match = BLOCKS_RE.match(transformed, start + len(content))
    if match is None:
        return []
    return [
        {"name": html.unescape(m.group(1)), "kind": "file", **({"truncated": True} if m.group(2) else {})}
        for m in BLOCK_RE.finditer(match.group(0))
    ]


def _decode_text(data: bytes, name: str) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # Excel's "Unicode テキスト"
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    elif b"\x00" not in data:
        for encoding in ("utf-8-sig", "cp932"):  # cp932: CSV files from Japanese banks and Excel
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
    raise AttachmentError(400, f"{name} をテキストとして読み込めません（UTF-8 か Shift_JIS で保存してください）")


def _pdf_text(data: bytes, name: str, budget: int) -> tuple[str, bool]:
    """Reads pages only until the text budget or the page limit is reached. Returns (text, cut).

    A page is read only when the iterator is asked for it, so the limits are checked before that: no page past
    them is ever extracted. A file with exactly ``MAX_PDF_PAGES`` pages is reported as cut, which is the safe
    way round (the model is told that something may be missing).
    """
    parts: list[str] = []
    size = 0
    try:
        pages = pdf_pages(data, max_pages=MAX_PDF_PAGES)
        while size < budget and len(parts) < MAX_PDF_PAGES:
            page = next(pages, None)
            if page is None:
                return "\n".join(parts), False
            parts.append(page)
            size += len(page) + 1
    except Exception as e:  # noqa: BLE001 - pypdf raises many exception types for broken or locked files
        raise AttachmentError(400, f"{name} を読み込めませんでした（パスワード付きや壊れた PDF は読めません）") from e
    return "\n".join(parts), True
