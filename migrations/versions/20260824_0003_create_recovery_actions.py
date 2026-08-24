"""Create recovery action journal.

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260824_0003"
down_revision: str | None = "20260824_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recovery_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_guid", sa.Text(), nullable=False),
        sa.Column("hook_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("github_delivery_id", sa.BigInteger(), nullable=False),
        sa.Column("authentication_method", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("upstream_status_code", sa.BigInteger(), nullable=True),
        sa.Column("failure_category", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "action_type = 'github_repository_webhook_redelivery'",
            name="ck_recovery_actions_action_type",
        ),
        sa.CheckConstraint(
            "state IN ('initiated', 'accepted', 'failed', 'outcome_unknown')",
            name="ck_recovery_actions_state",
        ),
        sa.CheckConstraint(
            "authentication_method = 'management_bearer'",
            name="ck_recovery_actions_authentication_method",
        ),
        sa.CheckConstraint("length(btrim(delivery_guid)) > 0", name="ck_recovery_actions_delivery_guid_not_blank"),
        sa.CheckConstraint("hook_id > 0", name="ck_recovery_actions_hook_id_positive"),
        sa.CheckConstraint("length(btrim(repository)) > 0", name="ck_recovery_actions_repository_not_blank"),
        sa.CheckConstraint("github_delivery_id > 0", name="ck_recovery_actions_github_delivery_id_positive"),
        sa.CheckConstraint(
            "upstream_status_code IS NULL OR (upstream_status_code >= 100 AND upstream_status_code <= 599)",
            name="ck_recovery_actions_upstream_status_code",
        ),
        sa.CheckConstraint(
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
        sa.UniqueConstraint("action_id", name="uq_recovery_actions_action_id"),
    )
    op.create_index(
        "ix_recovery_actions_recent",
        "recovery_actions",
        [sa.text("requested_at DESC"), sa.text("action_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_recovery_actions_recent", table_name="recovery_actions")
    op.drop_table("recovery_actions")

