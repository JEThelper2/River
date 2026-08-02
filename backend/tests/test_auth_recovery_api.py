"""Phase 7 password reset + email verification flows.

The email provider is the log stub in tests, so we capture the raw token by
reading the most recent EmailToken row's hash is not reversible — instead we
monkeypatch email.send_email to capture the link, which carries the raw token.
"""

import pytest
from sqlalchemy import select

from app.models.token import EmailToken
from app.services import email as email_service


@pytest.fixture
def captured_emails(monkeypatch):
    sent = []

    async def fake_send(*, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(email_service, "send_email", fake_send)
    return sent


def _token_from(body: str) -> str:
    # links look like ...?token=XXXX
    return body.split("token=")[1].split()[0].strip()


async def _signup(client, email="a@example.com", password="password123", username=None):
    if username is None:
        username = email.split("@")[0]
        # Ensure minimum length of 3 for username
        if len(username) < 3:
            username = username + "user"
    return await client.post(
        "/api/auth/signup", json={"email": email, "password": password, "username": username}
    )


# --- password reset ----------------------------------------------------------
async def test_password_reset_end_to_end(client, captured_emails):
    await _signup(client)
    req = await client.post(
        "/api/auth/password-reset/request", json={"email": "a@example.com"}
    )
    assert req.status_code == 202
    assert len(captured_emails) == 1
    token = _token_from(captured_emails[0]["body"])

    confirm = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "brandnewpass1"},
    )
    assert confirm.status_code == 200
    # can log in with the new password
    login = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "brandnewpass1"}
    )
    assert login.status_code == 200


async def test_password_reset_token_is_single_use(client, captured_emails):
    await _signup(client)
    await client.post("/api/auth/password-reset/request", json={"email": "a@example.com"})
    token = _token_from(captured_emails[0]["body"])
    first = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "brandnewpass1"},
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "anotherpass2"},
    )
    assert second.status_code == 400


async def test_password_reset_unknown_email_still_202(client, captured_emails):
    # No account → still 202, and no email sent (no existence oracle).
    resp = await client.post(
        "/api/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    assert resp.status_code == 202
    assert captured_emails == []


async def test_password_reset_bad_token_400(client):
    resp = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": "not-a-real-token", "new_password": "whateverpass1"},
    )
    assert resp.status_code == 400


async def test_password_reset_expired_token(client, captured_emails, session_factory):
    import datetime as dt

    await _signup(client)
    await client.post("/api/auth/password-reset/request", json={"email": "a@example.com"})
    # force-expire the token
    async with session_factory() as db:
        row = (await db.execute(select(EmailToken))).scalar_one()
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        await db.commit()
    token = _token_from(captured_emails[0]["body"])
    resp = await client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "brandnewpass1"},
    )
    assert resp.status_code == 400


# --- email verification ------------------------------------------------------
async def test_email_verification_flow(client, captured_emails):
    token_resp = await _signup(client)
    access = token_resp.json()["access_token"]
    assert token_resp.json()["user"]["email_verified"] is False

    req = await client.post(
        "/api/auth/verify-email/request",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert req.status_code == 202
    raw = _token_from(captured_emails[0]["body"])

    confirm = await client.post("/api/auth/verify-email/confirm", json={"token": raw})
    assert confirm.status_code == 200
    assert confirm.json()["email_verified"] is True
