import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

from app.config import Settings
from app.factory import create_app
from app.integrations.github.models import GitHubDeliveryPage, GitHubDeliverySummary
from app.persistence.schema import delivery_attempts, github_deliveries
from app.storage.deliveries import DeliveryStoreReadinessError


pytestmark = pytest.mark.integration


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_SECRET = "test-webhook-secret"
MANAGEMENT_TOKEN = "synthetic-management-token-000001"
GITHUB_TOKEN = "synthetic-github-token"


class RecordingGitHubDeliveryClient:
    def __init__(self, pages: list[GitHubDeliveryPage]):
        self.pages = list(pages)
        self.calls = []

    async def list_repository_webhook_deliveries(self, *, owner, repository, hook_id, cursor=None):
        self.calls.append(
            {
                "owner": owner,
                "repository": repository,
                "hook_id": hook_id,
                "cursor": cursor,
            }
        )
        return self.pages.pop(0)

    async def aclose(self):
        pass


def make_github_delivery(delivery_id: int, *, guid: str = "delivery-001", redelivery: bool = False):
    return GitHubDeliverySummary(
        github_delivery_id=delivery_id,
        delivery_guid=guid,
        delivered_at=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        redelivery=redelivery,
        duration=0.2,
        status="OK",
        status_code=200,
        event="pull_request",
        action="opened",
        installation_id=111,
        repository_id=222,
        throttled_at=None,
    )


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
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )


def reconciliation_runtime_settings(database_url: str, *, max_pages: int = 5) -> Settings:
    settings = runtime_settings(database_url)
    return Settings(
        webhook_secret=settings.webhook_secret.get_secret_value(),
        max_events=settings.max_events,
        max_webhook_body_bytes=settings.max_webhook_body_bytes,
        delivery_store_backend="postgresql",
        database_url=database_url,
        database_connect_timeout_seconds=settings.database_connect_timeout_seconds,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_reconciliation_max_pages=max_pages,
        _env_file=None,
    )


def encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def management_headers(token: str = MANAGEMENT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
        unauthenticated_events_response = client.get("/events")
        events_response = client.get("/events", headers=management_headers())

    assert response.status_code == 200
    assert response.json()["message"] == "Webhook received"
    assert unauthenticated_events_response.status_code == 401
    assert events_response.status_code == 200
    assert events_response.json()["events"] == [response.json()["event"]]

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(github_deliveries)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 1
        assert (
            connection.execute(select(delivery_attempts.c.payload_sha256)).scalar_one()
            == hashlib.sha256(payload).hexdigest()
        )


def test_postgresql_runtime_v1_diagnostics_traverses_pages_and_survives_restart(
    database_url: str,
    engine,
):
    ingested_attempt_ids = []
    with TestClient(create_app(settings=runtime_settings(database_url))) as first_client:
        for index in range(4):
            payload = encode_json(
                {
                    "action": f"event-{index}",
                    "repository": {"full_name": f"octo/example-{index}"},
                    "sender": {"login": f"octocat-{index}"},
                }
            )
            response = post_webhook(first_client, payload, delivery_id=f"delivery-{index}")
            assert response.status_code == 200
            ingested_attempt_ids.append(response.json()["event"]["attempt_id"])

        first_page_response = first_client.get(
            "/api/v1/delivery-attempts?limit=2",
            headers=management_headers(),
        )
        assert first_page_response.status_code == 200
        first_page = first_page_response.json()
        second_page_response = first_client.get(
            f"/api/v1/delivery-attempts?limit=2&cursor={first_page['next_cursor']}",
            headers=management_headers(),
        )
        assert second_page_response.status_code == 200
        second_page = second_page_response.json()
        detail_response = first_client.get(
            f"/api/v1/delivery-attempts/{first_page['items'][0]['attempt_id']}",
            headers=management_headers(),
        )

    assert detail_response.status_code == 200
    traversed_attempt_ids = [item["attempt_id"] for item in first_page["items"] + second_page["items"]]
    assert traversed_attempt_ids == list(reversed(ingested_attempt_ids))
    assert len(traversed_attempt_ids) == len(set(traversed_attempt_ids))
    assert second_page["next_cursor"] is None
    assert detail_response.json()["attempt_id"] == first_page["items"][0]["attempt_id"]
    assert "delivery_id" not in detail_response.json()
    assert "raw_payload" not in detail_response.json()

    with TestClient(create_app(settings=runtime_settings(database_url))) as second_client:
        restart_response = second_client.get(
            "/api/v1/delivery-attempts?limit=100",
            headers=management_headers(),
        )

    assert restart_response.status_code == 200
    assert [item["attempt_id"] for item in restart_response.json()["items"]] == traversed_attempt_ids
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 4


def test_postgresql_runtime_v1_pagination_is_stable_when_new_attempt_arrives_between_pages(
    database_url: str,
    engine,
):
    with TestClient(create_app(settings=runtime_settings(database_url))) as client:
        original_attempt_ids = []
        for index in range(4):
            payload = encode_json({"action": f"event-{index}"})
            response = post_webhook(client, payload, delivery_id=f"delivery-{index}")
            assert response.status_code == 200
            original_attempt_ids.append(response.json()["event"]["attempt_id"])

        first_page_response = client.get(
            "/api/v1/delivery-attempts?limit=2",
            headers=management_headers(),
        )
        assert first_page_response.status_code == 200
        first_page = first_page_response.json()

        newer_payload = encode_json({"action": "newer"})
        newer_response = post_webhook(client, newer_payload, delivery_id="delivery-newer")
        assert newer_response.status_code == 200

        second_page_response = client.get(
            f"/api/v1/delivery-attempts?limit=2&cursor={first_page['next_cursor']}",
            headers=management_headers(),
        )
        assert second_page_response.status_code == 200
        second_page = second_page_response.json()

    traversed_attempt_ids = [item["attempt_id"] for item in first_page["items"] + second_page["items"]]
    assert traversed_attempt_ids == list(reversed(original_attempt_ids))
    assert newer_response.json()["event"]["attempt_id"] not in traversed_attempt_ids
    assert len(traversed_attempt_ids) == len(set(traversed_attempt_ids))
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 5


def test_postgresql_runtime_reconciles_attempt_with_mocked_github_multiple_matches(
    database_url: str,
    engine,
):
    github_client = RecordingGitHubDeliveryClient(
        [
            GitHubDeliveryPage(
                deliveries=[
                    make_github_delivery(100, redelivery=False),
                    make_github_delivery(101, redelivery=True),
                    make_github_delivery(102, guid="other-guid"),
                ],
                next_cursor=None,
            )
        ]
    )
    payload = encode_json(
        {
            "action": "opened",
            "repository": {"full_name": "octo/example"},
            "sender": {"login": "octocat"},
        }
    )

    with TestClient(
        create_app(
            settings=reconciliation_runtime_settings(database_url),
            github_delivery_client=github_client,
        )
    ) as client:
        webhook_response = post_webhook(client, payload, delivery_id="delivery-001")
        assert webhook_response.status_code == 200
        attempt_id = webhook_response.json()["event"]["attempt_id"]
        reconciliation_response = client.get(
            f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
            headers=management_headers(),
        )

    assert reconciliation_response.status_code == 200
    body = reconciliation_response.json()
    assert body["attempt_id"] == attempt_id
    assert body["delivery_guid"] == "delivery-001"
    assert body["hook_id"] == 12345
    assert [match["github_delivery_id"] for match in body["matches"]] == [100, 101]
    assert [match["redelivery"] for match in body["matches"]] == [False, True]
    assert body["search_complete"] is True
    assert github_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "cursor": None}
    ]
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 1


