from collections import deque
from typing import Protocol

from app.domain.events import EventSummary


class EventStore(Protocol):
    def add(self, event: EventSummary) -> None:
        ...

    def list_recent(self) -> list[EventSummary]:
        ...


class InMemoryEventStore:
    def __init__(self, max_events: int):
        self._events: deque[EventSummary] = deque(maxlen=max_events)

    @property
    def max_events(self) -> int:
        maxlen = self._events.maxlen
        if maxlen is None:
            raise RuntimeError("In-memory event store must be bounded")
        return maxlen

    def add(self, event: EventSummary) -> None:
        self._events.appendleft(event)

    def list_recent(self) -> list[EventSummary]:
        return list(self._events)
