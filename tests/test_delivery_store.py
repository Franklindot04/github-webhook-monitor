from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.storage.deliveries import DeliveryAttemptKeyset, InMemoryDeliveryStore


def make_attempt(delivery_id: str, *, attempt_id=None, received_at=None) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=attempt_id or uuid4(),
        delivery_identity=GitHubDeliveryIdentity(delivery_guid=delivery_id, hook_id=12345),
        received_at=received_at or datetime(2026, 8, 24, tzinfo=timezone.utc),
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


def test_in_memory_delivery_store_lists_attempts_page_with_keyset_ordering():
    store = InMemoryDeliveryStore(max_events=10)
    shared_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    first = make_attempt(
        "first",
        attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        received_at=shared_time,
    )
    second = make_attempt(
        "second",
        attempt_id=UUID("00000000-0000-0000-0000-000000000002"),
        received_at=shared_time,
    )
    third = make_attempt(
        "third",
        attempt_id=UUID("00000000-0000-0000-0000-000000000003"),
        received_at=shared_time + timedelta(seconds=1),
    )
    store.add(first)
    store.add(third)
    store.add(second)

    first_page = store.list_attempts_page(limit=2)
    second_page = store.list_attempts_page(
        limit=2,
        after=DeliveryAttemptKeyset(
            received_at=first_page[-1].received_at,
            attempt_id=first_page[-1].attempt_id,
        ),
    )

    assert [attempt.attempt_id for attempt in first_page] == [third.attempt_id, second.attempt_id]
    assert [attempt.attempt_id for attempt in second_page] == [first.attempt_id]


def test_in_memory_delivery_store_get_attempt_by_attempt_id():
    store = InMemoryDeliveryStore(max_events=10)
    attempt = make_attempt("first", attempt_id=UUID("00000000-0000-0000-0000-000000000001"))
    store.add(attempt)

    assert store.get_attempt(attempt.attempt_id) == attempt
    assert store.get_attempt(UUID("00000000-0000-0000-0000-000000000002")) is None
