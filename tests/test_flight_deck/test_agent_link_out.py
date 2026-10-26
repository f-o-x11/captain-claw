"""Outbound agent links (agent_link_out.py): Flight Deck connects out to a system.

Pinned here:

* ``runs_local``: an Ollama model that isn't a cloud one, or any endpoint on a
  loopback / private address, answers locally; a hosted API doesn't;
* the hello: the link, the agent, the framework and version, the model and
  whether it runs locally;
* the connection, against a real WebSocket server standing in for the system:
  the key goes as a Bearer header and nowhere else, hello comes first, a
  message runs as a link turn and its answer comes back on the socket; after
  a drop Flight Deck dials again and says hello again; a refused key is said
  and tried again later; removing the link hangs up;
* the owner's routes: an outbound link needs its address and the system's
  key, keeps the key sealed, never shows a link key of its own, and can't be
  reached through /link/ws; a new address dials afresh.
"""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi import FastAPI

from captain_claw.flight_deck import agent_link as al
from captain_claw.flight_deck import agent_link_out as ao
from captain_claw.flight_deck import auth as fd_auth
from captain_claw.flight_deck import fd_crypto, usage_quota
from captain_claw.flight_deck import glasses_bridge as gb
from captain_claw.flight_deck.auth import create_access_token, hash_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB


async def _settle(n: int = 40):
    for _ in range(n):
        await asyncio.sleep(0)


async def _until(check, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


# ── pure parts ────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider,model,base_url,local", [
    ("ollama", "qwen3.5:4b", "", True),
    ("ollama", "minimax-m2.7:cloud", "", False),
    ("ollama", "qwen3.5:4b", "http://host.docker.internal:11434", True),
    ("openai", "Qwen/Qwen3.8", "http://192.168.1.20:1234/v1", True),
    ("openai", "fake", "http://127.0.0.1:26993/v1", True),
    ("openai", "gpt-5", "", False),
    ("anthropic", "claude-opus-5-5", "", False),
    ("openai", "x", "https://api.example.com/v1", False),
    ("", "", "", None),
])
def test_runs_local(provider, model, base_url, local):
    assert ao.runs_local(provider, model, base_url) is local


def test_the_hello_says_who_is_calling(monkeypatch):
    from captain_claw import __version__

    link = {"id": "l1", "user_id": "u1", "name": "Arena", "agent_slug": "a"}
    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: {"name": "Scout", "slug": "a", "port": 1})
    monkeypatch.setattr(ao, "_model_config", lambda slug: {"provider": "ollama", "model": "qwen3.5:4b"})
    assert ao.hello(link) == {"type": "hello", "link": "Arena", "agent": "Scout", "framework": "captain",
                              "version": __version__, "model": "ollama/qwen3.5:4b", "runs_local": True}
    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: None)
    assert {k: ao.hello(link)[k] for k in ("agent", "model", "runs_local")} == {
        "agent": None, "model": None, "runs_local": None}


