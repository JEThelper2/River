import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Visibility(str, enum.Enum):
    public_edit = "public_edit"
    public_view = "public_view"
    private = "private"


class CollaboratorRole(str, enum.Enum):
    viewer = "viewer"
    editor = "editor"


class PinFormat(str, enum.Enum):
    numeric = "numeric"
    alphanumeric = "alphanumeric"


class Pad(Base, TimestampMixin):
    __tablename__ = "pads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="visibility"),
        default=Visibility.public_edit,
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Historical names that used to address this pad are tracked in the
    # `redirects` table (see Redirect below) — superseding the old
    # `previous_names` JSON column, which couldn't enforce uniqueness or support
    # per-entry "kill the trail" deletion.
    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text('false')
    )

    # Phase 1: plain text body. Phase 2 adds the CRDT snapshot alongside.
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    crdt_snapshot: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    crdt_snapshot_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Phase 6: storage-cost marker for pads untouched past the cold-storage
    # window. NOT deletion and NOT user-facing "expiry" — see DECISIONS.md.
    cold_storage_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )

    # PIN protection (orthogonal to `visibility`): anyone with the link + the PIN
    # can get in. `pin_protected` is an explicit flag, not inferred from pin_hash,
    # for clarity. Never store the PIN in plaintext (argon2 hash only).
    pin_protected: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pin_format: Mapped[PinFormat | None] = mapped_column(
        Enum(PinFormat, name="pin_format"), nullable=True
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # DB-level backstop for rename uniqueness (the app pre-check is the fast path;
    # this catches concurrent same-name renames). The custom `name` is the
    # canonical address segment, scoped per namespace: globally unique among
    # anonymous pads, and unique per owner among claimed pads. NULL names (never
    # renamed) are excluded. The immutable `slug` keeps its own global unique
    # constraint (untouched, AUDIT B3/B4); name-vs-slug collisions are caught by
    # the application check in services/redirect.is_name_available.
    __table_args__ = (
        Index(
            "uq_pad_anon_name",
            "name",
            unique=True,
            postgresql_where=text("owner_id IS NULL AND name IS NOT NULL"),
            sqlite_where=text("owner_id IS NULL AND name IS NOT NULL"),
        ),
        Index(
            "uq_pad_owner_name",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("owner_id IS NOT NULL AND name IS NOT NULL"),
            sqlite_where=text("owner_id IS NOT NULL AND name IS NOT NULL"),
        ),
    )


class PadCollaborator(Base):
    __tablename__ = "pad_collaborators"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pad_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[CollaboratorRole] = mapped_column(
        Enum(CollaboratorRole, name="collaborator_role"),
        nullable=False,
    )
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("pad_id", "user_id", name="uq_pad_collaborator_pad_user"),
    )


class PadPinUnlock(Base):
    """A time-boxed unlock session for a PIN-protected pad.

    The opaque ``unlock_token`` is handed to the client as an httpOnly cookie;
    access is granted while ``expires_at`` is in the future. Expiry is checked on
    every access, so stale rows are harmless — a cleanup job just reaps them.
    """

    __tablename__ = "pad_pin_unlocks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pad_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    unlock_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Redirect(Base, TimestampMixin):
    """A historical address that still resolves to a pad's current canonical URL.

    Created when a pad is renamed or claimed (the old name becomes a redirect).
    Resolution is *not* an HTTP 301 — per AUDIT B4 the API returns the pad body
    directly (200) with ``canonical_url`` and the SPA canonicalizes the address
    bar. ``target_url`` records that canonical address for the SPA.

    Namespaces are kept separate (the spec's two pools): an ``anonymous`` redirect
    has ``namespace_owner IS NULL``; a ``claimed`` redirect carries the owner id.
    A name is "free" again only when no live pad uses it *and* no **active**
    redirect points from it — granular "kill the trail" sets ``active=False``.
    """

    __tablename__ = "redirects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pad_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The freed address segment — a slug (≤40) or a custom name (≤120).
    old_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    # 'anonymous' | 'claimed' — kept explicit (not just inferred from owner) to
    # match the spec and to make the partial unique indexes readable.
    namespace: Mapped[str] = mapped_column(String(16), nullable=False)
    namespace_owner: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_url: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )

    __table_args__ = (
        # One active redirect per name *within a namespace*. Two partial indexes
        # rather than one over (old_slug, namespace, namespace_owner): a single
        # index can't enforce anonymous uniqueness because namespace_owner is NULL
        # there and NULLs are distinct in a unique index. (Mirrors the existing
        # pads anon/owner partial-index split.)
        Index(
            "uq_redirect_anon_active",
            "old_slug",
            unique=True,
            postgresql_where=text("active AND namespace = 'anonymous'"),
            sqlite_where=text("active AND namespace = 'anonymous'"),
        ),
        Index(
            "uq_redirect_claimed_active",
            "namespace_owner",
            "old_slug",
            unique=True,
            postgresql_where=text("active AND namespace = 'claimed'"),
            sqlite_where=text("active AND namespace = 'claimed'"),
        ),
    )


class ClaimToken(Base):
    """A time-bound token that authorizes claiming an anonymous pad from the
    dashboard. Not single-use: it can be submitted repeatedly until it expires;
    a *successful* claim consumes it (``consumed=True``). Generating a new token
    invalidates any still-live one for the pad (one active token per pad)."""

    __tablename__ = "claim_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pad_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_claim_tokens_pad_unconsumed",
            "pad_id",
            postgresql_where=text("consumed = false"),
            sqlite_where=text("consumed = false"),
        ),
    )
