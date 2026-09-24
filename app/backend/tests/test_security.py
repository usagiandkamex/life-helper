from __future__ import annotations

from cryptography.fernet import Fernet

from life_helper.security import (
    MaskingLogFilter,
    SecretMasker,
    TokenVault,
    _my_number_valid,
    detect_sensitive,
    redact_sensitive,
)


def _make_my_number(prefix11: str) -> str:
    for check in range(10):
        if _my_number_valid(prefix11 + str(check)):
            return prefix11 + str(check)
    raise AssertionError("no valid check digit")


def test_vault_roundtrip_and_encryption(tmp_path):
    vault = TokenVault(tmp_path / "t.enc", Fernet.generate_key().decode())
    vault.save("gho_secret", 1, "me")
    assert b"gho_secret" not in (tmp_path / "t.enc").read_bytes()
    assert vault.load() == {"token": "gho_secret", "user_id": 1, "login": "me"}
    vault.clear()
    assert vault.load() is None


def test_vault_with_wrong_key_returns_none(tmp_path):
    TokenVault(tmp_path / "t.enc", Fernet.generate_key().decode()).save("gho_secret", 1, "me")
    assert TokenVault(tmp_path / "t.enc", Fernet.generate_key().decode()).load() is None


def test_masker_masks_nested_values():
    masker = SecretMasker(["apikey-123456", "short"])
    data = {"url": "https://x/?apikey=apikey-123456", "items": ["apikey-123456", 5]}
    assert masker.mask(data) == {"url": "https://x/?apikey=***", "items": ["***", 5]}
    # Values shorter than 6 characters are ignored to avoid masking common words.
    assert masker.mask_text("short") == "short"


def test_my_number_check_digit_known_value():
    # "123456789018" is the widely used sample number with a valid check digit.
    assert _my_number_valid("123456789018")
    assert not _my_number_valid("123456789017")


def test_detects_my_number_with_valid_check_digit():
    number = _make_my_number("12345678901")
    assert "my_number" in detect_sensitive(f"マイナンバーは {number[:4]}-{number[4:8]}-{number[8:]} です")
    wrong = number[:11] + str((int(number[11]) + 1) % 10)
    assert "my_number" not in detect_sensitive(wrong)


def test_detects_card_number_by_luhn():
    assert "card_number" in detect_sensitive("カード 4111 1111 1111 1111")
    assert "card_number" not in detect_sensitive("4111 1111 1111 1112")


def test_detects_account_and_password():
    assert "bank_account" in detect_sensitive("口座番号: 1234567")
    assert "password" in detect_sensitive("パスワードは hunter2")
    assert detect_sensitive("年収は 600 万円、ふるさと納税の上限を知りたい") == []


def test_redact_sensitive():
    text, kinds = redact_sensitive("口座番号: 1234567 と カード 4111 1111 1111 1111")
    assert kinds == ["card_number", "bank_account"]
    assert "1234567" not in text and "4111" not in text


def test_log_masking_filter_masks_messages_and_tracebacks(caplog):
    import logging

    masker = SecretMasker(["gho_secret_token_123"])
    logger = logging.getLogger("test.masking")
    handler = logging.Handler()
    records = []
    handler.emit = records.append  # type: ignore[method-assign]
    handler.addFilter(MaskingLogFilter(masker))
    logger.addHandler(handler)
    try:
        logger.warning("token=%s", "gho_secret_token_123")
        try:
            raise RuntimeError("failed with gho_secret_token_123")
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    formatted = [logging.Formatter().format(r) for r in records]
    assert formatted and all("gho_secret_token_123" not in f for f in formatted)
    assert "***" in formatted[0] and "RuntimeError" in formatted[1]


def test_log_masking_keeps_uvicorn_access_log_args():
    import logging

    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", "/api/x?key=gho_secret_token_123", "1.1", 200),
        None,
    )
    MaskingLogFilter(SecretMasker(["gho_secret_token_123"])).filter(record)
    line = AccessFormatter(fmt="%(client_addr)s %(request_line)s %(status_code)s", use_colors=False).format(record)
    assert "gho_secret_token_123" not in line and "GET /api/x?key=***" in line
