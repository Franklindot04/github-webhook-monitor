from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.deliveries import DeliveryAttempt
from app.domain.recovery_actions import (
    RECOVERY_ACTION_AUTHENTICATION_METHOD_MANAGEMENT_BEARER,
    RECOVERY_ACTION_STATE_INITIATED,
    RECOVERY_ACTION_TERMINAL_STATES,
    RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY,
    RecoveryAction,
)


class RecoveryActionStoreError(Exception):
    pass


class RecoveryActionNotFoundError(Exception):
    pass


class InvalidRecoveryActionTransitionError(Exception):
    pass


@dataclass(frozen=True)
class RecoveryActionKeyset:
    requested_at: datetime
    action_id: UUID


class RecoveryActionStore(Protocol):
    def create_initiated_github_redelivery(
        self,
        *,
        attempt: DeliveryAttempt,
        repository: str,
        github_delivery_id: int,
        requested_at: datetime,
    ) -> RecoveryAction:
        ...

    def finalize(
        self,
        *,
        action_id: UUID,
        state: str,
        completed_at: datetime,
        upstream_status_code: int | None = None,
        failure_category: str | None = None,
    ) -> RecoveryAction:
        ...

    def get(self, action_id: UUID) -> RecoveryAction | None:
        ...

    def list_recent(
        self,
        *,
        limit: int,
        after: RecoveryActionKeyset | None = None,
    ) -> list[RecoveryAction]:
        ...


class InMemoryRecoveryActionStore:
    def __init__(self, max_actions: int):
        if max_actions <= 0:
            raise ValueError("max_actions must be positive")
        self._actions: deque[RecoveryAction] = deque(maxlen=max_actions)

    def create_initiated_github_redelivery(
        self,
        *,
        attempt: DeliveryAttempt,
        repository: str,
        github_delivery_id: int,
        requested_at: datetime,
    ) -> RecoveryAction:
        action = RecoveryAction(
            action_id=uuid4(),
            action_type=RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY,
            requested_at=requested_at,
            completed_at=None,
            attempt_id=attempt.attempt_id,
            delivery_guid=attempt.delivery_identity.delivery_guid,
            hook_id=attempt.delivery_identity.hook_id,
            repository=repository,
            github_delivery_id=github_delivery_id,
            authentication_method=RECOVERY_ACTION_AUTHENTICATION_METHOD_MANAGEMENT_BEARER,
            state=RECOVERY_ACTION_STATE_INITIATED,
            upstream_status_code=None,
            failure_category=None,
        )
        self._actions.appendleft(action)
        return action

    def finalize(
        self,
        *,
        action_id: UUID,
        state: str,
        completed_at: datetime,
        upstream_status_code: int | None = None,
        failure_category: str | None = None,
    ) -> RecoveryAction:
        for index, action in enumerate(self._actions):
            if action.action_id != action_id:
                continue
            if action.state in RECOVERY_ACTION_TERMINAL_STATES or state not in RECOVERY_ACTION_TERMINAL_STATES:
                raise InvalidRecoveryActionTransitionError("Invalid recovery action transition")
            finalized = RecoveryAction(
                action_id=action.action_id,
                action_type=action.action_type,
                requested_at=action.requested_at,
                completed_at=completed_at,
                attempt_id=action.attempt_id,
                delivery_guid=action.delivery_guid,
                hook_id=action.hook_id,
                repository=action.repository,
                github_delivery_id=action.github_delivery_id,
                authentication_method=action.authentication_method,
                state=state,
                upstream_status_code=upstream_status_code,
                failure_category=failure_category,
            )
            self._actions[index] = finalized
            return finalized
        raise RecoveryActionNotFoundError("Recovery action not found")

    def get(self, action_id: UUID) -> RecoveryAction | None:
        for action in self._actions:
            if action.action_id == action_id:
                return action
        return None

    def list_recent(
        self,
        *,
        limit: int,
        after: RecoveryActionKeyset | None = None,
    ) -> list[RecoveryAction]:
        ordered_actions = sorted(
            self._actions,
            key=lambda action: (action.requested_at, action.action_id),
            reverse=True,
        )
        if after is not None:
            ordered_actions = [
                action
                for action in ordered_actions
                if (action.requested_at, action.action_id) < (after.requested_at, after.action_id)
            ]
        return ordered_actions[:limit]

