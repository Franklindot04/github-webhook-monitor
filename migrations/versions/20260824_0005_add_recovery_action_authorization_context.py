"""Add recovery action authorization context.

Revision ID: 20260824_0005
Revises: 20260824_0004
Create Date: 2026-08-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0005"
down_revision: str | None = "20260824_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recovery_actions", sa.Column("authorization_capability", sa.Text(), nullable=True))
    op.add_column("recovery_actions", sa.Column("authorization_scope", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("recovery_actions", "authorization_scope")
    op.drop_column("recovery_actions", "authorization_capability")
