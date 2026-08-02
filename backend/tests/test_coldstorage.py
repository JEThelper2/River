"""Phase 6 cold-storage eligibility flagging."""

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select

from app.models.pad import Pad
from app.services import coldstorage


@pytest_asyncio.fixture
async def csdb(session_factory, monkeypatch):
    monkeypatch.setattr(coldstorage, "SessionLocal", session_factory)
    return session_factory


async def test_flags_only_stale_pads(csdb):
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    recent = now - timedelta(days=10)
    async with csdb() as db:
        db.add(Pad(slug="stale-pad-01", content="", last_opened_at=old))
        db.add(Pad(slug="fresh-pad-01", content="", last_opened_at=recent))
        await db.commit()

    flagged = await coldstorage.flag_cold_pads(now=now)
    assert flagged == 1

    async with csdb() as db:
        stale = (await db.execute(select(Pad).where(Pad.slug == "stale-pad-01"))).scalar_one()
        fresh = (await db.execute(select(Pad).where(Pad.slug == "fresh-pad-01"))).scalar_one()
        assert stale.cold_storage_eligible is True
        assert fresh.cold_storage_eligible is False


async def test_idempotent_no_double_flag(csdb):
    now = datetime.now(UTC)
    async with csdb() as db:
        db.add(Pad(slug="stale-pad-02", content="", last_opened_at=now - timedelta(days=400)))
        await db.commit()

    assert await coldstorage.flag_cold_pads(now=now) == 1
    # second sweep finds nothing new to flag
    assert await coldstorage.flag_cold_pads(now=now) == 0
