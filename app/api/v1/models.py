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
