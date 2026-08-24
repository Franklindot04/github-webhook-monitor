import base64
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

import pytest

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.persistence.postgres import PostgresDeliveryStore
from app.services.delivery_queries import (
    DEFAULT_DELIVERY_ATTEMPTS_LIMIT,
    MAX_DELIVERY_ATTEMPTS_LIMIT,
    DeliveryQueryService,
    InvalidDeliveryAttemptsCursorError,
    InvalidDeliveryAttemptsLimitError,
    decode_delivery_attempts_cursor,
    encode_delivery_attempts_cursor,
    parse_delivery_attempts_limit,
)
from app.storage.deliveries import InMemoryDeliveryStore


BASE_TIME = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def make_attempt(index: int, *, received_at: datetime | None = None) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        delivery_identity=GitHubDeliveryIdentity(delivery_guid=f"guid-{index:03d}", hook_id=12345),
        received_at=received_at or BASE_TIME + timedelta(seconds=index),
        payload_sha256=f"{index:x}".rjust(64, "0"),
        event_type="pull_request",
        action="opened",
        repository="octo/example",
        sender="octocat",
        installation_target_id=None,
        installation_target_type=None,
    )


def populated_store(count: int) -> InMemoryDeliveryStore:
    store = InMemoryDeliveryStore(max_events=100)
    for index in range(1, count + 1):
        store.add(make_attempt(index))
    return store


def encode_cursor_payload(payload: object) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded.rstrip(b"=").decode("ascii")


def test_default_limit():
    assert parse_delivery_attempts_limit(None) == DEFAULT_DELIVERY_ATTEMPTS_LIMIT


def test_lower_limit():
    assert parse_delivery_attempts_limit("1") == 1


def test_maximum_limit():
    assert parse_delivery_attempts_limit(str(MAX_DELIVERY_ATTEMPTS_LIMIT)) == MAX_DELIVERY_ATTEMPTS_LIMIT


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_limit_below_range_or_not_integer(value):
    with pytest.raises(InvalidDeliveryAttemptsLimitError):
        parse_delivery_attempts_limit(value)


def test_limit_above_range():
    with pytest.raises(InvalidDeliveryAttemptsLimitError):
        parse_delivery_attempts_limit(str(MAX_DELIVERY_ATTEMPTS_LIMIT + 1))


def test_first_middle_and_final_pages_with_next_cursor():
    service = DeliveryQueryService(populated_store(5))

    first_page = service.list_attempts(limit=2, cursor=None)
    second_page = service.list_attempts(limit=2, cursor=first_page.next_cursor)
    final_page = service.list_attempts(limit=2, cursor=second_page.next_cursor)

    assert [attempt.attempt_id for attempt in first_page.items] == [
        UUID("00000000-0000-0000-0000-000000000005"),
        UUID("00000000-0000-0000-0000-000000000004"),
    ]
    assert [attempt.attempt_id for attempt in second_page.items] == [
        UUID("00000000-0000-0000-0000-000000000003"),
        UUID("00000000-0000-0000-0000-000000000002"),
    ]
    assert [attempt.attempt_id for attempt in final_page.items] == [
        UUID("00000000-0000-0000-0000-000000000001")
    ]
    assert first_page.next_cursor is not None
    assert second_page.next_cursor is not None
    assert final_page.next_cursor is None


def test_empty_history_has_no_next_cursor():
    service = DeliveryQueryService(InMemoryDeliveryStore(max_events=10))

    page = service.list_attempts(limit=10, cursor=None)

    assert page.items == []
    assert page.next_cursor is None


def test_malformed_cursor():
    with pytest.raises(InvalidDeliveryAttemptsCursorError):
        decode_delivery_attempts_cursor("not valid base64")


def test_tampered_cursor():
    cursor = encode_delivery_attempts_cursor(make_attempt(1))
    tampered_cursor = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")

    with pytest.raises(InvalidDeliveryAttemptsCursorError):
        decode_delivery_attempts_cursor(tampered_cursor)


def test_current_cursor_version_decodes():
    cursor = encode_cursor_payload(
        {
            "v": 1,
            "received_at": "2026-08-24T12:00:00+00:00",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        }
    )

    keyset = decode_delivery_attempts_cursor(cursor)

    assert keyset.received_at == datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    assert keyset.attempt_id == UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "v": 2,
            "received_at": "2026-08-24T12:00:00+00:00",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        },
        {
            "received_at": "2026-08-24T12:00:00+00:00",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        },
        {
            "v": 1,
            "received_at": "2026-08-24T12:00:00+00:00",
        },
        {
            "v": 1,
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        },
        {
            "v": 1,
            "received_at": "2026-08-24T12:00:00+00:00",
            "attempt_id": "not-a-uuid",
        },
        {
            "v": 1,
            "received_at": "2026-08-24T12:00:00",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        },
        {
            "v": 1,
            "received_at": 123,
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        },
        ["not", "an", "object"],
    ],
)
def test_structurally_decodable_invalid_cursor_payloads_are_rejected(payload):
    with pytest.raises(InvalidDeliveryAttemptsCursorError):
        decode_delivery_attempts_cursor(encode_cursor_payload(payload))


