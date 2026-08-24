from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.persistence.database import create_database_engine
from app.persistence.postgres import PostgresDeliveryStore, PostgresRecoveryActionStore
from app.persistence.schema import delivery_attempts, github_deliveries, recovery_actions
from app.storage.deliveries import (
    DeliveryStore,
    DeliveryStoreError,
    DeliveryStoreReadinessError,
    InMemoryDeliveryStore,
)
from app.storage.recovery_actions import InMemoryRecoveryActionStore, RecoveryActionStore


@dataclass(frozen=True)
class RuntimeResources:
    delivery_store: DeliveryStore
    recovery_action_store: RecoveryActionStore
    readiness_check: Callable[[], None]
    engine: Engine | None = None
    owns_engine: bool = False


def build_runtime_resources(settings: Settings) -> RuntimeResources:
    if settings.delivery_store_backend == "memory":
        return RuntimeResources(
            delivery_store=InMemoryDeliveryStore(max_events=settings.max_events),
            recovery_action_store=InMemoryRecoveryActionStore(max_actions=settings.max_events),
            readiness_check=memory_readiness_check,
        )

    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required for PostgreSQL runtime")

    engine = create_database_engine(
        settings.database_url.get_secret_value(),
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
        pool_pre_ping=True,
    )
    return RuntimeResources(
        delivery_store=PostgresDeliveryStore(engine, list_limit=settings.max_events),
        recovery_action_store=PostgresRecoveryActionStore(engine),
        readiness_check=lambda: verify_delivery_store_ready(engine),
        engine=engine,
        owns_engine=True,
    )


def memory_readiness_check() -> None:
    return None


def verify_delivery_store_ready(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(
                select(
                    github_deliveries.c.id,
                    github_deliveries.c.delivery_guid,
                    github_deliveries.c.hook_id,
                    delivery_attempts.c.id,
                    delivery_attempts.c.attempt_id,
                    delivery_attempts.c.github_delivery_id,
                    delivery_attempts.c.received_at,
                    delivery_attempts.c.payload_sha256,
                    delivery_attempts.c.event_type,
                    delivery_attempts.c.action,
                    delivery_attempts.c.repository,
                    delivery_attempts.c.sender,
                    delivery_attempts.c.installation_target_id,
                    delivery_attempts.c.installation_target_type,
                    recovery_actions.c.id,
                    recovery_actions.c.action_id,
                    recovery_actions.c.action_type,
                    recovery_actions.c.requested_at,
                    recovery_actions.c.completed_at,
                    recovery_actions.c.attempt_id,
                    recovery_actions.c.delivery_guid,
                    recovery_actions.c.hook_id,
                    recovery_actions.c.repository,
                    recovery_actions.c.github_delivery_id,
                    recovery_actions.c.authentication_method,
                    recovery_actions.c.state,
                    recovery_actions.c.upstream_status_code,
                    recovery_actions.c.failure_category,
                )
                .select_from(
                    delivery_attempts.join(
                        github_deliveries,
                        delivery_attempts.c.github_delivery_id == github_deliveries.c.id,
                    ).outerjoin(recovery_actions, recovery_actions.c.attempt_id == delivery_attempts.c.attempt_id)
                )
                .limit(0)
            )
    except (SQLAlchemyError, DeliveryStoreError) as exc:
        raise DeliveryStoreReadinessError("Delivery store is not ready") from exc
