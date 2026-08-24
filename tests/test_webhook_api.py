import hashlib
import hmac
import json
import asyncio
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.api.routes import is_json_content_type, read_bounded_body, validate_content_length
from app.config import Settings
from app.factory import create_app
from app.integrations.github.client import GitHubRedeliveryOutcomeUnknownError, GitHubRepositoryWebhookDeliveriesClient
from app.integrations.github.models import GitHubDeliveryPage, GitHubDeliverySummary
from app.services.github_reconciliation import (
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
    encode_reconciliation_cursor,
)
from app.storage.deliveries import DeliveryStoreError, InMemoryDeliveryStore


TEST_SECRET = "test-webhook-secret"
MANAGEMENT_TOKEN = "synthetic-management-token-000001"
GITHUB_TOKEN = "synthetic-github-token"
GITHUB_WRITE_TOKEN = "synthetic-github-write-token"


def encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def management_headers(token: str = MANAGEMENT_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def get_events(client: TestClient):
    return client.get("/events", headers=management_headers())


def get_delivery_attempts(client: TestClient, query: str = ""):
    return client.get(f"/api/v1/delivery-attempts{query}", headers=management_headers())


def make_github_delivery(delivery_id: int, guid: str = "delivery-001", redelivery: bool = False):
    return GitHubDeliverySummary(
        github_delivery_id=delivery_id,
        delivery_guid=guid,
        delivered_at=datetime.fromisoformat("2026-08-24T12:00:00+00:00"),
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


class RecordingGitHubDeliveryClient:
    def __init__(self, pages: list[GitHubDeliveryPage] | None = None, exc: Exception | None = None):
        self.pages = list(pages or [])
        self.exc = exc
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
        if self.exc is not None:
            raise self.exc
        return self.pages.pop(0)

    async def aclose(self):
        pass


class RecordingGitHubRedeliveryClient:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = []

    async def request_repository_webhook_redelivery(self, *, owner, repository, hook_id, github_delivery_id):
        self.calls.append(
            {
                "owner": owner,
                "repository": repository,
                "hook_id": hook_id,
                "github_delivery_id": github_delivery_id,
            }
        )
        if self.exc is not None:
            raise self.exc

    async def aclose(self):
        pass


@pytest.fixture
def delivery_store():
    return InMemoryDeliveryStore(max_events=50)


@pytest.fixture
def client(delivery_store):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        max_events=delivery_store.max_events,
        max_webhook_body_bytes=4096,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    return TestClient(create_app(settings=settings, delivery_store=delivery_store))


def post_webhook(client: TestClient, payload: bytes, headers: dict[str, str] | None = None):
    request_headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-001",
        "X-GitHub-Hook-ID": "12345",
        "X-Hub-Signature-256": signature_for(payload),
    }
    if headers:
        request_headers.update(headers)
    return client.post("/webhook/github", content=payload, headers=request_headers)


def test_health_endpoint_contract(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_reports_memory_runtime_ready(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_valid_signed_webhook_stores_and_returns_event_metadata(client, delivery_store):
    payload = encode_json(
        {
            "action": "opened",
            "repository": {"full_name": "octo/example"},
            "sender": {"login": "octocat"},
        }
    )

    response = post_webhook(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Webhook received"

    event = body["event"]
    assert event["attempt_id"]
    assert event["event"] == "pull_request"
    assert event["delivery_id"] == "delivery-001"
    assert event["hook_id"] == "12345"
    assert event["installation_target_id"] is None
    assert event["installation_target_type"] is None
    assert event["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert event["action"] == "opened"
    assert event["repository"] == "octo/example"
    assert event["sender"] == "octocat"
    assert datetime.fromisoformat(event["received_at"]).tzinfo is not None

    stored_events = [stored_event.to_dict() for stored_event in delivery_store.list_recent()]
    assert len(stored_events) == 1
    assert stored_events[0] == event


def test_webhook_missing_signature_returns_current_unauthorized_behavior(client, delivery_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload, {"X-Hub-Signature-256": ""})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert delivery_store.list_recent() == []


def test_webhook_invalid_signature_returns_current_unauthorized_behavior(client, delivery_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload, {"X-Hub-Signature-256": "sha256=bad"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert delivery_store.list_recent() == []


def test_webhook_signature_for_different_payload_is_rejected(client, delivery_store):
    signed_payload = encode_json({"action": "opened"})
    transmitted_payload = encode_json({"action": "closed"})

    response = post_webhook(
        client,
        transmitted_payload,
        {"X-Hub-Signature-256": signature_for(signed_payload)},
    )

    assert response.status_code == 401
    assert delivery_store.list_recent() == []


def test_malformed_json_with_valid_signature_returns_current_bad_request_behavior(client, delivery_store):
    payload = b'{"action":'

    response = post_webhook(client, payload)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON payload"}
    assert delivery_store.list_recent() == []


def test_application_json_with_charset_parameter_is_accepted(client):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {
            "Content-Type": "application/json; charset=utf-8",
            "X-Hub-Signature-256": signature_for(payload),
        },
    )

    assert response.status_code == 200


@pytest.mark.parametrize("content_type", [None, "application/x-www-form-urlencoded", "text/plain"])
def test_unsupported_or_missing_content_type_is_rejected(client, delivery_store, content_type):
    payload = encode_json({"action": "opened"})
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-001",
        "X-GitHub-Hook-ID": "12345",
        "X-Hub-Signature-256": signature_for(payload),
    }
    if content_type is not None:
        headers["Content-Type"] = content_type

    response = client.post("/webhook/github", content=payload, headers=headers)

    assert response.status_code == 415
    assert response.json() == {"detail": "Unsupported media type"}
    assert delivery_store.list_recent() == []


@pytest.mark.parametrize(
    ("header_name", "header_value", "detail"),
    [
        ("X-GitHub-Event", None, "Missing GitHub event"),
        ("X-GitHub-Event", " ", "Missing GitHub event"),
        ("X-GitHub-Delivery", None, "Missing GitHub delivery ID"),
        ("X-GitHub-Delivery", " ", "Missing GitHub delivery ID"),
        ("X-GitHub-Hook-ID", None, "Invalid GitHub hook ID"),
        ("X-GitHub-Hook-ID", " ", "Invalid GitHub hook ID"),
        ("X-GitHub-Hook-ID", "not-a-number", "Invalid GitHub hook ID"),
        ("X-GitHub-Hook-ID", "0", "Invalid GitHub hook ID"),
        ("X-GitHub-Hook-ID", "-1", "Invalid GitHub hook ID"),
    ],
)
def test_required_github_delivery_headers_are_validated(
    client,
    delivery_store,
    header_name,
    header_value,
    detail,
):
    payload = encode_json({"action": "opened"})
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-001",
        "X-GitHub-Hook-ID": "12345",
        "X-Hub-Signature-256": signature_for(payload),
    }
    if header_value is None:
        headers.pop(header_name)
    else:
        headers[header_name] = header_value

    response = client.post("/webhook/github", content=payload, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert delivery_store.list_recent() == []


def test_legacy_sha1_signature_header_alone_is_insufficient(client, delivery_store):
    payload = encode_json({"action": "opened"})
    legacy_signature = "sha1=" + hmac.new(TEST_SECRET.encode("utf-8"), payload, hashlib.sha1).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Hook-ID": "12345",
            "X-Hub-Signature": legacy_signature,
        },
    )

    assert response.status_code == 401
    assert delivery_store.list_recent() == []


def test_payload_at_configured_limit_is_accepted(delivery_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=13, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert len(payload) == 13
    assert response.status_code == 200


def test_payload_below_configured_limit_is_accepted(delivery_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=14, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert response.status_code == 200


def test_payload_above_configured_limit_is_rejected_without_storing(delivery_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=12, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert response.status_code == 413
    assert response.json() == {"detail": "Payload too large"}
    assert delivery_store.list_recent() == []


def test_oversized_declared_content_length_is_rejected_before_body_processing(client, delivery_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {"Content-Length": "4097", "X-Hub-Signature-256": signature_for(payload)},
    )

    assert response.status_code == 413
    assert delivery_store.list_recent() == []


@pytest.mark.parametrize("content_length", ["not-a-number", "-1", " -1 "])
def test_invalid_declared_content_length_is_rejected_by_route(client, delivery_store, content_length):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {"Content-Length": content_length, "X-Hub-Signature-256": signature_for(payload)},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Content-Length"}
    assert delivery_store.list_recent() == []


@pytest.mark.parametrize("content_length", ["not-a-number", "-1", " -1 "])
def test_invalid_declared_content_length_helper_raises_controlled_client_error(content_length):
    with pytest.raises(HTTPException) as exc_info:
        validate_content_length(content_length, max_body_bytes=10)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid Content-Length"


@pytest.mark.parametrize("content_length", [None, "0", "10", " 10 "])
def test_valid_or_missing_declared_content_length_helper_is_accepted(content_length):
    validate_content_length(content_length, max_body_bytes=10)


def test_oversized_declared_content_length_helper_raises_payload_too_large():
    with pytest.raises(HTTPException) as exc_info:
        validate_content_length("11", max_body_bytes=10)

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "Payload too large"


def test_content_type_helper_accepts_parameters_without_exact_string_matching():
    assert is_json_content_type("application/json; charset=utf-8")
    assert not is_json_content_type("application/x-www-form-urlencoded")


def test_streamed_body_overflow_is_rejected_without_relying_on_content_length():
    async def stream():
        yield b"12345"
        yield b"6"

    request = SimpleNamespace(stream=stream)

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(read_bounded_body, request, 5)

    assert exc_info.value.status_code == 413


def test_webhook_ingestion_uses_most_recent_first_ordering(client):
    first_payload = encode_json({"action": "opened", "repository": {"full_name": "octo/one"}})
    second_payload = encode_json({"action": "closed", "repository": {"full_name": "octo/two"}})

    first_response = post_webhook(
        client,
        first_payload,
        {"X-GitHub-Delivery": "delivery-001", "X-Hub-Signature-256": signature_for(first_payload)},
    )
    second_response = post_webhook(
        client,
        second_payload,
        {"X-GitHub-Delivery": "delivery-002", "X-Hub-Signature-256": signature_for(second_payload)},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    response = get_events(client)

    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert [event["delivery_id"] for event in response.json()["events"]] == [
        "delivery-002",
        "delivery-001",
    ]


def test_events_endpoint_exposes_current_in_memory_collection(client):
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})

    webhook_response = post_webhook(client, payload)
    events_response = get_events(client)

    assert webhook_response.status_code == 200
    assert events_response.status_code == 200
    assert events_response.json() == {"count": 1, "events": [webhook_response.json()["event"]]}


def test_events_endpoint_is_not_available_when_management_api_is_disabled(delivery_store):
    settings = Settings(webhook_secret=TEST_SECRET, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get("/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {MANAGEMENT_TOKEN}"},
        {"Authorization": "Token synthetic-management-token-000001"},
        {"Authorization": "Bearer"},
    ],
)
def test_disabled_management_api_returns_not_found_before_credential_validation(delivery_store, headers):
    settings = Settings(webhook_secret=TEST_SECRET, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get("/events", headers=headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"Authorization": "Bearer wrong-management-token-000001"},
        {"Authorization": "Token synthetic-management-token-000001"},
        {"Authorization": "Bearer"},
    ],
)
def test_events_endpoint_requires_valid_management_bearer_token(delivery_store, headers):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get("/events", headers=headers or {})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_github_webhook_secret_cannot_authenticate_management_endpoint(delivery_store):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get("/events", headers=management_headers(TEST_SECRET))

    assert response.status_code == 401


class ListProbeDeliveryStore(InMemoryDeliveryStore):
    def __init__(self):
        super().__init__(max_events=50)
        self.list_calls = 0
        self.page_calls = 0
        self.lookup_calls = 0

    def list_recent(self):
        self.list_calls += 1
        return super().list_recent()

    def list_attempts_page(self, *, limit, after=None):
        self.page_calls += 1
        return super().list_attempts_page(limit=limit, after=after)

    def get_attempt(self, attempt_id):
        self.lookup_calls += 1
        return super().get_attempt(attempt_id)


@pytest.mark.parametrize(
    ("management_api_enabled", "headers", "expected_status"),
    [
        (False, {}, 404),
        (True, {}, 401),
        (True, {"Authorization": "Bearer wrong-management-token-000001"}, 401),
    ],
)
def test_unauthorized_management_requests_do_not_access_delivery_store(
    management_api_enabled,
    headers,
    expected_status,
):
    store = ListProbeDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=management_api_enabled,
        management_api_token=MANAGEMENT_TOKEN if management_api_enabled else None,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = client.get("/events", headers=headers)

    assert response.status_code == expected_status
    assert store.list_calls == 0


@pytest.mark.parametrize(
    ("path", "management_api_enabled", "headers", "expected_status"),
    [
        ("/api/v1/delivery-attempts", False, {}, 404),
        ("/api/v1/delivery-attempts", True, {}, 401),
        ("/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001", False, {}, 404),
        ("/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001", True, {}, 401),
    ],
)
def test_unauthorized_v1_management_requests_do_not_access_delivery_store(
    path,
    management_api_enabled,
    headers,
    expected_status,
):
    store = ListProbeDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=management_api_enabled,
        management_api_token=MANAGEMENT_TOKEN if management_api_enabled else None,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = client.get(path, headers=headers)

    assert response.status_code == expected_status
    assert store.page_calls == 0
    assert store.lookup_calls == 0


def test_valid_management_token_allows_events_store_access():
    store = ListProbeDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = get_events(client)

    assert response.status_code == 200
    assert response.json() == {"count": 0, "events": []}
    assert store.list_calls == 1


def test_events_openapi_declares_bearer_auth_when_management_enabled(delivery_store):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_redelivery_enabled=True,
        github_repository_webhook_write_token=GITHUB_WRITE_TOKEN,
        _env_file=None,
    )
    app = create_app(settings=settings, delivery_store=delivery_store)

    openapi = app.openapi()

    assert openapi["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert openapi["paths"]["/events"]["get"]["security"] == [{"HTTPBearer": []}]
    assert openapi["paths"]["/events"]["get"]["deprecated"] is True
    assert openapi["paths"]["/api/v1/delivery-attempts"]["get"]["security"] == [{"HTTPBearer": []}]
    assert openapi["paths"]["/api/v1/delivery-attempts/{attempt_id}"]["get"]["security"] == [{"HTTPBearer": []}]
    reconciliation_operation = openapi["paths"]["/api/v1/delivery-attempts/{attempt_id}/github-deliveries"]["get"]
    redelivery_operation = openapi["paths"][
        "/api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery"
    ]["post"]
    assert reconciliation_operation["security"] == [{"HTTPBearer": []}]
    assert "Reconcile" in reconciliation_operation["summary"]
    assert redelivery_operation["security"] == [{"HTTPBearer": []}]
    assert "redeliver" in redelivery_operation["summary"].lower()
    list_parameters = {
        parameter["name"]: parameter
        for parameter in openapi["paths"]["/api/v1/delivery-attempts"]["get"]["parameters"]
    }
    assert list_parameters["limit"]["in"] == "query"
    assert list_parameters["limit"]["required"] is False
    assert list_parameters["limit"]["schema"]["anyOf"][0]["type"] == "string"
    assert "integer from 1 to 100" in list_parameters["limit"]["description"]
    assert "Defaults to 50" in list_parameters["limit"]["description"]
    assert list_parameters["cursor"]["in"] == "query"
    assert list_parameters["cursor"]["required"] is False
    assert "Opaque pagination cursor" in list_parameters["cursor"]["description"]
    detail_parameters = {
        parameter["name"]: parameter
        for parameter in openapi["paths"]["/api/v1/delivery-attempts/{attempt_id}"]["get"]["parameters"]
    }
    assert detail_parameters["attempt_id"]["in"] == "path"
    assert detail_parameters["attempt_id"]["required"] is True
    assert detail_parameters["attempt_id"]["schema"]["type"] == "string"
    assert detail_parameters["attempt_id"]["schema"]["format"] == "uuid"
    assert "delivery attempt UUID" in detail_parameters["attempt_id"]["description"]
    reconciliation_parameters = {
        parameter["name"]: parameter
        for parameter in reconciliation_operation["parameters"]
    }
    assert reconciliation_parameters["attempt_id"]["schema"]["format"] == "uuid"
    assert "Opaque reconciliation continuation cursor" in reconciliation_parameters["cursor"]["description"]
    redelivery_parameters = {parameter["name"]: parameter for parameter in redelivery_operation["parameters"]}
    assert redelivery_parameters["attempt_id"]["schema"]["format"] == "uuid"
    assert "GitHub upstream numeric" in redelivery_parameters["github_delivery_id"]["description"]


def reconciliation_client(
    delivery_store,
    github_client: RecordingGitHubDeliveryClient,
    *,
    max_pages: int = 5,
):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        max_events=delivery_store.max_events,
        max_webhook_body_bytes=4096,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_reconciliation_max_pages=max_pages,
        _env_file=None,
    )
    return TestClient(
        create_app(
            settings=settings,
            delivery_store=delivery_store,
            github_delivery_client=github_client,
        )
    )


def redelivery_client(
    delivery_store,
    github_client: RecordingGitHubDeliveryClient,
    github_redelivery_client: RecordingGitHubRedeliveryClient,
    *,
    max_pages: int = 5,
):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        max_events=delivery_store.max_events,
        max_webhook_body_bytes=4096,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        github_redelivery_enabled=True,
        github_repository_webhook_write_token=GITHUB_WRITE_TOKEN,
        github_reconciliation_max_pages=max_pages,
        _env_file=None,
    )
    return TestClient(
        create_app(
            settings=settings,
            delivery_store=delivery_store,
            github_delivery_client=github_client,
            github_redelivery_client=github_redelivery_client,
        )
    )


def test_github_reconciliation_endpoint_returns_multiple_upstream_matches(delivery_store):
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
    client = reconciliation_client(delivery_store, github_client)
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})
    webhook_response = post_webhook(client, payload)
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["attempt_id"] == attempt_id
    assert body["delivery_guid"] == "delivery-001"
    assert body["hook_id"] == 12345
    assert body["repository"] == "octo/example"
    assert [match["github_delivery_id"] for match in body["matches"]] == [100, 101]
    assert [match["redelivery"] for match in body["matches"]] == [False, True]
    assert body["search_complete"] is True
    assert body["next_cursor"] is None
    assert "delivery_id" not in body
    assert "raw_payload" not in body
    assert github_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "cursor": None}
    ]


