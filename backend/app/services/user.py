"""User creation and lookup. Phase 4."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services import auth as auth_service
from app.services import username as username_service


class EmailTakenError(Exception):
    """Raised when an email is already registered."""


class UsernameTakenError(Exception):
    """Raised when a username is already taken."""


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    return (
        await db.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    normalized = username.strip().lower()
    return (
        await db.execute(select(User).where(User.username == normalized))
    ).scalar_one_or_none()


def _sanitize_username_base(raw: str) -> str:
    """Coerce an arbitrary string (chosen username or email local-part) into the
    allowed username charset: lowercase ``[a-z0-9_-]``, starting/ending
    alphanumeric, no consecutive hyphens, length 3–40."""
    base = re.sub(r"[^a-z0-9_-]", "-", (raw or "").strip().lower())
    base = re.sub(r"-{2,}", "-", base).strip("-_")
    if len(base) < 3:
        base = (base + "user") if base else "user"
    return base[:40]


async def _unique_username(
    db: AsyncSession, *, chosen: str | None, email: str, exclude_id: uuid.UUID
) -> str:
    """A valid, unique username. Prefer the chosen one (the name the user actually
    picked at signup); fall back to the email local-part. On collision, append a
    numeric suffix instead of failing — so a username clash never 500s the mirror
    (AUDIT H2)."""
    base = _sanitize_username_base(chosen or email.split("@")[0])
    candidate = base
    existing = await get_by_username(db, candidate)
    if existing is None or existing.id == exclude_id:
        return candidate
    for n in range(2, 1000):
        suffix = f"-{n}"
        candidate = f"{base[: 40 - len(suffix)]}{suffix}"
        existing = await get_by_username(db, candidate)
        if existing is None or existing.id == exclude_id:
            return candidate
    # Pathological fallback: a random suffix from the user's own UUID.
    return f"{base[:31]}-{uuid.UUID(str(exclude_id)).hex[:8]}"


async def upsert_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | str,
    email: str,

    email_verified: bool = False,
    provider: str | None = None,
    username: str | None = None,
) -> User:
    """Create or refresh the ``public.users`` profile mirroring an
    ``auth.users`` row (same UUID). The linkage is matching-UUID
    (``public.users.id == auth.users.id``), enforced in application code — see
    DECISIONS.md for why we don't add a cross-schema DB foreign key.
    """
    uid = uuid.UUID(str(user_id))
    normalized = email.strip().lower()

    user = await get_by_id(db, uid)
    if user is None:
        chosen_username = await _unique_username(
            db, chosen=username, email=normalized, exclude_id=uid
        )
        user = User(
            id=uid,
            email=normalized,

            email_verified=email_verified,
            oauth_provider=provider,
            username=chosen_username,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            # Lost a race (same id or username inserted concurrently). Resolve to
            # the now-existing row by id; never return None (would 500 the caller).
            await db.rollback()
            existing = await get_by_id(db, uid)
            if existing is not None:
                return existing
            # The clash was on username, not id — retry once with a fresh suffix.
            retry_username = await _unique_username(
                db, chosen=username, email=normalized, exclude_id=uid
            )
            user = User(
                id=uid,
                email=normalized,

                email_verified=email_verified,
                oauth_provider=provider,
                username=retry_username,
            )
            db.add(user)
            await db.commit()
        await db.refresh(user)
        return user

    changed = False
    if normalized and user.email != normalized:
        user.email = normalized

        changed = True
    if provider and user.oauth_provider != provider:
        user.oauth_provider = provider
        changed = True
    # Only ever upgrade verification — never silently downgrade it.
    if email_verified and not user.email_verified:
        user.email_verified = True
        changed = True
    if changed:
        await db.commit()
        await db.refresh(user)
    return user


async def get_or_sync_from_claims(
    db: AsyncSession, claims: dict, *, mark_verified: bool
) -> User | None:
    """Resolve the profile for a verified access token, syncing it from the
    token claims. Returns None for a token whose ``sub`` has no profile and whose
    claims carry no email to bootstrap one (the legacy/test path)."""
    sub = claims.get("sub")
    if not sub:
        return None
    try:
        uid = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        return None

    email = (claims.get("email") or "").strip().lower()
    meta = claims.get("user_metadata") or {}

    provider = (claims.get("app_metadata") or {}).get("provider")

    if not email:
        # Legacy/offline token (sub only): only resolve an existing profile.
        existing = await get_by_id(db, uid)
        if existing is not None and mark_verified and not existing.email_verified:
            existing.email_verified = True
            await db.commit()
            await db.refresh(existing)
        return existing

    return await upsert_profile(
        db,
        user_id=uid,
        email=email,

        email_verified=mark_verified,
        provider=provider,
    )


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    username: str,
    password: str,

) -> User:
    # Validate and normalize username
    try:
        normalized_username = username_service.validate_username(username)
    except username_service.UsernameError as e:
        raise UsernameTakenError(str(e))

    user = User(
        email=email.strip().lower(),
        username=normalized_username,
        password_hash=auth_service.hash_password(password),

    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if "email" in str(e):
            raise EmailTakenError(email)
        elif "username" in str(e):
            raise UsernameTakenError(username)
        raise
    await db.refresh(user)
    return user


async def set_password(db: AsyncSession, user: User, new_password: str) -> User:
    user.password_hash = auth_service.hash_password(new_password)
    await db.commit()
    await db.refresh(user)
    return user


async def mark_email_verified(db: AsyncSession, user: User) -> User:
    user.email_verified = True
    await db.commit()
    await db.refresh(user)
    return user


async def upsert_google_user(
    db: AsyncSession,
    *,
    email: str,
    subject: str,

) -> User:
    """Find or create a user from a verified Google profile."""
    existing = await get_by_email(db, email)
    if existing is not None:
        if existing.oauth_provider is None:
            existing.oauth_provider = "google"
            existing.oauth_subject = subject
            existing.email_verified = True
            await db.commit()
            await db.refresh(existing)
        return existing

    # Generate a default username based on email
    normalized_email = email.strip().lower()
    username = normalized_email.split("@")[0]
    # Ensure minimum length of 3 for username
    if len(username) < 3:
        username = username + "user"

    user = User(
        email=normalized_email,
        oauth_provider="google",
        oauth_subject=subject,
        email_verified=True,

        username=username,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await get_by_email(db, email)
    await db.refresh(user)
    return user
