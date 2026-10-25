"""Agent links over HTTP, webhooks, and the agent's own messages (agent_link_http.py).

Pinned here:

* webhook addresses: public https only (http and private ones with
  FD_LINK_WEBHOOKS_PRIVATE), no credentials in them; the signature is
  HMAC-SHA256 of "<timestamp>.<body>"; a failed POST is tried again, then the
  event waits for /link/events;
* the event log: kept a week / the newest 500 per link, delivered the first
  way only, gone with its link;
* a message over HTTP runs like a socket's — on the socket when one is
  connected (its answer still goes to HTTP), else without one — and its
  answer is an event: returned when asked to wait, else posted to the
  webhook, else kept for /link/events; a repeated id gets the same event;
  its files are fetched from the agent when the system asks;
* the agent's own messages (/fd/agent-links/send, the linked_system tool):
  only to links bound to it that allow it, only its own files; on the socket
  when connected, else the webhook, else kept — and a socket that connects
  gets the ones it missed;
* the owner's settings: a webhook gets a signing secret shown once and kept
  sealed; a new address gets a new one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from captain_claw.flight_deck import agent_link as al
from captain_claw.flight_deck import agent_link_http as ah
from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import glasses_bridge as gb
from captain_claw.flight_deck import usage_quota
from captain_claw.flight_deck.auth import create_access_token, hash_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

KEY = "cc_link_test-key"


async def _settle(n: int = 40):
    for _ in range(n):
        await asyncio.sleep(0)


# ── pure parts ────────────────────────────────────────────────────────


def test_kinds_mimes_and_the_event_a_system_gets():
    assert [ah.kind_of(n) for n in ("a.PNG", "b.mp3", "c.pdf", "d")] == ["image", "audio", "file", "file"]
    assert ah.mime_of("n.md") == "text/markdown" and ah.mime_of("x.unknownext") == "application/octet-stream"
    assert ah.file_entry("f_1", "r.pdf", "file") == {
        "file": "f_1", "name": "r.pdf", "kind": "file", "mime": "application/pdf", "url": "/link/files/f_1"}
    event = {"seq": 7, "kind": "push", "re": "", "body": {"text": "Hi", "files": []}, "created_at": "t"}
    assert ah.public_event(event) == {"seq": 7, "type": "push", "re": None, "text": "Hi", "files": [],
                                      "created_at": "t"}


def test_the_signature_is_hmac_of_timestamp_and_body():
    expected = hmac.new(b"whsec_x", b"1700000000.{}", hashlib.sha256).hexdigest()
    assert ah.sign("whsec_x", "1700000000", "{}") == expected
    assert ah.new_webhook_secret().startswith("whsec_")


@pytest.mark.asyncio
async def test_webhooks_go_to_public_https_addresses_only(monkeypatch):
    monkeypatch.delenv("FD_LINK_WEBHOOKS_PRIVATE", raising=False)
    answers = {"hooks.example.com": "93.184.216.34", "internal.example.com": "10.0.0.7",
               "meta.example.com": "169.254.169.254", "me.example.com": "127.0.0.1"}

    async def _resolve(self, host, port, **kw):
        if host not in answers:
            raise OSError("nope")
        return [(2, 1, 6, "", (answers[host], port))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", _resolve)
    assert await ah.webhook_url_problem("https://hooks.example.com/in") == ""
    assert await ah.webhook_url_problem("http://hooks.example.com/in") == "Use an https:// address."
    assert await ah.webhook_url_problem("ftp://hooks.example.com") == "Use an https:// address."
    assert await ah.webhook_url_problem("https://u:p@hooks.example.com") == \
        "Leave the user name and password out of the address."
    for host in ("internal.example.com", "meta.example.com", "me.example.com"):
        assert "is a private address" in await ah.webhook_url_problem(f"https://{host}/x"), host
    assert await ah.webhook_url_problem("https://gone.example.com") == "gone.example.com can't be found."
    monkeypatch.setenv("FD_LINK_WEBHOOKS_PRIVATE", "1")
    assert await ah.webhook_url_problem("http://127.0.0.1:9000/hook") == ""


def test_only_its_own_links_that_allow_it():
    links = [{"id": "1", "agent_slug": "a", "agent_may_send": 1}, {"id": "2", "agent_slug": "", "agent_may_send": 1},
             {"id": "3", "agent_slug": "b", "agent_may_send": 1}, {"id": "4", "agent_slug": "a", "agent_may_send": 0}]
    assert [ln["id"] for ln in ah.reachable(links, "a")] == ["1", "2"]


# ── a real deck: the store, the bus, the routes ───────────────────────


class _AgentWS:
    def __init__(self):
        self.frames: list = []

    async def send(self, text):
        self.frames.append(json.loads(text))


class _WS:
    def __init__(self):
        self.sent: list = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        pass


class _AgentFiles:
    """The agent's file server as httpx sees it (installed after the test client)."""

    def __init__(self, monkeypatch, files: dict[str, bytes]):
        self.files = files
        self.asked: list[str] = []
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda *a, **kw: real(*a, transport=httpx.MockTransport(self.handle), **kw))

        async def _base(conn):
            return "http://agent:1", {"token": "t"}

        async def _endpoint(user_id, slug):
            return "http://agent:1", {"token": "t"}

        monkeypatch.setattr(al._LinkConn, "_agent_base", _base)
        monkeypatch.setattr(al, "_agent_endpoint", _endpoint)

    def handle(self, request):
        path = request.url.params.get("path", "")
        self.asked.append(path)
        blob = self.files.get(path)
        return httpx.Response(200, content=blob) if blob is not None else httpx.Response(403)


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch):
    import types

    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    await db._db.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
        " VALUES ('alice', 'a@x.co', 'x', 'Alice', 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    await db._db.commit()
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "data_key"))
    fd_crypto.reset_for_tests()
    for name in ("_turns", "_queues", "_seen", "_workers", "_live", "_headless", "_http_waits"):
        monkeypatch.setattr(al, name, {})
    monkeypatch.setattr(gb, "_channels", {})
    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: {"name": "Agent A", "slug": "a", "port": 1})
    agent = _AgentWS()

    async def _bind(ch, host, port):
        ch.agent_ws = agent
        ch.context_sent = True

    monkeypatch.setattr(gb, "_ensure_agent_binding", _bind)

    async def _admit(user_id, text):
        return True, None

    monkeypatch.setattr(usage_quota, "admit_message", _admit)
    from captain_claw.flight_deck import mcp_server_routes

    monkeypatch.setattr(mcp_server_routes, "_user_agents", lambda user_id: [{"slug": "a", "port": 1}])
    link = await db.create_agent_link("alice", name="Acme CRM", agent_slug="a", instructions="File leads.",
                                      key_hash=hash_token(KEY), key_hint="-key")
    cid = al._channel_id("alice", link["id"])
    app = FastAPI()
    app.include_router(al.router)
    app.include_router(ah.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fd.test")

    async def frame(data):
        await gb._forward_agent_msg(gb._channels[cid], data)
        await _settle()

    async def answer(text, media=None):
        await frame({"type": "chat_message", "role": "assistant", "content": text})
        if media:
            await frame({"type": "turn_media", "items": media})
        await frame({"type": "status", "status": "ready"})

    async def socket():
        conn = al._LinkConn(_WS(), {"id": "alice"}, dict(link))
        al._live[cid] = conn
        conn.take_over()
        assert await conn.bind("")
        return conn

    try:
        yield types.SimpleNamespace(db=db, link=link, cid=cid, client=client, agent=agent, frame=frame,
                                    answer=answer, socket=socket)
    finally:
        for task in list(al._workers.values()):
            task.cancel()
        await client.aclose()
        await db.close()
        fd_auth._db = prev
        fd_crypto.reset_for_tests()


H = {"Authorization": f"Bearer {KEY}"}


@pytest.mark.asyncio
async def test_the_event_log_keeps_a_week_and_the_newest_500(deck, monkeypatch):
    db, link = deck.db, deck.link
    monkeypatch.setattr(FlightDeckDB, "LINK_EVENTS_KEPT", 3)
    for n in range(5):
        await db.add_link_event(link["id"], "push", "", {"text": str(n)})
    events = await db.link_events_after(link["id"], 0)
    assert [e["body"]["text"] for e in events] == ["2", "3", "4"]
    await db._db.execute("UPDATE agent_link_events SET created_at = '2020-01-01T00:00:00+00:00' WHERE seq = ?",
                         (events[0]["seq"],))
    await db._db.commit()
    await db.add_link_event(link["id"], "push", "", {"text": "5"})
    assert [e["body"]["text"] for e in await db.link_events_after(link["id"], 0)] == ["3", "4", "5"]
    seq = events[1]["seq"]
    await db.mark_link_event(seq, "webhook")
    await db.mark_link_event(seq, "poll")  # the first way only
    assert (await db.link_events_after(link["id"], seq - 1, 1))[0]["delivered"] == "webhook"
    assert [e["body"]["text"] for e in await db.undelivered_link_events(link["id"], "push")] == ["4", "5"]
    file_id = await db.add_link_file(link["id"], agent_slug="a", path="/w/saved/x.pdf", name="x.pdf", kind="file")
    assert (await db.get_link_file(file_id))["path"] == "/w/saved/x.pdf"
    await db.delete_agent_link(link["id"], "alice")
    assert await db.link_events_after(link["id"], 0) == [] and await db.get_link_file(file_id) is None


@pytest.mark.asyncio
async def test_a_message_over_http_returns_its_answer_when_asked_to_wait(deck):
    post = asyncio.create_task(deck.client.post("/link/messages?wait=5", headers=H,
                                                json={"id": "lead-1", "text": "New lead", "data": {"name": "Ana"}}))
    for _ in range(100):
        if deck.agent.frames:
            break
        await asyncio.sleep(0.01)
    sent = deck.agent.frames[0]
    assert sent["content"].startswith("New lead\n\nData:") and sent["automation"]["kind"] == "link"
    await deck.answer("Filed Ana.")
    resp = await post
    assert resp.status_code == 200
    body = resp.json()
    assert (body["id"], body["status"]) == ("lead-1", "done")
    assert {k: body["event"][k] for k in ("type", "re", "text", "files")} == {
        "type": "reply", "re": "lead-1", "text": "Filed Ana.", "files": []}
    stored = await deck.db.link_event_for(deck.link["id"], "lead-1")
    assert stored["delivered"] == "response"
    again = await deck.client.post("/link/messages", headers=H, json={"id": "lead-1", "text": "New lead"})
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    assert again.json()["event"]["text"] == "Filed Ana." and len(deck.agent.frames) == 1


@pytest.mark.asyncio
async def test_without_waiting_the_answer_is_kept_for_polling(deck):
    resp = await deck.client.post("/link/messages", headers=H, json={"id": "q1", "text": "How many leads?"})
    assert resp.status_code == 202 and resp.json() == {"id": "q1", "status": "queued", "queued": 0}
    await _settle()
    await deck.frame({"type": "error", "message": "The model is down", "retryable": False})
    events = (await deck.client.get("/link/events", headers=H)).json()
    assert [(e["type"], e["re"], e["text"]) for e in events["events"]] == [("error", "q1", "The model is down")]
    nxt = events["next"]
    assert (await deck.client.get(f"/link/events?after={nxt}", headers=H)).json() == {"events": [], "next": nxt}
    assert (await deck.db.link_event_for(deck.link["id"], "q1"))["delivered"] == "poll"
    assert (await deck.client.get("/link/events")).status_code == 401


@pytest.mark.asyncio
async def test_an_http_message_on_a_connected_socket_still_answers_over_http(deck):
    conn = await deck.socket()
    resp = await deck.client.post("/link/messages", headers=H, json={"id": "h1", "text": "From HTTP"})
    assert resp.status_code == 202
    await _settle()
    await deck.answer("Answer for HTTP")
    assert not any(m.get("re") == "h1" for m in conn.ws.sent)  # nothing about it on the socket
    events = (await deck.client.get("/link/events", headers=H)).json()["events"]
    assert [(e["re"], e["text"]) for e in events] == [("h1", "Answer for HTTP")]
    await conn.submit("w1", "From the socket")
    await _settle()
    await deck.answer("Answer for the socket")
    assert {"type": "reply", "text": "Answer for the socket", "re": "w1"} in conn.ws.sent
    assert len((await deck.client.get("/link/events", headers=H)).json()["events"]) == 1


@pytest.mark.asyncio
async def test_an_answers_files_are_fetched_when_the_system_asks(deck, monkeypatch):
    files = _AgentFiles(monkeypatch, {"/w/saved/report.pdf": b"%PDF-1.7 report"})
    await deck.client.post("/link/messages", headers=H, json={"id": "r1", "text": "Report please"})
    await _settle()
    await deck.answer("Here's report.pdf.", media=[{"path": "/w/saved/report.pdf", "name": "report.pdf", "kind": "file"}])
    event = (await deck.client.get("/link/events", headers=H)).json()["events"][0]
    [entry] = event["files"]
    assert {k: entry[k] for k in ("name", "kind", "mime")} == {"name": "report.pdf", "kind": "file",
                                                                "mime": "application/pdf"}
    assert files.asked == []  # nothing fetched until asked
    got = await deck.client.get(entry["url"], headers=H)
    assert got.status_code == 200 and got.content == b"%PDF-1.7 report"
    assert got.headers["content-type"] == "application/pdf" and "report.pdf" in got.headers["content-disposition"]
    assert (await deck.client.get("/link/files/f_nope", headers=H)).status_code == 404


@pytest.mark.asyncio
async def test_bad_http_messages_are_refused(deck):
    assert (await deck.client.post("/link/messages", json={"text": "x"})).status_code == 401
    bad = await deck.client.post("/link/messages", headers=H, json={"id": "b", "files": [{"data": "%%"}]})
    assert bad.status_code == 400 and bad.json() == {"id": "b", "error": "File 1's data isn't base64."}
    assert (await deck.client.post("/link/messages", headers=H, content=b"[1]")).status_code == 400


@pytest.mark.asyncio
async def test_an_answer_goes_to_the_webhook_signed(deck, monkeypatch):
    monkeypatch.setenv("FD_LINK_WEBHOOKS_PRIVATE", "1")
    secret = "whsec_test"
    await deck.db.update_agent_link(deck.link["id"], "alice", webhook_url="http://hooks.local/in",
                                    webhook_secret=fd_crypto.seal(secret))
    posted: list = []

    def hook(request):
        posted.append(request)
        return httpx.Response(204)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(hook), **kw))
    resp = await deck.client.post("/link/messages", headers=H, json={"id": "w1", "text": "Hi"})
    assert resp.status_code == 202
    await _settle()
    await deck.answer("Hello back")
    for _ in range(50):
        if posted:
            break
        await asyncio.sleep(0.01)
    [req] = posted
    body = req.content.decode()
    assert json.loads(body)["text"] == "Hello back" and json.loads(body)["link"] == deck.link["id"]
    stamp = req.headers["x-captain-timestamp"]
    assert req.headers["x-captain-signature"] == "sha256=" + ah.sign(secret, stamp, body)
    assert req.headers["x-captain-event"] == "reply"
    await _settle()
    assert (await deck.db.link_event_for(deck.link["id"], "w1"))["delivered"] == "webhook"


