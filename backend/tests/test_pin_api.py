"""PIN-protected pads: lock/unlock, rate limiting, mutual exclusion, expiry, WS."""

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api import ws as ws_module
from app.models.pad import Pad, PadPinUnlock, Visibility
from app.services import auth as auth_service
from app.services import ratelimit
from app.services.ratelimit import InMemoryBackend


async def _signup(client, email, username=None):
    if username is None:
        username = email.split("@")[0]
        # Ensure minimum length of 3 for username
        if len(username) < 3:
            username = username + "user"
    resp = await client.post(
        "/api/auth/signup", json={"email": email, "password": "password123", "username": username}
    )
    body = resp.json()
    return body["access_token"], uuid.UUID(body["user"]["id"])


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def _make_pin_pad(client, owner_token, slug, pin="1234"):
    await client.post("/api/pads", json={"slug": slug, "content": "secret body"}, headers=_auth(owner_token))
    resp = await client.patch(
        f"/api/pads/{slug}",
        json={"pin_protected": True, "pin": pin, "pin_format": "numeric"},
        headers=_auth(owner_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pin_protected"] is True


# --- locked-then-unlock (acceptance criterion) -------------------------------
async def test_pin_pad_visible_but_locked_then_unlocks(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    await _make_pin_pad(client, owner_token, "locked-pad")

    # A fresh visitor (no auth, no cookie) sees it as locked, with NO content.
    client.cookies.clear()
    locked = await client.get("/api/pads/locked-pad")
    assert locked.status_code == 200
    body = locked.json()
    assert body["locked"] is True
    assert body["pin_protected"] is True
    assert body["content"] == ""  # content never leaks while locked

    # Wrong PIN → distinct 401.
    wrong = await client.post("/api/pads/locked-pad/unlock", json={"pin": "9999"})
    assert wrong.status_code == 401

    # Correct PIN → content returned, unlock cookie set.
    ok = await client.post("/api/pads/locked-pad/unlock", json={"pin": "1234"})
    assert ok.status_code == 200
    assert ok.json()["locked"] is False
    assert ok.json()["content"] == "secret body"

    # Subsequent GET carries the cookie → unlocked, fully functional.
    after = await client.get("/api/pads/locked-pad")
    assert after.json()["locked"] is False
    assert after.json()["content"] == "secret body"


async def test_owner_bypasses_pin(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    await _make_pin_pad(client, owner_token, "owner-pin-pad")
    # Owner sees content without unlocking.
    resp = await client.get("/api/pads/owner-pin-pad", headers=_auth(owner_token))
    assert resp.json()["locked"] is False
    assert resp.json()["content"] == "secret body"


# --- rate limiting (acceptance criterion) ------------------------------------
@pytest.fixture
def inmem_limiter():
    ratelimit.set_backend(InMemoryBackend())
    yield
    ratelimit.set_backend(None)


async def test_pin_attempts_rate_limited_distinct_error(client, inmem_limiter):
    owner_token, _ = await _signup(client, "owner@example.com")
    await _make_pin_pad(client, owner_token, "brute-pad")
    client.cookies.clear()

    # Default limit is 5 attempts; the 6th is rate-limited with a DISTINCT 429.
    for _ in range(5):
        r = await client.post("/api/pads/brute-pad/unlock", json={"pin": "0000"})
        assert r.status_code == 401  # incorrect PIN, not yet limited
    limited = await client.post("/api/pads/brute-pad/unlock", json={"pin": "0000"})
    assert limited.status_code == 429
    assert "retry-after" in {k.lower() for k in limited.headers}


# --- mutual exclusion with private (acceptance criterion) --------------------
async def test_private_and_pin_mutually_exclusive(client, session_factory):
    owner_token, user_id = await _signup(client, "owner@example.com")
    await client.post("/api/pads", json={"slug": "excl-pad"}, headers=_auth(owner_token))

    # verify email so private is allowed, then make it private
    from app.models.user import User

    async with session_factory() as db:
        u = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        u.email_verified = True
        await db.commit()
    await client.patch("/api/pads/excl-pad", json={"visibility": "private"}, headers=_auth(owner_token))

    # adding a PIN to a private pad is rejected
    resp = await client.patch(
        "/api/pads/excl-pad",
        json={"pin_protected": True, "pin": "1234", "pin_format": "numeric"},
        headers=_auth(owner_token),
    )
    assert resp.status_code == 422

    # the reverse: a PIN-protected pad can't be switched to private
    await _make_pin_pad(client, owner_token, "pinned-pad")
    rev = await client.patch(
        "/api/pads/pinned-pad", json={"visibility": "private"}, headers=_auth(owner_token)
    )
    assert rev.status_code == 422


# --- unlock expiry (acceptance criterion) ------------------------------------
async def test_unlock_expires_after_window(client, session_factory):
    owner_token, _ = await _signup(client, "owner@example.com")
    await _make_pin_pad(client, owner_token, "expiry-pad")
    client.cookies.clear()

    unlock = await client.post("/api/pads/expiry-pad/unlock", json={"pin": "1234"})
    assert unlock.json()["locked"] is False

    # Force the unlock session into the past.
    async with session_factory() as db:
        row = (await db.execute(select(PadPinUnlock))).scalar_one()
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await db.commit()

    relocked = await client.get("/api/pads/expiry-pad")
    assert relocked.json()["locked"] is True
    assert relocked.json()["content"] == ""


async def test_clear_pin_reopens_pad(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    await _make_pin_pad(client, owner_token, "clearable-pad")
    cleared = await client.patch(
        "/api/pads/clearable-pad", json={"pin_protected": False}, headers=_auth(owner_token)
    )
    assert cleared.json()["pin_protected"] is False
    client.cookies.clear()
    resp = await client.get("/api/pads/clearable-pad")
    assert resp.json()["locked"] is False
    assert resp.json()["content"] == "secret body"


# --- WS gate -----------------------------------------------------------------
@pytest_asyncio.fixture
async def wsdb(session_factory, monkeypatch):
    monkeypatch.setattr(ws_module, "SessionLocal", session_factory)
    return session_factory


async def _make_pad_row(factory, slug, *, owner_id=None, pin_protected=False, pin_hash=None):
    async with factory() as db:
        pad = Pad(
            slug=slug,
            owner_id=owner_id,
            visibility=Visibility.public_edit,
            is_anonymous=owner_id is None,
            pin_protected=pin_protected,
            pin_hash=pin_hash,
        )
        db.add(pad)
        await db.commit()
        return pad.id


async def test_ws_pin_pad_rejects_without_unlock(wsdb):
    from app.services import hashing

    await _make_pad_row(
        wsdb, "ws-pin-01", pin_protected=True, pin_hash=hashing.hash_secret("1234")
    )
    auth = await ws_module.authorize_ws("ws-pin-01", token=None, unlock_token=None)
    assert auth.allowed is False
    assert auth.close_code == ws_module.CLOSE_LOCKED


async def test_ws_pin_pad_allows_with_valid_unlock(wsdb):
    from app.services import hashing
    from app.services import pin as pin_service

    pad_id = await _make_pad_row(
        wsdb, "ws-pin-02", pin_protected=True, pin_hash=hashing.hash_secret("1234")
    )
    # create a valid unlock row + token
    async with wsdb() as db:
        pad = (await db.execute(select(Pad).where(Pad.id == pad_id))).scalar_one()
        token, _ = await pin_service.create_unlock(db, pad)

    auth = await ws_module.authorize_ws("ws-pin-02", token=None, unlock_token=token)
    assert auth.allowed is True


async def test_ws_pin_pad_allows_owner_without_unlock(wsdb):
    from app.services import hashing

    owner_id = uuid.uuid4()
    async with wsdb() as db:
        from app.models.user import User

        db.add(User(id=owner_id, email="o@example.com", password_hash="x", username="owner"))
        await db.commit()
    await _make_pad_row(
        wsdb, "ws-pin-03", owner_id=owner_id, pin_protected=True,
        pin_hash=hashing.hash_secret("1234"),
    )
    token = auth_service.create_access_token(owner_id)
    auth = await ws_module.authorize_ws("ws-pin-03", token=token, unlock_token=None)
    assert auth.allowed is True
