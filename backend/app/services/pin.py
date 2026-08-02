"""PIN-protected pads. Phase: Supabase + PIN.

PIN protection is *orthogonal* to ``visibility`` — a public_edit/public_view pad
can additionally require a short PIN. Unlock is time-boxed (a session window),
tracked by an opaque token handed to the client as an httpOnly cookie. The pad
owner bypasses the PIN (they set it and manage the pad); everyone else needs a
valid unlock token. PINs are argon2-hashed via the shared hashing util — never
stored or compared in plaintext.
"""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.pad import Pad, PadPinUnlock, PinFormat
from app.models.user import User
from app.services import access as access_service
from app.services import hashing

settings = get_settings()

# httpOnly cookie carrying the unlock token; path-scoped per pad at set time.
UNLOCK_COOKIE = "dl_pad_unlock"


class InvalidPinError(Exception):
    """Raised when a submitted PIN doesn't match the required format/length."""


def cookie_path(slug: str) -> str:
    return f"/api/pads/{slug}"


def validate_pin(pin: str, pin_format: PinFormat) -> None:
    if not (settings.pin_min_length <= len(pin) <= settings.pin_max_length):
        raise InvalidPinError(
            f"PIN must be {settings.pin_min_length}–{settings.pin_max_length} characters."
        )
    if pin_format is PinFormat.numeric:
        if not pin.isdigit():
            raise InvalidPinError("Numeric PIN must contain digits only.")
    else:
        allowed = set(string.ascii_letters + string.digits)
        if not set(pin) <= allowed:
            raise InvalidPinError("PIN must be letters and digits only.")


async def set_pin(db: AsyncSession, pad: Pad, *, pin: str, pin_format: PinFormat) -> Pad:
    validate_pin(pin, pin_format)
    pad.pin_protected = True
    pad.pin_format = pin_format
    pad.pin_hash = hashing.hash_secret(pin)
    await db.commit()
    await db.refresh(pad)
    return pad


async def clear_pin(db: AsyncSession, pad: Pad) -> Pad:
    pad.pin_protected = False
    pad.pin_hash = None
    pad.pin_format = None
    # Existing unlock sessions are now meaningless; drop them.
    await db.execute(delete(PadPinUnlock).where(PadPinUnlock.pad_id == pad.id))
    await db.commit()
    await db.refresh(pad)
    return pad


def verify_pin(pad: Pad, pin: str) -> bool:
    if not pad.pin_hash:
        return False
    return hashing.verify_secret(pad.pin_hash, pin)


async def create_unlock(db: AsyncSession, pad: Pad) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.pin_unlock_window_seconds
    )
    db.add(PadPinUnlock(pad_id=pad.id, unlock_token=token, expires_at=expires_at))
    await db.commit()
    return token, expires_at


async def is_unlock_token_valid(
    db: AsyncSession, pad: Pad, token: str | None, *, now: datetime | None = None
) -> bool:
    if not token:
        return False
    reference = now or datetime.now(UTC)
    row = (
        await db.execute(
            select(PadPinUnlock).where(
                PadPinUnlock.unlock_token == token,
                PadPinUnlock.pad_id == pad.id,  # token must belong to THIS pad
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # SQLite returns naive datetimes
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > reference


async def has_pin_access(
    db: AsyncSession,
    pad: Pad,
    user: User | None,
    unlock_token: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether the PIN gate is satisfied. Owner always bypasses the PIN."""
    if not pad.pin_protected:
        return True
    if access_service.is_owner(pad, user):
        return True
    return await is_unlock_token_valid(db, pad, unlock_token, now=now)


async def purge_expired(db: AsyncSession, *, now: datetime | None = None) -> int:
    reference = now or datetime.now(UTC)
    result = await db.execute(
        delete(PadPinUnlock).where(PadPinUnlock.expires_at < reference)
    )
    await db.commit()
    return result.rowcount or 0
