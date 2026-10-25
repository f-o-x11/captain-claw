"""The /fd/<thing>/{host}/{port}/... proxies fill in an agent's own web token
when the caller passes none. They look the agent up by port, so the token may
only go to an address this deck itself uses for that agent (loopback, or the
container's names on a shared Docker network) — never to a host the caller
named. A token the caller supplies is forwarded as given, to any host.
"""

from __future__ import annotations

import types
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from captain_claw.flight_deck import server
from captain_claw.flight_deck.auth import create_access_token, set_auth_db
from captain_claw.flight_deck.db import FlightDeckDB

REMOTE = ("203.0.113.9", 40001)
ALICE_PORT = 24111   # Alice's process agent
BOX_PORT = 24112     # Alice's Docker agent "box"
WS_FD = "ws://localhost:25080"
_RealAsyncClient = httpx.AsyncClient  # the routes' client is patched; ours is not


def _client() -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.ASGITransport(app=server.app, client=REMOTE),
                            base_url="http://localhost:25080")


def _bearer(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user['id'], user.get('role', 'user'))}"}


def _box(owner: str) -> types.SimpleNamespace:
    """A running container of this deck, on a user-defined network."""
    labels = {server.CONTAINER_LABEL: "true", server.OWNER_LABEL: owner,
              server.DECK_LABEL: server._deck_id(), "flight-deck.agent-name": "box",
              "flight-deck.web-port": str(BOX_PORT), "flight-deck.web-auth": "tok-box"}
    attrs = {"Config": {"Hostname": "box-host"},
             "NetworkSettings": {"Ports": {}, "Networks": {
                 "fd-net": {"Aliases": ["box-alias"], "IPAddress": "172.18.0.5"}}}}
    return types.SimpleNamespace(name="box", labels=labels, status="running", attrs=attrs)


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch):
    from captain_claw.flight_deck import auth as fd_auth

    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "users.db")
    await db.init()
    set_auth_db(db)
    alice = await db.create_user("alice@x.test", "h", "Alice")
    bob = await db.create_user("bob@x.test", "h", "Bob")
    admin = await db.create_user("admin@x.test", "h", "Admin", role="admin")
    registry = {"alice-agent": {"name": "Planner", "web_port": ALICE_PORT,
                                "web_auth": "tok-alice", "owner": alice["id"]}}
    (tmp_path / "fd-data").mkdir()
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "fd-data")
    containers = [_box(alice["id"])]
    fake_docker = types.SimpleNamespace(containers=types.SimpleNamespace(
        list=lambda all=False, filters=None: list(containers)))
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setattr(server, "_load_process_registry", lambda: registry)
    monkeypatch.setattr(server, "_process_is_alive", lambda slug: True)
    monkeypatch.setattr(server, "get_docker", lambda: fake_docker)
    try:
        yield types.SimpleNamespace(db=db, alice=alice, bob=bob, admin=admin, registry=registry)
    finally:
        await db.close()
        fd_auth._db = prev


@pytest.fixture
def upstream(monkeypatch) -> list[httpx.URL]:
    """Every request a proxy route sends to an agent; each is answered 200 {}."""
    sent: list[httpx.URL] = []

    class _Upstream:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, *, params=None, **kwargs):
            u = httpx.URL(url)
            sent.append(u.copy_merge_params(params) if params else u)
            return httpx.Response(200, json={}, request=httpx.Request(method, url))

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

        async def post(self, url, **kwargs):
            return await self.request("POST", url, **kwargs)

        async def delete(self, url, **kwargs):
            return await self.request("DELETE", url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Upstream)
    return sent


# One of each shape: inline routes (query string built by hand, quoted, or none
# taken from the caller at all) and the shared helpers.
ROUTES = [
    ("GET", "/fd/agent-datastore/{host}/{port}/tables"),
    ("GET", "/fd/agent-files/{host}/{port}"),
    ("GET", "/fd/agent-file-download/{host}/{port}?path=notes.txt"),
    ("GET", "/fd/agent-intentions/{host}/{port}"),          # _proxy_agent_intentions
    ("GET", "/fd/agent-skills/{host}/{port}"),              # _proxy_get_json
    ("POST", "/fd/orchestrator/{host}/{port}/prepare-tasks"),  # takes no token
]