def test_github_reconciliation_feature_disabled_returns_not_found_before_store_or_github(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(
        create_app(
            settings=settings,
            delivery_store=delivery_store,
            github_delivery_client=github_client,
        )
    )

    response = client.get(
        "/api/v1/delivery-attempts/not-a-uuid/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 404
    assert github_client.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/delivery-attempts/not-a-uuid/github-deliveries",
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries?cursor=bad",
    ],
)
def test_unauthorized_github_reconciliation_rejects_before_validation_or_github(delivery_store, path):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    client = reconciliation_client(delivery_store, github_client)

    response = client.get(path)

    assert response.status_code == 401
    assert github_client.calls == []


def test_missing_local_attempt_returns_not_found_without_github_call(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    client = reconciliation_client(delivery_store, github_client)

    response = client.get(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Delivery attempt not found"}
    assert github_client.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repository": {"full_name": "octo"}},
        {"repository": {"full_name": "octo/example/extra"}},
    ],
)
def test_unsupported_repository_target_returns_conflict_without_github_call(delivery_store, payload):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    client = reconciliation_client(delivery_store, github_client)
    webhook_response = post_webhook(client, encode_json(payload))
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Delivery attempt is not eligible for repository webhook reconciliation"
    }
    assert github_client.calls == []


def test_non_repository_installation_target_returns_conflict_without_github_call(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    client = reconciliation_client(delivery_store, github_client)
    payload = encode_json({"repository": {"full_name": "octo/example"}})
    webhook_response = post_webhook(
        client,
        payload,
        {
            "X-GitHub-Hook-Installation-Target-ID": "111",
            "X-GitHub-Hook-Installation-Target-Type": "organization",
            "X-Hub-Signature-256": signature_for(payload),
        },
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 409
    assert github_client.calls == []


def test_github_reconciliation_bound_search_returns_continuation_cursor(delivery_store):
    github_client = RecordingGitHubDeliveryClient(
        [GitHubDeliveryPage(deliveries=[make_github_delivery(100)], next_cursor="upstream-next")]
    )
    client = reconciliation_client(delivery_store, github_client, max_pages=1)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["search_complete"] is False
    assert body["next_cursor"] is not None


def test_github_reconciliation_continuation_cursor_mismatch_returns_bad_request(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    client = reconciliation_client(delivery_store, github_client)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]
    cursor = encode_reconciliation_cursor(
        attempt_id=UUID("00000000-0000-0000-0000-000000000999"),
        upstream_cursor="next",
    )

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries?cursor={cursor}",
        headers=management_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or expired reconciliation cursor"}
    assert github_client.calls == []


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_detail"),
    [
        (GitHubUpstreamUnavailableError("hidden upstream body"), 503, "Service unavailable"),
        (GitHubUpstreamProtocolError("hidden upstream body"), 502, "Upstream service unavailable"),
    ],
)
def test_github_reconciliation_maps_upstream_errors_without_leaking_details(
    delivery_store,
    exc,
    expected_status,
    expected_detail,
):
    github_client = RecordingGitHubDeliveryClient(exc=exc)
    client = reconciliation_client(delivery_store, github_client)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "hidden upstream body" not in response.text


@pytest.mark.parametrize(
    ("status_code", "response_headers", "expected_status", "expected_detail"),
    [
        (403, {"X-RateLimit-Remaining": "0"}, 503, "Service unavailable"),
        (403, {"Retry-After": "60"}, 503, "Service unavailable"),
        (403, {}, 502, "Upstream service unavailable"),
        (429, {}, 503, "Service unavailable"),
    ],
)
def test_github_reconciliation_maps_rate_limit_classification_without_leaking_details(
    delivery_store,
    status_code,
    response_headers,
    expected_status,
    expected_detail,
):
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(
            status_code,
            json={"message": "hidden upstream body"},
            headers=response_headers,
        )

    github_client = GitHubRepositoryWebhookDeliveriesClient(
        token=GITHUB_TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )
    client = reconciliation_client(delivery_store, github_client)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries",
        headers=management_headers(),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "hidden upstream body" not in response.text
    assert len(seen_requests) == 1


def test_github_redelivery_feature_disabled_returns_not_found_before_validation_or_github(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        github_reconciliation_enabled=True,
        github_repository_webhook_token=GITHUB_TOKEN,
        _env_file=None,
    )
    client = TestClient(
        create_app(
            settings=settings,
            delivery_store=delivery_store,
            github_delivery_client=github_client,
            github_redelivery_client=github_redelivery_client,
        )
    )

    response = client.post(
        "/api/v1/delivery-attempts/not-a-uuid/github-deliveries/not-an-int/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 404
    assert github_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/delivery-attempts/not-a-uuid/github-deliveries/not-an-int/redelivery",
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries/100/redelivery",
    ],
)
def test_unauthorized_github_redelivery_rejects_before_validation_or_github(delivery_store, path):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)

    response = client.post(path)

    assert response.status_code == 401
    assert github_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.parametrize(
    ("attempt_id", "github_delivery_id", "expected_detail"),
    [
        ("not-a-uuid", "100", "Invalid attempt_id"),
        ("00000000-0000-0000-0000-000000000001", "not-an-int", "Invalid github_delivery_id"),
        ("00000000-0000-0000-0000-000000000001", "0", "Invalid github_delivery_id"),
    ],
)
def test_github_redelivery_validates_path_identifiers_without_github_call(
    delivery_store,
    attempt_id,
    github_delivery_id,
    expected_detail,
):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)

    response = client.post(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": expected_detail}
    assert github_client.calls == []
    assert github_redelivery_client.calls == []


def test_missing_local_attempt_redelivery_returns_not_found_without_github_call(delivery_store):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)

    response = client.post(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries/100/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Delivery attempt not found"}
    assert github_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"repository": {"full_name": "octo"}},
        {"repository": {"full_name": "octo/example/extra"}},
    ],
)
def test_unsupported_redelivery_target_returns_conflict_without_github_call(delivery_store, payload):
    github_client = RecordingGitHubDeliveryClient([GitHubDeliveryPage(deliveries=[], next_cursor=None)])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)
    webhook_response = post_webhook(client, encode_json(payload))
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.post(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries/100/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Delivery attempt is not eligible for repository webhook redelivery"
    }
    assert github_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.parametrize(
    "page",
    [
        GitHubDeliveryPage(deliveries=[make_github_delivery(101)], next_cursor=None),
        GitHubDeliveryPage(deliveries=[make_github_delivery(100, guid="other-guid")], next_cursor=None),
        GitHubDeliveryPage(deliveries=[make_github_delivery(101)], next_cursor="more-history"),
    ],
)
def test_unverified_github_redelivery_target_returns_conflict_without_mutation(delivery_store, page):
    github_client = RecordingGitHubDeliveryClient([page])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client, max_pages=1)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.post(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries/100/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "GitHub delivery could not be verified for this local attempt"}
    assert len(github_client.calls) == 1
    assert github_redelivery_client.calls == []


