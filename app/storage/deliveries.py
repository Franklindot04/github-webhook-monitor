from collections import deque
from typing import Protocol

from app.domain.deliveries import DeliveryAttempt


class DeliveryStore(Protocol):
    def add(self, attempt: DeliveryAttempt) -> None:
        ...

    def list_recent(self) -> list[DeliveryAttempt]:
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
