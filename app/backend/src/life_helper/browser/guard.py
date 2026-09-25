"""Checks for text the model types into web forms."""

from __future__ import annotations

from typing import Any

from ..security import SENSITIVE_LABELS, SecretMasker, detect_sensitive

MAX_FILL_CHARS = 500
FILLABLE_TAGS = ("input", "textarea", "select")
BLOCKED_INPUT_TYPES = ("password", "file", "hidden")
# Card, password and one-time-code fields (autocomplete tokens such as cc-number, new-password, one-time-code).
BLOCKED_AUTOCOMPLETE = ("cc-", "password", "one-time-code")


def fill_rejection(value: str, attrs: dict[str, Any], masker: SecretMasker) -> str | None:
    """``attrs`` describes the target element: tag, type, autocomplete, editable, form_has_password."""
    if len(value) > MAX_FILL_CHARS:
        return f"入力できるのは {MAX_FILL_CHARS} 文字までです"
    tag = str(attrs.get("tag", "")).lower()
    if tag not in FILLABLE_TAGS and not attrs.get("editable"):
        return "入力欄ではありません"
    if tag == "input" and str(attrs.get("type", "")).lower() in BLOCKED_INPUT_TYPES:
        return "パスワード・ファイル・非表示の欄には入力できません"
    autocomplete = str(attrs.get("autocomplete", "")).lower()
    if any(token in autocomplete for token in BLOCKED_AUTOCOMPLETE):
        return "カード情報・パスワード・確認コードの欄には入力できません"
    if attrs.get("form_has_password"):
        return "ログインや会員登録のフォームには入力できません"
    kinds = detect_sensitive(value)
    if kinds:
        labels = "、".join(SENSITIVE_LABELS[k] for k in kinds)
        return f"機微情報（{labels}）は入力できません"
    if masker.contains_secret(value):
        return "秘密情報は入力できません"
    return None
