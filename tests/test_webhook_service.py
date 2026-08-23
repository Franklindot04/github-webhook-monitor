import hashlib
import hmac

import pytest

from app.services.webhooks import (
    InvalidWebhookSignatureError,
    MalformedWebhookPayloadError,
    WebhookIngestionService,
)
from app.storage.events import InMemoryEventStore


TEST_SECRET = "test-webhook-secret"


def signature_for(payload: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_webhook_service_stores_event_without_http_request():
    store = InMemoryEventStore(max_events=10)
    service = WebhookIngestionService(event_store=store, webhook_secret=TEST_SECRET)
    payload = b'{"action":"opened","repository":{"full_name":"octo/example"},"sender":{"login":"octocat"}}'

    event = service.ingest(
        raw_body=payload,
        signature=signature_for(payload),
        github_event="pull_request",
        github_delivery="delivery-001",
    )

    assert event.to_dict()["event"] == "pull_request"
    assert event.to_dict()["delivery_id"] == "delivery-001"
    assert event.to_dict()["repository"] == "octo/example"
    assert event.to_dict()["sender"] == "octocat"
    assert event.to_dict()["action"] == "opened"
    assert store.list_recent() == [event]


def test_webhook_service_rejects_invalid_signature_without_storing_event():
    store = InMemoryEventStore(max_events=10)
    service = WebhookIngestionService(event_store=store, webhook_secret=TEST_SECRET)

    with pytest.raises(InvalidWebhookSignatureError):
        service.ingest(
            raw_body=b'{"action":"opened"}',
            signature="sha256=bad",
            github_event="pull_request",
            github_delivery="delivery-001",
        )

    assert store.list_recent() == []


def test_webhook_service_rejects_malformed_json_without_storing_event():
    store = InMemoryEventStore(max_events=10)
    service = WebhookIngestionService(event_store=store, webhook_secret=TEST_SECRET)
    payload = b'{"action":'

    with pytest.raises(MalformedWebhookPayloadError):
        service.ingest(
            raw_body=payload,
            signature=signature_for(payload),
            github_event="pull_request",
            github_delivery="delivery-001",
        )

    assert store.list_recent() == []
