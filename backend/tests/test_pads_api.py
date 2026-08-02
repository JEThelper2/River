async def test_create_anonymous_pad_autoslug(client):
    resp = await client.post("/api/pads", json={})
    assert resp.status_code == 201
    data = resp.json()
    assert data["slug"]
    assert data["owner_id"] is None
    assert data["is_anonymous"] is True
    assert data["visibility"] == "public_edit"
    assert data["content"] == ""


async def test_create_pad_custom_slug(client):
    resp = await client.post("/api/pads", json={"slug": "my-notes", "content": "hi"})
    assert resp.status_code == 201
    assert resp.json()["slug"] == "my-notes"


async def test_create_pad_invalid_slug(client):
    resp = await client.post("/api/pads", json={"slug": "ab"})
    assert resp.status_code == 422


async def test_create_pad_reserved_slug(client):
    resp = await client.post("/api/pads", json={"slug": "admin"})
    assert resp.status_code == 422


async def test_create_pad_duplicate_slug(client):
    await client.post("/api/pads", json={"slug": "dupe-pad"})
    resp = await client.post("/api/pads", json={"slug": "dupe-pad"})
    assert resp.status_code == 409


async def test_get_existing_pad(client):
    await client.post("/api/pads", json={"slug": "read-me", "content": "hello"})
    resp = await client.get("/api/pads/read-me")
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello"


async def test_get_missing_valid_slug_is_creatable(client):
    resp = await client.get("/api/pads/ghost-pad-01")
    assert resp.status_code == 404
    assert resp.json()["detail"]["creatable"] is True


async def test_get_missing_invalid_slug_not_creatable(client):
    resp = await client.get("/api/pads/ab")
    assert resp.status_code == 404
    assert resp.json()["detail"]["creatable"] is False


async def test_update_pad_content(client):
    await client.post("/api/pads", json={"slug": "edit-me"})
    resp = await client.put("/api/pads/edit-me", json={"content": "# Title\n\nbody"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "# Title\n\nbody"


async def test_create_pad_content_too_large(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_pad_content_chars", 10, raising=False)
    resp = await client.post("/api/pads", json={"slug": "huge-pad", "content": "x" * 11})
    assert resp.status_code == 413
    assert "characters or fewer" in resp.json()["detail"]


async def test_update_pad_content_too_large(client, monkeypatch):
    from app.core.config import get_settings

    await client.post("/api/pads", json={"slug": "edit-large"})
    monkeypatch.setattr(get_settings(), "max_pad_content_chars", 10, raising=False)
    resp = await client.put("/api/pads/edit-large", json={"content": "x" * 11})
    assert resp.status_code == 413
    assert "characters or fewer" in resp.json()["detail"]


async def test_lock_blocks_put_edit(client):
    # Anonymous locked pad should reject content PUT until unlocked.
    await client.post("/api/pads", json={"slug": "locked-put", "content": "secret"})
    await client.patch(
        "/api/pads/locked-put",
        json={"pin_protected": True, "pin": "1234", "pin_format": "numeric"},
    )

    resp = await client.put("/api/pads/locked-put", json={"content": "new"})
    assert resp.status_code == 403
    assert "locked" in resp.json()["detail"].lower()


async def test_raw_endpoint(client):
    await client.post("/api/pads", json={"slug": "raw-pad", "content": "raw text"})
    resp = await client.get("/api/pads/raw-pad/raw")
    assert resp.status_code == 200
    assert resp.text == "raw text"
    assert resp.headers["content-type"].startswith("text/plain")


async def test_create_pad_custom_slug_conflicts_with_anonymous_name(client):
    await client.post("/api/pads", json={"slug": "anon-existing"})
    await client.patch("/api/pads/anon-existing", json={"name": "shadowed"})

    signup = await client.post(
        "/api/auth/signup",
        json={"email": "owner@example.com", "password": "password123", "username": "owner"},
    )
    token = signup.json()["access_token"]

    resp = await client.post(
        "/api/pads",
        json={"slug": "shadowed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "taken" in resp.json()["detail"].lower()
