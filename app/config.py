from pathlib import Path
from typing import Literal

from pydantic import Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.persistence.database import POSTGRESQL_PSYCOPG_SCHEME
from app.security import MIN_MANAGEMENT_API_TOKEN_LENGTH


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DeliveryStoreBackend = Literal["memory", "postgresql"]


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
    management_api_token: SecretStr | None = None
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

        if self.management_api_enabled and self.management_api_token is None:
            raise ValueError("MANAGEMENT_API_TOKEN is required when MANAGEMENT_API_ENABLED=true")
        if self.management_api_token is not None:
            management_token = self.management_api_token.get_secret_value()
            if len(management_token) < MIN_MANAGEMENT_API_TOKEN_LENGTH:
                raise ValueError(
                    f"MANAGEMENT_API_TOKEN must be at least {MIN_MANAGEMENT_API_TOKEN_LENGTH} characters"
                )
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