def test_github_redelivery_endpoint_accepts_verified_exact_upstream_record(delivery_store):
    github_client = RecordingGitHubDeliveryClient(
        [
            GitHubDeliveryPage(
                deliveries=[
                    make_github_delivery(100, redelivery=False),
                    make_github_delivery(101, redelivery=True),
                ],
                next_cursor=None,
            )
        ]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.post(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries/101/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "attempt_id": attempt_id,
        "delivery_guid": "delivery-001",
        "hook_id": 12345,
        "github_delivery_id": 101,
        "status": "accepted",
    }
    assert github_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "cursor": None}
    ]
    assert github_redelivery_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "github_delivery_id": 101}
    ]
    assert len(delivery_store.list_recent()) == 1


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_detail"),
    [
        (GitHubUpstreamUnavailableError("hidden upstream body"), 503, "Service unavailable"),
        (GitHubUpstreamProtocolError("hidden upstream body"), 502, "Upstream service unavailable"),
        (
            GitHubRedeliveryOutcomeUnknownError("hidden upstream body"),
            503,
            "GitHub redelivery submission outcome could not be confirmed; reconcile before retrying",
        ),
    ],
)
def test_github_redelivery_maps_upstream_errors_without_leaking_details(
    delivery_store,
    exc,
    expected_status,
    expected_detail,
):
    github_client = RecordingGitHubDeliveryClient(
        [GitHubDeliveryPage(deliveries=[make_github_delivery(100)], next_cursor=None)]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient(exc=exc)
    client = redelivery_client(delivery_store, github_client, github_redelivery_client)
    webhook_response = post_webhook(
        client,
        encode_json({"repository": {"full_name": "octo/example"}}),
    )
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.post(
        f"/api/v1/delivery-attempts/{attempt_id}/github-deliveries/100/redelivery",
        headers=management_headers(),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "hidden upstream body" not in response.text
    assert len(github_client.calls) == 1
    assert len(github_redelivery_client.calls) == 1


def test_github_upstream_outage_does_not_affect_ready(delivery_store):
    github_client = RecordingGitHubDeliveryClient(exc=GitHubUpstreamUnavailableError("synthetic outage"))
    client = reconciliation_client(delivery_store, github_client)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert github_client.calls == []


def test_github_upstream_outage_does_not_affect_webhook_ingestion(delivery_store):
    github_client = RecordingGitHubDeliveryClient(exc=GitHubUpstreamUnavailableError("synthetic outage"))
    client = reconciliation_client(delivery_store, github_client)
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})

    response = post_webhook(client, payload)

    assert response.status_code == 200
    assert delivery_store.list_recent()
    assert github_client.calls == []


