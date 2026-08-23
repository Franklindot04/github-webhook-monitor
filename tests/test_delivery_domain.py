from datetime import datetime, timezone
import hashlib
from uuid import UUID, uuid4

import pytest

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity


def make_attempt(payload: bytes = b'{"action":"opened"}') -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=uuid4(),
        delivery_identity=GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=12345),
        received_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        event_type="pull_request",
        action="opened",
        repository="octo/example",
        sender="octocat",
        installation_target_id=None,
        installation_target_type=None,
    )


def test_delivery_identity_requires_non_empty_github_delivery_guid():
    with pytest.raises(ValueError):
        GitHubDeliveryIdentity(delivery_guid=" ", hook_id=12345)


@pytest.mark.parametrize("hook_id", [0, -1])
def test_delivery_identity_requires_positive_hook_id(hook_id):
    with pytest.raises(ValueError):
        GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=hook_id)


def test_delivery_attempt_uses_application_attempt_id_distinct_from_github_delivery_guid():
    attempt = make_attempt()

    assert isinstance(attempt.attempt_id, UUID)
    assert str(attempt.attempt_id) != attempt.delivery_identity.delivery_guid


def test_delivery_attempt_requires_timezone_aware_received_at():
    with pytest.raises(ValueError):
        DeliveryAttempt(
            attempt_id=uuid4(),
            delivery_identity=GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=12345),
            received_at=datetime(2026, 8, 24),
            payload_sha256="a" * 64,
            event_type="pull_request",
            action=None,
            repository=None,
            sender=None,
            installation_target_id=None,
            installation_target_type=None,
        )


def test_delivery_attempt_requires_valid_sha256_hex_digest():
    with pytest.raises(ValueError):
        DeliveryAttempt(
            attempt_id=uuid4(),
            delivery_identity=GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=12345),
            received_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            payload_sha256="not-a-digest",
            event_type="pull_request",
            action=None,
            repository=None,
            sender=None,
            installation_target_id=None,
            installation_target_type=None,
        )


def test_payload_digest_is_derived_from_exact_raw_bytes():
    first_payload = '{"message":"répô"}'.encode("utf-8")
    second_payload = b'{"message":"r\\u00e9p\\u00f4"}'

    first_attempt = make_attempt(first_payload)
    second_attempt = make_attempt(second_payload)

    assert first_attempt.payload_sha256 == hashlib.sha256(first_payload).hexdigest()
    assert second_attempt.payload_sha256 == hashlib.sha256(second_payload).hexdigest()
    assert first_attempt.payload_sha256 != second_attempt.payload_sha256
