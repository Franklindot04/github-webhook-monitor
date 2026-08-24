import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

from app.config import Settings
from app.factory import create_app
from app.persistence.schema import delivery_attempts, github_deliveries
from app.storage.deliveries import DeliveryStoreReadinessError


pytestmark = pytest.mark.integration


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_SECRET = "test-webhook-secret"


def require_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("PostgreSQL runtime integration tests require TEST_DATABASE_URL")
    if not TEST_DATABASE_URL.startswith("postgresql+psycopg://"):
        pytest.skip("TEST_DATABASE_URL must use postgresql+psycopg")
    return TEST_DATABASE_URL


@pytest.fixture
def database_url() -> str:
    return require_test_database_url()


@pytest.fixture
def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def engine(database_url: str, alembic_config: Config):
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url, future=True)
    clean_tables(engine)
    yield engine
    clean_tables(engine)
    engine.dispose()


def clean_tables(engine) -> None:
    with engine.begin() as connection:
        connection.execute(delivery_attempts.delete())
        connection.execute(github_deliveries.delete())


def runtime_settings(database_url: str) -> Settings:
    return Settings(
        webhook_secret=TEST_SECRET,
        max_events=50,
        max_webhook_body_bytes=4096,
        delivery_store_backend="postgresql",
        database_url=database_url,
        database_connect_timeout_seconds=2,
        _env_file=None,
    )


def encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def post_webhook(
    client: TestClient,
    payload: bytes,
    *,
    delivery_id: str = "delivery-001",
    hook_id: str = "12345",
):
    return client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Hook-ID": hook_id,
            "X-Hub-Signature-256": signature_for(payload),
        },
    )


def test_postgresql_runtime_startup_requires_migrated_schema(
    database_url: str,
    alembic_config: Config,
):
    command.downgrade(alembic_config, "base")
    app = create_app(settings=runtime_settings(database_url))

    with pytest.raises(DeliveryStoreReadinessError):
        with TestClient(app):
            pass

    command.upgrade(alembic_config, "head")
    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        assert client.get("/ready").status_code == 200


def test_http_webhook_persists_to_postgresql_and_events_reads_database(
    database_url: str,
    engine,
):
    payload = encode_json(
        {
            "action": "opened",
            "repository": {"full_name": "octo/example"},
            "sender": {"login": "octocat"},
        }
    )

    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        response = post_webhook(client, payload)
        events_response = client.get("/events")

    assert response.status_code == 200
    assert response.json()["message"] == "Webhook received"
    assert events_response.status_code == 200
    assert events_response.json()["events"] == [response.json()["event"]]

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(github_deliveries)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 1
        assert (
            connection.execute(select(delivery_attempts.c.payload_sha256)).scalar_one()
            == hashlib.sha256(payload).hexdigest()
        )


def test_postgresql_runtime_persists_events_across_application_restart(
    database_url: str,
    engine,
):
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})

    with TestClient(create_app(settings=runtime_settings(database_url))) as first_client:
        first_response = post_webhook(first_client, payload)
        assert first_response.status_code == 200

    with TestClient(create_app(settings=runtime_settings(database_url))) as second_client:
        events_response = second_client.get("/events")

    assert events_response.status_code == 200
    assert events_response.json()["count"] == 1
    assert events_response.json()["events"][0]["delivery_id"] == "delivery-001"
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 1


def test_postgresql_runtime_handles_concurrent_http_webhooks_for_same_delivery(
    database_url: str,
    engine,
):
    first_payload = encode_json({"action": "opened"})
    second_payload = encode_json({"action": "closed"})

    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(post_webhook, client, first_payload, delivery_id="delivery-race"),
                executor.submit(post_webhook, client, second_payload, delivery_id="delivery-race"),
            ]
            responses = [future.result(timeout=10) for future in futures]
        events = client.get("/events").json()["events"]

    assert [response.status_code for response in responses] == [200, 200]
    assert len(events) == 2
    assert {event["delivery_id"] for event in events} == {"delivery-race"}
    assert {event["payload_sha256"] for event in events} == {
        hashlib.sha256(first_payload).hexdigest(),
        hashlib.sha256(second_payload).hexdigest(),
    }
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(github_deliveries)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 2


def test_ready_detects_postgresql_schema_outage_after_startup_but_health_remains_live(
    database_url: str,
    alembic_config: Config,
    engine,
):
    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        assert client.get("/ready").status_code == 200
        command.downgrade(alembic_config, "base")

        health_response = client.get("/health")
        ready_response = client.get("/ready")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert ready_response.status_code == 503
    assert ready_response.json() == {"detail": "Service unavailable"}
    command.upgrade(alembic_config, "head")


def test_postgresql_runtime_reports_service_unavailable_when_database_disappears_after_startup(
    database_url: str,
    alembic_config: Config,
    engine,
):
    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        command.downgrade(alembic_config, "base")
        payload = encode_json({"action": "opened"})

        post_response = post_webhook(client, payload)
        events_response = client.get("/events")

    assert post_response.status_code == 503
    assert post_response.json() == {"detail": "Service unavailable"}
    assert events_response.status_code == 503
    assert events_response.json() == {"detail": "Service unavailable"}
    command.upgrade(alembic_config, "head")
