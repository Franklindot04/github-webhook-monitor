from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.persistence.database import POSTGRESQL_PSYCOPG_SCHEME
from app.security import MIN_MANAGEMENT_API_TOKEN_LENGTH, parse_allowed_jwt_algorithms


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DeliveryStoreBackend = Literal["memory", "postgresql"]
ManagementAuthMode = Literal["shared_token", "oidc_jwt"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    webhook_secret: SecretStr = Field(min_length=1)
    max_events: PositiveInt = 50
    max_webhook_body_bytes: PositiveInt = 26_214_400
    delivery_store_backend: DeliveryStoreBackend = "memory"
    database_url: SecretStr | None = None
    database_connect_timeout_seconds: PositiveInt = 5
    management_api_enabled: bool = False
    management_auth_mode: ManagementAuthMode = "shared_token"
    management_api_token: SecretStr | None = None
    management_oidc_issuer: str | None = None
    management_oidc_audience: str | None = None
    management_oidc_required_scope: str | None = None
    management_oidc_full_management_scope: str = "webhook-monitor.manage"
    management_oidc_diagnostics_read_scope: str = "webhook-monitor.diagnostics.read"
    management_oidc_recovery_read_scope: str = "webhook-monitor.recovery.read"
    management_oidc_recovery_execute_scope: str = "webhook-monitor.recovery.execute"
    management_oidc_allowed_algorithms: str = "RS256"
    management_oidc_http_timeout_seconds: PositiveInt = 5
    github_reconciliation_enabled: bool = False
    github_repository_webhook_token: SecretStr | None = None
    github_redelivery_enabled: bool = False
    github_repository_webhook_write_token: SecretStr | None = None
    github_api_timeout_seconds: PositiveInt = 5
    github_reconciliation_max_pages: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_runtime_configuration(self) -> "Settings":
        if self.delivery_store_backend == "postgresql":
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required when DELIVERY_STORE_BACKEND=postgresql")
            database_url = self.database_url.get_secret_value()
            if not database_url.startswith(POSTGRESQL_PSYCOPG_SCHEME):
                raise ValueError("DATABASE_URL must use postgresql+psycopg when DELIVERY_STORE_BACKEND=postgresql")

        if self.management_api_enabled and self.management_auth_mode == "shared_token" and self.management_api_token is None:
            raise ValueError("MANAGEMENT_API_TOKEN is required when MANAGEMENT_API_ENABLED=true")
        if self.management_api_token is not None:
            management_token = self.management_api_token.get_secret_value()
            if len(management_token) < MIN_MANAGEMENT_API_TOKEN_LENGTH:
                raise ValueError(
                    f"MANAGEMENT_API_TOKEN must be at least {MIN_MANAGEMENT_API_TOKEN_LENGTH} characters"
                )
        if self.management_api_enabled and self.management_auth_mode == "oidc_jwt":
            if self.management_oidc_issuer is None:
                raise ValueError("MANAGEMENT_OIDC_ISSUER is required when MANAGEMENT_AUTH_MODE=oidc_jwt")
            if self.management_oidc_audience is None:
                raise ValueError("MANAGEMENT_OIDC_AUDIENCE is required when MANAGEMENT_AUTH_MODE=oidc_jwt")
            validate_oidc_issuer(self.management_oidc_issuer)
            if not self.management_oidc_audience.strip():
                raise ValueError("MANAGEMENT_OIDC_AUDIENCE must not be blank")
            full_management_scope = self.management_oidc_full_management_scope
            if self.management_oidc_required_scope is not None:
                deprecated_required_scope = self.management_oidc_required_scope
                if not deprecated_required_scope.strip():
                    raise ValueError("MANAGEMENT_OIDC_REQUIRED_SCOPE must not be blank")
                if (
                    full_management_scope != "webhook-monitor.manage"
                    and deprecated_required_scope != full_management_scope
                ):
                    raise ValueError(
                        "MANAGEMENT_OIDC_REQUIRED_SCOPE must match MANAGEMENT_OIDC_FULL_MANAGEMENT_SCOPE when both are set"
                    )
                full_management_scope = deprecated_required_scope
                object.__setattr__(self, "management_oidc_full_management_scope", full_management_scope)
            validate_management_scope_mapping(
                full_management_scope=full_management_scope,
                diagnostics_read_scope=self.management_oidc_diagnostics_read_scope,
                recovery_read_scope=self.management_oidc_recovery_read_scope,
                recovery_execute_scope=self.management_oidc_recovery_execute_scope,
            )
            parse_allowed_jwt_algorithms(self.management_oidc_allowed_algorithms)
        if self.github_reconciliation_enabled:
            if not self.management_api_enabled:
                raise ValueError("MANAGEMENT_API_ENABLED=true is required when GITHUB_RECONCILIATION_ENABLED=true")
            if self.github_repository_webhook_token is None:
                raise ValueError(
                    "GITHUB_REPOSITORY_WEBHOOK_TOKEN is required when GITHUB_RECONCILIATION_ENABLED=true"
                )
        if self.github_redelivery_enabled:
            if not self.management_api_enabled:
                raise ValueError("MANAGEMENT_API_ENABLED=true is required when GITHUB_REDELIVERY_ENABLED=true")
            if not self.github_reconciliation_enabled:
                raise ValueError("GITHUB_RECONCILIATION_ENABLED=true is required when GITHUB_REDELIVERY_ENABLED=true")
            if self.github_repository_webhook_write_token is None:
                raise ValueError(
                    "GITHUB_REPOSITORY_WEBHOOK_WRITE_TOKEN is required when GITHUB_REDELIVERY_ENABLED=true"
                )
        return self


def validate_oidc_issuer(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MANAGEMENT_OIDC_ISSUER must be an HTTPS issuer URI without query or fragment")


def validate_management_scope_mapping(
    *,
    full_management_scope: str,
    diagnostics_read_scope: str,
    recovery_read_scope: str,
    recovery_execute_scope: str,
) -> None:
    named_scopes = {
        "MANAGEMENT_OIDC_FULL_MANAGEMENT_SCOPE": full_management_scope,
        "MANAGEMENT_OIDC_DIAGNOSTICS_READ_SCOPE": diagnostics_read_scope,
        "MANAGEMENT_OIDC_RECOVERY_READ_SCOPE": recovery_read_scope,
        "MANAGEMENT_OIDC_RECOVERY_EXECUTE_SCOPE": recovery_execute_scope,
    }
    normalized_scopes = {}
    for name, value in named_scopes.items():
        if not value.strip():
            raise ValueError(f"{name} must not be blank")
        normalized_scopes[name] = value.strip()
        if normalized_scopes[name] != value:
            raise ValueError(f"{name} must not contain surrounding whitespace")

    values = list(normalized_scopes.values())
    if len(set(values)) != len(values):
        raise ValueError("Management OIDC capability scopes must be distinct")
