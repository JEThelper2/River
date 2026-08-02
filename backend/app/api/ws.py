"""WebSocket endpoint bridging FastAPI to the pycrdt y-websocket server.

Phase 5 adds an authorization gate *before* the room join: a connection to a
pad it isn't allowed to write is closed before any CRDT state is exchanged.

Auth transport: the access token is read from the ``token`` query parameter.
Browser WebSocket clients cannot set an ``Authorization`` header on the
handshake, and the y-websocket client appends connection params to the URL, so a
query param is the clean fit here (documented in DECISIONS.md).

Close codes (4000-4999, app-defined, survive to the browser):
  4404 — slug is not a valid pad name
  4403 — authenticated/anonymous user lacks write access to this pad
  4429 — per-connection edit rate limit exceeded
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.api import deps
from app.db.session import SessionLocal
from app.models.pad import Pad
from app.services import access as access_service
from app.services import pin as pin_service
from app.services import ratelimit
from app.services import slug as slug_service
from app.services import user as user_service
from app.services.crdt import PadWebsocketServer

router = APIRouter(tags=["realtime"])

server = PadWebsocketServer()

CLOSE_BAD_SLUG = 4404
CLOSE_NO_ACCESS = 4403
CLOSE_LOCKED = 4401
CLOSE_RATE_LIMITED = 4429


@dataclass
class Authorization:
    allowed: bool
    close_code: int | None = None


async def authorize_ws(
    slug: str, token: str | None, unlock_token: str | None = None
) -> Authorization:
    """Decide whether a WS handshake for ``slug`` may join (write access).

    A non-existent pad is treated as an open scratch (allowed) — there's no
    private pad to protect yet, matching the REST "creatable" behaviour. An
    existing pad applies the same content-write rules as REST, plus the PIN gate
    for PIN-protected pads (the unlock token rides in the handshake cookie).
    """
    async with SessionLocal() as db:
        pad = (
            await db.execute(select(Pad).where(Pad.slug == slug))
        ).scalar_one_or_none()
        if pad is None:
            return Authorization(allowed=True)

        # Resolve the user from the handshake ``?token=`` access token. Supabase
        # ES256 tokens are verified against the project JWKS (legacy HS256 is
        # accepted only outside production) — same path as REST, in deps.py.
        user = None
        if token:
            try:
                claims, is_supabase = deps.verify_access_token(token)
                user = await user_service.get_or_sync_from_claims(
                    db, claims, mark_verified=is_supabase
                )
            except deps.AuthError:
                user = None

        # PIN gate first: a locked pad rejects with a distinct code so the client
        # can prompt for the PIN rather than show a generic "no access".
        if not await pin_service.has_pin_access(db, pad, user, unlock_token):
            return Authorization(allowed=False, close_code=CLOSE_LOCKED)

        if await access_service.can_write_content(db, pad, user):
            return Authorization(allowed=True)
        return Authorization(allowed=False, close_code=CLOSE_NO_ACCESS)


class _FastAPIChannel:
    """Adapts a FastAPI WebSocket to the pycrdt ``Channel`` protocol.

    ``path`` is the room name (the pad slug); pycrdt's ``serve()`` reads it to
    pick the room. Each inbound frame is counted against the connection's edit
    rate limit (PRD §5.4: 60 edit-events/min/connection).
    """

    def __init__(self, websocket: WebSocket, path: str, conn_id: str) -> None:
        self._ws = websocket
        self.path = path
        self._conn_id = conn_id

    def __aiter__(self) -> _FastAPIChannel:
        return self

    async def __anext__(self) -> bytes:
        try:
            message = await self.recv()
        except WebSocketDisconnect:
            raise StopAsyncIteration
        retry = await ratelimit.check_ws_edit(self._conn_id)
        if retry is not None:
            await self._ws.close(
                code=CLOSE_RATE_LIMITED, reason="Editing too fast — slow down a moment."
            )
            raise StopAsyncIteration
        return message

    async def send(self, message: bytes) -> None:
        await self._ws.send_bytes(message)

    async def recv(self) -> bytes:
        return await self._ws.receive_bytes()


@router.websocket("/api/pads/{slug}/ws")
async def pad_ws(websocket: WebSocket, slug: str) -> None:
    try:
        slug = slug_service.validate_custom_slug(slug)
    except slug_service.SlugError:
        await websocket.close(code=CLOSE_BAD_SLUG)
        return

    token = websocket.query_params.get("token")
    unlock_token = websocket.cookies.get(pin_service.UNLOCK_COOKIE)
    auth = await authorize_ws(slug, token, unlock_token)
    if not auth.allowed:
        await websocket.close(code=auth.close_code or CLOSE_NO_ACCESS)
        return

    await websocket.accept()
    channel = _FastAPIChannel(websocket, slug, conn_id=str(uuid.uuid4()))
    try:
        await server.serve(channel)
    except WebSocketDisconnect:
        pass
