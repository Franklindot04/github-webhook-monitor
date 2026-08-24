from collections.abc import Mapping
from typing import Any
from uuid import UUID

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Engine, Select, update, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.domain.management import ManagementPrincipal, SHARED_TOKEN_PRINCIPAL
from app.domain.recovery_actions import (
    RECOVERY_ACTION_AUTHENTICATION_METHOD_MANAGEMENT_BEARER,
    RECOVERY_ACTION_STATE_INITIATED,
    RECOVERY_ACTION_TERMINAL_STATES,
    RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY,
    RecoveryAction,
)
from app.persistence.schema import delivery_attempts, github_deliveries, recovery_actions
from app.storage.deliveries import DeliveryAttemptKeyset, DeliveryStore, DeliveryStoreError
from app.storage.recovery_actions import (
    InvalidRecoveryActionTransitionError,
    RecoveryActionKeyset,
    RecoveryActionNotFoundError,
    RecoveryActionStore,
    RecoveryActionStoreError,
)


def _installation_target_id_to_db(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _installation_target_id_from_db(value: int | None) -> str | None:
    if value is None:
        return None
    return str(value)


def delivery_identity_values(attempt: DeliveryAttempt) -> dict[str, object]:
    return {
        "delivery_guid": attempt.delivery_identity.delivery_guid,
        "hook_id": attempt.delivery_identity.hook_id,
    }


def delivery_attempt_values(attempt: DeliveryAttempt, github_delivery_id: int) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "github_delivery_id": github_delivery_id,
        "received_at": attempt.received_at,
        "payload_sha256": attempt.payload_sha256,
        "event_type": attempt.event_type,
        "action": attempt.action,
        "repository": attempt.repository,
        "sender": attempt.sender,
        "installation_target_id": _installation_target_id_to_db(attempt.installation_target_id),
        "installation_target_type": attempt.installation_target_type,
    }


def row_to_delivery_attempt(row: Mapping[str, Any]) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=row["attempt_id"],
        delivery_identity=GitHubDeliveryIdentity(
            delivery_guid=row["delivery_guid"],
            hook_id=row["hook_id"],
        ),
        received_at=row["received_at"],
        payload_sha256=row["payload_sha256"],
        event_type=row["event_type"],
        action=row["action"],
        repository=row["repository"],
        sender=row["sender"],
        installation_target_id=_installation_target_id_from_db(row["installation_target_id"]),
        installation_target_type=row["installation_target_type"],
    )


class PostgresDeliveryStore(DeliveryStore):
    def __init__(self, engine: Engine, list_limit: int = 50):
        if list_limit <= 0:
            raise ValueError("list_limit must be positive")
        self._engine = engine
        self._list_limit = list_limit

    def add(self, attempt: DeliveryAttempt) -> None:
        try:
            with self._engine.begin() as connection:
                insert_delivery = (
                    insert(github_deliveries)
                    .values(delivery_identity_values(attempt))
                    .on_conflict_do_nothing(
                        index_elements=[
                            github_deliveries.c.delivery_guid,
                            github_deliveries.c.hook_id,
                        ]
                    )
                    .returning(github_deliveries.c.id)
                )
                github_delivery_id = connection.execute(insert_delivery).scalar_one_or_none()
                if github_delivery_id is None:
                    github_delivery_id = connection.execute(
                        select(github_deliveries.c.id).where(
                            github_deliveries.c.delivery_guid == attempt.delivery_identity.delivery_guid,
                            github_deliveries.c.hook_id == attempt.delivery_identity.hook_id,
                        )
                    ).scalar_one()

                connection.execute(
                    insert(delivery_attempts).values(
                        delivery_attempt_values(attempt, github_delivery_id=github_delivery_id)
                    )
                )
        except SQLAlchemyError as exc:
            raise DeliveryStoreError("Delivery store operation failed") from exc

    def list_recent(self) -> list[DeliveryAttempt]:
        statement = _recent_attempts_statement(self._list_limit)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as exc:
            raise DeliveryStoreError("Delivery store operation failed") from exc
        return [row_to_delivery_attempt(row) for row in rows]

    def list_attempts_page(
        self,
        *,
        limit: int,
        after: DeliveryAttemptKeyset | None = None,
    ) -> list[DeliveryAttempt]:
        statement = _attempts_page_statement(limit=limit, after=after)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as exc:
            raise DeliveryStoreError("Delivery store operation failed") from exc
        return [row_to_delivery_attempt(row) for row in rows]

    def get_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        statement = _attempt_by_id_statement(attempt_id)
        try:
            with self._engine.connect() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise DeliveryStoreError("Delivery store operation failed") from exc
        if row is None:
            return None
        return row_to_delivery_attempt(row)


def _recent_attempts_statement(limit: int) -> Select[tuple[Any, ...]]:
    return _base_attempts_statement().order_by(
        delivery_attempts.c.received_at.desc(),
        delivery_attempts.c.id.desc(),
    ).limit(limit)


def _attempts_page_statement(
    *,
    limit: int,
    after: DeliveryAttemptKeyset | None,
) -> Select[tuple[Any, ...]]:
    statement = _base_attempts_statement()
    if after is not None:
        statement = statement.where(
            (delivery_attempts.c.received_at < after.received_at)
            | (
                (delivery_attempts.c.received_at == after.received_at)
                & (delivery_attempts.c.attempt_id < after.attempt_id)
            )
        )
    return statement.order_by(
        delivery_attempts.c.received_at.desc(),
        delivery_attempts.c.attempt_id.desc(),
    ).limit(limit)