def test_delivery_attempts_list_endpoint_returns_v1_response_shape(client):
    first_payload = encode_json({"action": "opened", "repository": {"full_name": "octo/one"}})
    second_payload = encode_json({"action": "closed", "sender": {"login": "octocat"}})
    first_response = post_webhook(
        client,
        first_payload,
        {"X-GitHub-Delivery": "delivery-001", "X-Hub-Signature-256": signature_for(first_payload)},
    )
    second_response = post_webhook(
        client,
        second_payload,
        {"X-GitHub-Delivery": "delivery-002", "X-Hub-Signature-256": signature_for(second_payload)},
    )

    response = get_delivery_attempts(client, "?limit=1")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None
    item = body["items"][0]
    assert item["attempt_id"] == second_response.json()["event"]["attempt_id"]
    assert item["delivery_guid"] == "delivery-002"
    assert item["hook_id"] == 12345
    assert item["event_type"] == "pull_request"
    assert item["payload_sha256"] == hashlib.sha256(second_payload).hexdigest()
    assert "delivery_id" not in item
    assert "id" not in item
    assert "raw_payload" not in item
    assert "signature" not in item


def test_delivery_attempts_list_traverses_pages_without_duplicates(client):
    attempt_ids = []
    for index in range(3):
        payload = encode_json({"action": f"event-{index}"})
        response = post_webhook(
            client,
            payload,
            {
                "X-GitHub-Delivery": f"delivery-{index}",
                "X-Hub-Signature-256": signature_for(payload),
            },
        )
        assert response.status_code == 200
        attempt_ids.append(response.json()["event"]["attempt_id"])

    first_page = get_delivery_attempts(client, "?limit=2").json()
    second_page = get_delivery_attempts(client, f"?limit=2&cursor={first_page['next_cursor']}").json()
    traversed_attempt_ids = [item["attempt_id"] for item in first_page["items"] + second_page["items"]]

    assert traversed_attempt_ids == list(reversed(attempt_ids))
    assert len(traversed_attempt_ids) == len(set(traversed_attempt_ids))
    assert second_page["next_cursor"] is None


