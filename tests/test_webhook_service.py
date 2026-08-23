import hashlib
import hmac
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.services.webhooks import (
    InvalidWebhookSignatureError,
    MalformedWebhookPayloadError,
    WebhookIngestionService,
)
from app.storage.deliveries import InMemoryDeliveryStore


TEST_SECRET = "test-webhook-secret"
FIRST_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000001")


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_webhook_service_stores_event_without_http_request():
    store = InMemoryDeliveryStore(max_events=10)
    service = WebhookIngestionService(
        delivery_store=store,
        webhook_secret=TEST_SECRET,
        attempt_id_factory=lambda: FIRST_ATTEMPT_ID,
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    payload = b'{"action":"opened","repository":{"full_name":"octo/example"},"sender":{"login":"octocat"}}'

    event = service.ingest(
        raw_body=payload,
        signature=signature_for(payload),
        github_event="pull_request",
        github_delivery="delivery-001",
        github_hook_id="12345",
    )

    assert event.to_dict()["event"] == "pull_request"
    assert event.to_dict()["delivery_id"] == "delivery-001"
    assert event.to_dict()["attempt_id"] == str(FIRST_ATTEMPT_ID)
    assert event.to_dict()["hook_id"] == "12345"
    assert event.to_dict()["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert event.to_dict()["repository"] == "octo/example"
    assert event.to_dict()["sender"] == "octocat"
    assert event.to_dict()["action"] == "opened"
    assert store.list_recent() == [event]


def test_webhook_service_creates_distinct_attempt_ids_for_repeated_delivery_guid():
    attempt_ids = iter(
        [
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
        ]
    )
    store = InMemoryDeliveryStore(max_events=10)
    service = WebhookIngestionService(
        delivery_store=store,
        webhook_secret=TEST_SECRET,
        attempt_id_factory=lambda: next(attempt_ids),
    )
    payload = b'{"action":"opened"}'

    first = service.ingest(
        raw_body=payload,
        signature=signature_for(payload),
        github_event="pull_request",
        github_delivery="delivery-001",
        github_hook_id="12345",
    )
    second = service.ingest(
        raw_body=payload,
        signature=signature_for(payload),
        github_event="pull_request",
        github_delivery="delivery-001",
        github_hook_id="12345",
    )

    assert first.delivery_identity == second.delivery_identity
    assert first.attempt_id != second.attempt_id
    assert [attempt.attempt_id for attempt in store.list_recent()] == [second.attempt_id, first.attempt_id]


def test_webhook_service_payload_digest_changes_when_raw_body_changes():
    store = InMemoryDeliveryStore(max_events=10)
    service = WebhookIngestionService(delivery_store=store, webhook_secret=TEST_SECRET)
    first_payload = b'{"action":"opened"}'
    second_payload = b'{ "action" : "opened" }'

    first = service.ingest(
        raw_body=first_payload,
        signature=signature_for(first_payload),
        github_event="pull_request",
        github_delivery="delivery-001",
        github_hook_id="12345",
    )
    second = service.ingest(
        raw_body=second_payload,
        signature=signature_for(second_payload),
        github_event="pull_request",
        github_delivery="delivery-001",
        github_hook_id="12345",
    )

    assert first.payload_sha256 == hashlib.sha256(first_payload).hexdigest()
    assert second.payload_sha256 == hashlib.sha256(second_payload).hexdigest()
    assert first.payload_sha256 != second.payload_sha256


def test_webhook_service_rejects_invalid_signature_without_storing_event():
    store = InMemoryDeliveryStore(max_events=10)
    service = WebhookIngestionService(delivery_store=store, webhook_secret=TEST_SECRET)

    with pytest.raises(InvalidWebhookSignatureError):
        service.ingest(
            raw_body=b'{"action":"opened"}',
            signature="sha256=bad",
            github_event="pull_request",
            github_delivery="delivery-001",
            github_hook_id="12345",
        )

    assert store.list_recent() == []


def test_webhook_service_rejects_malformed_json_without_storing_event():
    store = InMemoryDeliveryStore(max_events=10)
    service = WebhookIngestionService(delivery_store=store, webhook_secret=TEST_SECRET)
    payload = b'{"action":'

    with pytest.raises(MalformedWebhookPayloadError):
        service.ingest(
            raw_body=payload,
            signature=signature_for(payload),
            github_event="pull_request",
            github_delivery="delivery-001",
            github_hook_id="12345",
        )

    assert store.list_recent() == []
