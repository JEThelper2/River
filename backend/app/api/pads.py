import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_user
from app.db.session import get_db
from app.models.pad import Pad, PinFormat, Visibility
from app.models.user import User
from app.schemas.pad import (
    ClaimIn,
    ClaimTokenOut,
    CollaboratorIn,
    CollaboratorOut,
    PadCreate,
    PadListItem,
    PadOut,
    PadPatch,
    PadUpdate,
    PinUnlockIn,
    RedirectOut,
)
from app.services import access as access_service
from app.services import claim as claim_service
from app.services import collaborator as collab_service
from app.services import pad as pad_service
from app.services import pin as pin_service
from app.services import ratelimit as ratelimit_service
from app.services import redirect as redirect_service
from app.services import slug as slug_service
from app.services import user as user_service

router = APIRouter(prefix="/api/pads", tags=["pads"])


async def _pad_out(
    db: AsyncSession, pad: Pad, user: User | None, unlock_token: str | None = None
) -> PadOut:
    out = PadOut.model_validate(pad)
    # The canonical address isn't secret — include it on every response (incl.
    # locked pads, rename, and claim) so the SPA can canonicalize the address bar
    # right after any operation (AUDIT B4 — no HTTP 301).
    out.canonical_url = await _canonical_url(db, pad)
    if not await pin_service.has_pin_access(db, pad, user, unlock_token):
        # Withhold content from a locked pad; the frontend renders the PIN screen.
        out.locked = True
        out.content = ""
        out.can_edit = False
        return out
    out.can_edit = await access_service.can_write_content(db, pad, user)
    return out


async def _canonical_url(db: AsyncSession, pad: Pad) -> str | None:
    """Browser-facing canonical address, or None when the address bar is already
    canonical (a never-renamed anonymous pad sits at ``/{slug}``). Used to
    canonicalize the SPA address bar client-side instead of 301-redirecting REST
    content fetches (AUDIT B4)."""
    return await redirect_service.canonical_url_for(db, pad)


def _require_owner(pad: Pad, user: User) -> None:
    if not access_service.is_owner(pad, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the pad owner can do that.",
        )


def _preview_text(content: str | None, limit: int = 140) -> str | None:
    if not content:
        return None
    text = re.sub(r"```[\s\S]*?```", " ", content)
    text = re.sub(r"[*_`>#-]+", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@router.get("", response_model=list[PadListItem])
async def list_my_pads(
    archived: bool = False,
    sort: str | None = None,
    locked: bool | None = None,
    owned: bool | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the current user's dashboard pads (owned and shared)."""
    rows = await pad_service.list_owned_pads(
        db, user.id, archived=archived, q=q, sort=sort, locked=locked, owned=owned
    )
    out: list[PadListItem] = []
    for r in rows:
        pad = r["pad"]
        out.append(
            PadListItem(
                id=pad.id,
                slug=pad.slug,
                name=pad.name,
                visibility=pad.visibility,
                is_archived=pad.is_archived,
                pin_protected=pad.pin_protected,
                last_opened_at=pad.last_opened_at,
                created_at=pad.created_at,
                updated_at=pad.updated_at,
                file_count=r["file_count"],
                size_bytes=r["size_bytes"],
                pinned=pad.pinned,
                color=pad.color,
                preview_text=_preview_text(pad.content),
            )
        )
    return out


@router.post("", response_model=PadOut, status_code=status.HTTP_201_CREATED)
async def create_pad(
    body: PadCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Create a pad. Empty body → anonymous pad with an auto-generated slug.

    Authenticated callers (dashboard "New Pad") own the pad at creation time, so
    no separate claim step is needed. Anonymous creation is rate-limited by IP.
    """
    if user is None:
        retry_after = await ratelimit_service.check_pad_creation(request)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="You're creating pads too quickly. Please wait a moment and try again.",
                headers={"Retry-After": str(retry_after)},
            )
    try:
        pad = await pad_service.create_pad(
            db,
            slug=body.slug,
            content=body.content,
            owner_id=user.id if user else None,
        )
    except slug_service.SlugError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    except pad_service.SlugTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That slug is already taken."
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc))
    return await _pad_out(db, pad, user)


@router.get("/{slug}")
async def get_pad(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Fetch a pad by slug. 404 with creatable flag if it's a valid-but-unused slug.

    Private pads are 403 to anyone who is not the owner or a collaborator. A
    PIN-protected pad returns 200 with ``locked: true`` and no content until the
    requester presents a valid unlock token (the existence is never hidden).

    The pad body is always returned directly (200), even for owned pads — the REST
    API never 301-redirects content fetches (AUDIT B4). When the pad is owned, the
    response carries a ``canonical_url`` (``/{username}/{padname}``) so the SPA can
    canonicalize the browser address bar itself; this keeps programmatic fetches
    (test client, WS auth, PIN unlock — whose cookie is path-scoped to this slug)
    working without a redirect.
    """
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        # An anonymous pad addressed by its *current* custom name (renamed but
        # still unclaimed) — the bare route resolves names in the anon pool too.
        pad = (
            await db.execute(
                select(Pad).where(Pad.owner_id.is_(None), Pad.name == slug)
            )
        ).scalar_one_or_none()
    if pad is None:
        # A historical anonymous name (the pad was renamed, or claimed) still
        # resolves to its pad — returned directly (200) with canonical_url, never
        # a 301 (AUDIT B4). The SPA then canonicalizes the address bar.
        pad = await redirect_service.resolve_redirect(
            db, slug, namespace=redirect_service.ANONYMOUS
        )
    if pad is None:
        creatable = True
        try:
            slug_service.validate_custom_slug(slug)
        except slug_service.SlugError:
            creatable = False
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "This pad doesn't exist yet.", "creatable": creatable},
        )

    if not await access_service.can_read(db, pad, user):
        # 403 (not 404): a private pad is "unlisted", its existence isn't secret.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This pad is private. Ask the owner to share it with you.",
        )

    await pad_service.touch_last_opened(db, pad)
    unlock_token = request.cookies.get(pin_service.UNLOCK_COOKIE)
    out = await _pad_out(db, pad, user, unlock_token)
    return out


