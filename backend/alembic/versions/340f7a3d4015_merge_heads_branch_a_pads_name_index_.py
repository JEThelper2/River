"""merge heads (branch A pads.name index + branch B username/previous_names)

Revision ID: 340f7a3d4015
Revises: c3d4e5f6a7b8, h3i4j5k6l7m8
Create Date: 2026-06-28 00:24:04.868928

"""
from collections.abc import Sequence

revision: str = '340f7a3d4015'
down_revision: str | None = ('c3d4e5f6a7b8', 'h3i4j5k6l7m8')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
