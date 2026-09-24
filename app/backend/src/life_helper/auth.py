"""GitHub OAuth sign-in, session handling, CSRF protection and rate limiting."""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from .context import AppContext, get_ctx

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_USER_URL = "https://api.github.com/user"


class RateLimiter:
    """Small in-memory sliding-window limiter (single replica, so no shared store needed)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def _client_ip(request: Request) -> str:
    # ACA's ingress appends the real client address, so the right-most entry is the trustworthy one.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request, ctx: AppContext = Depends(get_ctx)) -> None:
    limiter = ctx.extras.setdefault("login_limiter", RateLimiter(limit=10, window_seconds=300))
    if not limiter.check(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many sign-in attempts")


@dataclass
class CurrentUser:
    user_id: int
    login: str


def require_user(request: Request, ctx: AppContext = Depends(get_ctx)) -> CurrentUser:
    """Authenticates the request and enforces CSRF protection on state-changing methods."""
    session = request.session
    uid = session.get("uid")
    if uid is None or uid != ctx.settings.allowed_github_user_id or session.get("gen") != _generation(ctx):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign-in required")
    if request.method not in SAFE_METHODS:
        _check_csrf(request, ctx)
    return CurrentUser(user_id=uid, login=session.get("login", ""))


def _generation_path(ctx: AppContext):
    return ctx.settings.app_state_dir / "session-generation.txt"


def _generation(ctx: AppContext) -> int:
    """Server-side session generation: bumping it on logout revokes every previously issued cookie."""
    try:
        return int(_generation_path(ctx).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_generation(ctx: AppContext) -> None:
    from .knowledge.store import atomic_write

    atomic_write(_generation_path(ctx), str(_generation(ctx) + 1))


def _check_csrf(request: Request, ctx: AppContext) -> None:
    expected = request.session.get("csrf", "")
    provided = request.headers.get("x-csrf-token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != _origin_of(ctx.settings.base_url):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin request rejected")


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def fetch_github_user(token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "GitHub token is not valid")
    return resp.json()


def _start_session(request: Request, user: dict, ctx: AppContext) -> None:
    request.session.clear()
    request.session.update(
        {
            "uid": user["id"],
            "login": user.get("login", ""),
            "csrf": secrets.token_urlsafe(32),
            "iat": int(time.time()),
            "gen": _generation(ctx),
        }
    )


def _forbidden_page() -> HTMLResponse:
    return HTMLResponse(
        "<h1>アクセスできません</h1><p>このアプリは許可された GitHub アカウントだけが利用できます。</p>",
        status_code=status.HTTP_403_FORBIDDEN,
    )


router = APIRouter()


@router.get("/auth/login", dependencies=[Depends(_rate_limit)])
async def login(request: Request, ctx: AppContext = Depends(get_ctx)) -> RedirectResponse:
    if not ctx.settings.oauth_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub OAuth is not configured")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = {
        "client_id": ctx.settings.github_oauth_client_id,
        "redirect_uri": f"{ctx.settings.base_url.rstrip('/')}/auth/callback",
        "scope": ctx.settings.github_oauth_scopes,
        "state": state,
        "allow_signup": "false",
    }
    return RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)


@router.get("/auth/callback", dependencies=[Depends(_rate_limit)])
async def callback(request: Request, code: str = "", state: str = "", ctx: AppContext = Depends(get_ctx)):
    expected = request.session.pop("oauth_state", None)
    if not expected or not state or not hmac.compare_digest(expected, state) or not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid OAuth state")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": ctx.settings.github_oauth_client_id,
                "client_secret": ctx.settings.github_oauth_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": f"{ctx.settings.base_url.rstrip('/')}/auth/callback",
            },
        )
    token = resp.json().get("access_token") if resp.status_code == 200 else None
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "GitHub did not return an access token")
    user = await fetch_github_user(token)
    if user.get("id") != ctx.settings.allowed_github_user_id:
        # The token belongs to someone else: discard it without storing anything.
        request.session.clear()
        return _forbidden_page()
    ctx.vault.save(token, user["id"], user.get("login", ""))
    await ctx.on_token_changed()
    _start_session(request, user, ctx)
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.post("/auth/dev-login", dependencies=[Depends(_rate_limit)])
async def dev_login(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    token = ctx.settings.dev_github_token.get_secret_value()
    if not ctx.settings.is_dev or not token:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = await fetch_github_user(token)
    if user.get("id") != ctx.settings.allowed_github_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this GitHub account is not allowed")
    ctx.vault.save(token, user["id"], user.get("login", ""))
    await ctx.on_token_changed()
    _start_session(request, user, ctx)
    return {"ok": True}


@router.post("/auth/logout")
async def logout(
    request: Request, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    # Revoke every issued cookie (including copies), not only the one in this browser.
    _bump_generation(ctx)
    request.session.clear()
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    uid = request.session.get("uid")
    if uid is None or uid != ctx.settings.allowed_github_user_id or request.session.get("gen") != _generation(ctx):
        return {
            "authenticated": False,
            "dev_login": ctx.settings.is_dev and bool(ctx.settings.dev_github_token.get_secret_value()),
            "oauth_configured": ctx.settings.oauth_configured,
        }
    stored = ctx.vault.load()
    return {
        "authenticated": True,
        "login": request.session.get("login", ""),
        "csrf_token": request.session.get("csrf", ""),
        "token_available": bool(stored and stored.get("token")),
        "environment": ctx.settings.environment,
    }
