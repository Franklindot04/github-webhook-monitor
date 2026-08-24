import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.config import Settings


MANAGEMENT_TOKEN = "synthetic-management-token-000001"
GITHUB_TOKEN = "synthetic-github-token"


def test_valid_configuration():
    settings = Settings(
        webhook_secret="synthetic-secret",
        max_events=25,
        max_webhook_body_bytes=1024,
        _env_file=None,
    )

    assert settings.webhook_secret.get_secret_value() == "synthetic-secret"
    assert settings.max_events == 25
    assert settings.max_webhook_body_bytes == 1024


def test_webhook_secret_is_required(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("MAX_EVENTS", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_webhook_secret_is_rejected():
    with pytest.raises(ValidationError):
        Settings(webhook_secret="", _env_file=None)


def test_valid_max_events():
    settings = Settings(webhook_secret="synthetic-secret", max_events=1, _env_file=None)

    assert settings.max_events == 1


def test_default_max_events_is_preserved():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert settings.max_events == 50


def test_default_max_webhook_body_bytes_matches_github_payload_limit():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert settings.max_webhook_body_bytes == 26_214_400


@pytest.mark.parametrize("max_events", [0, -1])
def test_invalid_max_events_values_are_rejected(max_events):
    with pytest.raises(ValidationError):
        Settings(webhook_secret="synthetic-secret", max_events=max_events, _env_file=None)


def test_environment_variable_loading(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "synthetic-env-secret")
    monkeypatch.setenv("MAX_EVENTS", "12")
    monkeypatch.setenv("MAX_WEBHOOK_BODY_BYTES", "2048")

    settings = Settings(_env_file=None)

    assert settings.webhook_secret.get_secret_value() == "synthetic-env-secret"
    assert settings.max_events == 12
    assert settings.max_webhook_body_bytes == 2048


def test_database_url_environment_variable_coexists_with_application_settings(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "synthetic-env-secret")
    monkeypatch.setenv("MAX_EVENTS", "12")
    monkeypatch.setenv("MAX_WEBHOOK_BODY_BYTES", "2048")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
    )

    settings = Settings(_env_file=None)

    assert settings.webhook_secret.get_secret_value() == "synthetic-env-secret"
    assert settings.max_events == 12
    assert settings.max_webhook_body_bytes == 2048
    assert settings.delivery_store_backend == "memory"


def test_default_delivery_store_backend_is_memory():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert settings.delivery_store_backend == "memory"


def test_management_api_is_disabled_by_default_and_token_is_optional():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert settings.management_api_enabled is False
    assert settings.management_api_token is None


def test_management_token_presence_alone_does_not_enable_management_api():
    settings = Settings(
        webhook_secret="synthetic-secret",
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )

    assert settings.management_api_enabled is False
    assert settings.management_api_token is not None


def test_management_api_requires_token_when_enabled():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            webhook_secret="synthetic-secret",
            management_api_enabled=True,
            _env_file=None,
        )

    message = str(exc_info.value)
    assert "MANAGEMENT_API_TOKEN is required" in message
    assert MANAGEMENT_TOKEN not in message


def test_management_api_accepts_enabled_with_token():
    settings = Settings(
        webhook_secret="synthetic-secret",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )

    assert settings.management_api_enabled is True
    assert settings.management_api_token is not None
    assert settings.management_api_token.get_secret_value() == MANAGEMENT_TOKEN


@pytest.mark.parametrize("token", ["", "short-management-token"])
def test_management_token_must_meet_minimum_length(token):
    with pytest.raises(ValidationError):
        Settings(
            webhook_secret="synthetic-secret",
            management_api_enabled=True,
            management_api_token=token,
            _env_file=None,
        )


def test_management_token_is_redacted_in_settings_representation():
    settings = Settings(
        webhook_secret="synthetic-secret",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )

    representation = repr(settings)
    assert MANAGEMENT_TOKEN not in representation
    assert "**********" in representation


