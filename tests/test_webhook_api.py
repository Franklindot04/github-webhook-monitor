import hashlib
import hmac
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

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
    settings = Settings(webhook_secret=TEST_SECRET, max_events=event_store.max_events, _env_file=None)
    return TestClient(create_app(settings=settings, event_store=event_store))


def post_webhook(client: TestClient, payload: bytes, headers: dict[str, str] | None = None):
    request_headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-001",
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


def test_absent_event_and_delivery_headers_are_stored_as_null(client):
    payload = encode_json({"action": "opened"})

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(payload),
        },
    )

    assert response.status_code == 200
    assert response.json()["event"]["event"] is None
    assert response.json()["event"]["delivery_id"] is None


def test_unusual_header_values_are_stored_without_validation(client):
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


def test_empty_json_object_is_accepted_with_null_extracted_metadata(client):
    payload = encode_json({})

    response = post_webhook(client, payload)

    assert response.status_code == 200
    event = response.json()["event"]
    assert event["action"] is None
    assert event["repository"] is None
    assert event["sender"] is None


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
