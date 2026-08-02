import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.pad import CollaboratorRole, PinFormat, Visibility


class PadCreate(BaseModel):
    # Optional custom slug; if omitted, the server auto-generates one.
    slug: str | None = Field(default=None, max_length=40)
    content: str = ""


class PadUpdate(BaseModel):
    """Content (body) update — separate auth path from metadata (see PadPatch)."""

    content: str


class PadPatch(BaseModel):
    """Owner-only metadata update: rename, change visibility, archive/unarchive,
    set/clear PIN protection.

    All fields optional; only the provided keys are applied (partial update).
    Setting ``pin_protected: true`` requires ``pin`` (and ``pin_format``).
    """

    name: str | None = Field(default=None, max_length=120)
    visibility: Visibility | None = None
    is_archived: bool | None = None
    pin_protected: bool | None = None
    pinned: bool | None = None
    color: str | None = Field(default=None, max_length=32)
    pin: str | None = Field(default=None, max_length=64)
    pin_format: PinFormat | None = None


class PinUnlockIn(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


class ClaimIn(BaseModel):
    """Dashboard claim submission. ``token`` is required; ``pin`` only when the
    pad is PIN-protected (the form always shows the field; blank otherwise)."""

    token: str = Field(min_length=1, max_length=64)
    pin: str | None = Field(default=None, max_length=64)


class ClaimTokenOut(BaseModel):
    token: str
    expires_at: datetime


class RedirectOut(BaseModel):
    """A historical address that still resolves to a pad (the "old links" view)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    old_slug: str
    namespace: str
    target_url: str
    created_at: datetime


class PadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str | None
    owner_id: uuid.UUID | None
    visibility: Visibility
    is_archived: bool
    content: str
    is_anonymous: bool
    last_opened_at: datetime
    created_at: datetime
    updated_at: datetime
    pin_protected: bool = False
    pin_format: PinFormat | None = None
    # Computed per-request for the authenticated viewer: may they edit content?
    can_edit: bool = True
    pinned: bool = False
    color: str | None = None
    # True when the pad is PIN-gated and this requester hasn't unlocked it; when
    # set, `content` is withheld (empty) so locked content never leaks.
    locked: bool = False
    # Browser-facing canonical address for an owned pad (`/{username}/{padname}`).
    # The REST API always returns pad content directly (no 301); the SPA uses this
    # to canonicalize the address bar client-side (AUDIT B4). None for anon pads.
    canonical_url: str | None = None


class PadListItem(BaseModel):
    """Row in the dashboard pad list (no full content body — kept light)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str | None
    visibility: Visibility
    is_archived: bool
    pin_protected: bool = False
    last_opened_at: datetime
    created_at: datetime
    updated_at: datetime
    file_count: int = 0
    size_bytes: int = 0
    pinned: bool = False
    color: str | None = None
    preview_text: str | None = None


class CollaboratorIn(BaseModel):
    email: EmailStr
    role: CollaboratorRole = CollaboratorRole.editor


class CollaboratorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str

    role: CollaboratorRole
    invited_at: datetime
    accepted_at: datetime | None
