import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import UUID

from app.domain.deliveries import DeliveryAttempt
from app.storage.deliveries import DeliveryAttemptKeyset, DeliveryStore


DEFAULT_DELIVERY_ATTEMPTS_LIMIT = 50
MIN_DELIVERY_ATTEMPTS_LIMIT = 1
MAX_DELIVERY_ATTEMPTS_LIMIT = 100
CURSOR_VERSION = 1


class InvalidDeliveryAttemptsCursorError(Exception):
    pass


class InvalidDeliveryAttemptsLimitError(Exception):
    pass


@dataclass(frozen=True)
class DeliveryAttemptsPage:
    items: list[DeliveryAttempt]
    next_cursor: str | None


class DeliveryQueryService:
    def __init__(self, delivery_store: DeliveryStore):
        self._delivery_store = delivery_store

    def list_attempts(self, *, limit: int, cursor: str | None) -> DeliveryAttemptsPage:
        after = decode_delivery_attempts_cursor(cursor) if cursor else None
        attempts = self._delivery_store.list_attempts_page(limit=limit + 1, after=after)
        items = attempts[:limit]
        next_cursor = encode_delivery_attempts_cursor(items[-1]) if len(attempts) > limit and items else None
        return DeliveryAttemptsPage(items=items, next_cursor=next_cursor)

    def get_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        return self._delivery_store.get_attempt(attempt_id)


def parse_delivery_attempts_limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_DELIVERY_ATTEMPTS_LIMIT
    try:
        limit = int(value)
    except ValueError as exc:
        raise InvalidDeliveryAttemptsLimitError("limit must be an integer") from exc
    if not MIN_DELIVERY_ATTEMPTS_LIMIT <= limit <= MAX_DELIVERY_ATTEMPTS_LIMIT:
        raise InvalidDeliveryAttemptsLimitError("limit must be between 1 and 100")
    return limit


def encode_delivery_attempts_cursor(attempt: DeliveryAttempt) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "received_at": attempt.received_at.astimezone(timezone.utc).isoformat(),
        "attempt_id": str(attempt.attempt_id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded.rstrip(b"=").decode("ascii")


def decode_delivery_attempts_cursor(cursor: str) -> DeliveryAttemptKeyset:
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("v") != CURSOR_VERSION:
            raise ValueError
        received_at = datetime.fromisoformat(payload["received_at"])
        attempt_id = UUID(payload["attempt_id"])
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError
        received_at = received_at.astimezone(timezone.utc)
    except (binascii.Error, KeyError, TypeError, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidDeliveryAttemptsCursorError("Invalid or expired cursor") from exc
    return DeliveryAttemptKeyset(received_at=received_at, attempt_id=attempt_id)
