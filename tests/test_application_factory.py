import hashlib
import hmac
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.runtime as app_runtime
from app.config import Settings
from app.factory import create_app
from app.storage.deliveries import DeliveryStoreReadinessError, InMemoryDeliveryStore


MANAGEMENT_TOKEN = "synthetic-management-token-000001"
GITHUB_TOKEN = "synthetic-github-token"
GITHUB_WRITE_TOKEN = "synthetic-github-write-token"


def signature_for(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def management_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MANAGEMENT_TOKEN}"}


def synthetic_postgresql_settings() -> Settings:
    return Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="postgresql",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        database_connect_timeout_seconds=2,
        _env_file=None,
    )


def synthetic_reconciliation_settings() -> Settings:
    return Settings(
        webhook_secret="synthetic-secret",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )


def synthetic_redelivery_settings() -> Settings:
    return Settings(
        webhook_secret="synthetic-secret",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_redelivery_enabled=True,
        github_repository_webhook_write_token=GITHUB_WRITE_TOKEN,
        _env_file=None,
    )


def has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_independently_constructed_apps_do_not_share_events():
    secret = "synthetic-secret"
    settings = Settings(
        webhook_secret=secret,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    first_client = TestClient(create_app(settings=settings))
    second_client = TestClient(create_app(settings=settings))
    payload = b'{"action":"opened"}'

    response = first_client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Hook-ID": "12345",
            "X-Hub-Signature-256": signature_for(payload, secret),
        },
    )

    assert response.status_code == 200
    assert first_client.get("/events", headers=management_headers()).json()["count"] == 1
    assert second_client.get("/events", headers=management_headers()).json() == {"count": 0, "events": []}


def test_create_app_accepts_explicit_settings_instance():
    settings = Settings(webhook_secret="synthetic-secret", max_events=3, _env_file=None)

    app = create_app(settings=settings)

    assert app.state.settings is settings


def test_create_app_uses_custom_settings_capacity():
    settings = Settings(
        webhook_secret="synthetic-secret",
        max_events=2,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings))

    for index in range(3):
        payload = f'{{"action":"event-{index}"}}'.encode("utf-8")
        response = client.post(
            "/webhook/github",
            content=payload,
            headers={
                "X-GitHub-Delivery": f"delivery-{index}",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Hook-ID": "12345",
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature_for(payload, "synthetic-secret"),
            },
        )
        assert response.status_code == 200

    events = client.get("/events", headers=management_headers()).json()["events"]
    assert len(events) == 2
    assert [event["delivery_id"] for event in events] == ["delivery-2", "delivery-1"]


def test_injected_delivery_store_is_used_for_ingestion_and_listing():
    secret = "synthetic-secret"
    delivery_store = InMemoryDeliveryStore(max_events=10)
    settings = Settings(
        webhook_secret=secret,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))
    payload = b'{"action":"opened","repository":{"full_name":"octo/example"}}'

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Hook-ID": "12345",
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(payload, secret),
        },
    )

    assert response.status_code == 200
    stored_events = [event.to_dict() for event in delivery_store.list_recent()]
    assert stored_events == [response.json()["event"]]
    assert client.get("/events", headers=management_headers()).json() == {"count": 1, "events": stored_events}


def test_memory_runtime_does_not_create_database_engine():
    settings = Settings(webhook_secret="synthetic-secret", delivery_store_backend="memory", _env_file=None)
    app = create_app(settings=settings)

    assert isinstance(app.state.delivery_store, InMemoryDeliveryStore)
    assert app.state.runtime_resources.engine is None
    assert app.state.runtime_resources.owns_engine is False
    assert app.state.github_delivery_client is None


