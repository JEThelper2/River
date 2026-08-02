"""Claim tokens and the dashboard claim transaction.

A pad creator (any current viewer of an unclaimed pad) generates a time-bound
claim token from inside the pad; a logged-in user submits it from the dashboard
together with the pad URL (and the PIN, if the pad is locked) to take ownership.

Single-winner and no-PIN-oracle guarantees are enforced at the SQL layer:
* ownership transfer is an atomic ``UPDATE ... WHERE owner_id IS NULL``;
* token consumption is an atomic ``UPDATE ... WHERE consumed = false AND
  expires_at > now`` (re-validates expiry *inside* the transaction);
* when the pad is PIN-protected, token-validity and PIN-correctness are folded
  into one generic error so a guesser can't tell whether the token was valid.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.pad import ClaimToken, Pad
from app.services import pin as pin_service
from app.services import redirect as redirect_service

settings = get_settings()


class ClaimError(Exception):
    """Base for claim failures. ``message`` is safe to show the user."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PadAlreadyOwnedError(ClaimError):
    def __init__(self) -> None:
        super().__init__("This pad has already been claimed.")


class InvalidClaimError(ClaimError):
    """Token invalid/expired/consumed, or (for locked pads) token-or-PIN wrong —
    deliberately one generic error so PIN guessing has no token-validity oracle."""


async def generate_token(db: AsyncSession, pad: Pad) -> tuple[str, datetime]:
    """Mint a fresh claim token, invalidating any still-live one for this pad
    (one active token per pad keeps the flow unambiguous)."""
    await db.execute(
        update(ClaimToken)
        .where(ClaimToken.pad_id == pad.id, ClaimToken.consumed.is_(False))
        .values(consumed=True)
    )
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.claim_token_ttl_seconds
    )
    db.add(ClaimToken(pad_id=pad.id, token=token, expires_at=expires_at))
    await db.commit()
    return token, expires_at


async def _token_matches(
    db: AsyncSession, pad_id: uuid.UUID, token: str, now: datetime
) -> bool:
    if not token:
        return False
    row = (
        await db.execute(
            select(ClaimToken.id).where(
                ClaimToken.token == token,
                ClaimToken.pad_id == pad_id,
                ClaimToken.consumed.is_(False),
                ClaimToken.expires_at > now,
            )
        )
    ).first()
    return row is not None


async def claim_with_token(
    db: AsyncSession,
    pad: Pad,
    *,
    token: str,
    owner_id: uuid.UUID,
    pin: str | None = None,
) -> Pad:
    """Transfer ownership of an anonymous pad. One transaction; raises ClaimError.

    PIN persists through the claim (option (b)): the claimer proves the PIN here,
    and the pad stays ``pin_protected`` afterwards — ownership/visibility layer on
    top, and the existing private⊕pin_protected mutual-exclusion is untouched
    because a claim never sets ``private``.
    """
    if pad.owner_id is not None:
        raise PadAlreadyOwnedError()

    now = datetime.now(UTC)
    token_ok = await _token_matches(db, pad.id, token, now)

    if pad.pin_protected:
        # Fold token + PIN into one verdict — no oracle either way.
        pin_ok = bool(pin) and pin_service.verify_pin(pad, pin)
        if not (token_ok and pin_ok):
            raise InvalidClaimError(
                "That claim token or PIN isn't valid. Check both and try again."
            )
    elif not token_ok:
        raise InvalidClaimError("That claim token isn't valid or has expired.")

    # Atomic single-winner ownership transfer.
    res = await db.execute(
        update(Pad)
        .where(Pad.id == pad.id, Pad.owner_id.is_(None))
        .values(owner_id=owner_id, is_anonymous=False)
    )
    if (res.rowcount or 0) == 0:
        await db.rollback()
        raise PadAlreadyOwnedError()

    # Atomic token consumption — re-checks expiry/consumed inside the transaction.
    consumed = await db.execute(
        update(ClaimToken)
        .where(
            ClaimToken.token == token,
            ClaimToken.pad_id == pad.id,
            ClaimToken.consumed.is_(False),
            ClaimToken.expires_at > now,
        )
        .values(consumed=True)
    )
    if (consumed.rowcount or 0) == 0:
        await db.rollback()
        raise InvalidClaimError("That claim token isn't valid or has expired.")

    # Reload so the ORM object reflects the new owner before we compute the new
    # canonical URL for the redirect trail (reads this transaction's own writes).
    await db.refresh(pad)

    # The old *anonymous* address (its custom name, if any) now redirects to the
    # new /{username}/... canonical. A never-renamed pad keeps resolving by slug
    # at the bare route, so it needs no redirect row.
    await redirect_service.record_name_change(
        db,
        pad,
        old_name=pad.name,
        old_namespace=redirect_service.ANONYMOUS,
        old_namespace_owner=None,
    )
    await db.commit()
    await db.refresh(pad)
    return pad