def test_delivery_attempts_detail_endpoint_finds_attempt_by_attempt_id(client):
    payload = encode_json({"action": "opened"})
    webhook_response = post_webhook(client, payload)
    attempt_id = webhook_response.json()["event"]["attempt_id"]

    response = client.get(f"/api/v1/delivery-attempts/{attempt_id}", headers=management_headers())

    assert response.status_code == 200
    assert response.json()["attempt_id"] == attempt_id
    assert response.json()["delivery_guid"] == "delivery-001"


def test_delivery_attempts_detail_valid_absent_uuid_returns_not_found(client):
    response = client.get(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000999",
        headers=management_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Delivery attempt not found"}


def test_delivery_attempts_detail_malformed_uuid_returns_controlled_validation(client):
    response = client.get("/api/v1/delivery-attempts/not-a-uuid", headers=management_headers())

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid attempt_id"}


def test_delivery_attempts_malformed_cursor_returns_bad_request(client):
    response = get_delivery_attempts(client, "?cursor=not-valid")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or expired cursor"}


@pytest.mark.parametrize("query", ["?limit=0", "?limit=101", "?limit=abc"])
def test_delivery_attempts_invalid_limit_returns_validation_error(client, query):
    response = get_delivery_attempts(client, query)

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid limit"}


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/api/v1/delivery-attempts?limit=0&cursor=not-valid", {}),
        ("/api/v1/delivery-attempts/not-a-uuid", {"Authorization": f"Bearer {MANAGEMENT_TOKEN}"}),
    ],
)
def test_disabled_management_api_hides_v1_diagnostics_before_parameter_validation(
    delivery_store,
    path,
    headers,
):
    settings = Settings(webhook_secret=TEST_SECRET, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get(path, headers=headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/delivery-attempts?limit=0&cursor=not-valid",
        "/api/v1/delivery-attempts/not-a-uuid",
    ],
)
def test_unauthorized_v1_diagnostics_reject_before_parameter_validation(delivery_store, path):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    response = client.get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_management_token_cannot_substitute_for_invalid_github_hmac(client, delivery_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {
            "X-Hub-Signature-256": f"sha256={MANAGEMENT_TOKEN}",
            "Authorization": f"Bearer {MANAGEMENT_TOKEN}",
        },
    )

    assert response.status_code == 401
    assert delivery_store.list_recent() == []


