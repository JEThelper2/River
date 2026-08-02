"""PIN-protected pads: pad columns + pad_pin_unlocks table

Revision ID: b2c3d4e5f6a7
Revises: a7b8c9d0e1f2
Create Date: 2026-06-23 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pads",
        sa.Column(
            "pin_protected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("pads", sa.Column("pin_hash", sa.String(length=255), nullable=True))
    # ALTER TABLE ... ADD COLUMN does not auto-create the enum type (unlike
    # CREATE TABLE), so create it explicitly first, then reference it with
    # create_type=False so the column add doesn't try to create it again.
    pin_format = postgresql.ENUM("numeric", "alphanumeric", name="pin_format")
    pin_format.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "pads",
        sa.Column(
            "pin_format",
            postgresql.ENUM(
                "numeric", "alphanumeric", name="pin_format", create_type=False
            ),
            nullable=True,
        ),
    )

    op.create_table(
        "pad_pin_unlocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pad_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unlock_token", sa.String(length=64), nullable=False),
        sa.Column(
            "unlocked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pad_pin_unlocks_pad_id", "pad_pin_unlocks", ["pad_id"])
    # Model declares unlock_token unique=True, index=True → one UNIQUE index
    # (matches ORM metadata; keeps `alembic check` clean).
    op.create_index(
        "ix_pad_pin_unlocks_unlock_token",
        "pad_pin_unlocks",
        ["unlock_token"],
        unique=True,
    )
    op.create_index(
        "ix_pad_pin_unlocks_expires_at", "pad_pin_unlocks", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_pad_pin_unlocks_expires_at", table_name="pad_pin_unlocks")
    op.drop_index("ix_pad_pin_unlocks_unlock_token", table_name="pad_pin_unlocks")
    op.drop_index("ix_pad_pin_unlocks_pad_id", table_name="pad_pin_unlocks")
    op.drop_table("pad_pin_unlocks")
    op.drop_column("pads", "pin_format")
    op.drop_column("pads", "pin_hash")
    op.drop_column("pads", "pin_protected")
    # Drop the enum type explicitly (Postgres keeps it after column drop).
    sa.Enum(name="pin_format").drop(op.get_bind(), checkfirst=True)
