import hashlib
import hmac
import json
from datetime import datetime
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.api.routes import is_json_content_type, read_bounded_body, validate_content_length
from app.config import Settings
from app.factory import create_app
from app.storage.events import InMemoryEventStore


TEST_SECRET = "test-webhook-secret"


def encode_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest.fixture
def event_store():
    return InMemoryEventStore(max_events=50)


@pytest.fixture
def client(event_store):
    settings = Settings(
        webhook_secret=TEST_SECRET,
        max_events=event_store.max_events,
        max_webhook_body_bytes=4096,
        _env_file=None,
    )
    return TestClient(create_app(settings=settings, event_store=event_store))


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


def test_valid_signed_webhook_stores_and_returns_event_metadata(client, event_store):
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
    assert event["event"] == "pull_request"
    assert event["delivery_id"] == "delivery-001"
    assert event["hook_id"] == "12345"
    assert event["installation_target_id"] is None
    assert event["installation_target_type"] is None
    assert event["action"] == "opened"
    assert event["repository"] == "octo/example"
    assert event["sender"] == "octocat"
    assert datetime.fromisoformat(event["received_at"]).tzinfo is not None

    stored_events = [stored_event.to_dict() for stored_event in event_store.list_recent()]
    assert len(stored_events) == 1
    assert stored_events[0] == event


def test_webhook_missing_signature_returns_current_unauthorized_behavior(client, event_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload, {"X-Hub-Signature-256": ""})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert event_store.list_recent() == []


def test_webhook_invalid_signature_returns_current_unauthorized_behavior(client, event_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(client, payload, {"X-Hub-Signature-256": "sha256=bad"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    assert event_store.list_recent() == []


def test_webhook_signature_for_different_payload_is_rejected(client, event_store):
    signed_payload = encode_json({"action": "opened"})
    transmitted_payload = encode_json({"action": "closed"})

    response = post_webhook(
        client,
        transmitted_payload,
        {"X-Hub-Signature-256": signature_for(signed_payload)},
    )

    assert response.status_code == 401
    assert event_store.list_recent() == []


def test_malformed_json_with_valid_signature_returns_current_bad_request_behavior(client, event_store):
    payload = b'{"action":'

    response = post_webhook(client, payload)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON payload"}
    assert event_store.list_recent() == []


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
def test_unsupported_or_missing_content_type_is_rejected(client, event_store, content_type):
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
    assert event_store.list_recent() == []


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
    event_store,
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
    assert event_store.list_recent() == []


def test_legacy_sha1_signature_header_alone_is_insufficient(client, event_store):
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
    assert event_store.list_recent() == []


def test_payload_at_configured_limit_is_accepted(event_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=13, _env_file=None)
    client = TestClient(create_app(settings=settings, event_store=event_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert len(payload) == 13
    assert response.status_code == 200


def test_payload_below_configured_limit_is_accepted(event_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=14, _env_file=None)
    client = TestClient(create_app(settings=settings, event_store=event_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert response.status_code == 200


def test_payload_above_configured_limit_is_rejected_without_storing(event_store):
    settings = Settings(webhook_secret=TEST_SECRET, max_webhook_body_bytes=12, _env_file=None)
    client = TestClient(create_app(settings=settings, event_store=event_store))
    payload = b'{"a":"12345"}'

    response = post_webhook(client, payload, {"X-Hub-Signature-256": signature_for(payload)})

    assert response.status_code == 413
    assert response.json() == {"detail": "Payload too large"}
    assert event_store.list_recent() == []


def test_oversized_declared_content_length_is_rejected_before_body_processing(client, event_store):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {"Content-Length": "4097", "X-Hub-Signature-256": signature_for(payload)},
    )

    assert response.status_code == 413
    assert event_store.list_recent() == []


@pytest.mark.parametrize("content_length", ["not-a-number", "-1", " -1 "])
def test_invalid_declared_content_length_is_rejected_by_route(client, event_store, content_length):
    payload = encode_json({"action": "opened"})

    response = post_webhook(
        client,
        payload,
        {"Content-Length": content_length, "X-Hub-Signature-256": signature_for(payload)},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Content-Length"}
    assert event_store.list_recent() == []


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

    response = client.get("/events")

    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert [event["delivery_id"] for event in response.json()["events"]] == [
        "delivery-002",
        "delivery-001",
    ]


def test_events_endpoint_exposes_current_in_memory_collection(client):
    payload = encode_json({"action": "opened", "repository": {"full_name": "octo/example"}})

    webhook_response = post_webhook(client, payload)
    events_response = client.get("/events")

    assert webhook_response.status_code == 200
    assert events_response.status_code == 200
    assert events_response.json() == {"count": 1, "events": [webhook_response.json()["event"]]}


def test_event_store_respects_configured_maximum_length(client, event_store):
    for index in range(event_store.max_events + 1):
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

    stored_events = [event.to_dict() for event in event_store.list_recent()]
    assert len(stored_events) == event_store.max_events
    assert stored_events[0]["delivery_id"] == f"delivery-{event_store.max_events:03d}"
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
def test_invalid_installation_target_metadata_is_rejected(client, event_store, headers):
    payload = encode_json({"action": "opened"})
    headers["X-Hub-Signature-256"] = signature_for(payload)

    response = post_webhook(client, payload, headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid GitHub installation target metadata"}
    assert event_store.list_recent() == []


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
    assert [event["delivery_id"] for event in client.get("/events").json()["events"]] == [
        "same-delivery-id",
        "same-delivery-id",
    ]


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
