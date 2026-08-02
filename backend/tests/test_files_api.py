from app.models.file import ScanStatus


async def _make_pad(client, slug="file-pad"):
    resp = await client.post("/api/pads", json={"slug": slug})
    assert resp.status_code == 201
    return slug


async def test_upload_clean_then_download(client, fake_storage_and_scan):
    slug = await _make_pad(client)
    resp = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["scan_status"] == "clean"
    assert body["size_bytes"] == 11

    dl = await client.get(f"/api/pads/{slug}/files/{body['id']}")
    assert dl.status_code == 200
    assert dl.content == b"hello world"


async def test_failed_scan_is_not_served(client, fake_storage_and_scan):
    fake_storage_and_scan["verdict"]["status"] = ScanStatus.failed
    slug = await _make_pad(client, "infected-pad")
    resp = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("bad.exe", b"malware", "application/octet-stream")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["scan_status"] == "failed"
    # Object was removed from storage and is never served.
    assert fake_storage_and_scan["store"] == {}

    dl = await client.get(f"/api/pads/{slug}/files/{body['id']}")
    assert dl.status_code == 409


async def test_per_file_cap(client, fake_storage_and_scan, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "anon_max_file_bytes", 5, raising=False)
    slug = await _make_pad(client, "cap-pad")
    resp = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("big.txt", b"way too many bytes", "text/plain")},
    )
    assert resp.status_code == 413


async def test_file_count_cap(client, fake_storage_and_scan, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "anon_max_files_per_pad", 1, raising=False)
    slug = await _make_pad(client, "count-pad")
    first = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("a.txt", b"a", "text/plain")},
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("b.txt", b"b", "text/plain")},
    )
    assert second.status_code == 413


async def test_list_and_delete(client, fake_storage_and_scan):
    slug = await _make_pad(client, "list-pad")
    up = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("x.txt", b"xyz", "text/plain")},
    )
    fid = up.json()["id"]

    listed = await client.get(f"/api/pads/{slug}/files")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/api/pads/{slug}/files/{fid}")
    assert deleted.status_code == 204

    listed2 = await client.get(f"/api/pads/{slug}/files")
    assert listed2.json() == []


async def test_upload_to_missing_pad(client, fake_storage_and_scan):
    resp = await client.post(
        "/api/pads/does-not-exist/files",
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 404


async def test_upload_sanitizes_filename(client, fake_storage_and_scan):
    slug = await _make_pad(client, "sanitize-pad")
    resp = await client.post(
        f"/api/pads/{slug}/files",
        files={"file": ("..\\evil/na?me.txt", b"ok", "text/plain")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["filename"] == "evil_na_me.txt"
