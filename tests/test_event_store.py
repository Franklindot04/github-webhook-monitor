from app.domain.events import EventSummary
from app.storage.events import InMemoryEventStore


def make_event(delivery_id: str) -> EventSummary:
    return EventSummary(
        received_at="2026-08-24T00:00:00+00:00",
        event="pull_request",
        delivery_id=delivery_id,
        hook_id="12345",
        installation_target_id=None,
        installation_target_type=None,
        repository="octo/example",
        sender="octocat",
        action="opened",
    )


def test_in_memory_event_store_starts_empty():
    store = InMemoryEventStore(max_events=2)

    assert store.list_recent() == []


def test_in_memory_event_store_inserts_most_recent_first():
    store = InMemoryEventStore(max_events=2)

    store.add(make_event("first"))
    store.add(make_event("second"))

    assert [event.delivery_id for event in store.list_recent()] == ["second", "first"]


def test_in_memory_event_store_respects_capacity():
    store = InMemoryEventStore(max_events=2)

    store.add(make_event("first"))
    store.add(make_event("second"))
    store.add(make_event("third"))

    assert [event.delivery_id for event in store.list_recent()] == ["third", "second"]


def test_event_summary_serializes_to_existing_response_shape():
    event = make_event("delivery-001")

    assert event.to_dict() == {
        "received_at": "2026-08-24T00:00:00+00:00",
        "event": "pull_request",
        "delivery_id": "delivery-001",
        "hook_id": "12345",
        "installation_target_id": None,
        "installation_target_type": None,
        "repository": "octo/example",
        "sender": "octocat",
        "action": "opened",
    }