class FailingDeliveryStore:
    max_events = 50

    def add(self, attempt):
        raise DeliveryStoreError("synthetic storage failure")

    def list_recent(self):
        raise DeliveryStoreError("synthetic storage failure")

    def list_attempts_page(self, *, limit, after=None):
        raise DeliveryStoreError("synthetic storage failure")

    def get_attempt(self, attempt_id):
        raise DeliveryStoreError("synthetic storage failure")


def test_valid_webhook_returns_service_unavailable_when_persistence_fails():
    store = FailingDeliveryStore()
    settings = Settings(webhook_secret=TEST_SECRET, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=store))
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


def test_invalid_signature_does_not_become_persistence_failure():
    store = FailingDeliveryStore()
    settings = Settings(webhook_secret=TEST_SECRET, _env_file=None)
    client = TestClient(create_app(settings=settings, delivery_store=store))
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload, {"X-Hub-Signature-256": "sha256=bad"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}


def test_events_endpoint_returns_service_unavailable_when_listing_fails():
    store = FailingDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = get_events(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


def test_delivery_attempts_list_returns_service_unavailable_when_listing_fails():
    store = FailingDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = get_delivery_attempts(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


def test_delivery_attempts_detail_returns_service_unavailable_when_lookup_fails():
    store = FailingDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))

    response = client.get(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001",
        headers=management_headers(),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


class LoopRecordingDeliveryStore:
    def __init__(self):
        self.add_ran_without_active_loop: bool | None = None
        self.list_ran_without_active_loop: bool | None = None
        self._store = InMemoryDeliveryStore(max_events=50)

    def add(self, attempt):
        self.add_ran_without_active_loop = not _has_running_loop()
        self._store.add(attempt)

    def list_recent(self):
        self.list_ran_without_active_loop = not _has_running_loop()
        return self._store.list_recent()

    def list_attempts_page(self, *, limit, after=None):
        return self._store.list_attempts_page(limit=limit, after=after)

    def get_attempt(self, attempt_id):
        return self._store.get_attempt(attempt_id)


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_webhook_and_events_store_calls_run_outside_active_event_loop():
    store = LoopRecordingDeliveryStore()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings, delivery_store=store))
    payload = encode_json({"action": "opened"})

    webhook_response = post_webhook(client, payload)
    events_response = get_events(client)

    assert webhook_response.status_code == 200
    assert events_response.status_code == 200
    assert store.add_ran_without_active_loop is True
    assert store.list_ran_without_active_loop is True


