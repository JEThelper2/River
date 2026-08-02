"""Phase 5 collaborator + pad management endpoints."""

import uuid

from sqlalchemy import select


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


async def _verify_email(factory, user_id):
    from app.models.user import User

    async with factory() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        user.email_verified = True
        await db.commit()


# --- authenticated creation sets ownership ----------------------------------
async def test_authenticated_create_sets_owner(client):
    token, user_id = await _signup(client, "owner@example.com")
    resp = await client.post("/api/pads", json={"slug": "my-new-pad"}, headers=_auth(token))
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner_id"] == str(user_id)
    assert body["is_anonymous"] is False


# --- list / search / archived ------------------------------------------------
async def test_list_owned_pads_sorted_search_archived(client, session_factory):
    token, user_id = await _signup(client, "owner@example.com")
    await client.post("/api/pads", json={"slug": "alpha-notes"}, headers=_auth(token))
    await client.post("/api/pads", json={"slug": "beta-todo"}, headers=_auth(token))

    listing = await client.get("/api/pads", headers=_auth(token))
    assert listing.status_code == 200
    slugs = [p["slug"] for p in listing.json()]
    assert set(slugs) == {"alpha-notes", "beta-todo"}

    # search filters by slug substring
    search = await client.get("/api/pads?q=alpha", headers=_auth(token))
    assert [p["slug"] for p in search.json()] == ["alpha-notes"]

    # archive one, default view excludes it, ?archived=true shows it
    await client.patch(
        "/api/pads/beta-todo", json={"is_archived": True}, headers=_auth(token)
    )
    default_view = await client.get("/api/pads", headers=_auth(token))
    assert [p["slug"] for p in default_view.json()] == ["alpha-notes"]
    archived_view = await client.get("/api/pads?archived=true", headers=_auth(token))
    assert [p["slug"] for p in archived_view.json()] == ["beta-todo"]


async def test_list_requires_auth(client):
    assert (await client.get("/api/pads")).status_code == 401


# --- PATCH metadata: owner-only, rename, visibility gate ---------------------
async def test_patch_rename_owner_only(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    stranger_token, _ = await _signup(client, "stranger@example.com")
    await client.post("/api/pads", json={"slug": "rename-me"}, headers=_auth(owner_token))

    bad = await client.patch(
        "/api/pads/rename-me", json={"name": "hijack"}, headers=_auth(stranger_token)
    )
    assert bad.status_code == 403

    # The custom name is now the pad's canonical address, so it must satisfy the
    # same URL-safe slug rules (lowercase, hyphens, no spaces, reserved-excluded).
    ok = await client.patch(
        "/api/pads/rename-me", json={"name": "my-project"}, headers=_auth(owner_token)
    )
    assert ok.status_code == 200
    assert ok.json()["name"] == "my-project"


async def test_patch_set_pinned_color_persists(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    await client.post("/api/pads", json={"slug": "style-pad"}, headers=_auth(owner_token))

    resp = await client.patch(
        "/api/pads/style-pad",
        json={"pinned": True, "color": "lavender"},
        headers=_auth(owner_token),
    )
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True
    assert resp.json()["color"] == "lavender"

    listing = await client.get("/api/pads", headers=_auth(owner_token))
    assert listing.status_code == 200
    assert listing.json()[0]["pinned"] is True
    assert listing.json()[0]["color"] == "lavender"


async def test_patch_clear_custom_name(client):
    token, _ = await _signup(client, "cleanup@example.com")
    await client.post("/api/pads", json={"slug": "clear-name"}, headers=_auth(token))
    await client.patch(
        "/api/pads/clear-name",
        json={"name": "temporary-name"},
        headers=_auth(token),
    )

    resp = await client.patch(
        "/api/pads/clear-name",
        json={"name": None},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] is None
    assert resp.json()["canonical_url"] == "/cleanup/clear-name"


async def test_patch_private_requires_verified_email(client, session_factory):
    token, user_id = await _signup(client, "owner@example.com")
    await client.post("/api/pads", json={"slug": "wanna-be-private"}, headers=_auth(token))

    # unverified → blocked
    blocked = await client.patch(
        "/api/pads/wanna-be-private",
        json={"visibility": "private"},
        headers=_auth(token),
    )
    assert blocked.status_code == 403

    await _verify_email(session_factory, user_id)
    allowed = await client.patch(
        "/api/pads/wanna-be-private",
        json={"visibility": "private"},
        headers=_auth(token),
    )
    assert allowed.status_code == 200
    assert allowed.json()["visibility"] == "private"


# --- DELETE: owner-only, removes the pad -------------------------------------
async def test_delete_pad_owner_only(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    stranger_token, _ = await _signup(client, "stranger@example.com")
    await client.post("/api/pads", json={"slug": "delete-me"}, headers=_auth(owner_token))

    assert (
        await client.delete("/api/pads/delete-me", headers=_auth(stranger_token))
    ).status_code == 403
    assert (
        await client.delete("/api/pads/delete-me", headers=_auth(owner_token))
    ).status_code == 204
    # gone
    assert (await client.get("/api/pads/delete-me")).status_code == 404


# --- collaborators -----------------------------------------------------------
async def test_collaborator_add_list_remove(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    _, collab_id = await _signup(client, "collab@example.com")
    await client.post("/api/pads", json={"slug": "shared-pad"}, headers=_auth(owner_token))

    add = await client.post(
        "/api/pads/shared-pad/collaborators",
        json={"email": "collab@example.com", "role": "editor"},
        headers=_auth(owner_token),
    )
    assert add.status_code == 201
    assert add.json()["role"] == "editor"
    assert add.json()["user_id"] == str(collab_id)

    listing = await client.get(
        "/api/pads/shared-pad/collaborators", headers=_auth(owner_token)
    )
    assert [c["email"] for c in listing.json()] == ["collab@example.com"]

    remove = await client.delete(
        f"/api/pads/shared-pad/collaborators/{collab_id}", headers=_auth(owner_token)
    )
    assert remove.status_code == 204
    after = await client.get(
        "/api/pads/shared-pad/collaborators", headers=_auth(owner_token)
    )
    assert after.json() == []


async def test_collaborator_invite_unknown_email_422(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    await client.post("/api/pads", json={"slug": "lonely-pad"}, headers=_auth(owner_token))
    resp = await client.post(
        "/api/pads/lonely-pad/collaborators",
        json={"email": "nobody@example.com", "role": "editor"},
        headers=_auth(owner_token),
    )
    assert resp.status_code == 422


async def test_collaborator_endpoints_owner_only(client):
    owner_token, _ = await _signup(client, "owner@example.com")
    stranger_token, _ = await _signup(client, "stranger@example.com")
    await client.post("/api/pads", json={"slug": "guarded-pad"}, headers=_auth(owner_token))
    resp = await client.get(
        "/api/pads/guarded-pad/collaborators", headers=_auth(stranger_token)
    )
    assert resp.status_code == 403
