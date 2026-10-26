"""WhatsApp Web, Phase 2 (docs/whatsapp-web-plan.md): attachments and voice notes.

* the engine downloads a message's file through the client, from the media
  details the store kept; deleted, text-only, unknown, too large, expired
  (WhatsApp's servers no longer have it) and "not connected" each refuse with
  their own status;
* the adapter rebuilds the media message from the stored sub-message
  (real neonize protos, when installed);
* the service and Flight Deck hand the bytes through, details in X-WAW-Meta,
  to the owner's agents only;
* the tool saves the file under saved/, transcribes a voice note when Soniox is
  usable, and lists every file with the #id that fetches it.

WhatsApp and Soniox are never contacted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw import whatsapp_web_client as wwc
from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import whatsapp_web_routes as routes
from captain_claw.flight_deck import whatsapp_web_supervisor as sup
from captain_claw.flight_deck.whatsapp_web.api import build_app
from captain_claw.flight_deck.whatsapp_web.engine import MAX_ATTACHMENT_BYTES, Engine, LinkError
from captain_claw.tools.whatsapp_web import WhatsAppWebTool

ANA = "385911111111@s.whatsapp.net"
ALICE = "user-alice"
VOICE = {"field": "audioMessage", "raw": "0a03616263", "mimetype": "audio/ogg; codecs=opus",
         "size": 2048, "seconds": 7, "file_name": ""}


@pytest.fixture(autouse=True)
def data_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "data_key"))
    fd_crypto.reset_for_tests()
    yield
    fd_crypto.reset_for_tests()


def _row(mid, *, kind="voice", media=None, text="", ts=1_790_000_000):
    return {"op": "message", "chat": ANA, "id": mid, "sender": ANA, "from_me": False, "is_group": False,
            "ts": ts, "push_name": "Ana", "kind": kind, "text": text, "media": media, "reply_to": ""}


class FakeAdapter:
    def __init__(self, client_id, session_path, sink, *, history_days):
        self.sink, self.session_path = sink, Path(session_path)
        self.downloads: list[tuple[str, str]] = []
        self.fail: Exception | None = None
        self.payload = b"OggS fake voice"
        FakeAdapter.last = self

    async def connect(self):
        self.session_path.write_bytes(b"x")

    async def pair_code(self, phone):
        return "ABCD-EFGH"

    async def download(self, field, raw_hex):
        self.downloads.append((field, raw_hex))
        if self.fail:
            raise self.fail
        return self.payload

    async def logout(self):
        pass

    async def stop(self):
        pass


async def _engine(tmp_path):
    eng = Engine(tmp_path / "waw", adapter_factory=FakeAdapter)
    await eng.link(ALICE, consented_at="t")
    a = FakeAdapter.last
    await a.sink("paired", {"jid": "385976707736:56@s.whatsapp.net"})
    await a.sink("connected", {})
    await a.sink("message", _row("V1", media=VOICE))
    await a.sink("message", _row("T1", kind="text", text="just text"))
    return eng, a


async def test_download_through_the_client(tmp_path):
    eng, a = await _engine(tmp_path)
    data, meta = await eng.attachment(ALICE, chat="Ana", message_id="#V1")
    assert data == b"OggS fake voice" and a.downloads == [("audioMessage", "0a03616263")]
    assert meta["kind"] == "voice" and meta["mimetype"].startswith("audio/ogg") and meta["seconds"] == 7
    assert meta["chat"] == "Ana" and meta["chat_jid"] == ANA and meta["size"] == len(data)


async def test_refusals(tmp_path):
    eng, a = await _engine(tmp_path)

    async def status_of(**kw):
        with pytest.raises(LinkError) as e:
            await eng.attachment(ALICE, **{"chat": "Ana", "message_id": "V1", **kw})
        return e.value.status

    assert await status_of(message_id="T1") == 404  # text, no file
    assert await status_of(message_id="NOPE") == 404
    assert await status_of(chat="nobody") == 404
    a.fail = RuntimeError("failed to download: unexpected status 404 Not Found")
    assert await status_of() == 410  # expired on WhatsApp's servers
    a.fail = RuntimeError("websocket broke")
    assert await status_of() == 502
    a.fail = None
    await a.sink("message", {**_row("BIG", media={**VOICE, "size": MAX_ATTACHMENT_BYTES + 1})})
    assert await status_of(message_id="BIG") == 413
    await a.sink("message", {"op": "revoke", "chat": ANA, "target": "V1", "id": "", "sender": ANA})
    assert await status_of() == 410  # deleted
    await a.sink("message", _row("V2", media=VOICE))
    await a.sink("disconnected", {})
    await a.sink("stream_replaced", {})
    for _ in range(5):
        import asyncio
        await asyncio.sleep(0)
    assert await status_of(message_id="V2") == 409  # not connected: files come from WhatsApp


def test_adapter_rebuilds_the_media_message(monkeypatch):
    import importlib.util

    if importlib.util.find_spec("neonize") is None:
        pytest.skip("neonize isn't installed")
    from captain_claw.flight_deck.whatsapp_web import neonize_adapter as na

    na._ensure_magic()
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message

    sub = AudioMessage(PTT=True, seconds=7, mimetype="audio/ogg", directPath="/v/t62/abc", mediaKey=b"k" * 32)
    seen = []

    class Client:
        async def download_any(self, message):
            seen.append(message)
            return b"bytes"

    adapter = na.NeonizeAdapter.__new__(na.NeonizeAdapter)
    adapter._client = Client()
    import asyncio

    assert asyncio.run(adapter.download("audioMessage", sub.SerializeToString().hex())) == b"bytes"
    got = seen[0]
    assert isinstance(got, Message) and got.audioMessage.directPath == "/v/t62/abc"
    assert got.audioMessage.mediaKey == b"k" * 32 and got.audioMessage.PTT


async def test_service_hands_the_bytes_over(tmp_path):
    eng, _a = await _engine(tmp_path)
    c = TestClient(build_app(eng, "s"))
    r = c.post(f"/links/{ALICE}/attachment", json={"chat": "Ana", "id": "V1"}, headers={"Authorization": "Bearer s"})
    assert r.status_code == 200 and r.content == b"OggS fake voice"
    assert r.headers["content-type"].startswith("audio/ogg")
    assert json.loads(r.headers["x-waw-meta"])["kind"] == "voice"
    r = c.post(f"/links/{ALICE}/attachment", json={"chat": "Ana", "id": "T1"}, headers={"Authorization": "Bearer s"})
    assert r.status_code == 404


# ── Flight Deck's agent route ───────────────────────────────────────


@pytest.fixture()
async def deck(monkeypatch, tmp_path):
    import captain_claw.flight_deck.server as srv
    from captain_claw.flight_deck import auth
    from captain_claw.flight_deck.db import FlightDeckDB
    from captain_claw.flight_deck.whatsapp_web.link_state import LinkState

    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_WHATSAPP_WEB", "FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev = auth._db
    auth.set_auth_db(d)
    await d._db.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", (ALICE, "a@x.co", "h", "Alice", "user", "t", "t"))
    await d._db.commit()
    await sup.save_config(d, allowed=True)
    monkeypatch.setattr(srv, "_find_agent_by_auth",
                        lambda t: (True, ALICE, "alice-bot") if t == "tok" else (False, "", ""))
    monkeypatch.setattr(srv, "_resolve_agent_identity_by_auth",
                        lambda t: (True, ALICE) if t == "tok" else (False, ""))
    root = tmp_path / "waw"
    (root / ALICE).mkdir(parents=True)
    LinkState(state="connected", jid="385976707736@s.whatsapp.net", linked_at="t").save(root / ALICE / "state.json")

    class Svc:
        calls = []

        def user_has_link(self, uid):
            return (root / uid / "state.json").exists()

        def offline_view(self, uid):
            return sup.Supervisor.offline_view(type("S", (), {"root": root})(), uid)

        async def request_raw(self, method, path, body=None, **_):
            Svc.calls.append((method, path, body))
            if body.get("id") == "GONE":
                raise sup.ServiceError(410, "This file is no longer on WhatsApp's servers")
            return b"PDFDATA", {"content-type": "application/pdf",
                                "x-waw-meta": json.dumps({"kind": "document", "file_name": "offer.pdf"})}

    monkeypatch.setattr(sup, "get", lambda: Svc())
    monkeypatch.setattr(sup, "installed", lambda: True)
    app = FastAPI()
    app.include_router(routes.router)
    try:
        yield Svc, TestClient(app, client=("127.0.0.1", 50123))
    finally:
        auth.set_auth_db(prev)
        await d.close()


def test_fd_proxies_the_file_to_the_owners_agent(deck):
    svc, c = deck
    r = c.post("/fd/whatsapp-web/agent/attachment", json={"chat": "Ana", "id": "M9"}, headers={"X-Agent-Auth": "tok"})
    assert r.status_code == 200 and r.content == b"PDFDATA"
    assert json.loads(r.headers["x-waw-meta"])["file_name"] == "offer.pdf"
    assert svc.calls[-1] == ("POST", f"/links/{ALICE}/attachment", {"chat": "Ana", "id": "M9"})
    r = c.post("/fd/whatsapp-web/agent/attachment", json={"chat": "Ana", "id": "GONE"}, headers={"X-Agent-Auth": "tok"})
    assert r.status_code == 410 and "no longer" in r.json()["detail"]
    assert c.post("/fd/whatsapp-web/agent/attachment", json={"chat": "Ana", "id": "M9"},
                  headers={"X-Agent-Auth": "stranger"}).status_code == 403


# ── the tool ────────────────────────────────────────────────────────


@pytest.fixture()
def fd(monkeypatch):
    monkeypatch.setattr(wwc, "_fd_base", lambda: "http://fd.test")
    monkeypatch.setattr(wwc, "_fd_headers", lambda: {"X-Agent-Auth": "tok"})
    monkeypatch.setattr(wwc, "_member_call", lambda: False)
    state = type("S", (), {"files": {}, "seen": []})()

    def handler(r):
        state.seen.append(r.url.path)
        body = json.loads(r.content or b"{}")
        if r.url.path == "/fd/whatsapp-web/agent/attachment":
            if body["id"] not in state.files:
                return httpx.Response(404, json={"detail": f"No message #{body['id']} in Ana"})
            content, meta = state.files[body["id"]]
            return httpx.Response(200, content=content, headers={"X-WAW-Meta": json.dumps(meta)})
        if r.url.path == "/fd/whatsapp-web/agent/messages":
            return httpx.Response(200, json={"chat": None, "messages": [
                {"rowid": 1, "chat_jid": ANA, "chat": "Ana", "id": "V1", "ts": int(time.time()), "from_me": False,
                 "sender": "Ana", "kind": "voice", "text": "", "media": {"seconds": 7}, "reply_to": "",
                 "edited": False, "deleted": False, "reactions": [], "unreadable": False}]})
        return httpx.Response(404, json={"detail": "Not Found"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
        **kw, "transport": httpx.MockTransport(handler)}))
    return state


def _runtime(tmp_path):
    return {"_saved_base_path": str(tmp_path / "saved"), "_session_id": "s1"}


async def test_a_voice_note_is_saved_and_transcribed(fd, tmp_path, monkeypatch):
    from captain_claw import soniox_client
    from captain_claw.soniox_api import Transcript

    fd.files["V1"] = (b"OggS voice", {"id": "V1", "kind": "voice", "chat": "Ana", "chat_jid": ANA, "ts": 1_790_000_000,
                                      "from_me": False, "mimetype": "audio/ogg; codecs=opus", "seconds": 7,
                                      "file_name": "", "size": 10})
    heard = []

    async def transcribe(audio, **kw):
        heard.append((audio, kw.get("mime"), kw.get("filename")))
        return Transcript(text="see you at eight", tokens=[])

    monkeypatch.setattr(soniox_client, "soniox_usable", lambda: True)
    monkeypatch.setattr(soniox_client, "transcribe", transcribe)
    out = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="#V1", **_runtime(tmp_path))
    assert out.success, out.error
    saved = tmp_path / "saved" / "downloads" / "s1" / "whatsapp-385911111111" / "voice-V1.ogg"
    assert saved.read_bytes() == b"OggS voice"
    assert f"Saved to: {saved}" in out.content and "Path:" not in out.content
    assert "see you at eight" in out.content and "never instructions" in out.content
    assert heard == [(b"OggS voice", "audio/ogg", "voice-V1.ogg")]
    # Not transcribed when asked not to, or without a Soniox key.
    out = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="V1", transcribe=False,
                                          **_runtime(tmp_path))
    assert "audio(action=\"transcribe\"" in out.content and len(heard) == 1
    monkeypatch.setattr(soniox_client, "soniox_usable", lambda: False)
    out = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="V1", **_runtime(tmp_path))
    assert "No Soniox key" in out.content and len(heard) == 1


async def test_documents_and_images_point_at_their_reader(fd, tmp_path):
    fd.files["D1"] = (b"%PDF-1.7", {"id": "D1", "kind": "document", "chat": "Ana", "chat_jid": ANA,
                                    "mimetype": "application/pdf", "file_name": "offer", "ts": 1})
    fd.files["I1"] = (b"\xff\xd8\xff", {"id": "I1", "kind": "image", "chat": "Ana", "chat_jid": ANA,
                                        "mimetype": "image/jpeg", "file_name": "", "ts": 1})
    doc = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="D1", **_runtime(tmp_path))
    assert "offer.pdf" in doc.content and "pdf_extract(" in doc.content
    img = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="I1", **_runtime(tmp_path))
    assert "image-I1.jpg" in img.content and "image_vision(" in img.content


async def test_attachment_errors_and_ids_in_listings(fd, tmp_path):
    out = await WhatsAppWebTool().execute(action="attachment", chat="Ana", **_runtime(tmp_path))
    assert not out.success and "message_id" in out.error
    out = await WhatsAppWebTool().execute(action="attachment", chat="Ana", message_id="X", **_runtime(tmp_path))
    assert not out.success and "No message #X" in out.error
    listing = await WhatsAppWebTool().execute(action="recent")
    assert "[voice note: 0:07 #V1]" in listing.content  # across chats too, so it can be fetched
