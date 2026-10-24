"""Flight Deck A → BotPort → Flight Deck B → agent, end to end over real sockets.

A real BotPort hub (aiohttp), two real BotPortLink decks, and behind deck B a
fake Captain Claw agent that speaks the agent web socket protocol — driven by
FD's real ``_consult_peer_events`` proxy, exactly as /fd/consult-peer does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# From the repo root the top-level botport/ directory (no __init__) shadows
# the real package botport/botport as a namespace package: put the project
# dir first and drop a namespace stand-in if one was already imported.
_BOTPORT_PROJECT = Path(__file__).resolve().parents[2] / "botport"
if str(_BOTPORT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOTPORT_PROJECT))
if "botport" in sys.modules and getattr(sys.modules["botport"], "__file__", None) is None:
    for _name in [m for m in sys.modules if m == "botport" or m.startswith("botport.")]:
        del sys.modules[_name]

from botport.config import AuthKeyEntry, BotPortConfig, set_config  # noqa: E402
from botport.server import BotPortServer  # noqa: E402
from captain_claw.flight_deck import botport_link as bl  # noqa: E402
from captain_claw.flight_deck import server as fd_server  # noqa: E402

REAL_CONSULT_EVENTS = fd_server._consult_peer_events


class FakeAgent:
    """A Captain Claw agent's /ws: welcome, replay_done, then answers chats."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.received: list[dict[str, Any]] = []
        self.lanes: list[str] = []
        self.gate: asyncio.Event | None = None
        self.server: TestServer | None = None
        self.cancelled = asyncio.Event()

    async def _turn(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        await ws.send_str(json.dumps({"type": "status", "status": "thinking hard"}))
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if ws.closed:
            return
        await ws.send_str(json.dumps({"type": "chat_message", "role": "assistant",
                                      "content": "ANSWER: " + data["content"].splitlines()[-1]}))
        await ws.send_str(json.dumps({"type": "usage", "model": "m", "input_tokens": 3}))

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.lanes.append(request.query.get("lane", ""))
        await ws.send_str(json.dumps({"type": "welcome"}))
        await ws.send_str(json.dumps({"type": "replay_done"}))
        turns: list[asyncio.Task] = []
        try:
            # Like a real agent: the turn runs in the background while the
            # socket keeps being read (so a close is noticed at once).
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("type") != "chat":
                    continue
                self.received.append(data)
                turns.append(asyncio.create_task(self._turn(ws, data)))
        finally:
            if self.gate is not None and not self.gate.is_set():
                self.cancelled.set()
            for t in turns:
                t.cancel()
        return ws

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return self.server.port

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()


async def _wait(cond, timeout: float = 8.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def _collect(gen) -> tuple[list[dict], dict]:
    events: list[dict] = []
    async with contextlib.aclosing(gen) as g:
        async for e in g:
            events.append(e)
    return events, events[-1]


@pytest.fixture
async def net(tmp_path, monkeypatch):
    """hub + deck A ("vps1", no agents) + deck B ("pi", exposes Researcher)."""
    monkeypatch.setenv("BOTPORT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("FD_BOTPORT_NO_PROXY", "1")
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")  # single-user decks (agents have no owner)
    for var in list(bl._ENV.values()):
        monkeypatch.delenv(var, raising=False)

    cfg = BotPortConfig()
    cfg.server.data_dir = str(tmp_path / "hub")
    cfg.server.ws_heartbeat_seconds = 0
    cfg.auth.enabled = True
    cfg.auth.keys = [AuthKeyEntry(instance="vps1", key="ka", secret="secret-a-0123456789"),
                     AuthKeyEntry(instance="pi", key="kb", secret="secret-b-0123456789")]
    cfg.concerns.timeout_check_interval_seconds = 3600
    set_config(cfg)
    hub = BotPortServer(cfg)
    await hub.store._ensure_db()
    hub_server = TestServer(hub.create_app(), host="127.0.0.1")
    await hub_server.start_server()
    url = f"ws://127.0.0.1:{hub_server.port}/ws"

    agent = FakeAgent()
    agent_port = await agent.start()
    fleet_b = [
        {"name": "Researcher", "slug": "researcher", "kind": "process", "port": agent_port,
         "status": "running", "description": "Finds things"},
        {"name": "Private", "slug": "private", "kind": "process", "port": 1, "status": "running"},
    ]

    a, b = bl.BotPortLink(), bl.BotPortLink()
    a.configure(fleet=lambda: [], consult_events=REAL_CONSULT_EVENTS, resolve_auth=lambda p: "", version="t")
    b.configure(fleet=lambda: fleet_b, consult_events=REAL_CONSULT_EVENTS, resolve_auth=lambda p: "tok",
                version="t")
    await a.start({"enabled": True, "url": url, "deck_name": "vps1", "key": "ka",
                   "secret": "secret-a-0123456789"}, "deck-a")
    await b.start({"enabled": True, "url": url, "deck_name": "pi", "key": "kb",
                   "secret": "secret-b-0123456789", "expose": ["researcher"]}, "deck-b")
    await _wait(lambda: a.connected and b.connected, what="both links up")
    await _wait(lambda: a.find_remote("pi", "researcher") is not None, what="roster")

    yield {"hub": hub, "url": url, "a": a, "b": b, "agent": agent, "fleet_b": fleet_b}

    await a.stop()
    await b.stop()
    await agent.close()
    await hub.shutdown()
    await hub_server.close()


async def test_consult_reaches_the_remote_agent_and_back(net):
    a, agent = net["a"], net["agent"]
    agent.delay = 1.5  # a turn that takes a moment, so its status is worth relaying
    names = [r["name"] for r in a.remote_agents()]
    assert names == ["Researcher@pi"], "only exposed agents are advertised"

    events, final = await _collect(a.consult(target_deck="pi", target_agent="Researcher",
                                             message="what is 6x7", from_agent="Planner", timeout=60))
    assert final == {"ok": True, "done": True, "response": "ANSWER: what is 6x7"}
    # The agent's own status reaches the requester (throttled frames are
    # coalesced, not dropped).
    assert any((e.get("data") or {}).get("status") == "thinking hard" for e in events), events

    sent = agent.received[-1]
    assert "Planner on Flight Deck 'vps1'" in sent["content"]
    assert sent["automation"] == {"kind": "botport", "job_text": "", "mail_write": "deny"}
    assert sent["no_flow"] is True and sent["no_broadcast"] is True
    # Loop guard: a remote turn cannot reach back into the network.
    assert {"flight_deck", "consult_peer", "botport"} <= set(sent["deny_tools"])
    # Never the owner's main chat: a lane of the requesting deck's own.
    assert agent.lanes[-1] == bl.remote_lane("vps1") and agent.lanes[-1].startswith("BP")
    assert bl.remote_lane("vps1") != bl.remote_lane("vps2") and len(bl.remote_lane("x")) == 8

    st = net["b"].status()
    assert st["counters"]["inbound_ok"] == 1 and st["exposed_agents"] == ["Researcher"]
    assert a.status()["counters"]["outbound_ok"] == 1


async def test_unexposed_and_unknown_agents_are_refused(net):
    a = net["a"]
    _, final = await _collect(a.consult(target_deck="pi", target_agent="Private", message="hi"))
    assert final["ok"] is False and "Researcher@pi" in final["error"]


async def test_accept_from_blocks_other_decks(net):
    b = net["b"]
    await b.apply({**b.settings.to_dict(), "accept_from": ["someone-else"]})
    await _wait(lambda: b.connected, what="b back")
    await _wait(lambda: net["a"].find_remote("pi", "researcher") is not None, what="roster")
    _, final = await _collect(net["a"].consult(target_deck="pi", target_agent="researcher", message="x"))
    assert final["ok"] is False and "does not accept requests from 'vps1'" in final["error"]


async def test_requester_link_drop_mid_task_still_gets_the_answer(net):
    a, agent, hub = net["a"], net["agent"], net["hub"]
    agent.gate = asyncio.Event()
    task = asyncio.create_task(_collect(a.consult(target_deck="pi", target_agent="researcher",
                                                  message="slow one", timeout=60)))
    await _wait(lambda: agent.received, what="agent got the task")
    # Kill deck A's socket from the hub side (a network drop).
    await hub.connections.get_ws("deck:vps1").close(code=1006)
    await _wait(lambda: not a.connected, what="a dropped")
    agent.gate.set()  # the work finishes while A is offline
    await _wait(lambda: hub._outbox.get("deck:vps1"), what="result held for A")
    await a.reconnect()
    _, final = await asyncio.wait_for(task, timeout=15)
    assert final["ok"] is True and final["response"] == "ANSWER: slow one"
    await _wait(lambda: not hub._outbox.get("deck:vps1"), what="A acknowledged")


async def test_worker_link_drop_mid_task_keeps_the_task(net):
    a, b, agent, hub = net["a"], net["b"], net["agent"], net["hub"]
    agent.gate = asyncio.Event()
    task = asyncio.create_task(_collect(a.consult(target_deck="pi", target_agent="researcher",
                                                  message="survive", timeout=60)))
    await _wait(lambda: agent.received, what="agent got the task")
    await hub.connections.get_ws("deck:pi").close(code=1006)
    await _wait(lambda: not b.connected, what="b dropped")
    agent.gate.set()
    await _wait(lambda: b._outbox, what="b holds its result")
    await b.reconnect()
    _, final = await asyncio.wait_for(task, timeout=15)
    assert final["ok"] is True and final["response"] == "ANSWER: survive"
    await _wait(lambda: not b._outbox, what="hub acknowledged b's result")


async def test_giving_up_cancels_the_remote_task(net):
    a, b, agent = net["a"], net["b"], net["agent"]
    agent.gate = asyncio.Event()
    gen = a.consult(target_deck="pi", target_agent="researcher", message="never mind", timeout=60)
    async with contextlib.aclosing(gen) as g:
        async for _e in g:
            if agent.received:
                break
            await asyncio.sleep(0)
        await _wait(lambda: agent.received, what="agent got it")
    # The consumer left: the hub tells B to stop, B cancels its proxy task
    # and hangs up on its agent.
    await _wait(lambda: not b._inbound, what="b cancelled the inbound task")
    await _wait(agent.cancelled.is_set, what="agent connection closed")
    agent.gate.set()


async def test_bad_credentials_stop_retrying(net):
    c = bl.BotPortLink()
    c.configure(fleet=lambda: [], consult_events=REAL_CONSULT_EVENTS, resolve_auth=lambda p: "")
    await c.start({"enabled": True, "url": net["url"], "deck_name": "vps1", "key": "ka",
                   "secret": "wrong"}, "deck-c")
    try:
        await _wait(lambda: c.state == "error", what="error state")
        assert "Authentication failed" in c.last_error
        connects_before = c.counters["connects"]
        await asyncio.sleep(1.5)
        assert c.state == "error" and c.counters["connects"] == connects_before
        assert net["a"].connected, "the real vps1 keeps its link"
    finally:
        await c.stop()


async def test_fleet_changes_are_readvertised(net):
    a, b, fleet_b = net["a"], net["b"], net["fleet_b"]
    await b.apply({**b.settings.to_dict(), "expose": ["*"]})
    await _wait(lambda: b.connected, what="b back")
    await _wait(lambda: a.find_remote("pi", "private") is not None, what="Private exposed")
    fleet_b[0]["status"] = "exited"
    b.notify_fleet_changed()
    await _wait(lambda: a.find_remote("pi", "researcher") is None, what="stopped agent hidden")


async def test_star_skips_orphans_and_managed_workers_on_a_multi_user_deck(monkeypatch):
    fleet = [
        {"name": "Mine", "slug": "mine", "kind": "process", "port": 1, "status": "running", "owner": "u1"},
        {"name": "Orphan", "slug": "orphan", "kind": "process", "port": 2, "status": "running", "owner": ""},
        {"name": "w", "slug": "basna-0123abcd-x", "kind": "process", "port": 3, "status": "running", "owner": "u1"},
    ]
    link = bl.BotPortLink()
    link.configure(fleet=lambda: fleet, consult_events=REAL_CONSULT_EVENTS, resolve_auth=lambda p: "")
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    link.settings = bl.LinkSettings.from_dict({"expose": ["*"]})
    assert [a["name"] for a in await link._exposed()] == ["Mine"]
    link.settings = bl.LinkSettings.from_dict({"expose": ["orphan", "basna-0123abcd-x"]})
    assert [a["name"] for a in await link._exposed()] == ["Orphan"], "named explicitly: the admin chose it"
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    link.settings = bl.LinkSettings.from_dict({"expose": ["*"]})
    assert [a["name"] for a in await link._exposed()] == ["Mine", "Orphan"]


def test_settings_from_env_and_urls(monkeypatch):
    monkeypatch.setenv("FD_BOTPORT_URL", "pi.local:23180")
    monkeypatch.setenv("FD_BOTPORT_DECK_NAME", "vps2")
    monkeypatch.setenv("FD_BOTPORT_EXPOSE", "researcher, coder")
    link = bl.BotPortLink()
    s = link.effective({"enabled": False, "url": "ignored", "accept_from": ["*"]})
    assert s.enabled is True and s.url == "pi.local:23180" and s.deck_name == "vps2"
    assert s.expose == ["researcher", "coder"] and s.problems() == []
    assert bl.normalize_url("https://hub.example.com") == "wss://hub.example.com/ws"
    assert bl.normalize_url("ws://10.0.0.2:23180/ws") == "ws://10.0.0.2:23180/ws"
    with pytest.raises(ValueError):
        bl.normalize_url("ftp://x")
    bad = bl.LinkSettings.from_dict({"enabled": True, "url": "x:1", "deck_name": "has space"})
    assert bad.problems()


def test_secret_is_sealed_at_rest(monkeypatch, tmp_path):
    from captain_claw.flight_deck import fd_crypto

    if not fd_crypto.available():
        pytest.skip("cryptography not installed")
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "k"))
    fd_crypto.reset_for_tests()
    raw = bl.seal_settings({"url": "x", "secret": "s3cret"})
    assert "s3cret" not in raw and "secret_sealed" in raw
    assert bl.unseal_settings(raw)["secret"] == "s3cret"
    fd_crypto.reset_for_tests()