@pytest.mark.asyncio
async def test_a_failing_webhook_is_tried_again_then_left_for_polling(deck, monkeypatch):
    monkeypatch.setattr(ah, "WEBHOOK_RETRIES_S", (0, 0, 0))
    tries: list = []

    async def _post(link, event):
        tries.append(event["seq"])
        return len(tries) == 3, "HTTP 503", True

    monkeypatch.setattr(ah, "post_webhook", _post)
    event = await deck.db.add_link_event(deck.link["id"], "push", "", {"text": "x"})
    await ah._webhook(deck.link, event)
    assert len(tries) == 3 and (await deck.db.link_events_after(deck.link["id"], 0))[0]["delivered"] == "webhook"
    tries.clear()

    async def _refused(link, event):
        tries.append(1)
        return False, "is a private address", False

    monkeypatch.setattr(ah, "post_webhook", _refused)
    other = await deck.db.add_link_event(deck.link["id"], "push", "", {"text": "y"})
    await ah._webhook(deck.link, other)
    assert tries == [1]  # not worth trying again
    assert (await deck.db.link_events_after(deck.link["id"], other["seq"] - 1))[0]["delivered"] == ""


# ── the agent's own messages ──────────────────────────────────────────


def _as_agent(monkeypatch, slug="a", owner="alice"):
    monkeypatch.setattr(ah, "_calling_agent", lambda request: (owner, slug))