def test_deterministic_equal_timestamp_ordering():
    store = InMemoryDeliveryStore(max_events=10)
    shared_time = BASE_TIME
    store.add(make_attempt(1, received_at=shared_time))
    store.add(make_attempt(3, received_at=shared_time))
    store.add(make_attempt(2, received_at=shared_time))
    service = DeliveryQueryService(store)

    page = service.list_attempts(limit=10, cursor=None)

    assert [attempt.attempt_id for attempt in page.items] == [
        UUID("00000000-0000-0000-0000-000000000003"),
        UUID("00000000-0000-0000-0000-000000000002"),
        UUID("00000000-0000-0000-0000-000000000001"),
    ]


def test_new_insertion_between_pages_does_not_restart_traversal():
    store = populated_store(4)
    service = DeliveryQueryService(store)

    first_page = service.list_attempts(limit=2, cursor=None)
    store.add(make_attempt(5, received_at=BASE_TIME + timedelta(minutes=5)))
    second_page = service.list_attempts(limit=2, cursor=first_page.next_cursor)

    assert [attempt.attempt_id for attempt in first_page.items] == [
        UUID("00000000-0000-0000-0000-000000000004"),
        UUID("00000000-0000-0000-0000-000000000003"),
    ]
    assert [attempt.attempt_id for attempt in second_page.items] == [
        UUID("00000000-0000-0000-0000-000000000002"),
        UUID("00000000-0000-0000-0000-000000000001"),
    ]


def test_cursor_encoding_round_trip():
    attempt = make_attempt(1)

    keyset = decode_delivery_attempts_cursor(encode_delivery_attempts_cursor(attempt))

    assert keyset.received_at == attempt.received_at
    assert keyset.attempt_id == attempt.attempt_id


def test_cursor_encoding_normalizes_received_at_to_utc():
    attempt = make_attempt(1, received_at=datetime(2026, 8, 24, 15, 0, tzinfo=timezone(timedelta(hours=3))))

    keyset = decode_delivery_attempts_cursor(encode_delivery_attempts_cursor(attempt))

    assert keyset.received_at == datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class ProbeDeliveryStore:
    def __init__(self):
        self.page_calls = 0

    def add(self, attempt):
        raise AssertionError("not used")

    def list_recent(self):
        raise AssertionError("not used")

    def list_attempts_page(self, *, limit, after=None):
        self.page_calls += 1
        raise AssertionError("naive cursor must not reach storage")

    def get_attempt(self, attempt_id):
        raise AssertionError("not used")


def test_naive_timestamp_cursor_is_rejected_before_storage_access():
    store = ProbeDeliveryStore()
    service = DeliveryQueryService(store)
    cursor = encode_cursor_payload(
        {
            "v": 1,
            "received_at": "2026-08-24T01:23:45",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        }
    )

    with pytest.raises(InvalidDeliveryAttemptsCursorError):
        service.list_attempts(limit=10, cursor=cursor)

    assert store.page_calls == 0


@pytest.mark.parametrize(
    "store",
    [
        InMemoryDeliveryStore(max_events=10),
        PostgresDeliveryStore(engine=object()),
    ],
)
def test_naive_timestamp_cursor_never_reaches_backend_page_query(store, monkeypatch):
    page_calls = 0

    def fail_if_called(*, limit, after=None):
        nonlocal page_calls
        page_calls += 1
        raise AssertionError("naive cursor must not reach backend page query")

    monkeypatch.setattr(store, "list_attempts_page", fail_if_called)
    service = DeliveryQueryService(store)
    cursor = encode_cursor_payload(
        {
            "v": 1,
            "received_at": "2026-08-24T01:23:45",
            "attempt_id": "00000000-0000-0000-0000-000000000001",
        }
    )

    with pytest.raises(InvalidDeliveryAttemptsCursorError):
        service.list_attempts(limit=10, cursor=cursor)

    assert page_calls == 0


def test_non_utc_offset_cursor_matches_equivalent_utc_cursor_continuation():
    store = InMemoryDeliveryStore(max_events=10)
    first = make_attempt(1, received_at=datetime(2026, 8, 23, 23, 0, tzinfo=timezone.utc))
    boundary = make_attempt(2, received_at=datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc))
    newer = make_attempt(3, received_at=datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc))
    store.add(first)
    store.add(boundary)
    store.add(newer)
    service = DeliveryQueryService(store)
    utc_cursor = encode_cursor_payload(
        {
            "v": 1,
            "received_at": "2026-08-24T00:00:00+00:00",
            "attempt_id": str(boundary.attempt_id),
        }
    )
    offset_cursor = encode_cursor_payload(
        {
            "v": 1,
            "received_at": "2026-08-24T03:00:00+03:00",
            "attempt_id": str(boundary.attempt_id),
        }
    )

    utc_page = service.list_attempts(limit=10, cursor=utc_cursor)
    offset_page = service.list_attempts(limit=10, cursor=offset_cursor)

    assert [attempt.attempt_id for attempt in offset_page.items] == [attempt.attempt_id for attempt in utc_page.items]
    assert [attempt.attempt_id for attempt in offset_page.items] == [first.attempt_id]
