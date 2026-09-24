"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    extra_fetch_domains: str = ""

    # External API connectors (values come from ACA secrets; never stored in files).
    stooq_api_key: SecretStr = SecretStr("")
    rakuten_application_id: SecretStr = SecretStr("")
    rakuten_access_key: SecretStr = SecretStr("")
    # Rakuten changes endpoint versions from time to time; override without a code change if needed.
    rakuten_vacant_endpoint: str = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"

    # GitHub App used to create notification issues.
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_installation_id: str = ""
    notify_repo: str = ""
    notify_mention: str = "usagiandkamex"
    # All automations stop without a valid token, so this notice is sent even when per-automation notify is off.
    notify_reauth: bool = True

    automation_max_runtime_seconds: int = 20 * 60
    automation_lock_ttl_seconds: int = 25 * 60
    automation_monthly_run_limit: int = 300

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

    @property
    def fetch_domains(self) -> list[str]:
        base = [
            "go.jp",
            "lg.jp",
            "toushin.or.jp",
            "am.mufg.jp",
            "nikkoam.com",
            "global-am.co.jp",
            "sbiokasan-am.co.jp",
            "rakuten-toushin.co.jp",
            "nomura-am.co.jp",
            "daiwa-am.co.jp",
            "fidelity.co.jp",
        ]
        extra = [d.strip().lower() for d in self.extra_fetch_domains.split(",") if d.strip()]
        return base + extra

    def secret_values(self) -> list[str]:
        """All secret values that must never appear in tool results or logs."""
        values = [
            self.github_oauth_client_secret,
            self.session_secret,
            self.token_encryption_key,
            self.dev_github_token,
            self.stooq_api_key,
            self.rakuten_application_id,
            self.rakuten_access_key,
            self.github_app_private_key,
        ]
        return [v.get_secret_value() for v in values if v.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