def test_delivery_store_respects_configured_maximum_length(client, delivery_store):
    for index in range(delivery_store.max_events + 1):
        payload = encode_json({"action": f"event-{index}"})
        response = post_webhook(
            client,
            payload,
            {
                "X-GitHub-Delivery": f"delivery-{index:03d}",
                "X-Hub-Signature-256": signature_for(payload),
            },
        )
        assert response.status_code == 200

    stored_events = [event.to_dict() for event in delivery_store.list_recent()]
    assert len(stored_events) == delivery_store.max_events
    assert stored_events[0]["delivery_id"] == f"delivery-{delivery_store.max_events:03d}"
    assert stored_events[-1]["delivery_id"] == "delivery-001"


def test_missing_required_event_and_delivery_headers_are_rejected(client):
    payload = encode_json({"action": "opened"})

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Hook-ID": "12345",
            "X-Hub-Signature-256": signature_for(payload),
        },
    )

    assert response.status_code == 400


def test_unusual_delivery_and_event_values_are_stored_without_event_allowlist(client):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {
            "X-GitHub-Event": "unexpected.event/value",
            "X-GitHub-Delivery": "delivery with spaces",
        },
    )

    assert response.status_code == 200
    assert response.json()["event"]["event"] == "unexpected.event/value"
    assert response.json()["event"]["delivery_id"] == "delivery with spaces"


