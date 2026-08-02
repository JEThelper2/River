"""File upload orchestration: cap enforcement, storage, scanning. Phase 3."""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.file import File, ScanStatus
from app.models.pad import Pad
from app.models.user import User
from app.services import scan as scan_service
from app.services import storage

settings = get_settings()

logger = logging.getLogger("spacepad.file")

_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str) -> str:
    filename = filename.strip().replace("\\", "_").replace("/", "_")
    filename = _FILENAME_RE.sub("_", filename)
    filename = filename.strip("._- ")
    if not filename:
        filename = "file"
    return filename[:255]

# Stream uploads in chunks so an oversized body is rejected at the cutoff instead
# of being fully buffered in memory first (AUDIT M1).
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


class UploadCapError(Exception):
    """Raised when an upload would exceed a pad's quota."""


def _caps(user: User | None) -> tuple[int, int, int]:
    """Per-tier upload caps. Authenticated owners get the higher auth-tier limits;
    anonymous uploads get the stricter anon-tier limits (AUDIT M2 — these
    auth-tier settings were previously dead config)."""
    if user is not None:
        return (
            settings.auth_max_files_per_pad,
            settings.auth_max_file_bytes,
            settings.auth_max_total_bytes,
        )
    return (
        settings.anon_max_files_per_pad,
        settings.anon_max_file_bytes,
        settings.anon_max_total_bytes,
    )


async def list_files(db: AsyncSession, pad: Pad) -> list[File]:
    result = await db.execute(
        select(File).where(File.pad_id == pad.id).order_by(File.created_at)
    )
    return list(result.scalars().all())


async def get_file(db: AsyncSession, pad: Pad, file_id: uuid.UUID) -> File | None:
    result = await db.execute(
        select(File).where(File.id == file_id, File.pad_id == pad.id)
    )
    return result.scalar_one_or_none()


async def _current_usage(db: AsyncSession, pad: Pad) -> tuple[int, int]:
    """(file count, total bytes) already stored against this pad."""
    agg = await db.execute(
        select(func.count(File.id), func.coalesce(func.sum(File.size_bytes), 0)).where(
            File.pad_id == pad.id
        )
    )
    count, total = agg.one()
    return int(count), int(total)


async def _read_capped(
    upload: UploadFile, *, per_file_cap: int, remaining_total: int
) -> bytes:
    """Stream the upload in chunks, aborting the moment it would breach either the
    per-file cap or the pad's remaining total budget — so an oversized body is
    rejected at the cutoff instead of being fully buffered first (AUDIT M1)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > per_file_cap:
            raise UploadCapError(
                f"File exceeds the {per_file_cap} byte per-file limit."
            )
        if size > remaining_total:
            raise UploadCapError(
                "Upload would exceed the total storage limit for this pad."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def create_file(
    db: AsyncSession,
    pad: Pad,
    *,
    filename: str,
    content_type: str,
    upload: UploadFile,
    user: User | None,
) -> File:
    """Validate caps, store the bytes, scan, and persist the resulting status.

    Caps are tier-dependent: authenticated owners get the higher auth-tier limits,
    anonymous uploads the stricter anon-tier ones (AUDIT M2). The body is streamed
    with an early cutoff rather than fully buffered before the size check (M1).

    If the scan does not return clean, the object is deleted from storage and the
    row is kept with ``failed`` status so the file is never served.
    """
    max_files, max_file_bytes, max_total_bytes = _caps(user)
    await db.execute(select(Pad).where(Pad.id == pad.id).with_for_update())
    count, total = await _current_usage(db, pad)
    if count + 1 > max_files:
        raise UploadCapError(f"This pad already has the maximum of {max_files} files.")

    data = await _read_capped(
        upload, per_file_cap=max_file_bytes, remaining_total=max_total_bytes - total
    )

    storage_key = f"{pad.id}/{uuid.uuid4()}"
    await storage.put_object(storage_key, data, content_type)

    filename = sanitize_filename(filename or "file")
    file = File(
        pad_id=pad.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=storage_key,
        scan_status=ScanStatus.pending,
    )
    db.add(file)
    await db.commit()
    await db.refresh(file)

    status = await scan_service.scan(data)
    file.scan_status = status
    if status is not ScanStatus.clean:
        await storage.delete_object(storage_key)
    await db.commit()
    await db.refresh(file)
    return file


async def delete_file(db: AsyncSession, file: File) -> None:
    # Best-effort storage removal (object may already be gone if the scan failed).
    try:
        await storage.delete_object(file.storage_key)
    except storage.StorageError as exc:
        # Best-effort: object may already be gone or storage hiccup.
        logger.warning("delete_file: storage delete failed for %s: %s", file.storage_key, exc)
    await db.delete(file)
    await db.commit()
