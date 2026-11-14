"""Console settings, read from environment variables or `.env` (12-factor). See README, "Configuration".

The agent's own settings (models, caps, AGENT_DB_PATH, ...) are cost-capped-agent's `Settings`,
read from the same environment with the `AGENT_` prefix.

Startup refuses unsafe combinations instead of warning about them:
  - dev login on anything but a localhost BASE_URL, or with ENVIRONMENT=production
  - the offline demo Claude client with ENVIRONMENT=production
  - non-Secure session cookies on anything but a localhost BASE_URL
  - a short or placeholder SESSION_SECRET
  - production without Google client credentials or an allow-list
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIN_SECRET_LENGTH = 32


class ConfigError(ValueError):
    """A setting combination the console refuses to start with."""


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    # hide_input_in_errors: a refused config must never echo SESSION_SECRET or the Google secret into logs.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    environment: Literal["development", "test", "production"] = "production"
    base_url: str = Field(
        default="http://127.0.0.1:8000", description="Public origin; the OAuth redirect URI is built from it"
    )

    # Sessions
    session_secret: str = ""
    session_cookie_secure: bool = True
    session_max_age_seconds: int = Field(default=8 * 3600, ge=300, le=7 * 86_400)

    # Google sign-in (OpenID Connect)
    google_client_id: str = ""
    google_client_secret: str = ""
    oidc_discovery_url: str = GOOGLE_DISCOVERY_URL
    oidc_allowed_issuers: str = "https://accounts.google.com,accounts.google.com"
    allowed_emails: str = Field(default="", description="Comma-separated; only these addresses get in")

    # Local development only
    dev_login_enabled: bool = False
    dev_login_email: str = "dev@example.com"
    claude_client: Literal["anthropic", "demo"] = "anthropic"

    # Storage and indexing
    console_db_path: str = "data/console.db"
    index_on_start: str = Field(
        default="sample_corpus", description="Folder to index (offline) at startup; empty to skip"
    )

    # Runs
    max_concurrent_runs: int = Field(default=2, ge=1, le=32)
    run_timeout_seconds: int = Field(default=300, ge=10, le=3_600)
    max_question_chars: int = Field(default=2_000, ge=50, le=20_000)

    # Abuse limits (per minute)
    rate_limit_login_per_minute: int = Field(default=10, ge=1, le=1_000)
    rate_limit_ask_per_minute: int = Field(default=6, ge=1, le=1_000)
    rate_limit_global_per_minute: int = Field(default=300, ge=10, le=100_000)
    trust_proxy_headers: bool = False
    max_body_bytes: int = Field(default=16_384, ge=1_024, le=1_048_576)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- derived ---------------------------------------------------------------------------------

    @property
    def allowed_email_set(self) -> frozenset[str]:
        return frozenset(e.lower() for e in _csv(self.allowed_emails))

    @property
    def issuer_list(self) -> list[str]:
        return _csv(self.oidc_allowed_issuers)

    @property
    def is_local(self) -> bool:
        return (urlsplit(self.base_url).hostname or "") in LOCAL_HOSTS

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/auth/callback"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    # --- startup checks ----------------------------------------------------------------------------

    @model_validator(mode="after")
    def _refuse_unsafe(self) -> Settings:
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
            raise ConfigError("BASE_URL must be an origin such as https://console.example.com")
        if parts.scheme == "http" and not self.is_local:
            raise ConfigError("BASE_URL must use https unless it is localhost")
        if len(self.session_secret) < MIN_SECRET_LENGTH or "replace-with" in self.session_secret:
            raise ConfigError(
                f"SESSION_SECRET must be a random value of at least {MIN_SECRET_LENGTH} characters "
                "(run: python scripts/make_env.py)"
            )
        if not self.session_cookie_secure and not self.is_local:
            raise ConfigError("SESSION_COOKIE_SECURE=false is allowed only for a localhost BASE_URL")
        if self.dev_login_enabled:
            if self.environment == "production":
                raise ConfigError("DEV_LOGIN_ENABLED is refused when ENVIRONMENT=production")
            if not self.is_local:
                raise ConfigError("DEV_LOGIN_ENABLED is allowed only for a localhost BASE_URL")
            if self.dev_login_email.lower() not in self.allowed_email_set:
                raise ConfigError("DEV_LOGIN_EMAIL must also be in ALLOWED_EMAILS")
        if self.claude_client == "demo" and self.environment == "production":
            raise ConfigError("CLAUDE_CLIENT=demo is refused when ENVIRONMENT=production")
        if self.environment == "production" and not (self.google_configured and self.allowed_email_set):
            raise ConfigError("production needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and ALLOWED_EMAILS")
        if any("@" not in e for e in self.allowed_email_set):
            raise ConfigError("ALLOWED_EMAILS must be a comma-separated list of email addresses")
        return self


def run_cap_ok(value: Decimal, cap: Decimal) -> bool:
    return Decimal("0.01") <= value <= cap