def test_hook_id_and_installation_target_metadata_are_captured(client):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {
            "X-GitHub-Hook-ID": "67890",
            "X-GitHub-Hook-Installation-Target-ID": "111",
            "X-GitHub-Hook-Installation-Target-Type": "repository",
            "X-Hub-Signature-256": signature_for(payload),
        },
    )

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["hook_id"] == "67890"
    assert event["installation_target_id"] == "111"
    assert event["installation_target_type"] == "repository"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-GitHub-Hook-Installation-Target-ID": "0", "X-GitHub-Hook-Installation-Target-Type": "repository"},
        {"X-GitHub-Hook-Installation-Target-ID": "abc", "X-GitHub-Hook-Installation-Target-Type": "repository"},
        {"X-GitHub-Hook-Installation-Target-ID": "111", "X-GitHub-Hook-Installation-Target-Type": " "},
        {"X-GitHub-Hook-Installation-Target-ID": "111"},
        {"X-GitHub-Hook-Installation-Target-Type": "repository"},
    ],
)
def test_invalid_installation_target_metadata_is_rejected(client, delivery_store, headers):
    payload = encode_json({"action": "opened"})
    headers["X-Hub-Signature-256"] = signature_for(payload)

    response = post_webhook(client, payload, headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid GitHub installation target metadata"}
    assert delivery_store.list_recent() == []


def test_empty_json_object_is_accepted_with_null_extracted_metadata(client):
    payload = encode_json({})

    response = post_webhook(client, payload)

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["action"] is None
    assert event["repository"] is None
    assert event["sender"] is None


def test_ping_event_with_sparse_payload_is_accepted(client):
    payload = encode_json({"zen": "Keep it logically awesome."})

    response = post_webhook(
        client,
        payload,
        {"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature_for(payload)},
    )

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["event"] == "ping"
    assert event["action"] is None
    assert event["repository"] is None
    assert event["sender"] is None


def test_duplicate_delivery_id_is_not_rejected_during_stage_4(client):
    payload = encode_json({"action": "opened"})
    headers = {"X-GitHub-Delivery": "same-delivery-id", "X-Hub-Signature-256": signature_for(payload)}

    first_response = post_webhook(client, payload, headers)
    second_response = post_webhook(client, payload, headers)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    events = get_events(client).json()["events"]
    assert [event["delivery_id"] for event in events] == [
        "same-delivery-id",
        "same-delivery-id",
    ]
    assert events[0]["attempt_id"] != events[1]["attempt_id"]


def test_same_delivery_id_with_different_payloads_keeps_distinct_attempt_digests(client):
    first_payload = encode_json({"action": "opened"})
    second_payload = encode_json({"action": "closed"})
    delivery_headers = {"X-GitHub-Delivery": "same-delivery-id"}

    first_response = post_webhook(
        client,
        first_payload,
        {**delivery_headers, "X-Hub-Signature-256": signature_for(first_payload)},
    )
    second_response = post_webhook(
        client,
        second_payload,
        {**delivery_headers, "X-Hub-Signature-256": signature_for(second_payload)},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    events = get_events(client).json()["events"]
    assert events[0]["delivery_id"] == events[1]["delivery_id"] == "same-delivery-id"
    assert events[0]["attempt_id"] != events[1]["attempt_id"]
    assert events[0]["payload_sha256"] == hashlib.sha256(second_payload).hexdigest()
    assert events[1]["payload_sha256"] == hashlib.sha256(first_payload).hexdigest()
    assert events[0]["payload_sha256"] != events[1]["payload_sha256"]


def test_attempt_based_capacity_does_not_keep_unbounded_history():
    settings = Settings(
        webhook_secret=TEST_SECRET,
        max_events=2,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    delivery_store = InMemoryDeliveryStore(max_events=2)
    client = TestClient(create_app(settings=settings, delivery_store=delivery_store))

    for delivery_id, action in [
        ("delivery-a", "first"),
        ("delivery-a", "second"),
        ("delivery-b", "third"),
    ]:
        payload = encode_json({"action": action})
        response = post_webhook(
            client,
            payload,
            {"X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": signature_for(payload)},
        )
        assert response.status_code == 200

    events = get_events(client).json()["events"]
    assert [event["action"] for event in events] == ["third", "second"]
    assert [event["delivery_id"] for event in events] == ["delivery-b", "delivery-a"]


def test_unicode_repository_and_sender_values_are_extracted(client):
    payload = encode_json(
        {
            "action": "opened",
            "repository": {"full_name": "octo/répô"},
            "sender": {"login": "álîçé"},
        }
    )

    response = post_webhook(client, payload)

    assert response.status_code == 200
    assert response.json()["event"]["repository"] == "octo/répô"
    assert response.json()["event"]["sender"] == "álîçé"


def test_missing_nested_repository_and_sender_structures_are_handled(client):
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload)

    assert response.status_code == 200
    assert response.json()["event"]["repository"] is None
    assert response.json()["event"]["sender"] is None