async def _call(user: dict, method: str, path: str) -> httpx.Response:
    async with _client() as c:
        return await c.request(method, path, headers=_bearer(user),
                               json={} if method == "POST" else None)


def _only(sent: list[httpx.URL]) -> tuple[str, str | None]:
    (u,) = sent
    return u.host, u.params.get("token")


# ── (a) a host the caller named never receives the resolved token ─────────


@pytest.mark.parametrize("method,route", ROUTES)
@pytest.mark.parametrize("host", ["evil.example", "localhost@evil.example"])
async def test_resolved_token_never_goes_to_a_foreign_host(deck, upstream, method, route, host):
    r = await _call(deck.alice, method, route.format(host=host, port=ALICE_PORT))
    assert r.status_code == 200, r.text
    assert _only(upstream) == ("evil.example", None)


async def test_admin_gets_no_token_to_a_foreign_host_either(deck, upstream):
    r = await _call(deck.admin, "GET", f"/fd/agent-datastore/evil.example/{ALICE_PORT}/tables")
    assert r.status_code == 200
    assert _only(upstream) == ("evil.example", None)


@pytest.mark.parametrize("helper,args", [
    (server._proxy_get_json, ("/api/skills",)),
    (server._proxy_post_json, ("/api/x", "", b"{}")),
    (server._proxy_delete_json, ("/api/x", "")),
])
@pytest.mark.parametrize("host,token", [("evil.example", None), ("localhost", "tok-alice")])
async def test_proxy_helpers(deck, upstream, helper, args, host, token):
    await helper(host, ALICE_PORT, *args)
    assert _only(upstream) == (host, token)


# ── (b) the agent's own address still gets it ─────────────────────────────


@pytest.mark.parametrize("method,route", ROUTES)
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
async def test_loopback_still_carries_the_resolved_token(deck, upstream, method, route, host):
    r = await _call(deck.alice, method, route.format(host=host, port=ALICE_PORT))
    assert r.status_code == 200, r.text
    assert _only(upstream) == (host, "tok-alice")


def test_loopback_spellings_and_internal_callers(deck):
    for host in ("localhost", "LOCALHOST", "127.0.0.1", "::1", "[::1]"):
        assert server._resolve_agent_auth(ALICE_PORT, host=host) == "tok-alice", host
    # "localhost." and "127.0.0.1." are DNS names the request would resolve.
    for host in ("evil.example", "", "0.0.0.0", "127.0.0.2", "localhost:1@evil.example", "box",
                 "localhost.", "127.0.0.1."):
        assert server._resolve_agent_auth(ALICE_PORT, host=host) == "", host
    # Internal callers (botport_link, flows, bridges) pass no host: unchanged.
    assert server._resolve_agent_auth(ALICE_PORT) == "tok-alice"
    assert server._resolve_agent_auth(BOX_PORT) == "tok-box"


# ── (c) a token the caller supplies is forwarded unchanged, to any host ───


@pytest.mark.parametrize("host", ["evil.example", "localhost"])
async def test_explicit_token_is_forwarded_unchanged(deck, upstream, host):
    r = await _call(deck.alice, "GET",
                    f"/fd/agent-datastore/{host}/{ALICE_PORT}/tables?token=my-own")
    assert r.status_code == 200
    assert _only(upstream) == (host, "my-own")


# ── (d) Docker agents: loopback and the container's own names ─────────────


def test_docker_agent_hosts(deck):
    for host in ("localhost", "127.0.0.1", "box", "BOX", "box-host", "box-alias", "172.18.0.5"):
        assert server._resolve_agent_auth(BOX_PORT, host=host) == "tok-box", host
    for host in ("evil.example", "other-box", "172.18.0.6"):
        assert server._resolve_agent_auth(BOX_PORT, host=host) == "", host


def test_every_host_the_fleet_reports_gets_the_token(deck):
    fleet = server._fleet_snapshot()
    assert {(a["kind"], a["port"]) for a in fleet} == {
        ("docker", BOX_PORT), ("process", ALICE_PORT)}
    for a in fleet:
        assert server._resolve_agent_auth(a["port"], host=a["host"]), a