def test_postgresql_runtime_builds_one_owned_engine_and_disposes_it(monkeypatch):
    created_engines = []
    readiness_checks = []

    class FakeEngine:
        def __init__(self):
            self.disposed = False

        def dispose(self):
            self.disposed = True

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        engine = FakeEngine()
        created_engines.append((engine, database_url, connect_timeout_seconds, pool_pre_ping))
        return engine

    def fake_readiness_check(engine):
        readiness_checks.append(engine)

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)
    settings = synthetic_postgresql_settings()
    app_instance = create_app(settings=settings)

    assert len(created_engines) == 1
    engine, database_url, timeout, pool_pre_ping = created_engines[0]
    assert database_url == settings.database_url.get_secret_value()
    assert timeout == 2
    assert pool_pre_ping is True
    assert app_instance.state.runtime_resources.engine is engine
    assert app_instance.state.runtime_resources.owns_engine is True

    with TestClient(app_instance):
        assert readiness_checks == [engine]
        assert engine.disposed is False

    assert engine.disposed is True


def test_injected_delivery_store_does_not_create_or_dispose_engine(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("database engine should not be created for injected stores")

    monkeypatch.setattr(app_runtime, "create_database_engine", fail_if_called)
    delivery_store = InMemoryDeliveryStore(max_events=10)
    settings = synthetic_postgresql_settings()
    app_instance = create_app(settings=settings, delivery_store=delivery_store)

    with TestClient(app_instance):
        assert app_instance.state.delivery_store is delivery_store
        assert app_instance.state.runtime_resources.engine is None
        assert app_instance.state.runtime_resources.owns_engine is False


def test_postgresql_lifespan_readiness_runs_outside_active_event_loop(monkeypatch):
    readiness_ran_without_loop = []

    class FakeEngine:
        def dispose(self):
            pass

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        return FakeEngine()

    def fake_readiness_check(engine):
        readiness_ran_without_loop.append(not has_running_loop())

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)

    with TestClient(create_app(settings=synthetic_postgresql_settings())):
        pass

    assert readiness_ran_without_loop == [True]


def test_postgresql_ready_endpoint_readiness_runs_outside_active_event_loop(monkeypatch):
    readiness_ran_without_loop = []

    class FakeEngine:
        def dispose(self):
            pass

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        return FakeEngine()

    def fake_readiness_check(engine):
        readiness_ran_without_loop.append(not has_running_loop())

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)

    with TestClient(create_app(settings=synthetic_postgresql_settings())) as client:
        readiness_ran_without_loop.clear()
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert readiness_ran_without_loop == [True]


def test_postgresql_owned_engine_is_disposed_when_startup_readiness_fails(monkeypatch):
    created_engines = []
    request_served = False

    class FakeEngine:
        def __init__(self):
            self.dispose_count = 0

        def dispose(self):
            self.dispose_count += 1

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        engine = FakeEngine()
        created_engines.append(engine)
        return engine

    def fake_readiness_check(engine):
        raise DeliveryStoreReadinessError("synthetic readiness failure")

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)
    app = create_app(settings=synthetic_postgresql_settings())

    with pytest.raises(DeliveryStoreReadinessError):
        with TestClient(app) as client:
            request_served = True
            client.get("/health")

    assert len(created_engines) == 1
    assert created_engines[0].dispose_count == 1
    assert request_served is False
    assert app.state.runtime_resources.owns_engine is True


def test_postgresql_runtime_unavailable_database_fails_lifespan_without_memory_fallback():
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="postgresql",
        database_url="postgresql+psycopg://example_user:example_password@127.0.0.1:1/example_database",
        database_connect_timeout_seconds=1,
        _env_file=None,
    )
    app = create_app(settings=settings)

    with pytest.raises(DeliveryStoreReadinessError):
        with TestClient(app):
            pass

    assert not isinstance(app.state.delivery_store, InMemoryDeliveryStore)


class RecordingGitHubDeliveryClient:
    def __init__(self):
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1


def test_reconciliation_disabled_does_not_create_github_client():
    settings = Settings(webhook_secret="synthetic-secret", _env_file=None)
    app = create_app(settings=settings)

    assert app.state.github_delivery_client is None
    assert app.state.github_reconciliation_service.enabled is False
    assert app.state.github_redelivery_client is None
    assert app.state.github_redelivery_service.enabled is False