@pytest.mark.asyncio
async def test_the_waits_grow_while_it_fails_and_start_afresh_once_it_was_up(monkeypatch):
    import time as _time
    import types

    # (why, was up, for a while)
    outcomes = ([("refused", False, False)] * 4 + [("closed", True, True)]
                + [("closed", True, False)] * 3 + [("refused", False, False)])
    waits: list = []
    clock = {"t": 0.0}

    class _DB:
        async def get_agent_link(self, link_id):
            return {"id": link_id, "user_id": "u1", "agent_slug": "a", "direction": "outbound"}

    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: {"name": "Scout", "slug": "a", "port": 1})

    async def _once(link):
        if not outcomes:
            raise asyncio.CancelledError
        why, up, long = outcomes.pop(0)
        if long:
            clock["t"] += ao.UP_A_WHILE_S + 1
        return why, up

    async def _sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(fd_auth, "_db", _DB())
    monkeypatch.setattr(ao, "_connect_once", _once)
    # (only what the connector sees: the deck's own loop keeps the real clock)
    monkeypatch.setattr(ao, "asyncio", types.SimpleNamespace(sleep=_sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(ao, "time", types.SimpleNamespace(monotonic=lambda: clock["t"], time=_time.time))
    with pytest.raises(asyncio.CancelledError):
        await ao._run("l1")
    # failing: growing; up a while: at once; closing at once again and again: 1, 2, 5; failing: on from there
    assert waits == [1, 2, 5, 10, 1, 1, 2, 5, 10]


# ── against a stand-in system ─────────────────────────────────────────


class _System:
    """An orchestrator's WebSocket: takes only ``Bearer <key>``, keeps what it hears."""

    def __init__(self, key: str):
        self.key = key
        self.connections: list = []
        self.heard: list[list[dict]] = []  # per connection
        self.auth: list[str] = []
        self.server = None
        self.port = 0

    def _check(self, connection, request):
        got = request.headers.get("Authorization", "")
        self.auth.append(got)
        if got != f"Bearer {self.key}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unknown agent key\n")
        return None

    async def _handle(self, ws):
        self.connections.append(ws)
        frames: list[dict] = []
        self.heard.append(frames)
        try:
            async for raw in ws:
                frames.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async def start(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0, process_request=self._check)
        self.port = self.server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v0/agent"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


class _AgentWS:
    def __init__(self):
        self.frames: list = []

    async def send(self, text):
        self.frames.append(json.loads(text))


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
    monkeypatch.setenv("FD_LINK_PRIVATE_ADDRESSES", "1")
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "data_key"))
    fd_crypto.reset_for_tests()
    for name in ("_turns", "_queues", "_seen", "_workers", "_live", "_headless", "_http_waits"):
        monkeypatch.setattr(al, name, {})
    monkeypatch.setattr(ao, "_tasks", {})
    monkeypatch.setattr(ao, "_status", {})
    monkeypatch.setattr(ao, "RETRY_S", (0.05, 0.05))
    monkeypatch.setattr(gb, "_channels", {})
    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: {"name": "Scout", "slug": "a", "port": 1})
    monkeypatch.setattr(ao, "_model_config", lambda slug: {"provider": "ollama", "model": "qwen3.5:4b"})
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
    system = _System("k-123")
    await system.start()
    app = FastAPI()
    app.include_router(al.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fd.test")

    async def outbound_link(key: str = "k-123", url: str | None = None) -> dict:
        return await db.create_agent_link(
            "alice", name="Arena", agent_slug="a", instructions="", key_hash=hash_token("cc_link_unused"),
            key_hint="used", direction="outbound", remote_url=url or system.url, remote_key=fd_crypto.seal(key))

    async def frame(link: dict, data: dict):
        await gb._forward_agent_msg(gb._channels[al._channel_id("alice", link["id"])], data)
        await _settle()

    try:
        yield types.SimpleNamespace(db=db, system=system, agent=agent, client=client,
                                    outbound_link=outbound_link, frame=frame)
    finally:
        await ao.stop_all()
        for task in list(al._workers.values()):
            task.cancel()
        await system.stop()
        await client.aclose()
        await db.close()
        fd_auth._db = prev
        fd_crypto.reset_for_tests()


@pytest.mark.asyncio
async def test_flight_deck_dials_out_says_hello_and_runs_turns(deck):
    link = await deck.outbound_link()
    ao.ensure(link["id"])
    await _until(lambda: deck.system.heard and len(deck.system.heard[0]) >= 2)
    assert deck.system.auth == ["Bearer k-123"]
    hello, bound = deck.system.heard[0][:2]
    assert (hello["type"], hello["link"], hello["agent"], hello["model"], hello["runs_local"]) == (
        "hello", "Arena", "Scout", "ollama/qwen3.5:4b", True)
    assert bound["type"] == "bound"
    assert "k-123" not in json.dumps(deck.system.heard[0])
    assert ao.status(link["id"])["state"] == "connected"
    assert al.public_link(link)["connected"] is True

    await deck.system.connections[0].send(json.dumps({"type": "message", "id": "t1", "text": "Summarise this"}))
    await _until(lambda: deck.agent.frames)
    sent = deck.agent.frames[0]
    assert sent["content"] == "Summarise this" and sent["automation"]["kind"] == "link"
    await deck.frame(link, {"type": "chat_message", "role": "assistant", "content": "Done: short summary."})
    await deck.frame(link, {"type": "status", "status": "ready"})
    await _until(lambda: any(f.get("state") == "ready" for f in deck.system.heard[0]))
    got = [f for f in deck.system.heard[0][2:] if f["type"] in ("accepted", "reply", "state")]
    assert got == [{"type": "accepted", "id": "t1", "queued": 0},
                   {"type": "state", "state": "thinking", "re": "t1"},
                   {"type": "reply", "text": "Done: short summary.", "re": "t1"},
                   {"type": "state", "state": "ready", "re": "t1"}]


@pytest.mark.asyncio
async def test_after_a_drop_it_dials_again_and_says_hello_again(deck):
    link = await deck.outbound_link()
    ao.ensure(link["id"])
    await _until(lambda: deck.system.connections)
    await deck.system.connections[0].close()
    await _until(lambda: len(deck.system.heard) == 2 and deck.system.heard[1])
    assert deck.system.heard[1][0]["type"] == "hello"
    await _until(lambda: ao.status(link["id"])["state"] == "connected")


@pytest.mark.asyncio
async def test_it_dials_only_once_its_agent_runs(deck, monkeypatch):
    running = {"yes": False}
    monkeypatch.setattr(al, "_link_agent", lambda user_id, slug: (
        {"name": "Scout", "slug": "a", "port": 1} if running["yes"] else None))
    monkeypatch.setattr(ao, "AGENT_POLL_S", 0.05)
    link = await deck.outbound_link()
    ao.ensure(link["id"])
    await _until(lambda: ao.status(link["id"]).get("error"))
    assert ao.status(link["id"])["error"] == 'agent "a" isn\'t running'
    await asyncio.sleep(0.15)
    assert deck.system.auth == []  # not dialled meanwhile
    running["yes"] = True
    await _until(lambda: deck.system.heard and deck.system.heard[0])
    assert deck.system.heard[0][0]["agent"] == "Scout"


@pytest.mark.asyncio
async def test_a_refused_key_is_said_and_tried_again(deck):
    link = await deck.outbound_link(key="wrong")
    ao.ensure(link["id"])
    await _until(lambda: ao.status(link["id"]).get("error"))
    state = ao.status(link["id"])
    assert (state["state"], state["error"]) == ("waiting", "the system refused the key (HTTP 401)")
    await _until(lambda: len(deck.system.auth) >= 2)  # tried again
    assert deck.system.connections == []


@pytest.mark.asyncio
async def test_the_owner_sets_up_an_outbound_link_and_removing_it_hangs_up(deck):
    me = {"Authorization": f"Bearer {create_access_token('alice')}"}
    no_key = await deck.client.post("/fd/links", headers=me, json={
        "name": "Arena", "direction": "outbound", "remote_url": deck.system.url})
    assert no_key.status_code == 400
    made = await deck.client.post("/fd/links", headers=me, json={
        "name": "Arena", "agent_slug": "a", "direction": "outbound", "remote_url": deck.system.url,
        "remote_key": "k-123"})
    body = made.json()
    assert made.status_code == 200 and "key" not in body and "webhook_secret" not in body
    assert (body["direction"], body["remote_url"]) == ("outbound", deck.system.url)
    row = await deck.db.get_agent_link(body["id"])
    assert "k-123" not in row["remote_key"] and fd_crypto.unseal(row["remote_key"]) == "k-123"
    await _until(lambda: deck.system.connections)  # it dials as soon as it's made
    listed = (await deck.client.get("/fd/links", headers=me)).json()["links"][0]
    assert listed["outbound"]["state"] == "connected" and listed["connected"] is True
    assert (await deck.client.post(f"/fd/links/{body['id']}/key", headers=me)).status_code == 400

    other = _System("k-456")
    await other.start()
    try:
        moved = await deck.client.patch(f"/fd/links/{body['id']}", headers=me,
                                        json={"remote_url": other.url, "remote_key": "k-456"})
        assert moved.status_code == 200
        await _until(lambda: other.heard and other.heard[0])  # dialled afresh, to the new address
        assert other.auth == ["Bearer k-456"] and other.heard[0][0]["type"] == "hello"
        renamed = await deck.client.patch(f"/fd/links/{body['id']}", headers=me, json={"name": "Arena 2"})
        assert renamed.status_code == 200
        await _settle()
        assert len(other.connections) == 1  # a new name doesn't redial
        assert (await deck.client.delete(f"/fd/links/{body['id']}", headers=me)).json() == {"ok": True}
        await _until(lambda: other.connections[0].state.name == "CLOSED")
        assert body["id"] not in ao._tasks
    finally:
        await other.stop()

    bad_address = await deck.client.post("/fd/links", headers=me, json={
        "direction": "outbound", "remote_url": "ftp://x.example", "remote_key": "k"})
    assert bad_address.status_code == 400 and bad_address.json()["detail"] == "Address: Use a wss:// address."


@pytest.mark.asyncio
async def test_an_outbound_link_is_not_reached_through_link_ws(deck):
    link = await deck.outbound_link()

    class _Socket:
        headers = {"authorization": "Bearer cc_link_unused"}
        query_params: dict = {}
        closed = None

        async def close(self, code=1000, reason=""):
            self.closed = code

        async def accept(self):
            raise AssertionError("accepted")

    sock = _Socket()
    await al.link_ws(sock)
    assert sock.closed == 4403
    assert link["id"] not in ao._tasks
