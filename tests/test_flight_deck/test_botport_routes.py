"""FD routes for the BotPort link: remote consult/delegate, fleet, admin config."""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import httpx
import pytest

from captain_claw.flight_deck import botport_link as bl
from captain_claw.flight_deck import server as fd_server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

LOOPBACK = ("127.0.0.1", 40001)
REMOTE = ("203.0.113.9", 40001)
ALICE_PORT = 24111


def _client(addr) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fd_server.app, client=addr),
                             base_url="http://localhost:25080")


def _bearer(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user['id'], user.get('role', 'user'))}"}


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch):
    from captain_claw.flight_deck import auth as fd_auth
    from captain_claw.flight_deck import fd_crypto

    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "users.db")
    await db.init()
    set_auth_db(db)
    alice = await db.create_user("alice@x.test", "h", "Alice")
    admin = await db.create_user("admin@x.test", "h", "Admin", role="admin")
    registry = {"alice-agent": {"name": "Planner", "web_port": ALICE_PORT, "web_auth": "tok-alice",
                                "owner": alice["id"]}}
    (tmp_path / "fd-data").mkdir()

    def _no_docker():
        raise RuntimeError("no docker in tests")

    monkeypatch.setattr(fd_server, "DATA_DIR", tmp_path / "fd-data")
    monkeypatch.setattr(fd_server, "_load_process_registry", lambda: registry)
    monkeypatch.setattr(fd_server, "_process_is_alive", lambda slug: True)
    monkeypatch.setattr(fd_server, "get_docker", _no_docker)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "data_key"))
    for var in bl._ENV.values():
        monkeypatch.delenv(var, raising=False)
    fd_crypto.reset_for_tests()

    link = bl.BotPortLink()
    link.state, link._ws = "connected", object()
    link.network = {"decks": [{"name": "pi", "status": "connected", "agents": [
        {"name": "Researcher", "slug": "researcher", "description": "Finds things", "status": "running"}]}],
        "agents": []}
    calls: list[dict] = []

    async def fake_consult(**kw):
        calls.append(kw)
        yield {"event": "status", "data": {"status": "Routed"}}
        yield {"ok": True, "done": True, "response": f"remote says hi to {kw['from_agent']}"}

    link.consult = fake_consult  # type: ignore[method-assign]
    applied: list[dict] = []

    async def fake_apply(stored):
        applied.append(stored)
        link.settings = link.effective(stored)

    link.apply = fake_apply  # type: ignore[method-assign]
    monkeypatch.setattr(bl, "_LINK", link)

    delivered: list[dict] = []

    async def fake_deliver(req, source_port, source_auth, peer_display, response_text, relay_level=""):
        delivered.append({"port": source_port, "auth": source_auth, "peer": peer_display, "text": response_text})

    monkeypatch.setattr(fd_server, "_deliver_delegate_result", fake_deliver)
    try:
        yield types.SimpleNamespace(db=db, alice=alice, admin=admin, link=link, calls=calls,
                                    applied=applied, delivered=delivered)
    finally:
        await db.close()
        fd_auth._db = prev
        fd_crypto.reset_for_tests()


async def test_agent_consults_a_remote_agent_through_fd(deck):
    async with _client(LOOPBACK) as c:
        r = await c.post("/fd/consult-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "port": 0, "target_deck": "pi", "target_agent": "researcher", "message": "hello",
            "source_name": "spoofed-name"})
    assert r.status_code == 200
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert lines[-1] == {"ok": True, "done": True, "response": "remote says hi to Planner"}
    # The name the remote deck sees comes from FD's records, not the body.
    assert deck.calls[0]["from_agent"] == "Planner" and deck.calls[0]["target_deck"] == "pi"


async def test_remote_consult_keeps_the_peer_route_guard(deck):
    async with _client(REMOTE) as c:
        r = await c.post("/fd/consult-peer", json={"target_deck": "pi", "target_agent": "researcher",
                                                   "message": "x"})
    assert r.status_code == 403 and deck.calls == []


