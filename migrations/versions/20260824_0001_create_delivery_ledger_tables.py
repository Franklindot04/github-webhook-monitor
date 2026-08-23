"""Create delivery ledger tables.

Revision ID: 20260824_0001
Revises:
Create Date: 2026-08-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260824_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("delivery_guid", sa.Text(), nullable=False),
        sa.Column("hook_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(delivery_guid)) > 0",
            name="ck_github_deliveries_delivery_guid_not_blank",
        ),
        sa.CheckConstraint("hook_id > 0", name="ck_github_deliveries_hook_id_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_guid", "hook_id", name="uq_github_deliveries_identity"),
    )
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("github_delivery_id", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("repository", sa.Text(), nullable=True),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("installation_target_id", sa.BigInteger(), nullable=True),
        sa.Column("installation_target_type", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_delivery_attempts_payload_sha256_length",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["github_delivery_id"],
            ["github_deliveries.id"],
            name=op.f("fk_delivery_attempts_github_delivery_id_github_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_delivery_attempts_attempt_id"),
    )
    op.create_index(
        "ix_delivery_attempts_recent",
        "delivery_attempts",
        [sa.text("received_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_recent", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    op.drop_table("github_deliveries")
