from __future__ import annotations

import ipaddress

import pytest

from life_helper import netguard
from life_helper.netguard import literal_ip, outbound_rejection, resolved_rejection, url_rejection
from life_helper.security import SecretMasker


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("0", "0.0.0.0"),  # noqa: S104
        ("::1", "::1"),
        ("[::ffff:127.0.0.1]", "::ffff:7f00:1"),
        ("fe80::1%eth0", "fe80::1"),
    ],
)
def test_literal_ip_parses_browser_style_forms(host, expected):
    assert literal_ip(host) == ipaddress.ip_address(expected)


@pytest.mark.parametrize("host", ["example.com", "123.example", "1..1", "256.256.256.256.1", "09.1.1.1"])
def test_literal_ip_ignores_host_names(host):
    assert literal_ip(host) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://example.com/path?q=ふるさと納税",
        "https://www.nta.go.jp/",
        "https://93.184.215.14/",
        "https://[64:ff9b::5db8:d70e]/",
        "https://EXAMPLE.com./",
    ],
)
def test_public_http_and_https_are_allowed(url):
    assert url_rejection(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://app.localhost:8000/",
        "http://127.0.0.1:8000/healthz",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0.0.0.0/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://169.254.169.254/metadata/identity",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::7f00:1]/",
        "http://[64:ff9b::a00:1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://224.0.0.1/",
    ],
)
def test_internal_addresses_are_rejected(url):
    assert "内部のアドレス" in (url_rejection(url) or "")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https:///nohost",
        "https://user:pass@example.com/",
        "https://example.com:99999/",
    ],
)
def test_other_schemes_and_malformed_urls_are_rejected(url):
    assert url_rejection(url) is not None


def test_connector_hosts_are_rejected():
    assert "コネクタ" in (url_rejection("https://openapi.rakuten.co.jp/engine/api?applicationId=x") or "")
    assert "コネクタ" in (url_rejection("https://api.github.com/user") or "")


def test_sensitive_data_and_secrets_in_urls_are_rejected():
    masker = SecretMasker(["SUPERSECRETKEY"])
    card = url_rejection("https://example.com/?q=4111%201111%201111%201111")
    assert card is not None and "カード番号" in card and "4111" not in card
    assert url_rejection("https://example.com/?password=hunter2") is not None
    assert url_rejection("https://example.com/?k=SUPERSECRETKEY", masker) is not None
    assert url_rejection("https://example.com/?k=SUPER%53ECRETKEY", masker) is not None
    assert url_rejection("https://example.com/?k=other", masker) is None


async def test_resolved_rejection_checks_every_answer_and_caches(fake_dns, monkeypatch):
    fake_dns["mixed.example.com"] = ["93.184.215.14", "10.0.0.1"]
    fake_dns["mapped.example.com"] = ["::ffff:192.168.0.1"]
    fake_dns["gone.example.com"] = []
    assert "内部のアドレス" in (await resolved_rejection("https://mixed.example.com/") or "")
    assert "内部のアドレス" in (await resolved_rejection("https://mapped.example.com/") or "")
    assert "名前解決" in (await resolved_rejection("https://gone.example.com/") or "")
    assert await resolved_rejection("https://public.example.com/") is None
    # Literal addresses are judged by url_rejection alone, without a lookup.
    assert await resolved_rejection("http://127.0.0.1/") is None

    calls: list[str] = []

    async def counting(host: str) -> list[str]:
        calls.append(host)
        return ["93.184.215.14"]

    netguard.clear_dns_cache()
    monkeypatch.setattr(netguard, "_lookup", counting)
    await resolved_rejection("https://cached.example.com/a")
    await resolved_rejection("https://cached.example.com/b")
    assert calls == ["cached.example.com"]


async def test_outbound_rejection_combines_both_checks(fake_dns):
    fake_dns["internal.example.com"] = ["192.168.10.10"]
    assert await outbound_rejection("https://example.com/") is None
    assert await outbound_rejection("http://localhost/") is not None
    assert await outbound_rejection("https://internal.example.com/") is not None
