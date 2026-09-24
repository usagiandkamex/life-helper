"""Encryption, secret masking and sensitive-data detection helpers."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

MASK = "***"


class TokenVault:
    """Stores the signed-in user's GitHub token encrypted on the shared data volume.

    The scheduled job cannot read browser cookies, so the token lives here (encrypted with a key that
    exists only as an ACA secret) and both the web app and the job read it.
    """

    def __init__(self, path: Path, key: str) -> None:
        self._path = path
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def save(self, token: str, user_id: int, login: str) -> None:
        if not self._fernet:
            raise RuntimeError("token encryption key is not configured")
        payload = json.dumps({"token": token, "user_id": user_id, "login": login}).encode()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(self._fernet.encrypt(payload))
        os.replace(tmp, self._path)

    def load(self) -> dict[str, Any] | None:
        if not self._fernet or not self._path.exists():
            return None
        try:
            return json.loads(self._fernet.decrypt(self._path.read_bytes()))
        except (InvalidToken, ValueError):
            return None

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class SecretMasker:
    """Replaces known secret values (API keys, tokens) with ``***`` in arbitrary data."""

    def __init__(self, secrets: list[str]) -> None:
        # Longest first so overlapping values are fully masked.
        self._secrets = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)

    def add(self, secret: str) -> None:
        if len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def mask_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def mask(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, dict):
            return {k: self.mask(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.mask(v) for v in value]
        return value

    def contains_secret(self, text: str) -> bool:
        return any(secret in text for secret in self._secrets)


class MaskingLogFilter(logging.Filter):
    """Masks secrets in every log record, including exception tracebacks (defence in depth for Azure logs)."""

    def __init__(self, masker: SecretMasker) -> None:
        super().__init__()
        self.masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        # Keep the msg/args structure: some formatters (e.g. uvicorn's access log) unpack record.args.
        if isinstance(record.msg, str):
            record.msg = self.masker.mask_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self.masker.mask(a) if isinstance(a, (str, dict, list)) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = self.masker.mask(record.args)
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self.masker.mask_text(text)
            record.exc_info = None
        return True


def install_log_masking(masker: SecretMasker) -> None:
    flt = MaskingLogFilter(masker)
    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    ]
    for lg in loggers:
        for handler in lg.handlers:
            if not any(isinstance(f, MaskingLogFilter) for f in handler.filters):
                handler.addFilter(flt)


_MY_NUMBER_RE = re.compile(r"(?<!\d)(?<!\d[\s-])(\d{4}[\s-]?\d{4}[\s-]?\d{4})(?![\s-]?\d)")
_CARD_RE = re.compile(r"(?<!\d)((?:\d[\s-]?){13,19})(?!\d)")
_ACCOUNT_RE = re.compile(r"(口座番号|口座\s*No\.?|account\s*(?:number|no\.?))\s*[:：]?\s*\d{6,8}", re.IGNORECASE)
_PASSWORD_RE = re.compile(r"(パスワード|暗証番号|password|passcode|pin)\s*(?:は|[:：=])\s*\S+", re.IGNORECASE)


def _my_number_valid(digits: str) -> bool:
    """Checks the Japanese Individual Number (My Number) check digit."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    body = [int(c) for c in digits[:11]][::-1]
    total = sum(d * (n + 1 if n <= 6 else n - 5) for n, d in enumerate(body, start=1))
    remainder = total % 11
    check = 0 if remainder <= 1 else 11 - remainder
    return check == int(digits[11])


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_sensitive(text: str) -> list[str]:
    """Returns the kinds of sensitive data found in ``text`` (empty list when none)."""
    return sorted({kind for kind, _start, _end in _sensitive_spans(text)}, key=_ORDER.index)


_ORDER = ["my_number", "card_number", "bank_account", "password"]


def _sensitive_spans(text: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    for m in _MY_NUMBER_RE.finditer(text):
        if _my_number_valid(re.sub(r"\D", "", m.group(1))):
            spans.append(("my_number", m.start(1), m.end(1)))
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits) and not _my_number_valid(digits):
            spans.append(("card_number", m.start(1), m.end(1)))
    for m in _ACCOUNT_RE.finditer(text):
        spans.append(("bank_account", m.start(), m.end()))
    for m in _PASSWORD_RE.finditer(text):
        spans.append(("password", m.start(), m.end()))
    return spans


def redact_sensitive(text: str) -> tuple[str, list[str]]:
    """Replaces detected sensitive values with a placeholder. Returns the new text and the kinds found."""
    spans = _sensitive_spans(text)
    if not spans:
        return text, []
    out, last = [], 0
    for _kind, start, end in sorted(spans, key=lambda s: s[1]):
        if start < last:
            continue
        out.append(text[last:start])
        out.append("［機微情報のため削除］")
        last = end
    out.append(text[last:])
    return "".join(out), detect_sensitive(text)


SENSITIVE_LABELS = {
    "my_number": "マイナンバー",
    "card_number": "カード番号",
    "bank_account": "口座番号",
    "password": "パスワード・暗証番号",
}
