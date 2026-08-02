"""pads.name index (reconcile model drift)

The Pad.name column (added in 726857c6f636) is declared index=True in the ORM
model, but that committed migration created the column without the index. Add it
in a new migration rather than editing the released one, so `alembic check` is
clean against the model metadata.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-23 13:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_pads_name", "pads", ["name"])


def downgrade() -> None:
    op.drop_index("ix_pads_name", table_name="pads")