@pytest.mark.parametrize("host,token", [
    ("box", "tok-box"), ("localhost", "tok-box"), ("evil.example", None)])
async def test_docker_agent_through_the_proxy(deck, upstream, host, token):
    r = await _call(deck.alice, "GET", f"/fd/agent-datastore/{host}/{BOX_PORT}/tables")
    assert r.status_code == 200
    assert _only(upstream) == (host, token)


@pytest.mark.parametrize("host,body_auth,token", [
    ("evil.example", "", ""), ("localhost", "", "tok-alice"), ("evil.example", "my-own", "my-own")])
async def test_public_file_share_snapshot(deck, monkeypatch, host, body_auth, token):
    """/fd/agent-file-share/{host}/{port} (public_file_shares) copies the file
    from the agent with the same rule."""
    from fastapi import HTTPException

    from captain_claw.flight_deck import public_file_shares as pfs

    seen: list[tuple[str, str]] = []

    async def fake_snapshot(host, port, auth, path, token):
        seen.append((host, auth))
        raise HTTPException(404, "stop here")

    monkeypatch.setattr(pfs, "_snapshot_from_agent", fake_snapshot)
    # The wiring server.py installs (another test module may have replaced it).
    monkeypatch.setattr(pfs, "_resolve_auth",
                        lambda port, host=None: server._resolve_agent_auth(port, host=host))
    async with _client() as c:
        r = await c.post(f"/fd/agent-file-share/{host}/{ALICE_PORT}", headers=_bearer(deck.alice),
                         json={"path": "notes.txt", "agent_ref": "proc-alice-agent",
                               "agent_auth": body_auth})
    assert r.status_code == 404, r.text
    assert seen == [(host, token)]


# ── (e) the chat WebSocket proxy: owner rule and host rule ────────────────


@pytest.fixture
def agent_ws(deck, monkeypatch) -> list[str]:
    """The agent URLs the WS proxy dials; each dial fails at once (not retried)."""
    import websockets

    dialled: list[str] = []

    async def fake_connect(url, **kwargs):
        dialled.append(url)
        raise ValueError("no agent in tests")

    monkeypatch.setattr(websockets, "connect", fake_connect)
    return dialled


def _ws(user: dict, host: str, port: int, extra: str = "") -> dict:
    fd_token = create_access_token(user["id"], user.get("role", "user"))
    url = f"{WS_FD}/fd/agent-ws/{host}/{port}?fd_token={fd_token}{extra}"
    with TestClient(server.app).websocket_connect(url) as s:
        return s.receive_json()


def _ws_token(url: str) -> str | None:
    return httpx.URL(url.replace("ws://", "http://", 1)).params.get("token")


@pytest.mark.parametrize("extra", ["", "&agent=proc-alice-agent"])
def test_ws_foreign_host_gets_no_token(agent_ws, deck, extra):
    frame = _ws(deck.alice, "evil.example", ALICE_PORT, extra)
    assert frame["code"] == "agent_unreachable"
    (url,) = agent_ws
    assert url.startswith(f"ws://evil.example:{ALICE_PORT}/ws") and _ws_token(url) is None


@pytest.mark.parametrize("extra", ["", "&agent=proc-alice-agent"])
def test_ws_loopback_carries_the_token(agent_ws, deck, extra):
    _ws(deck.alice, "localhost", ALICE_PORT, extra)
    (url,) = agent_ws
    assert url.startswith(f"ws://localhost:{ALICE_PORT}/ws") and _ws_token(url) == "tok-alice"


def test_ws_docker_agent_by_container_name(agent_ws, deck):
    _ws(deck.alice, "box", BOX_PORT)
    (url,) = agent_ws
    assert _ws_token(url) == "tok-box"


def test_ws_explicit_token_is_forwarded_unchanged(agent_ws, deck):
    _ws(deck.alice, "evil.example", ALICE_PORT, "&token=my-own")
    (url,) = agent_ws
    assert _ws_token(url) == "my-own"


