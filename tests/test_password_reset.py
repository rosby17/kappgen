import uuid

from fastapi.testclient import TestClient

from src.api.app import app
from src.api.routes import auth
from src.db.models import PasswordReset, User
from src.db.session import SessionLocal, init_db


client = TestClient(app)


class _BrevoResponse:
    status_code = 201


def test_password_reset_requires_email_code(monkeypatch):
    init_db()
    email = f"reset-{uuid.uuid4().hex}@example.com"
    old_password = "OldPassword123!"
    new_password = "NewPassword456!"
    sent_payload = {}

    monkeypatch.setattr(auth, "BREVO_API_KEY", "test-key")
    monkeypatch.setattr(auth, "BREVO_SENDER_EMAIL", "security@nichecut.test")
    monkeypatch.setattr(auth.secrets, "randbelow", lambda _limit: 123456)

    def fake_brevo_post(*_args, **kwargs):
        sent_payload.update(kwargs["json"])
        return _BrevoResponse()

    monkeypatch.setattr(auth.httpx, "post", fake_brevo_post)

    register = client.post("/api/auth/register", json={"email": email, "password": old_password})
    assert register.status_code == 201

    request_code = client.post("/api/auth/forgot-password", json={"email": email})
    assert request_code.status_code == 200
    assert "123456" in sent_payload["textContent"]

    wrong_code = client.post("/api/auth/reset-password", json={
        "email": email,
        "code": "000000",
        "new_password": new_password,
    })
    assert wrong_code.status_code == 400

    valid_code = client.post("/api/auth/reset-password", json={
        "email": email,
        "code": "123456",
        "new_password": new_password,
    })
    assert valid_code.status_code == 200
    assert client.post("/api/auth/login", json={"email": email, "password": old_password}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": new_password}).status_code == 200

    reused_code = client.post("/api/auth/reset-password", json={
        "email": email,
        "code": "123456",
        "new_password": "AnotherPassword789!",
    })
    assert reused_code.status_code == 400

    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    if user:
        db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
        db.delete(user)
        db.commit()
    db.close()


def test_forgot_password_does_not_reveal_unknown_email(monkeypatch):
    monkeypatch.setattr(auth, "BREVO_API_KEY", "test-key")
    monkeypatch.setattr(auth, "BREVO_SENDER_EMAIL", "security@nichecut.test")
    response = client.post("/api/auth/forgot-password", json={
        "email": f"missing-{uuid.uuid4().hex}@example.com",
    })
    assert response.status_code == 200
    assert "Si un compte correspond" in response.json()["message"]