@router.put("/{slug}", response_model=PadOut)
async def update_pad(
    slug: str,
    body: PadUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Content (body) save fallback. Honours the same write rules as the live WS."""
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    unlock_token = request.cookies.get(pin_service.UNLOCK_COOKIE)
    if not await pin_service.has_pin_access(db, pad, user, unlock_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This pad is locked. Enter its PIN before saving.",
        )
    if not await access_service.can_write_content(db, pad, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this pad.",
        )
    try:
        pad = await pad_service.update_pad_content(db, pad, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc))
    return await _pad_out(db, pad, user, unlock_token)


@router.patch("/{slug}", response_model=PadOut)
async def patch_pad(
    slug: str,
    body: PadPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Metadata update: rename, visibility, archive/unarchive, PIN.

    Authorization splits on ownership: an *owned* pad is owner-only; an
    *anonymous* (unclaimed) pad is world-editable — any viewer may rename it or
    set a PIN — consistent with anonymous pads already being publicly editable
    and "first valid claim wins". A locked anonymous pad still requires the PIN
    (a valid unlock token) before it can be changed, so a creator can protect a
    pad by PIN-locking it first.
    """
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    if pad.owner_id is not None:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
            )
        _require_owner(pad, user)
    else:
        # Anonymous & unclaimed → world-editable, but gated by the PIN if locked.
        unlock_token = request.cookies.get(pin_service.UNLOCK_COOKIE)
        if not await pin_service.has_pin_access(db, pad, user, unlock_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This pad is locked. Enter its PIN before changing it.",
            )
    fields = body.model_dump(exclude_unset=True)

    # PRD §5.6: a pad can only be made private by a verified account (so an
    # anonymous actor, who has no account, can never set private).
    if body.visibility is Visibility.private and (user is None or not user.email_verified):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify your email address before making a pad private.",
        )

    # PIN protection is orthogonal to visibility but mutually exclusive with
    # `private` (private already requires an account — a strictly stronger gate).
    effective_visibility = fields.get("visibility", pad.visibility)
    effective_pin = fields.get("pin_protected", pad.pin_protected)
    if effective_visibility is Visibility.private and effective_pin:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A private pad can't also be PIN-protected — private is a stronger gate.",
        )

    # Rename is its own transactional, namespaced, collision-checked operation.
    if "name" in fields:
        try:
            pad = await pad_service.rename_pad(db, pad, fields["name"])
        except slug_service.SlugError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            )
        except pad_service.NameTakenError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That name is taken. Try another.",
            )

    pad = await pad_service.update_pad_metadata(
        db,
        pad,
        visibility=fields.get("visibility", pad_service._UNSET),
        is_archived=fields.get("is_archived", pad_service._UNSET),
        pinned=fields.get("pinned", pad_service._UNSET),
        color=fields.get("color", pad_service._UNSET),
    )

    if "pin_protected" in fields:
        if body.pin_protected:
            if not body.pin:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="A PIN is required to enable PIN protection.",
                )
            pin_format = body.pin_format or PinFormat.numeric
            try:
                pad = await pin_service.set_pin(
                    db, pad, pin=body.pin, pin_format=pin_format
                )
            except pin_service.InvalidPinError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
                )
        else:
            pad = await pin_service.clear_pin(db, pad)

    return await _pad_out(db, pad, user)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pad(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Owner-only hard delete (cascades to files + collaborators + storage)."""
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    await pad_service.delete_pad(db, pad)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/claim-token", response_model=ClaimTokenOut)
async def create_claim_token(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Generate a time-bound claim token for an unclaimed pad.

    Available to any current viewer of an unclaimed pad (no auth required) —
    consistent with anonymous pads being world-editable; the token is harmless on
    its own. A PIN-protected pad still can't be claimed without the PIN (enforced
    at submission), and an already-owned pad can't be claimed at all (409).
    """
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    if pad.owner_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This pad has already been claimed.",
        )
    token, expires_at = await claim_service.generate_token(db, pad)
    return ClaimTokenOut(token=token, expires_at=expires_at)


