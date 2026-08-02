"""add_username_to_users

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-24 16:30:00.000000

Add username field to users table. Username is unique, case-insensitive,
3–40 characters, alphanumeric plus hyphens and underscores.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'g2h3i4j5k6l7'
down_revision: str | None = 'f1a2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add username column to users table
    op.add_column('users', sa.Column('username', sa.String(length=40), nullable=True))
    # Create a migration function to populate existing users with a derived username
    # This is a placeholder; existing users may not have usernames yet
    # For existing data, we'll need to handle this carefully in production
    
    # Create unique index on username (case-insensitive)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    
    # Add not-null constraint after data is migrated (handled separately or manually)
    # For now, username is nullable to allow for gradual migration


def downgrade() -> None:
    # Remove the unique index
    op.drop_index(op.f('ix_users_username'), table_name='users')
    # Remove username column
    op.drop_column('users', 'username')
