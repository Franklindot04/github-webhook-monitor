from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY = "github_repository_webhook_redelivery"
RECOVERY_ACTION_STATE_INITIATED = "initiated"
RECOVERY_ACTION_STATE_ACCEPTED = "accepted"
RECOVERY_ACTION_STATE_FAILED = "failed"
RECOVERY_ACTION_STATE_OUTCOME_UNKNOWN = "outcome_unknown"
RECOVERY_ACTION_AUTHENTICATION_METHOD_MANAGEMENT_BEARER = "management_bearer"

RECOVERY_ACTION_TYPES = frozenset({RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY})
RECOVERY_ACTION_STATES = frozenset(
    {
        RECOVERY_ACTION_STATE_INITIATED,
        RECOVERY_ACTION_STATE_ACCEPTED,
        RECOVERY_ACTION_STATE_FAILED,
        RECOVERY_ACTION_STATE_OUTCOME_UNKNOWN,
    }
)
RECOVERY_ACTION_TERMINAL_STATES = frozenset(
    {
        RECOVERY_ACTION_STATE_ACCEPTED,
        RECOVERY_ACTION_STATE_FAILED,
        RECOVERY_ACTION_STATE_OUTCOME_UNKNOWN,
    }
)


@dataclass(frozen=True)
class RecoveryAction:
    action_id: UUID
    action_type: str
    requested_at: datetime
    completed_at: datetime | None
    attempt_id: UUID
    delivery_guid: str
    hook_id: int
    repository: str
    github_delivery_id: int
    authentication_method: str
    principal_issuer: str | None
    principal_subject: str | None
    principal_client_id: str | None
    state: str
    upstream_status_code: int | None
    failure_category: str | None
