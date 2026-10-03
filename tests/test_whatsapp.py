"""Screenshots sent on WhatsApp (Meta Cloud API webhook)."""
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.webhooks import whatsapp as wa
from config.settings import settings
from models.database import SearchJob
from search import jobs


@pytest.fixture
def whatsapp_on(monkeypatch, redis, session_factory):
    import notifications.whatsapp as sender

    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setattr(wa, "get_redis", lambda: redis)
    monkeypatch.setattr(wa, "SessionLocal", session_factory)
    replies, started = [], []

    async def fake_text(to, text):
        from notifications.base import DeliveryResult

        replies.append((to, text))
        return DeliveryResult(True)

    async def fake_download(media_id, max_bytes):
        return b"image-bytes"

    monkeypatch.setattr(sender, "send_whatsapp_text", fake_text)
    monkeypatch.setattr(sender, "download_whatsapp_media", fake_download)
    monkeypatch.setattr(jobs, "start_job", lambda job_id: started.append(job_id))
    return replies, started


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def image_message(sender="919876543210", msg_id="wamid.1") -> dict:
    return {"id": msg_id, "from": sender, "type": "image", "image": {"id": "media-1", "mime_type": "image/jpeg"}}


async def test_meta_verification(client, whatsapp_on):
    r = await client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                      "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    r = await client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong"})
    assert r.status_code == 403


async def test_rejects_unsigned_deliveries(client, whatsapp_on):
    r = await client.post("/webhook/whatsapp", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 403
    body, headers = signed({"entry": []})
    assert (await client.post("/webhook/whatsapp", content=body, headers=headers)).status_code == 200


async def test_photo_from_a_lawyer_starts_a_search(db, user, whatsapp_on):
    replies, started = whatsapp_on
    await wa.handle_message(image_message())
    job = await db.scalar(select(SearchJob))
    assert job.kind == "screenshot" and job.params["whatsapp_to"] == "919876543210" and started == [job.id]
    assert "added to your list automatically" in replies[-1][1]
    # Meta re-delivers the same message: ignored
    await wa.handle_message(image_message())
    assert len((await db.scalars(select(SearchJob))).all()) == 1


async def test_unknown_number_is_told_to_sign_up(db, whatsapp_on):
    replies, started = whatsapp_on
    await wa.handle_message(image_message(sender="919000000000", msg_id="wamid.2"))
    assert "on CourtPilot yet" in replies[-1][1] and not started


async def test_text_message_gets_help(db, user, whatsapp_on):
    replies, started = whatsapp_on
    await wa.handle_message({"id": "wamid.3", "from": "919876543210", "type": "text", "text": {"body": "hi"}})
    assert "screenshot or photo" in replies[-1][1] and not started


async def test_disabled_without_settings(client):
    assert (await client.post("/webhook/whatsapp", content=b"{}")).status_code == 404
