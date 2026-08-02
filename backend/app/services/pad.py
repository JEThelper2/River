import logging
import random
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import File
from app.models.pad import Pad, PadCollaborator, Visibility
from app.services import redirect as redirect_service
from app.services import slug as slug_service
from app.services import storage

logger = logging.getLogger("spacepad.pad")

_MAX_GENERATION_ATTEMPTS = 25


class SlugTakenError(Exception):
    """Raised when a requested slug already exists."""


class NameTakenError(Exception):
    """Raised when a rename target is already used in the pad's namespace."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


async def get_pad_by_slug(db: AsyncSession, slug: str) -> Pad | None:
    result = await db.execute(select(Pad).where(Pad.slug == slug))
    return result.scalar_one_or_none()


async def get_pad_by_owner_and_name(
    db: AsyncSession, username: str, padname: str
) -> Pad | None:
    """Get a pad owned by a user, by the pad's name (slug or custom name).
    
    The padname can be either:
    - The pad's slug (default name, case-insensitive)
    - The pad's custom name (case-sensitive)
    """
    from app.models.user import User
    
    # Look up the owner by username
    owner = await db.execute(
        select(User).where(User.username == username.lower())
    )
    owner_user = owner.scalar_one_or_none()
    if owner_user is None:
        return None
    
    # Look up the pad by owner_id and padname
    # Try matching slug first (case-insensitive), then custom name (case-sensitive)
    result = await db.execute(
        select(Pad).where(
            (Pad.owner_id == owner_user.id) &
            ((Pad.slug == padname) | (Pad.name == padname))
        )
    )
    return result.scalar_one_or_none()


async def get_anonymous_pad_by_segment(db: AsyncSession, segment: str) -> Pad | None:
    """Resolve a single URL segment to a pad, for the dashboard claim form.

    Accepts the pad's slug (global) or its current anonymous custom name. The
    caller checks ``owner_id is None`` to decide claimability — a slug that
    resolves to an already-owned pad is reported as "already claimed", not "not
    found".
    """
    pad = await get_pad_by_slug(db, segment)
    if pad is not None:
        return pad
    return (
        await db.execute(
            select(Pad).where(Pad.owner_id.is_(None), Pad.name == segment)
        )
    ).scalar_one_or_none()


async def touch_last_opened(db: AsyncSession, pad: Pad) -> None:
    pad.last_opened_at = func.now()
    await db.commit()
    await db.refresh(pad)


async def _generate_unique_slug(
    db: AsyncSession,
    rng: random.Random | None = None,
) -> str:
    for _ in range(_MAX_GENERATION_ATTEMPTS):
        candidate = slug_service.generate_slug(rng)
        if await get_pad_by_slug(db, candidate) is not None:
            continue
        if not await redirect_service.is_name_available(
            db,
            candidate,
            namespace=redirect_service.ANONYMOUS,
            namespace_owner=None,
        ):
            continue
        return candidate
    base = slug_service.generate_slug(rng)
    for _ in range(100):
        candidate = f"{base}-{random.randint(100, 9999)}"
        if await get_pad_by_slug(db, candidate) is not None:
            continue
        if not await redirect_service.is_name_available(
            db,
            candidate,
            namespace=redirect_service.ANONYMOUS,
            namespace_owner=None,
        ):
            continue
        return candidate
    raise SlugTakenError("Unable to generate a unique slug.")


async def create_pad(
    db: AsyncSession,
    *,
    slug: str | None,
    content: str = "",
    owner_id: uuid.UUID | None = None,
    rng: random.Random | None = None,
) -> Pad:
    """Create a pad. If slug is None, auto-generate. Validates custom slugs.

    When ``owner_id`` is given (authenticated creation, e.g. from the dashboard)
    the pad is owned and non-anonymous from the start — no separate claim step.

    Raises slug_service.SlugError on invalid custom slug, SlugTakenError on collision.
    """
    if slug is None:
        final_slug = await _generate_unique_slug(db, rng)
    else:
        final_slug = slug_service.validate_custom_slug(slug)
        if await get_pad_by_slug(db, final_slug) is not None:
            raise SlugTakenError(final_slug)
        if not await redirect_service.is_name_available(
            db,
            final_slug,
            namespace=redirect_service.ANONYMOUS,
            namespace_owner=None,
        ):
            raise SlugTakenError(final_slug)

    pad = Pad(
        slug=final_slug,
        content=content,
        owner_id=owner_id,
        is_anonymous=owner_id is None,
    )
    db.add(pad)
    try:
        await db.commit()
    except IntegrityError:
        # Race: another request inserted the same slug between check and commit.
        await db.rollback()
        raise SlugTakenError(final_slug)
    await db.refresh(pad)
    return pad


async def update_pad_content(db: AsyncSession, pad: Pad, content: str) -> Pad:
    pad.content = content
    await db.commit()
    await db.refresh(pad)
    return pad


_UNSET = object()


async def update_pad_metadata(
    db: AsyncSession,
    pad: Pad,
    *,
    name=_UNSET,
    visibility: Visibility | None = _UNSET,
    is_archived: bool | None = _UNSET,
) -> Pad:
    """Owner-only partial metadata update (rename / visibility / archive).

    Only fields explicitly passed (not ``_UNSET``) are applied, so ``name=None``
    clears the custom name while an omitted ``name`` leaves it untouched.
    
    Renaming is handled separately by ``rename_pad`` (it needs a namespaced
    uniqueness check + redirect bookkeeping in one transaction); ``name`` here is
    accepted only as a no-op convenience and ignored if it equals the current
    name. Callers that rename must use ``rename_pad``.
    """
    if visibility is not _UNSET and visibility is not None:
        pad.visibility = visibility
    if is_archived is not _UNSET and is_archived is not None:
        pad.is_archived = is_archived
    await db.commit()
    await db.refresh(pad)
    return pad


async def rename_pad(db: AsyncSession, pad: Pad, new_name: str | None) -> Pad:
    """Rename a pad's canonical name within its namespace (anonymous or claimed).

    The custom name is the pad's address segment, so it must satisfy the same
    URL-safe format + reserved-word rules as a slug. Uniqueness is checked within
    the pad's namespace only (anonymous = global flat pool; claimed = per owner).
    On collision we reject (never auto-suffix); the partial unique indexes on
    ``pads.name`` are the race backstop. One transaction:
    validate → check → set name → update redirect trail → commit.

    Raises ``slug_service.SlugError`` (bad format) or ``NameTakenError`` (taken).
    """
    namespace, ns_owner = redirect_service.namespace_of(pad)
    current_canonical = pad.name or pad.slug

    if new_name is None:
        if pad.name is None:
            return pad  # no-op
        normalized = None
        new_canonical = pad.slug
    else:
        normalized = slug_service.validate_custom_slug(new_name)
        new_canonical = normalized

    if new_canonical == current_canonical:
        return pad  # no-op rename

    if normalized is not None:
        if not await redirect_service.is_name_available(
            db,
            normalized,
            namespace=namespace,
            namespace_owner=ns_owner,
            exclude_pad_id=pad.id,
        ):
            raise NameTakenError(normalized)

    old_name = pad.name
    pad.name = normalized
    await redirect_service.record_name_change(
        db,
        pad,
        old_name=old_name,
        old_namespace=namespace,
        old_namespace_owner=ns_owner,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise NameTakenError(normalized or "")
    await db.refresh(pad)
    return pad


async def delete_pad(db: AsyncSession, pad: Pad) -> None:
    """Hard-delete a pad: remove file objects from storage, then DB rows.

    Collaborator and file rows are removed explicitly (rather than relying on DB
    FK cascade) so the behaviour is identical across Postgres and the SQLite test
    harness, and so storage objects are never orphaned.
    """
    files = (
        await db.execute(select(File).where(File.pad_id == pad.id))
    ).scalars().all()
    for f in files:
        try:
            await storage.delete_object(f.storage_key)
        except storage.StorageError as exc:
            # Object may already be gone (e.g. scan-failed uploads) or storage
            # transient errors; treat as best-effort and log for visibility.
            logger.warning("delete_pad: failed to delete storage object %s: %s", f.storage_key, exc)
        await db.delete(f)
    collabs = (
        await db.execute(
            select(PadCollaborator).where(PadCollaborator.pad_id == pad.id)
        )
    ).scalars().all()
    for c in collabs:
        await db.delete(c)
    await db.delete(pad)
    await db.commit()


async def list_owned_pads(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    archived: bool = False,
    q: str | None = None,
    sort: str | None = None,
    locked: bool | None = None,
    owned: bool | None = None,
) -> list[dict]:
    """Owned pads for the dashboard, newest-opened first, with file aggregates.

    Returns plain dicts (pad + file_count + size_bytes) so the route can build
    ``PadListItem`` without a second round-trip per pad.
    """
    stmt = (
        select(
            Pad,
            func.count(File.id).label("file_count"),
            func.coalesce(func.sum(File.size_bytes), 0).label("size_bytes"),
        )
        .outerjoin(File, File.pad_id == Pad.id)
        .where(Pad.is_archived == archived)
        .group_by(Pad.id)
    )

    # Ownership / collaborator filter: when `owned` is true, restrict to pads
    # the user owns. When omitted or false, include pads the user owns or where
    # they are a collaborator.
    if owned:
        stmt = stmt.where(Pad.owner_id == owner_id)
    else:
        collaborator_subq = select(PadCollaborator.pad_id).where(
            PadCollaborator.user_id == owner_id
        )
        stmt = stmt.where(
            or_(Pad.owner_id == owner_id, Pad.id.in_(collaborator_subq))
        )

    # PIN-locked filter (optional)
    if locked is not None:
        stmt = stmt.where(Pad.pin_protected == locked)

    # Search
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Pad.name.ilike(like), Pad.slug.ilike(like)))

    # Sorting
    if sort is None or sort == "updated":
        stmt = stmt.order_by(Pad.last_opened_at.desc())
    elif sort == "created":
        stmt = stmt.order_by(Pad.created_at.desc())
    elif sort == "name":
        try:
            stmt = stmt.order_by(Pad.name.asc().nulls_last(), Pad.last_opened_at.desc())
        except Exception:
            stmt = stmt.order_by(Pad.last_opened_at.desc())
    else:
        stmt = stmt.order_by(Pad.last_opened_at.desc())

    result = await db.execute(stmt)
    items = []
    for pad, file_count, size_bytes in result.all():
        items.append({"pad": pad, "file_count": file_count, "size_bytes": size_bytes})
    return items
