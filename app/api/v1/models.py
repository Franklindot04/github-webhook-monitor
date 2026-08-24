from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DeliveryAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_id: UUID
    delivery_guid: str
    hook_id: int
    received_at: datetime
    payload_sha256: str
    event_type: str
    action: str | None
    repository: str | None
    sender: str | None
    installation_target_id: str | None
    installation_target_type: str | None


class DeliveryAttemptsListResponse(BaseModel):
    items: list[DeliveryAttemptResponse]
    next_cursor: str | None


class GitHubDeliverySummaryResponse(BaseModel):
    github_delivery_id: int
    delivery_guid: str
    delivered_at: datetime
    redelivery: bool
    duration: float | None
    status: str | None
    status_code: int | None
    event: str | None
    action: str | None
    installation_id: int | None
    repository_id: int | None
    throttled_at: datetime | None


class GitHubDeliveriesReconciliationResponse(BaseModel):
    attempt_id: UUID
    delivery_guid: str
    hook_id: int
    repository: str
    matches: list[GitHubDeliverySummaryResponse]
    search_complete: bool
    next_cursor: str | None


class GitHubRedeliveryResponse(BaseModel):
    action_id: UUID
    attempt_id: UUID
    delivery_guid: str
    hook_id: int
    github_delivery_id: int
    status: str


class RecoveryActionResponse(BaseModel):
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
    state: str
    upstream_status_code: int | None
    failure_category: str | None


class RecoveryActionsListResponse(BaseModel):
    items: list[RecoveryActionResponse]
    next_cursor: str | None
