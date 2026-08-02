"""pad cold_storage_eligible flag (Phase 6)

Revision ID: f1a2b3c4d5e6
Revises: e5f8b2c3d4a1
Create Date: 2026-06-23 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e5f8b2c3d4a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pads",
        sa.Column(
            "cold_storage_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pads", "cold_storage_eligible")