def _attempt_by_id_statement(attempt_id: UUID) -> Select[tuple[Any, ...]]:
    return _base_attempts_statement().where(delivery_attempts.c.attempt_id == attempt_id)


def _base_attempts_statement() -> Select[tuple[Any, ...]]:
    return (
        select(
            delivery_attempts.c.attempt_id,
            github_deliveries.c.delivery_guid,
            github_deliveries.c.hook_id,
            delivery_attempts.c.received_at,
            delivery_attempts.c.payload_sha256,
            delivery_attempts.c.event_type,
            delivery_attempts.c.action,
            delivery_attempts.c.repository,
            delivery_attempts.c.sender,
            delivery_attempts.c.installation_target_id,
            delivery_attempts.c.installation_target_type,
        )
        .select_from(
            delivery_attempts.join(
                github_deliveries,
                delivery_attempts.c.github_delivery_id == github_deliveries.c.id,
            )
        )
    )


def row_to_recovery_action(row: Mapping[str, Any]) -> RecoveryAction:
    return RecoveryAction(
        action_id=row["action_id"],
        action_type=row["action_type"],
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        attempt_id=row["attempt_id"],
        delivery_guid=row["delivery_guid"],
        hook_id=row["hook_id"],
        repository=row["repository"],
        github_delivery_id=row["github_delivery_id"],
        authentication_method=row["authentication_method"],
        principal_issuer=row["principal_issuer"],
        principal_subject=row["principal_subject"],
        principal_client_id=row["principal_client_id"],
        state=row["state"],
        upstream_status_code=row["upstream_status_code"],
        failure_category=row["failure_category"],
    )


class PostgresRecoveryActionStore(RecoveryActionStore):
    def __init__(self, engine: Engine):
        self._engine = engine

    def create_initiated_github_redelivery(
        self,
        *,
        attempt: DeliveryAttempt,
        repository: str,
        github_delivery_id: int,
        requested_at: datetime,
        principal: ManagementPrincipal = SHARED_TOKEN_PRINCIPAL,
    ) -> RecoveryAction:
        action_id = uuid4()
        values = {
            "action_id": action_id,
            "action_type": RECOVERY_ACTION_TYPE_GITHUB_REDELIVERY,
            "requested_at": requested_at,
            "completed_at": None,
            "attempt_id": attempt.attempt_id,
            "delivery_guid": attempt.delivery_identity.delivery_guid,
            "hook_id": attempt.delivery_identity.hook_id,
            "repository": repository,
            "github_delivery_id": github_delivery_id,
            "authentication_method": principal.authentication_method,
            "principal_issuer": principal.issuer,
            "principal_subject": principal.subject,
            "principal_client_id": principal.client_id,
            "state": RECOVERY_ACTION_STATE_INITIATED,
            "upstream_status_code": None,
            "failure_category": None,
        }
        try:
            with self._engine.begin() as connection:
                row = connection.execute(
                    recovery_actions.insert().values(values).returning(*recovery_actions.c)
                ).mappings().one()
        except SQLAlchemyError as exc:
            raise RecoveryActionStoreError("Recovery action store operation failed") from exc
        return row_to_recovery_action(row)

    def finalize(
        self,
        *,
        action_id: UUID,
        state: str,
        completed_at: datetime,
        upstream_status_code: int | None = None,
        failure_category: str | None = None,
    ) -> RecoveryAction:
        if state not in RECOVERY_ACTION_TERMINAL_STATES:
            raise InvalidRecoveryActionTransitionError("Invalid recovery action transition")
        statement = (
            update(recovery_actions)
            .where(
                recovery_actions.c.action_id == action_id,
                recovery_actions.c.state == RECOVERY_ACTION_STATE_INITIATED,
            )
            .values(
                state=state,
                completed_at=completed_at,
                upstream_status_code=upstream_status_code,
                failure_category=failure_category,
            )
            .returning(*recovery_actions.c)
        )
        try:
            with self._engine.begin() as connection:
                row = connection.execute(statement).mappings().one_or_none()
                if row is None:
                    existing = connection.execute(
                        select(recovery_actions.c.action_id, recovery_actions.c.state).where(
                            recovery_actions.c.action_id == action_id
                        )
                    ).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RecoveryActionStoreError("Recovery action store operation failed") from exc
        if row is not None:
            return row_to_recovery_action(row)
        if existing is None:
            raise RecoveryActionNotFoundError("Recovery action not found")
        raise InvalidRecoveryActionTransitionError("Invalid recovery action transition")

    def get(self, action_id: UUID) -> RecoveryAction | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    select(recovery_actions).where(recovery_actions.c.action_id == action_id)
                ).mappings().one_or_none()
        except SQLAlchemyError as exc:
            raise RecoveryActionStoreError("Recovery action store operation failed") from exc
        if row is None:
            return None
        return row_to_recovery_action(row)

    def list_recent(
        self,
        *,
        limit: int,
        after: RecoveryActionKeyset | None = None,
    ) -> list[RecoveryAction]:
        statement = select(recovery_actions)
        if after is not None:
            statement = statement.where(
                (recovery_actions.c.requested_at < after.requested_at)
                | (
                    (recovery_actions.c.requested_at == after.requested_at)
                    & (recovery_actions.c.action_id < after.action_id)
                )
            )
        statement = statement.order_by(
            recovery_actions.c.requested_at.desc(),
            recovery_actions.c.action_id.desc(),
        ).limit(limit)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as exc:
            raise RecoveryActionStoreError("Recovery action store operation failed") from exc
        return [row_to_recovery_action(row) for row in rows]
