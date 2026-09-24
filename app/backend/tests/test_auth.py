from __future__ import annotations

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends
from httpx import Response
from pydantic import SecretStr, ValidationError

from life_helper.auth import RateLimiter, require_user
from life_helper.config import Settings

from .conftest import ALLOWED_ID, sign_in


def test_production_requires_secrets(tmp_path):
    with pytest.raises(ValidationError):
        Settings(environment="production", data_dir=tmp_path)


def test_production_starts_without_oauth_but_login_reports_it(tmp_path):
    s = Settings(
        environment="production",
        data_dir=tmp_path,
        session_secret="s" * 32,
        token_encryption_key=Fernet.generate_key().decode(),
    )
    assert s.oauth_configured is False


def test_login_not_configured(client, settings):
    settings.github_oauth_client_secret = SecretStr("")
    assert client.get("/api/me").json()["oauth_configured"] is False
    assert client.get("/auth/login", follow_redirects=False).status_code == 503


def test_production_rejects_dev_token(tmp_path):
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            data_dir=tmp_path,
            session_secret="s" * 32,
            token_encryption_key=Fernet.generate_key().decode(),
            github_oauth_client_id="id",
            github_oauth_client_secret="secret",
            dev_github_token="gho_x",
        )


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_me_unauthenticated(client):
    body = client.get("/api/me").json()
    assert body["authenticated"] is False
    assert body["dev_login"] is True


def test_login_redirects_to_github_with_state(client):
    resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=client-id" in location
    assert "state=" in location
    assert "allow_signup=false" in location


def _state_from(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query)["state"][0]


@respx.mock
def test_callback_allowed_user_stores_token(client, ctx):
    state = _state_from(client.get("/auth/login", follow_redirects=False).headers["location"])
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_real_token_1234"})
    )
    respx.get("https://api.github.com/user").mock(
        return_value=Response(200, json={"id": ALLOWED_ID, "login": "usagiandkamex"})
    )
    resp = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 302
    assert ctx.vault.load()["token"] == "gho_real_token_1234"
    me = client.get("/api/me").json()
    assert me["authenticated"] is True and me["login"] == "usagiandkamex"
    # The token must never be exposed to the browser.
    assert "gho_real_token_1234" not in str(me)
    assert "gho_real_token_1234" not in resp.headers.get("set-cookie", "")


@respx.mock
def test_callback_other_user_is_rejected_and_not_stored(client, ctx):
    state = _state_from(client.get("/auth/login", follow_redirects=False).headers["location"])
    respx.post("https://github.com/login/oauth/access_token").mock(
        return_value=Response(200, json={"access_token": "gho_other"})
    )
    respx.get("https://api.github.com/user").mock(return_value=Response(200, json={"id": 1, "login": "someone"}))
    resp = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert resp.status_code == 403
    assert ctx.vault.load() is None
    assert client.get("/api/me").json()["authenticated"] is False


def test_callback_rejects_bad_state(client):
    client.get("/auth/login", follow_redirects=False)
    assert client.get("/auth/callback?code=abc&state=wrong", follow_redirects=False).status_code == 400


def test_csrf_required_for_mutations(app, client, ctx):
    router = APIRouter()

    @router.post("/api/_test")
    def _mutate(user=Depends(require_user)):
        return {"ok": True}

    # Insert before the SPA catch-all route.
    app.router.routes.insert(0, router.routes[0])
    assert client.post("/api/_test").status_code == 401
    csrf = sign_in(client, ctx)
    assert client.post("/api/_test").status_code == 403
    assert client.post("/api/_test", headers={"x-csrf-token": "wrong"}).status_code == 403
    assert (
        client.post("/api/_test", headers={"x-csrf-token": csrf, "origin": "https://evil.example"}).status_code == 403
    )
    assert client.post("/api/_test", headers={"x-csrf-token": csrf, "origin": "http://testserver"}).json() == {
        "ok": True
    }


def test_logout_clears_session(client, ctx):
    csrf = sign_in(client, ctx)
    assert client.post("/auth/logout", headers={"x-csrf-token": csrf}).status_code == 200
    assert client.get("/api/me").json()["authenticated"] is False


def test_logout_revokes_copied_cookies(client, ctx):
    csrf = sign_in(client, ctx)
    copied = dict(client.cookies)
    assert client.post("/auth/logout", headers={"x-csrf-token": csrf}).status_code == 200
    client.cookies.clear()
    client.cookies.update(copied)
    # A cookie issued before logout must no longer work.
    assert client.get("/api/files").status_code == 401
    assert client.get("/api/me").json()["authenticated"] is False


def test_rate_limiter():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.check("a") and limiter.check("a")
    assert not limiter.check("a")
    assert limiter.check("b")


def test_security_headers_and_spa(client):
    resp = client.get("/some/page")
    assert resp.text == "<html>app</html>"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert client.get("/api/unknown").status_code == 404
