"""redirects table + claim_tokens table; migrate previous_names; drop the column

Revision ID: i9j0k1l2m3n4
Revises: 340f7a3d4015
Create Date: 2026-06-29 21:30:00.000000

Pad naming / claiming / redirect system (Path A — adapt onto the existing model).
Replaces the ``pads.previous_names`` JSON column with a real ``redirects`` table
(per-entry "kill the trail", DB-enforced namespaced uniqueness) and adds a
``claim_tokens`` table for the dashboard claim flow. Also adds partial unique
indexes on ``pads.name`` as the rename-collision backstop. The immutable global
``pads.slug`` unique constraint is left untouched (AUDIT B3/B4).

Postgres-only migration (the test harness builds tables from the ORM via
create_all). Data migration from previous_names is a no-op on current data
(verified 0 pads with previous_names) but handled for completeness.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "i9j0k1l2m3n4"
down_revision: str | None = "340f7a3d4015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redirects",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pad_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_slug", sa.String(length=120), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column(
            "namespace_owner",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("target_url", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_redirects_pad_id", "redirects", ["pad_id"])
    op.create_index("ix_redirects_namespace_owner", "redirects", ["namespace_owner"])
    # One active redirect per name within a namespace (two partial indexes — a
    # single index can't enforce anon uniqueness because namespace_owner is NULL).
    op.create_index(
        "uq_redirect_anon_active",
        "redirects",
        ["old_slug"],
        unique=True,
        postgresql_where=sa.text("active AND namespace = 'anonymous'"),
    )
    op.create_index(
        "uq_redirect_claimed_active",
        "redirects",
        ["namespace_owner", "old_slug"],
        unique=True,
        postgresql_where=sa.text("active AND namespace = 'claimed'"),
    )

    op.create_table(
        "claim_tokens",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pad_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token", name="uq_claim_tokens_token"),
    )
    op.create_index("ix_claim_tokens_pad_id", "claim_tokens", ["pad_id"])
    op.create_index("ix_claim_tokens_token", "claim_tokens", ["token"])
    op.create_index(
        "ix_claim_tokens_pad_unconsumed",
        "claim_tokens",
        ["pad_id"],
        postgresql_where=sa.text("consumed = false"),
    )

    # Rename-collision backstop: name unique within its namespace (NULLs excluded).
    op.create_index(
        "uq_pad_anon_name",
        "pads",
        ["name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NULL AND name IS NOT NULL"),
    )
    op.create_index(
        "uq_pad_owner_name",
        "pads",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NOT NULL AND name IS NOT NULL"),
    )

    # Migrate previous_names -> redirects (no-op on current data). Each old name
    # becomes an active redirect pointing at the pad's current canonical URL.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT p.id, p.slug, p.name, p.owner_id, p.previous_names, u.username "
            "FROM pads p LEFT JOIN users u ON u.id = p.owner_id "
            "WHERE p.previous_names IS NOT NULL AND p.previous_names::text <> '[]'"
        )
    ).fetchall()
    for r in rows:
        prev = r.previous_names or []
        if r.owner_id is not None:
            namespace = "claimed"
            target = f"/{r.username}/{r.name or r.slug}"
        else:
            namespace = "anonymous"
            target = f"/{r.name or r.slug}"
        for old in prev:
            conn.execute(
                sa.text(
                    "INSERT INTO redirects "
                    "(id, pad_id, old_slug, namespace, namespace_owner, target_url, active, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :pad_id, :old, :ns, :owner, :target, true, now(), now()) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "pad_id": r.id,
                    "old": old,
                    "ns": namespace,
                    "owner": r.owner_id,
                    "target": target,
                },
            )

    op.drop_column("pads", "previous_names")


def downgrade() -> None:
    # Restore the previous_names column and best-effort repopulate it from the
    # active redirect rows, then drop the new structures.
    op.add_column(
        "pads",
        sa.Column("previous_names", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE pads SET previous_names = COALESCE("
            "(SELECT json_agg(r.old_slug) FROM redirects r "
            "WHERE r.pad_id = pads.id AND r.active), '[]'::json)"
        )
    )

    op.drop_index("uq_pad_owner_name", table_name="pads")
    op.drop_index("uq_pad_anon_name", table_name="pads")

    op.drop_index("ix_claim_tokens_pad_unconsumed", table_name="claim_tokens")
    op.drop_index("ix_claim_tokens_token", table_name="claim_tokens")
    op.drop_index("ix_claim_tokens_pad_id", table_name="claim_tokens")
    op.drop_table("claim_tokens")

    op.drop_index("uq_redirect_claimed_active", table_name="redirects")
    op.drop_index("uq_redirect_anon_active", table_name="redirects")
    op.drop_index("ix_redirects_namespace_owner", table_name="redirects")
    op.drop_index("ix_redirects_pad_id", table_name="redirects")
    op.drop_table("redirects")
