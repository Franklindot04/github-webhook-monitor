import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import UUID

from app.domain.recovery_actions import RecoveryAction
from app.storage.recovery_actions import RecoveryActionKeyset, RecoveryActionStore


DEFAULT_RECOVERY_ACTIONS_LIMIT = 50
MIN_RECOVERY_ACTIONS_LIMIT = 1
MAX_RECOVERY_ACTIONS_LIMIT = 100
RECOVERY_ACTIONS_CURSOR_VERSION = 1


class InvalidRecoveryActionsCursorError(Exception):
    pass


class InvalidRecoveryActionsLimitError(Exception):
    pass


@dataclass(frozen=True)
class RecoveryActionsPage:
    items: list[RecoveryAction]
    next_cursor: str | None


class RecoveryActionQueryService:
    def __init__(self, recovery_action_store: RecoveryActionStore):
        self._recovery_action_store = recovery_action_store

    def list_actions(self, *, limit: int, cursor: str | None) -> RecoveryActionsPage:
        after = decode_recovery_actions_cursor(cursor) if cursor else None
        actions = self._recovery_action_store.list_recent(limit=limit + 1, after=after)
        items = actions[:limit]
        next_cursor = encode_recovery_actions_cursor(items[-1]) if len(actions) > limit and items else None
        return RecoveryActionsPage(items=items, next_cursor=next_cursor)

    def get_action(self, action_id: UUID) -> RecoveryAction | None:
        return self._recovery_action_store.get(action_id)


def parse_recovery_actions_limit(value: str | None) -> int:
    if value is None:
        return DEFAULT_RECOVERY_ACTIONS_LIMIT
    try:
        limit = int(value)
    except ValueError as exc:
        raise InvalidRecoveryActionsLimitError("limit must be an integer") from exc
    if not MIN_RECOVERY_ACTIONS_LIMIT <= limit <= MAX_RECOVERY_ACTIONS_LIMIT:
        raise InvalidRecoveryActionsLimitError("limit must be between 1 and 100")
    return limit


def encode_recovery_actions_cursor(action: RecoveryAction) -> str:
    payload = {
        "v": RECOVERY_ACTIONS_CURSOR_VERSION,
        "requested_at": action.requested_at.astimezone(timezone.utc).isoformat(),
        "action_id": str(action.action_id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded.rstrip(b"=").decode("ascii")


def decode_recovery_actions_cursor(cursor: str) -> RecoveryActionKeyset:
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("v") != RECOVERY_ACTIONS_CURSOR_VERSION:
            raise ValueError
        requested_at = datetime.fromisoformat(payload["requested_at"])
        action_id = UUID(payload["action_id"])
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError
        requested_at = requested_at.astimezone(timezone.utc)
    except (binascii.Error, KeyError, TypeError, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidRecoveryActionsCursorError("Invalid or expired cursor") from exc
    return RecoveryActionKeyset(requested_at=requested_at, action_id=action_id)