def test_github_reconciliation_is_disabled_by_default_and_token_is_optional():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert settings.github_reconciliation_enabled is False
    assert settings.github_repository_webhook_token is None
    assert settings.github_api_timeout_seconds == 5
    assert settings.github_reconciliation_max_pages == 5


def test_github_token_presence_alone_does_not_enable_reconciliation():
    settings = Settings(
        webhook_secret="synthetic-secret",
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )

    assert settings.github_reconciliation_enabled is False
    assert settings.github_repository_webhook_token is not None


def test_github_reconciliation_requires_management_api_enabled():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            webhook_secret="synthetic-secret",
            github_reconciliation_enabled=True,
            github_repository_webhook_token=GITHUB_TOKEN,
            _env_file=None,
        )

    message = str(exc_info.value)
    assert "MANAGEMENT_API_ENABLED=true is required" in message
    assert GITHUB_TOKEN not in message


def test_github_reconciliation_requires_repository_webhook_token_when_enabled():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            webhook_secret="synthetic-secret",
            management_api_enabled=True,
            management_api_token=MANAGEMENT_TOKEN,
            github_reconciliation_enabled=True,
            _env_file=None,
        )

    assert "GITHUB_REPOSITORY_WEBHOOK_TOKEN is required" in str(exc_info.value)


def test_github_reconciliation_accepts_enabled_management_and_token():
    settings = Settings(
        webhook_secret="synthetic-secret",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )

    assert settings.github_reconciliation_enabled is True
    assert settings.github_repository_webhook_token.get_secret_value() == GITHUB_TOKEN


@pytest.mark.parametrize("value", [0, -1])
def test_github_api_timeout_must_be_positive(value):
    with pytest.raises(ValidationError):
        Settings(webhook_secret="synthetic-secret", github_api_timeout_seconds=value, _env_file=None)


@pytest.mark.parametrize("value", [0, 21])
def test_github_reconciliation_max_pages_range(value):
    with pytest.raises(ValidationError):
        Settings(webhook_secret="synthetic-secret", github_reconciliation_max_pages=value, _env_file=None)


def test_github_repository_webhook_token_is_redacted_in_settings_representation():
    settings = Settings(
        webhook_secret="synthetic-secret",
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )

    representation = repr(settings)
    assert GITHUB_TOKEN not in representation
    assert "**********" in representation


def test_explicit_memory_backend_does_not_require_database_url():
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="memory",
        _env_file=None,
    )

    assert settings.delivery_store_backend == "memory"
    assert settings.database_url is None


def test_explicit_memory_backend_allows_database_url():
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="memory",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        _env_file=None,
    )

    assert settings.delivery_store_backend == "memory"
    assert settings.database_url is not None


def test_postgresql_backend_requires_database_url():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            webhook_secret="synthetic-secret",
            delivery_store_backend="postgresql",
            _env_file=None,
        )

    message = str(exc_info.value)
    assert "DATABASE_URL is required" in message
    assert "example_password" not in message


def test_postgresql_backend_accepts_psycopg_database_url():
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="postgresql",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        _env_file=None,
    )

    assert settings.delivery_store_backend == "postgresql"
    assert settings.database_url is not None
    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")


def test_invalid_delivery_store_backend_is_rejected():
    with pytest.raises(ValidationError):
        Settings(
            webhook_secret="synthetic-secret",
            delivery_store_backend="sqlite",
            _env_file=None,
        )


def test_postgresql_backend_rejects_unsupported_database_driver_without_leaking_url():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            webhook_secret="synthetic-secret",
            delivery_store_backend="postgresql",
            database_url="postgresql://example_user:example_password@example-host:5432/example_database",
            _env_file=None,
        )

    message = str(exc_info.value)
    assert "postgresql+psycopg" in message
    assert "example_password" not in message


@pytest.mark.parametrize("timeout", [0, -1])
def test_database_connect_timeout_must_be_positive(timeout):
    with pytest.raises(ValidationError):
        Settings(
            webhook_secret="synthetic-secret",
            database_connect_timeout_seconds=timeout,
            _env_file=None,
        )