@pytest.mark.asyncio
async def test_the_agent_messages_a_connected_system_with_files(deck, monkeypatch):
    _as_agent(monkeypatch)
    conn = await deck.socket()
    _AgentFiles(monkeypatch, {"/w/saved/summary.md": b"# Week"})
    listed = (await deck.client.post("/fd/agent-links/list")).json()
    assert listed == {"links": [{"id": deck.link["id"], "name": "Acme CRM", "connected": True, "webhook": False}]}
    resp = await deck.client.post("/fd/agent-links/send", json={
        "link": "acme crm", "text": "This week's summary", "files": ["/w/saved/summary.md"]})
    assert resp.status_code == 200
    seq = resp.json()["seq"]
    assert resp.json() == {"seq": seq, "link": "Acme CRM", "delivered": "socket"}
    push, head, part = [m for m in conn.ws.sent if m["type"] in ("push", "file", "file_part")]
    assert {k: push[k] for k in ("seq", "text", "files")} == {"seq": seq, "text": "This week's summary", "files": 1}
    assert (head["push"], head["re"], head["name"], head["mime"]) == (seq, None, "summary.md", "text/markdown")
    assert part["push"] == seq and base64.b64decode(part["data"]) == b"# Week"
    assert (await deck.db.link_events_after(deck.link["id"], 0))[0]["delivered"] == "socket"


