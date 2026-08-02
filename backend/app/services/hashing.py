"""Shared argon2 secret hashing.

Extracted from the Phase 4 auth service so it survives the Supabase Auth
migration (which removes FastAPI's own password hashing): PIN hashing for
PIN-protected pads reuses *this* utility rather than rolling a second scheme or
depending on auth code that no longer exists after the migration.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError

from app.core.config import get_settings

settings = get_settings()
_ph = PasswordHasher(
    time_cost=settings.pin_argon2_time_cost,
    memory_cost=settings.pin_argon2_memory_cost,
    parallelism=settings.pin_argon2_parallelism,
)


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret_hash: str, secret: str) -> bool:
    """Constant-time argon2 verification. False on any mismatch/format error."""
    try:
        return _ph.verify(secret_hash, secret)
    except VerifyMismatchError:
        return False
    except Argon2Error:
        return False
