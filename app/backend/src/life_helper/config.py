"""Application settings loaded from environment variables."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resource ID of a Container Apps job; the app calls Azure Resource Manager with it, so nothing else is accepted.
_JOB_ID = re.compile(
    r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[\w.()-]{1,90}/providers/Microsoft\.App/jobs/[A-Za-z0-9-]{1,32}"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LH_", env_file=".env", extra="ignore")

    environment: Literal["production", "development"] = "production"
    base_url: str = "http://localhost:8000"
    data_dir: Path = Path("/data")
    static_dir: Path = Path("/app/static")
    skills_dir: Path = Path(__file__).parent / "resources" / "skills"
    seed_dir: Path = Path(__file__).parent / "resources" / "seed"
    tax_params_dir: Path = Path(__file__).parent / "resources" / "tax_params"
    broker_csv_dir: Path = Path(__file__).parent / "resources" / "broker_csv"

    # Only this numeric GitHub user id may sign in (usagiandkamex = 134019422).
    allowed_github_user_id: int = 134019422

    github_oauth_client_id: str = ""
    github_oauth_client_secret: SecretStr = SecretStr("")
    github_oauth_scopes: str = "read:user"

    session_secret: SecretStr = SecretStr("")
    token_encryption_key: SecretStr = SecretStr("")
    session_max_age_seconds: int = 60 * 60 * 24 * 14

    # Local development only: sign in with a token from `gh auth token`.
    dev_github_token: SecretStr = SecretStr("")

    default_model: str = "auto"
    utility_model: str = "gpt-5-mini"

    # Headless Chromium tools (browser_*). Turn off to save memory or when Chromium is not installed.
    browser_enabled: bool = True

    # External API connectors (values come from ACA secrets; never stored in files).
    # One Rakuten Web Service app (Ichiba, Travel, Books, Kobo, GORA and Recipe scopes) serves every Rakuten tool.
    rakuten_application_id: SecretStr = SecretStr("")
    rakuten_access_key: SecretStr = SecretStr("")
    # Rakuten changes endpoint versions from time to time. Production follows DEFAULT_ENDPOINTS in
    # connectors/rakuten.py (not wired into Bicep); for local trials this JSON object overrides them by name, e.g.
    # {"ichiba_item_search": "https://openapi.rakuten.co.jp/…"}. Kept as text so an empty value means "no overrides";
    # it is checked at startup (rakuten_endpoint_overrides).
    rakuten_endpoints: str = ""

    # GitHub App used to create notification issues.
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_installation_id: str = ""
    notify_repo: str = ""
    notify_mention: str = "usagiandkamex"
    # All automations stop without a valid token, so this notice is sent even when per-automation notify is off.
    notify_reauth: bool = True

    # Must exceed the longest allowed run (60 minutes plus the bounded clean-up) so the per-automation lock outlives
    # the run.
    automation_lock_ttl_seconds: int = 65 * 60
    automation_monthly_run_limit: int = 300
    # The ACA job's replicaTimeout (infra/resources.bicep sets both). Runs in the job are cut short before it, and
    # runs that no longer fit wait for the next job execution, so the platform never stops a run half-way.
    automation_job_timeout_seconds: int = Field(default=70 * 60, ge=10 * 60)
    # 「今すぐ実行」 starts this ACA job (its resource ID) instead of running in the web app, which scales in to zero
    # once nobody uses it. Empty (local development) runs it in the app. The app signs in to Azure with its
    # user-assigned managed identity (this client ID), which needs permission to start the job.
    automation_job_id: str = ""
    managed_identity_client_id: str = ""

    # Automation runs, chat conversations and Copilot session state left unused for this many days are deleted.
    data_retention_days: int = Field(default=180, ge=1)

    upload_max_bytes: int = Field(default=10 * 1024 * 1024)

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.environment == "production":
            # OAuth may be empty right after the first provision (the OAuth App needs the app URL); /auth/login then
            # reports "not configured". These two protect cookies and the stored token, so they are always required.
            missing = [
                name
                for name, value in (
                    ("LH_SESSION_SECRET", self.session_secret.get_secret_value()),
                    ("LH_TOKEN_ENCRYPTION_KEY", self.token_encryption_key.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"missing required settings in production: {', '.join(missing)}")
            if self.dev_github_token.get_secret_value():
                raise ValueError("LH_DEV_GITHUB_TOKEN must not be set in production")
        return self

    @field_validator("automation_job_id")
    @classmethod
    def _check_automation_job_id(cls, value: str) -> str:
        value = value.strip()
        if value and not _JOB_ID.fullmatch(value):
            raise ValueError("LH_AUTOMATION_JOB_ID must be the resource ID of a Container Apps job")
        return value

    @model_validator(mode="after")
    def _check_rakuten_endpoints(self) -> Settings:
        from .connectors.rakuten import check_endpoints

        check_endpoints(self.rakuten_endpoint_overrides)
        return self

    @property
    def rakuten_endpoint_overrides(self) -> dict[str, str]:
        text = self.rakuten_endpoints.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError("LH_RAKUTEN_ENDPOINTS must be a JSON object") from None
        if not isinstance(value, dict):
            raise ValueError("LH_RAKUTEN_ENDPOINTS must be a JSON object")
        return value

    @property
    def oauth_configured(self) -> bool:
        return bool(self.github_oauth_client_id and self.github_oauth_client_secret.get_secret_value())

    @property
    def is_dev(self) -> bool:
        return self.environment == "development"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    @property
    def app_state_dir(self) -> Path:
        return self.data_dir / "app"

    @property
    def copilot_chat_dir(self) -> Path:
        return self.data_dir / "copilot"

    @property
    def copilot_automation_dir(self) -> Path:
        return self.data_dir / "copilot-automation"

    @property
    def copilot_workdir(self) -> Path:
        # Kept empty on purpose: the CLI process never starts inside the app source tree.
        return self.data_dir / "copilot-workdir"

    def secret_values(self) -> list[str]:
        """All secret values that must never appear in tool results or logs."""
        values = [
            self.github_oauth_client_secret,
            self.session_secret,
            self.token_encryption_key,
            self.dev_github_token,
            self.rakuten_application_id,
            self.rakuten_access_key,
            self.github_app_private_key,
        ]
        return [v.get_secret_value() for v in values if v.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
