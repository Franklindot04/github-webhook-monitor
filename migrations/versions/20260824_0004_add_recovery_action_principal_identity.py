"""Add recovery action principal identity.

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260824_0004"
down_revision: str | None = "20260824_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_recovery_actions_authentication_method",
        "recovery_actions",
        type_="check",
    )
    op.add_column("recovery_actions", sa.Column("principal_issuer", sa.Text(), nullable=True))
    op.add_column("recovery_actions", sa.Column("principal_subject", sa.Text(), nullable=True))
    op.add_column("recovery_actions", sa.Column("principal_client_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_recovery_actions_authentication_method",
        "recovery_actions",
        "authentication_method IN ('management_bearer', 'oidc_jwt')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_recovery_actions_authentication_method",
        "recovery_actions",
        type_="check",
    )
    op.drop_column("recovery_actions", "principal_client_id")
    op.drop_column("recovery_actions", "principal_subject")
    op.drop_column("recovery_actions", "principal_issuer")
    op.create_check_constraint(
        "ck_recovery_actions_authentication_method",
        "recovery_actions",
        "authentication_method = 'management_bearer'",
    )
