import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.persistence.postgres import PostgresDeliveryStore
from app.persistence.schema import delivery_attempts, github_deliveries


pytestmark = pytest.mark.integration


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def require_test_database_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("PostgreSQL integration tests require TEST_DATABASE_URL")
    if not TEST_DATABASE_URL.startswith("postgresql+psycopg://"):
        pytest.skip("TEST_DATABASE_URL must use postgresql+psycopg")
    return TEST_DATABASE_URL


@pytest.fixture(scope="module")
def database_url() -> str:
    return require_test_database_url()


@pytest.fixture(scope="module")
def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="module")
def engine(database_url: str, alembic_config: Config):
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url, future=True)
    yield engine
    engine.dispose()
    command.downgrade(alembic_config, "base")


@pytest.fixture(autouse=True)
def clean_tables(engine):
    with engine.begin() as connection:
        connection.execute(delivery_attempts.delete())
        connection.execute(github_deliveries.delete())


def make_attempt(
    attempt_id: str,
    delivery_guid: str = "delivery-001",
    payload: bytes = b'{"action":"opened"}',
    received_at: datetime = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
    action: str | None = "opened",
    repository: str | None = "octo/example",
    sender: str | None = "octocat",
    installation_target_id: str | None = "67890",
    installation_target_type: str | None = "repository",
) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=UUID(attempt_id),
        delivery_identity=GitHubDeliveryIdentity(delivery_guid=delivery_guid, hook_id=12345),
        received_at=received_at,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        event_type="pull_request",
        action=action,
        repository=repository,
        sender=sender,
        installation_target_id=installation_target_id,
        installation_target_type=installation_target_type,
    )


def test_alembic_upgrade_creates_required_tables(engine):
    inspector = inspect(engine)

    assert {"github_deliveries", "delivery_attempts"}.issubset(set(inspector.get_table_names()))


def test_schema_exposes_required_constraints_and_recent_index(engine):
    inspector = inspect(engine)

    github_unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("github_deliveries")
    }
    delivery_unique_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("delivery_attempts")
    }
    github_check_constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("github_deliveries")
    }
    delivery_check_constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("delivery_attempts")
    }
    delivery_indexes = {
        index["name"]
        for index in inspector.get_indexes("delivery_attempts")
    }

    assert "uq_github_deliveries_identity" in github_unique_constraints
    assert "uq_delivery_attempts_attempt_id" in delivery_unique_constraints
    assert "ck_github_deliveries_delivery_guid_not_blank" in github_check_constraints
    assert "ck_github_deliveries_hook_id_positive" in github_check_constraints
    assert "ck_delivery_attempts_payload_sha256_length" in delivery_check_constraints
    assert "ck_delivery_attempts_installation_target_pair" in delivery_check_constraints
    assert "ix_delivery_attempts_recent" in delivery_indexes


def test_single_attempt_round_trips_all_domain_fields(engine):
    store = PostgresDeliveryStore(engine)
    attempt = make_attempt("00000000-0000-0000-0000-000000000001")

    store.add(attempt)

    assert store.list_recent() == [attempt]
    assert store.list_recent()[0].received_at.tzinfo is not None


def test_repeated_github_delivery_keeps_two_attempts_and_one_logical_delivery(engine):
    store = PostgresDeliveryStore(engine)
    first = make_attempt("00000000-0000-0000-0000-000000000001")
    second = make_attempt(
        "00000000-0000-0000-0000-000000000002",
        payload=b'{"action":"closed"}',
        received_at=datetime(2026, 8, 24, 12, 1, tzinfo=timezone.utc),
        action="closed",
    )

    store.add(first)
    store.add(second)

    attempts = store.list_recent()
    assert [attempt.attempt_id for attempt in attempts] == [second.attempt_id, first.attempt_id]
    assert attempts[0].delivery_identity == attempts[1].delivery_identity
    assert attempts[0].payload_sha256 != attempts[1].payload_sha256
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(github_deliveries)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 2


