"""add_pad_collaborators

Revision ID: e5f8b2c3d4a1
Revises: dcd7e589eb1b
Create Date: 2025-04-21 01:00:00.000000

"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = 'e5f8b2c3d4a1'
down_revision = 'dcd7e589eb1b'
branch_labels = None
depends_on = None


def upgrade():
    # Create the pad_collaborators table
    op.create_table(
        'pad_collaborators',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('pad_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('pads.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('role', sa.Enum('viewer', 'editor', name='collaborator_role'), nullable=False),
        sa.Column('invited_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('pad_id', 'user_id', name='uq_pad_collaborator_pad_user'),
    )


def downgrade():
    op.drop_table('pad_collaborators')
