"""Starts the automation job on demand, so 「今すぐ実行」 runs in the job rather than in the web app.

The web app scales in to zero once nobody uses it for a while, which stops a run in progress; a job execution runs
until the run is over. The app signs in to Azure Resource Manager with its managed identity (Container Apps provides
the token endpoint) and asks it to start the job, which then runs the requests left for it (see
AutomationStore.add_run_request).
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..context import AppContext

ARM_ENDPOINT = "https://management.azure.com"
JOBS_API_VERSION = "2024-03-01"
IDENTITY_API_VERSION = "2019-08-01"
REQUEST_TIMEOUT_SECONDS = 20
# A token is used until shortly before it expires.
TOKEN_MARGIN_SECONDS = 5 * 60


class JobStartError(RuntimeError):
    """The job could not be started (the reason never contains the token)."""


class JobStarter:
    def __init__(
        self,
        job_id: str,
        *,
        client_id: str = "",
        identity_endpoint: str = "",
        identity_header: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.job_id = job_id
        self.client_id = client_id
        self.identity_endpoint = identity_endpoint
        self.identity_header = identity_header
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.job_id)

    async def start(self) -> None:
        """Starts one execution of the job; raises JobStartError when Azure does not accept it."""
        if not (self.identity_endpoint and self.identity_header):
            raise JobStartError("the managed identity endpoint is not available")
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=self.transport) as client:
                token = await self._access_token(client)
                resp = await client.post(
                    f"{ARM_ENDPOINT}{self.job_id}/start",
                    params={"api-version": JOBS_API_VERSION},
                    headers={"Authorization": "Bearer " + token},
                )
        except httpx.HTTPError as e:
            raise JobStartError(f"request failed ({type(e).__name__})") from None
        if not resp.is_success:
            raise JobStartError(f"Azure answered HTTP {resp.status_code}")

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_MARGIN_SECONDS:
            return self._token
        params = {"resource": f"{ARM_ENDPOINT}/", "api-version": IDENTITY_API_VERSION}
        if self.client_id:
            params["client_id"] = self.client_id
        resp = await client.get(
            self.identity_endpoint, params=params, headers={"X-IDENTITY-HEADER": self.identity_header}
        )
        if not resp.is_success:
            raise JobStartError(f"managed identity answered HTTP {resp.status_code}")
        try:
            data = resp.json()
            token, expires_at = str(data["access_token"]), float(data["expires_on"])
        except (ValueError, KeyError, TypeError):
            raise JobStartError("managed identity returned an unexpected answer") from None
        if not token:
            raise JobStartError("managed identity returned no token")
        self._token, self._token_expires_at = token, expires_at
        return token


def job_starter(ctx: AppContext) -> JobStarter:
    """The app's job starter (configured only on Azure, where the job's resource ID is set)."""
    starter = ctx.extras.get("job_starter")
    if starter is None:
        s = ctx.settings
        starter = JobStarter(
            s.automation_job_id,
            client_id=s.managed_identity_client_id,
            identity_endpoint=os.environ.get("IDENTITY_ENDPOINT", ""),
            identity_header=os.environ.get("IDENTITY_HEADER", ""),
            transport=ctx.extras.get("http_transport"),
        )
        ctx.extras["job_starter"] = starter
    return starter
