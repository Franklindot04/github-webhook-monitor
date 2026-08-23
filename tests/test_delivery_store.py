from datetime import datetime, timezone
from uuid import uuid4

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.storage.deliveries import InMemoryDeliveryStore


def make_attempt(delivery_id: str) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=uuid4(),
        delivery_identity=GitHubDeliveryIdentity(delivery_guid=delivery_id, hook_id=12345),
        received_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        payload_sha256="a" * 64,
        event_type="pull_request",
        installation_target_id=None,
        installation_target_type=None,
        repository="octo/example",
        sender="octocat",
        action="opened",
    )


def test_in_memory_delivery_store_starts_empty():
    store = InMemoryDeliveryStore(max_events=2)

    assert store.list_recent() == []


def test_in_memory_delivery_store_inserts_most_recent_first():
    store = InMemoryDeliveryStore(max_events=2)

    store.add(make_attempt("first"))
    store.add(make_attempt("second"))

    assert [attempt.delivery_identity.delivery_guid for attempt in store.list_recent()] == ["second", "first"]


def test_in_memory_delivery_store_respects_capacity():
    store = InMemoryDeliveryStore(max_events=2)

    store.add(make_attempt("first"))
    store.add(make_attempt("second"))
    store.add(make_attempt("third"))

    assert [attempt.delivery_identity.delivery_guid for attempt in store.list_recent()] == ["third", "second"]


def test_delivery_attempt_serializes_to_existing_response_shape():
    attempt = make_attempt("delivery-001")

    event = attempt.to_dict()
    assert event["attempt_id"] == str(attempt.attempt_id)
    assert event == {
        "attempt_id": str(attempt.attempt_id),
        "received_at": "2026-08-24T00:00:00+00:00",
        "event": "pull_request",
        "delivery_id": "delivery-001",
        "hook_id": "12345",
        "installation_target_id": None,
        "installation_target_type": None,
        "payload_sha256": "a" * 64,
        "repository": "octo/example",
        "sender": "octocat",
        "action": "opened",
    }
