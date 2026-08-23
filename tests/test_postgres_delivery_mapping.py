import hashlib
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.persistence.postgres import (
    delivery_attempt_values,
    delivery_identity_values,
    row_to_delivery_attempt,
)


ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000001")
RECEIVED_AT = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)
PAYLOAD_DIGEST = hashlib.sha256(b'{"action":"opened"}').hexdigest()


def make_attempt() -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=ATTEMPT_ID,
        delivery_identity=GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=12345),
        received_at=RECEIVED_AT,
        payload_sha256=PAYLOAD_DIGEST,
        event_type="pull_request",
        action="opened",
        repository="octo/example",
        sender="octocat",
        installation_target_id="67890",
        installation_target_type="repository",
    )


def test_delivery_identity_values_preserve_domain_identity():
    assert delivery_identity_values(make_attempt()) == {
        "delivery_guid": "delivery-001",
        "hook_id": 12345,
    }


def test_delivery_attempt_values_preserve_domain_attempt_without_raw_payload():
    values = delivery_attempt_values(make_attempt(), github_delivery_id=99)

    assert values == {
        "attempt_id": ATTEMPT_ID,
        "github_delivery_id": 99,
        "received_at": RECEIVED_AT,
        "payload_sha256": PAYLOAD_DIGEST,
        "event_type": "pull_request",
        "action": "opened",
        "repository": "octo/example",
        "sender": "octocat",
        "installation_target_id": 67890,
        "installation_target_type": "repository",
    }
    assert "raw_payload" not in values
    assert "payload_body" not in values


def test_row_to_delivery_attempt_returns_domain_object():
    attempt = row_to_delivery_attempt(
        {
            "attempt_id": ATTEMPT_ID,
            "delivery_guid": "delivery-001",
            "hook_id": 12345,
            "received_at": RECEIVED_AT,
            "payload_sha256": PAYLOAD_DIGEST,
            "event_type": "pull_request",
            "action": "opened",
            "repository": "octo/example",
            "sender": "octocat",
            "installation_target_id": 67890,
            "installation_target_type": "repository",
        }
    )

    assert attempt == make_attempt()


def test_row_to_delivery_attempt_preserves_null_optional_metadata():
    attempt = row_to_delivery_attempt(
        {
            "attempt_id": ATTEMPT_ID,
            "delivery_guid": "delivery-001",
            "hook_id": 12345,
            "received_at": RECEIVED_AT,
            "payload_sha256": PAYLOAD_DIGEST,
            "event_type": "ping",
            "action": None,
            "repository": None,
            "sender": None,
            "installation_target_id": None,
            "installation_target_type": None,
        }
    )

    assert attempt.action is None
    assert attempt.repository is None
    assert attempt.sender is None
    assert attempt.installation_target_id is None
    assert attempt.installation_target_type is None


def test_delivery_attempt_values_reject_non_numeric_installation_target_id():
    attempt = DeliveryAttempt(
        attempt_id=ATTEMPT_ID,
        delivery_identity=GitHubDeliveryIdentity(delivery_guid="delivery-001", hook_id=12345),
        received_at=RECEIVED_AT,
        payload_sha256=PAYLOAD_DIGEST,
        event_type="pull_request",
        action=None,
        repository=None,
        sender=None,
        installation_target_id="not-numeric",
        installation_target_type="repository",
    )

    with pytest.raises(ValueError):
        delivery_attempt_values(attempt, github_delivery_id=99)