@router.post("/{slug}/claim", response_model=PadOut)
async def claim_pad(
    slug: str,
    body: ClaimIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Claim an anonymous pad with a token (submitted from the dashboard).

    ``slug`` is the segment the SPA parsed from the pasted pad URL (a slug or the
    pad's current anonymous name). Requires a valid claim token; for a locked pad,
    also the PIN (rate-limited, generic error so no token-validity oracle leaks).
    The PIN persists through the claim (option (b)).
    """
    pad = await pad_service.get_anonymous_pad_by_segment(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    if pad.owner_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This pad has already been claimed.",
        )

    if pad.pin_protected:
        retry_after = await ratelimit_service.check_pin_attempt(str(pad.id), request)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please wait before trying again.",
                headers={"Retry-After": str(retry_after)},
            )

    try:
        pad = await claim_service.claim_with_token(
            db, pad, token=body.token, owner_id=user.id, pin=body.pin
        )
    except claim_service.PadAlreadyOwnedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)
    except claim_service.InvalidClaimError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message
        )
    return await _pad_out(db, pad, user)


@router.get("/{slug}/redirects", response_model=list[RedirectOut])
async def list_pad_redirects(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Owner-only: list the active historical links pointing at a pad."""
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    return [RedirectOut.model_validate(r) for r in await redirect_service.list_for_pad(db, pad.id)]


@router.delete("/{slug}/redirects/{redirect_id}", status_code=status.HTTP_204_NO_CONTENT)
async def kill_pad_redirect(
    slug: str,
    redirect_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Owner-only: kill one historical link (frees the name for reuse)."""
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    killed = await redirect_service.kill_redirect(
        db, redirect_id=redirect_id, pad_id=pad.id
    )
    if not killed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Redirect not found."
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{slug}/collaborators", response_model=list[CollaboratorOut])
async def list_collaborators(
    slug: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    rows = await collab_service.list_collaborators(db, pad.id)
    return [
        CollaboratorOut(
            user_id=c.user_id,
            email=u.email,

            role=c.role,
            invited_at=c.invited_at,
            accepted_at=c.accepted_at,
        )
        for c, u in rows
    ]


@router.post(
    "/{slug}/collaborators",
    response_model=CollaboratorOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_collaborator(
    slug: str,
    body: CollaboratorIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    try:
        collab, invited = await collab_service.add_collaborator(
            db, pad_id=pad.id, owner_id=pad.owner_id, email=body.email, role=body.role
        )
    except collab_service.NoSuchUserError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No account exists for that email. Inviting new users isn't supported yet.",
        )
    except collab_service.CannotInviteOwnerError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You already own this pad.",
        )
    return CollaboratorOut(
        user_id=collab.user_id,
        email=invited.email,

        role=collab.role,
        invited_at=collab.invited_at,
        accepted_at=collab.accepted_at,
    )


@router.delete(
    "/{slug}/collaborators/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_collaborator(
    slug: str,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    _require_owner(pad, user)
    removed = await collab_service.remove_collaborator(
        db, pad_id=pad.id, user_id=user_id
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Collaborator not found."
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{slug}/unlock", response_model=PadOut)
async def unlock_pad(
    slug: str,
    body: PinUnlockIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Submit a PIN to unlock a PIN-protected pad for the session window.

    Strictly rate-limited per pad per IP (brute-force defense). A wrong PIN
    (401) is a distinct error from being rate-limited (429) so the frontend can
    render each correctly.
    """
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    if not pad.pin_protected:
        # Nothing to unlock — return the pad as normal.
        return await _pad_out(db, pad, user)

    retry_after = await ratelimit_service.check_pin_attempt(str(pad.id), request)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Please wait before trying again.",
            headers={"Retry-After": str(retry_after)},
        )

    if not pin_service.verify_pin(pad, body.pin):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect PIN."
        )

    token, _expires = await pin_service.create_unlock(db, pad)
    response.set_cookie(
        key=pin_service.UNLOCK_COOKIE,
        value=token,
        max_age=pin_service.settings.pin_unlock_window_seconds,
        httponly=True,
        secure=pin_service.settings.cookies_secure,
        samesite="lax",
        path=pin_service.cookie_path(slug),
    )
    return await _pad_out(db, pad, user, token)


@router.get("/{slug}/raw")
async def get_pad_raw(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Raw markdown/text export — no editor chrome, suitable for curl (PRD §4)."""
    pad = await pad_service.get_pad_by_slug(db, slug)
    if pad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pad not found.")
    if not await access_service.can_read(db, pad, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This pad is private."
        )
    unlock_token = request.cookies.get(pin_service.UNLOCK_COOKIE)
    if not await pin_service.has_pin_access(db, pad, user, unlock_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This pad is locked. Enter its PIN to view it.",
        )
    return Response(content=pad.content, media_type="text/plain; charset=utf-8")


# NOTE: declared LAST, and namespaced under `/u/` rather than the bare
# `/{username}/{padname}`, on purpose (AUDIT B3). A two-path-param route at
# `/api/pads/{username}/{padname}` shadows the literal sub-routes above
# (`/{slug}/raw`, `/{slug}/collaborators`, `/{slug}/unlock`, `/{slug}/files`, …)
# because Starlette matches in declaration order. Pure reordering helps but still
# leaves a real ambiguity: `raw`/`new` are reserved slugs, but `collaborators`,
# `files`, `unlock`, and `claim` are NOT — a pad slug/name could legitimately be
# one of those, and the literal route would then swallow `/{username}/<that>`.
# The `/u/` prefix removes the ambiguity entirely. The browser-facing URL scheme
# (`/{username}/{padname}`) is unaffected — that is served by the SPA, not this
# REST route. See DECISIONS.md.
@router.get("/u/{username}/{padname}")
async def get_owned_pad(
    username: str,
    padname: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Fetch an owned pad by username + padname (slug or custom name).

    Like ``GET /{slug}``, this returns the pad body directly (200) — never a 301
    (AUDIT B4). When the pad is reached via a *previous* name (it was renamed),
    the response still returns the current content and carries ``canonical_url``
    pointing at the current name, so the SPA can update the address bar itself.
    """
    owner = await user_service.get_by_username(db, username)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "User not found.", "creatable": False},
        )

    result = await db.execute(
        select(Pad).where(
            (Pad.owner_id == owner.id)
            & ((Pad.slug == padname) | (Pad.name == padname))
        )
    )
    pad = result.scalar_one_or_none()
    if pad is None:
        # Fall back to a previous name (renamed pad) via the redirects table —
        # scoped to this owner's namespace — and serve directly (200 + canonical).
        pad = await redirect_service.resolve_redirect(
            db, padname, namespace=redirect_service.CLAIMED, namespace_owner=owner.id
        )
    if pad is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "This pad doesn't exist yet.", "creatable": False},
        )

    if not await access_service.can_read(db, pad, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This pad is private. Ask the owner to share it with you.",
        )
    await pad_service.touch_last_opened(db, pad)
    unlock_token = request.cookies.get(pin_service.UNLOCK_COOKIE)
    out = await _pad_out(db, pad, user, unlock_token)
    return out