def test_ws_refuses_another_users_agent(agent_ws, deck):
    with pytest.raises(WebSocketDisconnect) as e:
        _ws(deck.bob, "localhost", ALICE_PORT)
    assert e.value.code == 4403 and agent_ws == []
    # An admin may reach it (and still sends no token to a foreign host).
    _ws(deck.admin, "evil.example", ALICE_PORT)
    (url,) = agent_ws
    assert _ws_token(url) is None


def test_ws_refuses_a_missing_jwt(agent_ws, deck):
    url = f"{WS_FD}/fd/agent-ws/localhost/{ALICE_PORT}"
    with TestClient(server.app).websocket_connect(url) as s:
        with pytest.raises(WebSocketDisconnect) as e:
            s.receive_json()
    assert e.value.code == 4001 and agent_ws == []


# ── (f) _agent_record_by_auth still finds the agent ───────────────────────


def test_agent_record_by_auth(deck):
    rec = server._agent_record_by_auth("tok-alice")
    assert rec == {"kind": "process", "slug": "alice-agent", "port": ALICE_PORT,
                   "auth": "tok-alice", "owner": deck.alice["id"]}
    rec = server._agent_record_by_auth("tok-box")
    assert (rec["kind"], rec["slug"], rec["port"], rec["owner"]) == (
        "docker", "box", BOX_PORT, deck.alice["id"])
    for bad in ("", "tok-alic", "tok-alice2", "tök-alice", None, 123):
        assert server._agent_record_by_auth(bad) is None, bad
    # A malformed registry entry (non-str / missing token) is skipped, not fatal.
    deck.registry["broken"] = {"web_port": 1, "web_auth": 42}
    deck.registry["empty"] = {"web_port": 2}
    assert server._agent_record_by_auth("tok-alice")["slug"] == "alice-agent"
    assert server._agent_record_by_auth("tok-box")["slug"] == "box"


# ── Same rule outside the proxies: flows and the glasses bridge ─────


def test_a_flow_origin_on_another_host_gets_no_token():
    from captain_claw.flight_deck.flow_runner import FlowRunner

    fr = FlowRunner(store=None, get_agents=lambda: [], resolve_auth=lambda port: "tok-by-port",
                    fd_self_base="http://localhost:1")
    far = fr._explicit_origin({"origin_host": "evil.example", "origin_port": 24999}, [])
    near = fr._explicit_origin({"origin_host": "localhost", "origin_port": 24999}, [])
    assert far["host"] == "evil.example" and far["auth"] == ""
    assert near["auth"] == "tok-by-port"


async def test_a_glasses_channel_bound_elsewhere_gets_no_token(deck):
    from captain_claw.flight_deck.glasses_bridge import _ChannelState, _resolve_channel_agent_auth

    ch = _ChannelState(channel_id="c1")
    ch.bound_port = ALICE_PORT
    ch.bound_host = "evil.example"
    assert await _resolve_channel_agent_auth(ch) == ""
    ch.bound_host = "localhost"
    assert await _resolve_channel_agent_auth(ch) == "tok-alice"
    ch.bound_host = None  # never bound by host: the agent's own (loopback) address
    assert await _resolve_channel_agent_auth(ch) == "tok-alice"


async def test_a_run_callback_goes_with_a_token_only_to_the_agents_own_address(deck, monkeypatch):
    """A background run reports back to the agent that started it. Its source
    host comes from the request body: the token only goes to loopback."""
    import websockets

    from captain_claw.flight_deck.agent_notify import notify_source_agent

    dialed: list[str] = []

    def fake_connect(url, **kw):
        dialed.append(url)
        raise OSError("not dialing out in tests")

    monkeypatch.setattr(websockets, "connect", fake_connect)
    for host in ("evil.example", "localhost"):
        await notify_source_agent(source_host=host, source_port=ALICE_PORT, origin={}, kind="Basna",
                                  title="t", run_ref="r", ok=True, summary="s")
    assert dialed[0] == f"ws://evil.example:{ALICE_PORT}/ws"
    assert dialed[1] == f"ws://localhost:{ALICE_PORT}/ws?token=tok-alice"