def test_database_url_is_redacted_in_settings_representation():
    settings = Settings(
        webhook_secret="synthetic-secret",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        _env_file=None,
    )

    representation = repr(settings)
    assert "example_password" not in representation
    assert "postgresql+psycopg://" not in representation
    assert "**********" in representation


@pytest.mark.parametrize("max_webhook_body_bytes", [0, -1])
def test_invalid_max_webhook_body_bytes_values_are_rejected(max_webhook_body_bytes):
    with pytest.raises(ValidationError):
        Settings(
            webhook_secret="synthetic-secret",
            max_webhook_body_bytes=max_webhook_body_bytes,
            _env_file=None,
        )


def test_dotenv_loading_without_developer_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("MAX_EVENTS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEBHOOK_SECRET=synthetic-dotenv-secret\n"
        "MAX_EVENTS=9\n"
        "MAX_WEBHOOK_BODY_BYTES=4096\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.webhook_secret.get_secret_value() == "synthetic-dotenv-secret"
    assert settings.max_events == 9
    assert settings.max_webhook_body_bytes == 4096


def test_dotenv_database_url_coexists_with_application_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("MAX_EVENTS", raising=False)
    monkeypatch.delenv("MAX_WEBHOOK_BODY_BYTES", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEBHOOK_SECRET=synthetic-dotenv-secret\n"
        "MAX_EVENTS=9\n"
        "MAX_WEBHOOK_BODY_BYTES=4096\n"
        "DATABASE_URL=postgresql+psycopg://example_user:example_password@example-host:5432/example_database\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.webhook_secret.get_secret_value() == "synthetic-dotenv-secret"
    assert settings.max_events == 9
    assert settings.max_webhook_body_bytes == 4096


def test_secret_value_is_redacted_in_settings_representation():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)

    assert "synthetic-secret" not in repr(settings)
    assert "**********" in repr(settings)


def test_app_import_succeeds_with_valid_synthetic_configuration():
    env = os.environ.copy()
    env["WEBHOOK_SECRET"] = "synthetic-import-secret"
    env["MAX_EVENTS"] = "5"
    env["MAX_WEBHOOK_BODY_BYTES"] = "1024"

    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print(app.main.app.title)"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "GitHub Webhook Monitor"


def test_app_import_succeeds_without_database_url():
    env = os.environ.copy()
    env["WEBHOOK_SECRET"] = "synthetic-import-secret"
    env["MAX_EVENTS"] = "5"
    env["MAX_WEBHOOK_BODY_BYTES"] = "1024"
    env.pop("DATABASE_URL", None)

    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print(app.main.app.title)"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "GitHub Webhook Monitor"


def test_app_import_succeeds_with_unavailable_database_url_without_connecting():
    env = os.environ.copy()
    env["WEBHOOK_SECRET"] = "synthetic-import-secret"
    env["MAX_EVENTS"] = "5"
    env["MAX_WEBHOOK_BODY_BYTES"] = "1024"
    env["DELIVERY_STORE_BACKEND"] = "postgresql"
    env["DATABASE_CONNECT_TIMEOUT_SECONDS"] = "1"
    env["DATABASE_URL"] = "postgresql+psycopg://example_user:example_password@127.0.0.1:1/example_database"

    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print(app.main.app.title)"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "GitHub Webhook Monitor"


def test_app_import_fails_fast_when_webhook_secret_is_missing():
    env = os.environ.copy()
    env.pop("WEBHOOK_SECRET", None)
    env["MAX_EVENTS"] = "5"
    env["MAX_WEBHOOK_BODY_BYTES"] = "1024"

    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "WEBHOOK_SECRET" not in result.stderr
    assert "synthetic" not in result.stderr


@pytest.mark.parametrize("payload_limit", ["0", "-1"])
def test_app_import_fails_fast_when_webhook_body_limit_is_invalid(payload_limit):
    env = os.environ.copy()
    env["WEBHOOK_SECRET"] = "synthetic-import-secret"
    env["MAX_EVENTS"] = "5"
    env["MAX_WEBHOOK_BODY_BYTES"] = payload_limit

    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
