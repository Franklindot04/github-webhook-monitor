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
    )

    webhook_secret: SecretStr = Field(min_length=1)
    max_events: PositiveInt = 50
    max_webhook_body_bytes: PositiveInt = 26_214_400
    delivery_store_backend: DeliveryStoreBackend = "memory"
    database_url: SecretStr | None = None
    database_connect_timeout_seconds: PositiveInt = 5
    management_api_enabled: bool = False
    management_api_token: SecretStr | None = None

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
        return self
