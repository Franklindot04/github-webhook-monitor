from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GitHubDeliverySummary:
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


@dataclass(frozen=True)
class GitHubDeliveryPage:
    deliveries: list[GitHubDeliverySummary]
    next_cursor: str | None