def test_concurrent_first_insert_for_same_github_delivery_creates_one_logical_row(engine):
    store = PostgresDeliveryStore(engine)
    barrier = Barrier(2)
    first = make_attempt("00000000-0000-0000-0000-000000000001", delivery_guid="delivery-race")
    second = make_attempt(
        "00000000-0000-0000-0000-000000000002",
        delivery_guid="delivery-race",
        payload=b'{"action":"closed"}',
        received_at=datetime(2026, 8, 24, 12, 1, tzinfo=timezone.utc),
        action="closed",
    )

    def add_attempt(attempt: DeliveryAttempt) -> UUID:
        barrier.wait(timeout=10)
        store.add(attempt)
        return attempt.attempt_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(add_attempt, first),
            executor.submit(add_attempt, second),
        ]
        inserted_attempt_ids = {future.result(timeout=10) for future in futures}

    assert inserted_attempt_ids == {first.attempt_id, second.attempt_id}
    attempts = store.list_recent()
    assert {attempt.attempt_id for attempt in attempts} == {first.attempt_id, second.attempt_id}
    assert attempts[0].delivery_identity == attempts[1].delivery_identity

    with engine.connect() as connection:
        logical_delivery_id = connection.execute(
            select(github_deliveries.c.id).where(
                github_deliveries.c.delivery_guid == "delivery-race",
                github_deliveries.c.hook_id == 12345,
            )
        ).scalar_one()
        assert (
            connection.execute(
                select(func.count()).where(
                    github_deliveries.c.delivery_guid == "delivery-race",
                    github_deliveries.c.hook_id == 12345,
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(func.count()).where(delivery_attempts.c.github_delivery_id == logical_delivery_id)
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                select(func.count(func.distinct(delivery_attempts.c.attempt_id))).where(
                    delivery_attempts.c.github_delivery_id == logical_delivery_id
                )
            ).scalar_one()
            == 2
        )


def test_list_recent_uses_deterministic_recent_first_ordering(engine):
    store = PostgresDeliveryStore(engine, list_limit=2)
    shared_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    first = make_attempt("00000000-0000-0000-0000-000000000001", delivery_guid="delivery-001", received_at=shared_time)
    second = make_attempt("00000000-0000-0000-0000-000000000002", delivery_guid="delivery-002", received_at=shared_time)
    third = make_attempt(
        "00000000-0000-0000-0000-000000000003",
        delivery_guid="delivery-003",
        received_at=shared_time + timedelta(seconds=1),
    )

    store.add(first)
    store.add(second)
    store.add(third)

    assert [attempt.attempt_id for attempt in store.list_recent()] == [third.attempt_id, second.attempt_id]


def test_null_optional_metadata_round_trips(engine):
    store = PostgresDeliveryStore(engine)
    attempt = make_attempt(
        "00000000-0000-0000-0000-000000000001",
        action=None,
        repository=None,
        sender=None,
        installation_target_id=None,
        installation_target_type=None,
    )

    store.add(attempt)

    assert store.list_recent() == [attempt]


def test_duplicate_attempt_id_is_rejected(engine):
    store = PostgresDeliveryStore(engine)
    attempt = make_attempt("00000000-0000-0000-0000-000000000001")

    store.add(attempt)
    with pytest.raises(IntegrityError):
        store.add(attempt)


def test_failed_attempt_insert_does_not_leave_new_logical_delivery(engine):
    store = PostgresDeliveryStore(engine)
    invalid_attempt = make_attempt("00000000-0000-0000-0000-000000000001", delivery_guid="delivery-invalid")
    invalid_attempt = DeliveryAttempt(
        attempt_id=invalid_attempt.attempt_id,
        delivery_identity=invalid_attempt.delivery_identity,
        received_at=invalid_attempt.received_at,
        payload_sha256=invalid_attempt.payload_sha256,
        event_type=invalid_attempt.event_type,
        action=invalid_attempt.action,
        repository=invalid_attempt.repository,
        sender=invalid_attempt.sender,
        installation_target_id="0",
        installation_target_type="repository",
    )

    with pytest.raises(IntegrityError):
        store.add(invalid_attempt)

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(github_deliveries)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(delivery_attempts)).scalar_one() == 0


def test_alembic_downgrade_removes_tables_and_reupgrade_restores_store(
    database_url: str,
    alembic_config: Config,
):
    engine = create_engine(database_url, future=True)
    try:
        command.downgrade(alembic_config, "base")
        inspector = inspect(engine)
        assert "github_deliveries" not in inspector.get_table_names()
        assert "delivery_attempts" not in inspector.get_table_names()

        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        assert {"github_deliveries", "delivery_attempts"}.issubset(set(inspector.get_table_names()))

        store = PostgresDeliveryStore(engine)
        attempt = make_attempt("00000000-0000-0000-0000-000000000001")
        store.add(attempt)
        assert store.list_recent() == [attempt]
    finally:
        engine.dispose()


def test_schema_has_no_raw_payload_body_columns(engine):
    column_names = {
        column["name"]
        for column in inspect(engine).get_columns("delivery_attempts")
    }

    assert "raw_payload" not in column_names
    assert "payload_body" not in column_names
    assert "request_body" not in column_names


def test_postgresql_version_is_available(engine):
    with engine.connect() as connection:
        assert connection.execute(text("select version()")).scalar_one().startswith("PostgreSQL")
