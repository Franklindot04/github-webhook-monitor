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

Index(
    "ix_delivery_attempts_diagnostics_keyset",
    delivery_attempts.c.received_at.desc(),
    delivery_attempts.c.attempt_id.desc(),
)


recovery_actions = Table(
    "recovery_actions",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("action_id", UUID(as_uuid=True), nullable=False),
    Column("action_type", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("attempt_id", UUID(as_uuid=True), nullable=False),
    Column("delivery_guid", Text, nullable=False),
    Column("hook_id", BigInteger, nullable=False),
    Column("repository", Text, nullable=False),
    Column("github_delivery_id", BigInteger, nullable=False),
    Column("authentication_method", Text, nullable=False),
    Column("principal_issuer", Text, nullable=True),
    Column("principal_subject", Text, nullable=True),
    Column("principal_client_id", Text, nullable=True),
    Column("authorization_capability", Text, nullable=True),
    Column("authorization_scope", Text, nullable=True),
    Column("state", Text, nullable=False),
    Column("upstream_status_code", BigInteger, nullable=True),
    Column("failure_category", Text, nullable=True),
    CheckConstraint(
        "action_type = 'github_repository_webhook_redelivery'",
        name="ck_recovery_actions_action_type",
    ),
    CheckConstraint(
        "state IN ('initiated', 'accepted', 'failed', 'outcome_unknown')",
        name="ck_recovery_actions_state",
    ),
    CheckConstraint(
        "authentication_method IN ('management_bearer', 'oidc_jwt')",
        name="ck_recovery_actions_authentication_method",
    ),
    CheckConstraint("length(btrim(delivery_guid)) > 0", name="ck_recovery_actions_delivery_guid_not_blank"),
    CheckConstraint("hook_id > 0", name="ck_recovery_actions_hook_id_positive"),
    CheckConstraint("length(btrim(repository)) > 0", name="ck_recovery_actions_repository_not_blank"),
    CheckConstraint("github_delivery_id > 0", name="ck_recovery_actions_github_delivery_id_positive"),
    CheckConstraint(
        "upstream_status_code IS NULL OR (upstream_status_code >= 100 AND upstream_status_code <= 599)",
        name="ck_recovery_actions_upstream_status_code",
    ),
    CheckConstraint(
        """
        (
            state = 'initiated'
            AND completed_at IS NULL
            AND upstream_status_code IS NULL
            AND failure_category IS NULL
        )
        OR
        (
            state IN ('accepted', 'failed', 'outcome_unknown')
            AND completed_at IS NOT NULL
        )
        """,
        name="ck_recovery_actions_state_completion",
    ),
    UniqueConstraint("action_id", name="uq_recovery_actions_action_id"),
)


Index(
    "ix_recovery_actions_recent",
    recovery_actions.c.requested_at.desc(),
    recovery_actions.c.action_id.desc(),
)