async def test_remote_consult_refuses_attachments_and_bad_addresses(deck):
    async with _client(LOOPBACK) as c:
        r = await c.post("/fd/consult-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "target_deck": "pi", "target_agent": "researcher", "message": "x",
            "attach_path": "/etc/passwd"})
        r2 = await c.post("/fd/consult-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "target_agent": "researcher", "message": "x"})
    assert r.status_code == 400 and "cannot be attached" in r.text
    assert r2.status_code == 400 and deck.calls == []


async def test_agent_at_deck_form_is_accepted(deck):
    async with _client(LOOPBACK) as c:
        r = await c.post("/fd/consult-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "target_agent": "researcher@pi", "message": "x"})
    assert r.status_code == 200 and deck.calls[0]["target_agent"] == "researcher"


async def test_remote_delegate_delivers_back_to_the_caller(deck):
    async with _client(LOOPBACK) as c:
        r = await c.post("/fd/delegate-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "target_deck": "pi", "target_agent": "researcher", "message": "dig"})
    assert r.status_code == 200 and r.json()["ok"] is True
    for _ in range(50):
        if deck.delivered:
            break
        await asyncio.sleep(0.02)
    assert deck.delivered == [{"port": ALICE_PORT, "auth": "tok-alice", "peer": "Researcher@pi",
                               "text": "remote says hi to Planner"}]
    assert deck.calls[0]["mode"] == "delegate"


async def test_remote_delegate_to_an_unknown_agent_says_so(deck):
    async with _client(LOOPBACK) as c:
        r = await c.post("/fd/delegate-peer", headers={"X-Agent-Auth": "tok-alice"}, json={
            "target_deck": "pi", "target_agent": "ghost", "message": "dig"})
    body = r.json()
    assert body["ok"] is False and "Researcher@pi" in body["message"]


async def test_fleet_lists_remote_agents_only_on_request(deck):
    async with _client(LOOPBACK) as c:
        plain = (await c.get("/fd/fleet", headers=_bearer(deck.alice))).json()
        full = (await c.get("/fd/fleet?include_remote=1", headers=_bearer(deck.alice))).json()
    assert all(a["kind"] != "remote" for a in plain)
    async with _client(REMOTE) as c:
        anon = (await c.get("/fd/fleet?include_remote=1")).json()
    async with _client(LOOPBACK) as c:
        as_agent = (await c.get("/fd/fleet?include_remote=1", headers={"X-Agent-Auth": "tok-alice"})).json()
    assert all(a["kind"] != "remote" for a in anon), "no roster for an anonymous caller"
    assert any(a["kind"] == "remote" for a in as_agent)
    remote = [a for a in full if a["kind"] == "remote"]
    assert remote == [{"name": "Researcher@pi", "slug": "researcher", "kind": "remote", "host": "",
                       "port": 0, "status": "running", "description": "Finds things", "deck": "pi"}]


async def test_config_is_admin_only_and_secret_never_returned(deck):
    async with _client(LOOPBACK) as c:
        denied = await c.put("/fd/botport/config", headers=_bearer(deck.alice), json={"enabled": True})
        bad = await c.put("/fd/botport/config", headers=_bearer(deck.admin), json={"deck_name": "has space"})
        bad_url = await c.put("/fd/botport/config", headers=_bearer(deck.admin), json={"url": "ftp://x"})
        ok = await c.put("/fd/botport/config", headers=_bearer(deck.admin), json={
            "enabled": True, "url": "pi.local:23180", "deck_name": "vps1", "key": "k",
            "secret": "very-secret-value", "expose": ["researcher"]})
        got = await c.get("/fd/botport/config", headers=_bearer(deck.admin))
        keep = await c.put("/fd/botport/config", headers=_bearer(deck.admin), json={"secret": ""})
        status_user = await c.get("/fd/botport/status", headers=_bearer(deck.alice))
    assert denied.status_code == 403 and bad.status_code == 400 and bad_url.status_code == 400
    assert ok.status_code == 200
    body = got.json()
    assert "very-secret-value" not in got.text and body["secret_set"] is True
    assert body["normalized_url"] == "ws://pi.local:23180/ws" and body["problems"] == []
    assert keep.json()["secret_set"] is True, "an empty secret keeps the stored one"
    raw = await deck.db.get_system_setting(bl.SETTING_KEY)
    assert "very-secret-value" not in raw
    assert deck.applied[-1]["secret"] == "very-secret-value"
    assert set(status_user.json()) == {"enabled", "state", "deck_name", "remote_agents"}


async def test_sign_in_off_deck_takes_link_settings_only_from_this_machine(deck, monkeypatch):
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    async with _client(REMOTE) as c:
        far = await c.put("/fd/botport/config", json={"enabled": True})
        far_get = await c.get("/fd/botport/config")
    async with _client(LOOPBACK) as c:
        near = await c.put("/fd/botport/config", json={"deck_name": "pi"})
    assert far.status_code == 403 and far_get.status_code == 403
    assert "from this machine" in far.json()["detail"]
    assert near.status_code == 200 and near.json()["deck_name"] == "pi"
