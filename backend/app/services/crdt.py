"""Phase 2 real-time collaboration: a y-websocket-compatible CRDT server.

One Yjs room per pad slug, hosted in-process via pycrdt-websocket. Each room's
``Doc`` holds a single ``Text`` named ``content``. Rooms are seeded from the DB
on first open (from ``crdt_snapshot`` if present, else from the Phase 1 plain
``content`` column) and flushed back on a debounce so the REST ``GET``/``/raw``
paths keep working unchanged.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from pycrdt import Doc, Text
from pycrdt.websocket import WebsocketServer, YRoom
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.pad import Pad

settings = get_settings()

CONTENT_KEY = "content"
_SAVE_DEBOUNCE_SECONDS = 1.0


def _doc_text(doc: Doc) -> str:
    return str(doc.get(CONTENT_KEY, type=Text))


class PadWebsocketServer(WebsocketServer):
    """WebsocketServer that seeds each room from the DB and persists changes.

    ``serve()`` routes by ``channel.path``; we use the pad slug as the room name,
    so the only thing we override is room creation: seed the Doc and attach a
    debounced flush observer before the room starts syncing to clients.
    """

    def __init__(self) -> None:
        super().__init__(auto_clean_rooms=True)
        self._save_tasks: dict[str, asyncio.Task] = {}
        self._subscriptions: dict[str, object] = {}

    async def get_room(self, name: str) -> YRoom:
        if name not in self.rooms:
            doc = await self._seed_doc(name)
            room = YRoom(ready=self.rooms_ready, ydoc=doc, log=self.log)
            self.rooms[name] = room
            self._subscriptions[name] = doc.observe(
                lambda _event, slug=name: self._schedule_save(slug)
            )
        await self.start_room(self.rooms[name])
        return self.rooms[name]

    async def delete_room(self, *, name: str | None = None, room: YRoom | None = None) -> None:
        resolved_name = name or (self.get_room_name(room) if room else None)
        if resolved_name is not None:
            await self._flush(resolved_name)
            self._subscriptions.pop(resolved_name, None)

        if room is not None:
            await super().delete_room(room=room)
        else:
            await super().delete_room(name=name)

    async def _seed_doc(self, slug: str) -> Doc:
        doc = Doc()
        async with SessionLocal() as db:
            pad = (
                await db.execute(select(Pad).where(Pad.slug == slug))
            ).scalar_one_or_none()
        if pad is None:
            return doc
        if pad.crdt_snapshot:
            doc.apply_update(pad.crdt_snapshot)
        elif pad.content:
            # First real-time open of a Phase 1 pad: seed the CRDT from plain text.
            doc.get(CONTENT_KEY, type=Text).insert(0, pad.content)
        return doc

    def _schedule_save(self, slug: str) -> None:
        existing = self._save_tasks.get(slug)
        if existing and not existing.done():
            existing.cancel()
        self._save_tasks[slug] = asyncio.create_task(self._debounced_save(slug))

    async def _debounced_save(self, slug: str) -> None:
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(_SAVE_DEBOUNCE_SECONDS)
            await self._flush(slug)

    async def _flush(self, slug: str) -> None:
        room = self.rooms.get(slug)
        if room is None:
            return
        snapshot = room.ydoc.get_update()
        text = _doc_text(room.ydoc)
        async with SessionLocal() as db:
            pad = (
                await db.execute(select(Pad).where(Pad.slug == slug))
            ).scalar_one_or_none()
            if pad is None:
                return
            if len(text) > settings.max_pad_content_chars:
                self.log.warning(
                    "CRDT flush skipped: pad content exceeds max %d chars",
                    settings.max_pad_content_chars,
                )
                return
            pad.crdt_snapshot = snapshot
            pad.crdt_snapshot_updated_at = func.now()
            pad.content = text
            await db.commit()
