"""Shared fixtures: a real BotPort hub on an ephemeral port plus WS clients."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from botport.config import AuthKeyEntry, BotPortConfig, set_config
from botport.server import BotPortServer


class Peer:
    """A WebSocket client speaking the BotPort protocol (deck or agent)."""

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self._url = url
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self.registered: dict[str, Any] = {}

    async def connect(self, **register: Any) -> dict[str, Any]:
        self.ws = await self._session.ws_connect(self._url)
        await self.send({"type": "register", **register})
        msg = await self.ws.receive(timeout=5)
        self.registered = json.loads(msg.data) if msg.type == aiohttp.WSMsgType.TEXT else {}
        self._reader = asyncio.create_task(self._read())
        return self.registered

    async def _read(self) -> None:
        assert self.ws is not None
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self.inbox.put(json.loads(msg.data))
        await self.inbox.put({"type": "_closed", "code": self.ws.close_code})

    async def send(self, data: dict[str, Any]) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(data))

    async def expect(self, msg_type: str, timeout: float = 5.0, **match: Any) -> dict[str, Any]:
        """Next message of ``msg_type`` (others are skipped)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no {msg_type!r} within {timeout}s")
            msg = await asyncio.wait_for(self.inbox.get(), timeout=remaining)
            if msg.get("type") == msg_type and all(msg.get(k) == v for k, v in match.items()):
                return msg

    async def nothing(self, msg_type: str, wait: float = 0.4) -> None:
        """Assert no ``msg_type`` arrives within ``wait`` seconds."""
        try:
            got = await self.expect(msg_type, timeout=wait)
        except (AssertionError, asyncio.TimeoutError):
            return
        raise AssertionError(f"unexpected {msg_type!r}: {got}")

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=2)
            except Exception:
                self._reader.cancel()


class Hub:
    def __init__(self, server: BotPortServer, test_server: TestServer, session: aiohttp.ClientSession) -> None:
        self.server = server
        self.test_server = test_server
        self.session = session
        self.peers: list[Peer] = []

    @property
    def ws_url(self) -> str:
        return str(self.test_server.make_url("/ws"))

    def url(self, path: str) -> str:
        return str(self.test_server.make_url(path))

    def peer(self) -> Peer:
        p = Peer(self.session, self.ws_url)
        self.peers.append(p)
        return p

    async def deck(self, name: str, agents: list[dict[str, Any]] | None = None, **extra: Any) -> Peer:
        p = self.peer()
        reg = await p.connect(
            kind="deck", protocol=2, instance_name=name, deck_id=extra.pop("deck_id", f"id-{name}"),
            key=extra.pop("key", ""), secret=extra.pop("secret", ""),
            capabilities={"agents": agents if agents is not None else [], "max_concurrent": 4, "version": "test"},
            **extra,
        )
        assert reg.get("ok"), reg
        return p


def make_config(tmp_path, **overrides: Any) -> BotPortConfig:
    cfg = BotPortConfig()
    cfg.server.data_dir = str(tmp_path / "data")
    cfg.server.ws_heartbeat_seconds = 0  # no pings in tests
    cfg.server.register_timeout_seconds = 3
    cfg.concerns.reconnect_grace_seconds = 60
    cfg.concerns.timeout_check_interval_seconds = 3600  # tests drive timeouts directly
    auth = overrides.pop("auth", None)
    if auth:
        cfg.auth.enabled = True
        cfg.auth.keys = [AuthKeyEntry(**k) for k in auth]
    for section, values in overrides.items():
        for k, v in values.items():
            setattr(getattr(cfg, section), k, v)
    return cfg


@pytest.fixture
async def make_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTPORT_WORKSPACE", str(tmp_path / "ws"))
    created: list[Hub] = []

    async def _make(**overrides: Any) -> Hub:
        cfg = make_config(tmp_path, **overrides)
        set_config(cfg)
        server = BotPortServer(cfg)
        await server.store._ensure_db()
        app = server.create_app()
        ts = TestServer(app)
        await ts.start_server()
        session = aiohttp.ClientSession()
        hub = Hub(server, ts, session)
        created.append(hub)
        return hub

    yield _make

    for hub in created:
        for p in hub.peers:
            await p.close()
        await hub.session.close()
        await hub.server.shutdown()
        await hub.test_server.close()
