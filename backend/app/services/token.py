"""Issue and consume single-use emailed tokens. Phase 7."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.token import EmailToken, TokenPurpose


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    purpose: TokenPurpose,
    ttl_seconds: int,
) -> str:
    """Create a token row, returning the *raw* token (only the hash is stored)."""
    raw = secrets.token_urlsafe(32)
    token = EmailToken(
        user_id=user_id,
        purpose=purpose,
        token_hash=_hash(raw),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    db.add(token)
    await db.commit()
    return raw


async def consume(
    db: AsyncSession, *, raw: str, purpose: TokenPurpose
) -> uuid.UUID | None:
    """Validate and single-use-consume a token. Returns the user_id or None.

    None covers every failure mode (unknown, wrong purpose, expired, already
    used) so callers can't distinguish them — no oracle for attackers.
    """
    row = (
        await db.execute(
            select(EmailToken).where(
                EmailToken.token_hash == _hash(raw),
                EmailToken.purpose == purpose,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # SQLite returns naive datetimes
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        return None
    row.used_at = datetime.now(UTC)
    await db.commit()
    return row.user_id