@pytest.mark.asyncio
async def test_a_push_waits_for_the_system_and_comes_when_it_connects(deck, monkeypatch):
    _as_agent(monkeypatch)
    resp = await deck.client.post("/fd/agent-links/send", json={"link": deck.link["id"], "text": "While away"})
    assert resp.json()["delivered"] == "kept"
    conn = await deck.socket()
    await ah.send_backlog(conn)
    assert [m["text"] for m in conn.ws.sent if m["type"] == "push"] == ["While away"]
    await ah.send_backlog(conn)  # once only
    assert len([m for m in conn.ws.sent if m["type"] == "push"]) == 1


@pytest.mark.asyncio
async def test_the_agent_reaches_only_links_that_let_it(deck, monkeypatch):
    _as_agent(monkeypatch, slug="b")
    resp = await deck.client.post("/fd/agent-links/send", json={"link": "Acme CRM", "text": "x"})
    assert resp.status_code == 404 and resp.json()["detail"].endswith("It can message none.")
    _as_agent(monkeypatch, slug="a")
    await deck.db.update_agent_link(deck.link["id"], "alice", agent_may_send=0)
    assert (await deck.client.post("/fd/agent-links/send", json={"link": "Acme CRM", "text": "x"})).status_code == 404
    await deck.db.update_agent_link(deck.link["id"], "alice", agent_may_send=1)
    assert (await deck.client.post("/fd/agent-links/send", json={"link": "Acme CRM"})).status_code == 400
    _AgentFiles(monkeypatch, {})
    refused = await deck.client.post("/fd/agent-links/send", json={"link": "Acme CRM", "files": ["/etc/passwd"]})
    assert refused.status_code == 400 and "isn't a file this agent can send" in refused.json()["detail"]
    assert await deck.db.link_events_after(deck.link["id"], 0) == []