def test_postgresql_runtime_reconciliation_continues_bounded_search(
    database_url: str,
    engine,
):
    github_client = RecordingGitHubDeliveryClient(
        [
            GitHubDeliveryPage(deliveries=[make_github_delivery(100)], next_cursor="page-two"),
            GitHubDeliveryPage(deliveries=[make_github_delivery(101, redelivery=True)], next_cursor=None),
        ]
    )
    payload = encode_json({"repository": {"full_name": "octo/example"}})

    with TestClient(
        create_app(
            settings=reconciliation_runtime_settings(database_url, max_pages=1),
            github_delivery_client=github_client,
        )
    ) as client:
        webhook_response = post_webhook(client, payload, delivery_id="delivery-001")
        assert webhook_response.status_code == 200
        attempt_id = webhook_response.json()["event"]["attempt_id"]
        first_response = client.get(
            f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
            headers=management_headers(),
        )
        assert first_response.status_code == 200
        second_response = client.get(
            f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries"
            f"?cursor={first_response.json()['next_cursor']}",
            headers=management_headers(),
        )

    assert second_response.status_code == 200
    assert first_response.json()["search_complete"] is False
    assert [match["github_delivery_id"] for match in first_response.json()["matches"]] == [100]
    assert second_response.json()["search_complete"] is True
    assert [match["github_delivery_id"] for match in second_response.json()["matches"]] == [101]
    assert [call["cursor"] for call in github_client.calls] == [None, "page-two"]


def test_postgresql_runtime_persists_events_across_application_restart(
    database_url: str,
    engine,
):
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})

    with TestClient(create_app(settings=runtime_settings(database_url))) as first_client:
        first_response = post_webhook(first_client, payload)
        assert first_response.status_code == 200

    with TestClient(create_app(settings=runtime_settings(database_url))) as second_client:
        events_response = second_client.get("/events", headers=management_headers())

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
        events = client.get("/events", headers=management_headers()).json()["events"]

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
        events_response = client.get("/events", headers=management_headers())
        attempts_response = client.get("/api/v1/delivery-attempts", headers=management_headers())
        attempt_response = client.get(
            "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001",
            headers=management_headers(),
        )

        unauthenticated_events_response = client.get("/events")

    assert post_response.status_code == 503
    assert post_response.json() == {"detail": "Service unavailable"}
    assert events_response.status_code == 503
    assert events_response.json() == {"detail": "Service unavailable"}
    assert attempts_response.status_code == 503
    assert attempts_response.json() == {"detail": "Service unavailable"}
    assert attempt_response.status_code == 503
    assert attempt_response.json() == {"detail": "Service unavailable"}
    assert unauthenticated_events_response.status_code == 401
    command.upgrade(alembic_config, "head")