def test_reconciliation_enabled_creates_one_app_owned_client_and_closes_on_shutdown():
    app = create_app(settings=synthetic_reconciliation_settings())
    github_client = app.state.github_delivery_client

    assert github_client is not None

    with TestClient(app):
        assert app.state.github_delivery_client is github_client

    assert github_client._http_client.is_closed


def test_injected_github_client_is_reused_and_not_owned():
    github_client = RecordingGitHubDeliveryClient()
    app = create_app(settings=synthetic_reconciliation_settings(), github_delivery_client=github_client)

    with TestClient(app):
        assert app.state.github_delivery_client is github_client
        assert app.state.github_reconciliation_service.enabled is True

    assert github_client.close_count == 0


def test_github_client_closes_when_startup_fails_for_database_readiness(monkeypatch):
    created_engines = []

    class FakeEngine:
        def dispose(self):
            pass

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        engine = FakeEngine()
        created_engines.append(engine)
        return engine

    def fake_readiness_check(engine):
        raise DeliveryStoreReadinessError("synthetic readiness failure")

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="postgresql",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )
    app = create_app(settings=settings)
    github_client = app.state.github_delivery_client

    with pytest.raises(DeliveryStoreReadinessError):
        with TestClient(app):
            pass

    assert created_engines
    assert github_client._http_client.is_closed


def test_redelivery_disabled_does_not_create_write_client():
    app = create_app(settings=synthetic_reconciliation_settings())

    assert app.state.github_delivery_client is not None
    assert app.state.github_redelivery_client is None
    assert app.state.github_redelivery_service.enabled is False


def test_redelivery_enabled_creates_one_app_owned_write_client_and_closes_on_shutdown():
    app = create_app(settings=synthetic_redelivery_settings())
    read_client = app.state.github_delivery_client
    write_client = app.state.github_redelivery_client

    assert read_client is not None
    assert write_client is not None

    with TestClient(app):
        assert app.state.github_delivery_client is read_client
        assert app.state.github_redelivery_client is write_client

    assert read_client._http_client.is_closed
    assert write_client._http_client.is_closed


def test_injected_github_redelivery_client_is_reused_and_not_owned():
    github_delivery_client = RecordingGitHubDeliveryClient()
    github_redelivery_client = RecordingGitHubDeliveryClient()
    app = create_app(
        settings=synthetic_redelivery_settings(),
        github_delivery_client=github_delivery_client,
        github_redelivery_client=github_redelivery_client,
    )

    with TestClient(app):
        assert app.state.github_delivery_client is github_delivery_client
        assert app.state.github_redelivery_client is github_redelivery_client
        assert app.state.github_redelivery_service.enabled is True

    assert github_delivery_client.close_count == 0
    assert github_redelivery_client.close_count == 0


def test_github_redelivery_client_closes_when_startup_fails_for_database_readiness(monkeypatch):
    created_engines = []

    class FakeEngine:
        def dispose(self):
            pass

    def create_fake_engine(database_url, *, connect_timeout_seconds, pool_pre_ping):
        engine = FakeEngine()
        created_engines.append(engine)
        return engine

    def fake_readiness_check(engine):
        raise DeliveryStoreReadinessError("synthetic readiness failure")

    monkeypatch.setattr(app_runtime, "create_database_engine", create_fake_engine)
    monkeypatch.setattr(app_runtime, "verify_delivery_store_ready", fake_readiness_check)
    settings = Settings(
        webhook_secret="synthetic-secret",
        delivery_store_backend="postgresql",
        database_url="postgresql+psycopg://example_user:example_password@example-host:5432/example_database",
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_redelivery_enabled=True,
        github_repository_webhook_write_token=GITHUB_WRITE_TOKEN,
        _env_file=None,
    )
    app = create_app(settings=settings)
    read_client = app.state.github_delivery_client
    write_client = app.state.github_redelivery_client

    with pytest.raises(DeliveryStoreReadinessError):
        with TestClient(app):
            pass

    assert created_engines
    assert read_client._http_client.is_closed
    assert write_client._http_client.is_closed