# ── the owner's settings ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_webhook_gets_a_signing_secret_shown_once_and_kept_sealed(deck, monkeypatch):
    monkeypatch.setenv("FD_LINK_WEBHOOKS_PRIVATE", "1")
    me = {"Authorization": f"Bearer {create_access_token('alice')}"}
    made = (await deck.client.post("/fd/links", headers=me, json={
        "name": "Shop", "webhook_url": "http://hooks.local/a", "agent_may_send": False})).json()
    secret = made["webhook_secret"]
    assert secret.startswith("whsec_") and made["agent_may_send"] is False
    row = await deck.db.get_agent_link(made["id"])
    assert secret not in row["webhook_secret"] and fd_crypto.unseal(row["webhook_secret"]) == secret
    listed = (await deck.client.get("/fd/links", headers=me)).json()["links"]
    assert all("webhook_secret" not in ln for ln in listed)
    same = (await deck.client.patch(f"/fd/links/{made['id']}", headers=me,
                                    json={"webhook_url": "http://hooks.local/a"})).json()
    assert "webhook_secret" not in same
    moved = (await deck.client.patch(f"/fd/links/{made['id']}", headers=me,
                                     json={"webhook_url": "http://hooks.local/b"})).json()
    assert moved["webhook_secret"] not in (secret, None)
    rotated = (await deck.client.post(f"/fd/links/{made['id']}/webhook-secret", headers=me)).json()
    assert rotated["webhook_secret"] != moved["webhook_secret"]
    cleared = (await deck.client.patch(f"/fd/links/{made['id']}", headers=me, json={"webhook_url": ""})).json()
    assert cleared["webhook_url"] == "" and (await deck.db.get_agent_link(made["id"]))["webhook_secret"] == ""
    assert (await deck.client.post(f"/fd/links/{made['id']}/webhook-secret", headers=me)).status_code == 400
    monkeypatch.delenv("FD_LINK_WEBHOOKS_PRIVATE")
    bad = await deck.client.patch(f"/fd/links/{made['id']}", headers=me, json={"webhook_url": "http://x.example"})
    assert bad.status_code == 400 and bad.json()["detail"] == "Webhook: Use an https:// address."


