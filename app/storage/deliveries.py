from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.deliveries import DeliveryAttempt


class DeliveryStoreError(Exception):
    pass


class DeliveryStoreReadinessError(Exception):
    pass


@dataclass(frozen=True)
class DeliveryAttemptKeyset:
    received_at: datetime
    attempt_id: UUID


class DeliveryStore(Protocol):
    def add(self, attempt: DeliveryAttempt) -> None:
        ...

    def list_recent(self) -> list[DeliveryAttempt]:
        ...

    def list_attempts_page(
        self,
        *,
        limit: int,
        after: DeliveryAttemptKeyset | None = None,
    ) -> list[DeliveryAttempt]:
        ...

    def get_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        ...


class InMemoryDeliveryStore:
    def __init__(self, max_events: int):
        self._attempts: deque[DeliveryAttempt] = deque(maxlen=max_events)

    @property
    def max_events(self) -> int:
        maxlen = self._attempts.maxlen
        if maxlen is None:
            raise RuntimeError("In-memory delivery store must be bounded")
        return maxlen

    def add(self, attempt: DeliveryAttempt) -> None:
        self._attempts.appendleft(attempt)

    def list_recent(self) -> list[DeliveryAttempt]:
        return list(self._attempts)

    def list_attempts_page(
        self,
        *,
        limit: int,
        after: DeliveryAttemptKeyset | None = None,
    ) -> list[DeliveryAttempt]:
        ordered_attempts = sorted(
            self._attempts,
            key=lambda attempt: (attempt.received_at, attempt.attempt_id),
            reverse=True,
        )
        if after is not None:
            ordered_attempts = [
                attempt
                for attempt in ordered_attempts
                if (attempt.received_at, attempt.attempt_id) < (after.received_at, after.attempt_id)
            ]
        return ordered_attempts[:limit]

    def get_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        for attempt in self._attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        return None
