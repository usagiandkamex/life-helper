from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from life_helper.config import Settings
from life_helper.context import build_context
from life_helper.main import create_app

ALLOWED_ID = 134019422


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>app</html>", encoding="utf-8")
    return Settings(
        environment="development",
        base_url="http://testserver",
        data_dir=tmp_path / "data",
        static_dir=static,
        allowed_github_user_id=ALLOWED_ID,
        github_oauth_client_id="client-id",
        github_oauth_client_secret="client-secret-value",
        session_secret="test-session-secret-0123456789",
        token_encryption_key=Fernet.generate_key().decode(),
        dev_github_token="gho_devtoken_example",
    )


@pytest.fixture
def ctx(settings: Settings):
    return build_context(settings)


@pytest.fixture
def app(settings: Settings, ctx):
    return create_app(settings, ctx)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def sign_in(client: TestClient, ctx, login: str = "usagiandkamex") -> str:
    """Signs in through the dev-login endpoint with a mocked GitHub user and returns the CSRF token."""
    import respx
    from httpx import Response

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"id": ALLOWED_ID, "login": login})
        )
        resp = client.post("/auth/dev-login")
    assert resp.status_code == 200, resp.text
    return client.get("/api/me").json()["csrf_token"]


def stooq_csv(close: float, day: str = "2026-09-24") -> str:
    return f"Date,Open,High,Low,Close,Volume\n{day},1,1,1,{close},1\n"


def mock_stooq(bodies: dict[str, float | str]):
    """Mocks the Stooq CSV endpoint per symbol. Unknown symbols answer "No data", like Stooq does."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies.get(request.url.params["s"])
        if body is None:
            return httpx.Response(200, text="No data")
        return httpx.Response(200, text=body if isinstance(body, str) else stooq_csv(body))

    return respx.get("https://stooq.com/q/d/l/").mock(side_effect=handler)