# ── the tool ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_sends_through_flight_deck(monkeypatch, tmp_path):
    from captain_claw import fd_client
    from captain_claw.tools.linked_system import LinkedSystemTool

    monkeypatch.setattr(fd_client, "flight_deck_base", lambda: "http://fd:1")
    monkeypatch.setattr(fd_client, "flight_deck_headers", lambda: {"X-Agent-Auth": "tok"})
    report = tmp_path / "saved" / "reports" / "week.pdf"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"%PDF")
    seen: list = []

    def handle(request):
        seen.append((request.url.path, json.loads(request.content or b"{}"), request.headers.get("x-agent-auth")))
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"links": [{"name": "Acme CRM", "connected": False, "webhook": True}]})
        return httpx.Response(200, json={"seq": 3, "link": "Acme CRM", "delivered": "webhook"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, transport=httpx.MockTransport(handle), **kw))
    tool = LinkedSystemTool()
    listed = await tool.execute(action="list")
    assert listed.success and "• Acme CRM — not connected, has a webhook" in listed.content
    sent = await tool.execute(action="send", link="Acme CRM", text="Weekly report", files=["week.pdf"],
                              _saved_base_path=str(tmp_path / "saved"))
    assert sent.success and sent.content == ("DONE — sent to Acme CRM with 1 file: "
                                             "it was posted to the system's webhook.")
    path, body, auth = seen[-1]
    assert (path, body["link"], body["text"], auth) == ("/fd/agent-links/send", "Acme CRM", "Weekly report", "tok")
    assert body["files"] == [str(report.resolve())]
    missing = await tool.execute(action="send", link="Acme CRM", files=["nope.pdf"],
                                 _saved_base_path=str(tmp_path / "saved"))
    assert not missing.success and "No file 'nope.pdf'" in missing.error
