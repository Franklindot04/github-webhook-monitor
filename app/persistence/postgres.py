from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.persistence.schema import delivery_attempts, github_deliveries
from app.storage.deliveries import DeliveryAttemptKeyset, DeliveryStore, DeliveryStoreError


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
