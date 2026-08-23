from dataclasses import dataclass
from datetime import datetime
import re
from uuid import UUID


SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class GitHubDeliveryIdentity:
    delivery_guid: str
    hook_id: int

    def __post_init__(self) -> None:
        if not self.delivery_guid.strip():
            raise ValueError("GitHub delivery GUID must be non-empty")
        if self.hook_id <= 0:
            raise ValueError("GitHub hook ID must be positive")


@dataclass(frozen=True)
class DeliveryAttempt:
    attempt_id: UUID
    delivery_identity: GitHubDeliveryIdentity
    received_at: datetime
    payload_sha256: str
    event_type: str
    action: str | None
    repository: str | None
    sender: str | None
    installation_target_id: str | None
    installation_target_type: str | None

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        if not SHA256_HEX_PATTERN.fullmatch(self.payload_sha256):
            raise ValueError("payload_sha256 must be a lowercase SHA-256 hex digest")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "attempt_id": str(self.attempt_id),
            "received_at": self.received_at.isoformat(),
            "event": self.event_type,
            "delivery_id": self.delivery_identity.delivery_guid,
            "hook_id": str(self.delivery_identity.hook_id),
            "installation_target_id": self.installation_target_id,
            "installation_target_type": self.installation_target_type,
            "payload_sha256": self.payload_sha256,
            "repository": self.repository,
            "sender": self.sender,
            "action": self.action,
        }
