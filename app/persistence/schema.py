from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID


metadata = MetaData()


github_deliveries = Table(
    "github_deliveries",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("delivery_guid", Text, nullable=False),
    Column("hook_id", BigInteger, nullable=False),
    CheckConstraint("length(btrim(delivery_guid)) > 0", name="ck_github_deliveries_delivery_guid_not_blank"),
    CheckConstraint("hook_id > 0", name="ck_github_deliveries_hook_id_positive"),
    UniqueConstraint("delivery_guid", "hook_id", name="uq_github_deliveries_identity"),
)


delivery_attempts = Table(
    "delivery_attempts",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("attempt_id", UUID(as_uuid=True), nullable=False),
    Column(
        "github_delivery_id",
        BigInteger,
        ForeignKey("github_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("action", Text, nullable=True),
    Column("repository", Text, nullable=True),
    Column("sender", Text, nullable=True),
    Column("installation_target_id", BigInteger, nullable=True),
    Column("installation_target_type", Text, nullable=True),
    CheckConstraint("length(payload_sha256) = 64", name="ck_delivery_attempts_payload_sha256_length"),
    CheckConstraint(
        """
        (
            installation_target_id IS NULL
            AND installation_target_type IS NULL
        )
        OR
        (
            installation_target_id IS NOT NULL
            AND installation_target_id > 0
            AND installation_target_type IS NOT NULL
            AND length(btrim(installation_target_type)) > 0
        )
        """,
        name="ck_delivery_attempts_installation_target_pair",
    ),
    UniqueConstraint("attempt_id", name="uq_delivery_attempts_attempt_id"),
)


Index(
    "ix_delivery_attempts_recent",
    delivery_attempts.c.received_at.desc(),
    delivery_attempts.c.id.desc(),
)
