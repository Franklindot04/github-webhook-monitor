from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EventSummary:
    received_at: str
    event: str | None
    delivery_id: str | None
    hook_id: str
    installation_target_id: str | None
    installation_target_type: str | None
    repository: str | None
    sender: str | None
    action: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)
