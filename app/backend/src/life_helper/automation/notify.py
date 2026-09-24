"""GitHub Issue notifications through a GitHub App owned by usagiandkamex.

A user token cannot be used: issues created by yourself do not notify you. The App has only the Issues write
permission and is installed on the private notification repository only.
"""

from __future__ import annotations

import logging
import re
import time

import httpx
import jwt

from ..security import SecretMasker

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


class NotifyError(RuntimeError):
    pass


class GitHubNotifier:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        installation_id: str,
        repo: str,
        mention: str,
        masker: SecretMasker,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key.replace("\\n", "\n")
        self.installation_id = installation_id
        self.repo = repo
        self.mention = mention
        self.masker = masker
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(
            self.app_id
            and self.private_key
            and self.installation_id
            and re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo or "")
        )

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    async def create_issue(self, title: str, body: str) -> str:
        if not self.configured:
            raise NotifyError("GitHub 通知が設定されていません")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                token_resp = await client.post(
                    f"{GITHUB_API}/app/installations/{self.installation_id}/access_tokens",
                    headers=headers | {"Authorization": f"Bearer {self._app_jwt()}"},
                )
                if token_resp.status_code != 201:
                    raise NotifyError(f"GitHub App のトークンを取得できませんでした（HTTP {token_resp.status_code}）")
                token = token_resp.json()["token"]
                self.masker.add(token)
                resp = await client.post(
                    f"{GITHUB_API}/repos/{self.repo}/issues",
                    headers=headers | {"Authorization": f"Bearer {token}"},
                    json={"title": self.masker.mask_text(title)[:250], "body": self.masker.mask_text(body)},
                )
        except httpx.HTTPError as e:
            logger.warning("GitHub notification failed: %s", type(e).__name__)
            raise NotifyError("GitHub に接続できませんでした") from None
        if resp.status_code != 201:
            raise NotifyError(f"Issue を作成できませんでした（HTTP {resp.status_code}）")
        return resp.json().get("html_url", "")
