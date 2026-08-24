"""Add delivery attempt diagnostics keyset index.

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0002"
down_revision: str | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_delivery_attempts_diagnostics_keyset",
        "delivery_attempts",
        [sa.text("received_at DESC"), sa.text("attempt_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_diagnostics_keyset", table_name="delivery_attempts")
