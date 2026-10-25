"""Outbound agent links — Flight Deck connects out to a system's WebSocket.

Some systems won't take connections from agents: an orchestrator that hands
agents tasks wants each agent to dial in, so one running at home behind a
router needs no public address. Such a link (``direction = "outbound"``)
names the system's address (``wss://…``) and the key it issued for this agent;
Flight Deck opens the socket with ``Authorization: Bearer <key>`` and keeps it
open — WebSocket pings every 30 s, no pong within 10 s is a drop — and after a
drop dials again: in 1 s, then 2, 5, 10, 30, and every 60 s while it fails
(a connection that was up starts the waits afresh). It dials only while the
link's agent is running — so a system never gets a connection with nobody to
do its tasks (a deck that just started brings its agents up first).

On that socket it is the same conversation as /link/ws (agent_link.py): the
system sends ``message`` frames and gets accepted / state / reply / file /
push frames back. Flight Deck only says who it is first, on every connect:

  {"type":"hello","link":"<link name>","agent":"<agent name>"|null,"framework":"captain",
   "version":"<captain version>","model":"<provider>/<model>"|null,"runs_local":true|false|null}

(``runs_local``: the agent's model answers from this machine or its network —
an Ollama model that isn't a ``:cloud`` one, or any endpoint on a loopback or
private address; null when the agent isn't running.) A turn running when the
socket drops carries on, and its answer comes on the next connection (within
10 min, as on /link/ws).

The key is sealed at rest (fd_crypto) and sent only to that address, which is
checked like a webhook's (agent_link_http.address_problem): wss:// to public
hosts; ws:// and private ones too with FD_LINK_PRIVATE_ADDRESSES=1.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from captain_claw.flight_deck import agent_link as al

log = logging.getLogger(__name__)

RETRY_S = (1, 2, 5, 10, 30, 60)  # the wait before each new try after a drop or a failure
UP_A_WHILE_S = 10.0  # a connection up this long that drops: the waits start afresh
AGENT_POLL_S = 5.0   # while the link's agent isn't running, look again this often (no dialling meanwhile)
PING_S = 30.0
PONG_S = 10.0
OPEN_S = 15.0
MAX_FRAME_BYTES = 16 * 1024 * 1024

# Per outbound link: the task keeping its connection, and how that is going
# ({"state": "connecting|connected|waiting", "error": "…", "since": iso, "retry_at": iso}).
_tasks: dict[str, asyncio.Task] = {}
_status: dict[str, dict] = {}


def _now_iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset_s, UTC).isoformat()


def status(link_id: str) -> dict:
    return dict(_status.get(link_id) or {"state": "stopped", "error": "", "since": None})


def _set(link_id: str, state: str, error: str = "", retry_in: float | None = None) -> None:
    entry: dict[str, Any] = {"state": state, "error": error, "since": _now_iso()}
    if retry_in is not None:
        entry["retry_at"] = _now_iso(retry_in)
    _status[link_id] = entry


# ── Who's calling: the hello ──────────────────────────────────────────


def runs_local(provider: str, model: str, base_url: str) -> bool | None:
    """Does this model answer from this machine or its network? None when the
    provider isn't known."""
    if not provider:
        return None
    if model.endswith(":cloud") or model.endswith("-cloud"):
        return False  # an Ollama cloud model
    url = base_url or ("http://127.0.0.1:11434" if provider == "ollama" else "")
    if not url:
        return False  # a hosted provider's API
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _model_config(slug: str) -> dict:
    """The ``model`` section of agent *slug*'s config.yaml, {} when unreadable."""
    import yaml

    from captain_claw.flight_deck.server import DATA_DIR

    try:
        data = yaml.safe_load((DATA_DIR / slug / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, dict) else {}


def hello(link: dict) -> dict:
    """The frame Flight Deck opens each connection with."""
    from captain_claw import __version__

    agent = al._link_agent(str(link["user_id"]), str(link.get("agent_slug") or ""))
    frame: dict[str, Any] = {"type": "hello", "link": link.get("name", ""), "agent": None, "framework": "captain",
                             "version": __version__, "model": None, "runs_local": None}
    if agent is not None:
        cfg = _model_config(str(agent.get("slug") or ""))
        provider, model = str(cfg.get("provider") or ""), str(cfg.get("model") or "")
        frame.update(agent=agent.get("name") or agent.get("slug"),
                     model=f"{provider}/{model}" if provider and model else (model or None),
                     runs_local=runs_local(provider, model, str(cfg.get("base_url") or "")))
    return frame


# ── The connection ────────────────────────────────────────────────────


class _ClientSocket:
    """A ``websockets`` client connection with the Starlette WebSocket calls
    agent_link reads and writes it through."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send_text(self, text: str) -> None:
        await self._ws.send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code, reason)

    async def receive(self) -> dict:
        import websockets

        try:
            data = await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            return {"type": "websocket.disconnect", "code": exc.rcvd.code if exc.rcvd else 1006}
        if isinstance(data, bytes):
            return {"type": "websocket.receive", "bytes": data}
        return {"type": "websocket.receive", "text": data}


async def _connect_once(link: dict) -> tuple[str, bool]:
    """One connection to the link's system, for as long as it lasts: why it
    ended, and whether it was up at all."""
    import websockets

    from captain_claw.flight_deck import fd_crypto
    from captain_claw.flight_deck.agent_link_http import address_problem
    from captain_claw.flight_deck.auth import get_db

    url = str(link.get("remote_url") or "")
    problem = await address_problem(url, secure="wss", plain="ws") if url else "no address"
    if problem:
        return problem, False
    try:
        key = str(fd_crypto.unseal(str(link.get("remote_key") or "")) or "")
    except fd_crypto.CryptoUnavailable as exc:
        return f"the key can't be read here ({exc})", False
    user = await get_db().get_user_by_id(link["user_id"])
    if user is None:
        return "its owner is gone", False
    _set(link["id"], "connecting")
    try:
        async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {key}"} if key else None,
            ping_interval=PING_S, ping_timeout=PONG_S, open_timeout=OPEN_S, close_timeout=5,
            max_size=MAX_FRAME_BYTES, user_agent_header="Captain-Agent-Link/1",
        ) as ws:
            _set(link["id"], "connected")
            log.info("agent link %s: connected out to %s", link["id"], urllib.parse.urlsplit(url).hostname)
            conn = al._LinkConn(_ClientSocket(ws), user, link)  # type: ignore[arg-type]
            await conn.send_json(hello(link))
            code = await al.serve(conn)
            return f"the connection closed (code {code})", True
    except websockets.InvalidStatus as exc:
        status_code = exc.response.status_code
        if status_code in (401, 403):
            return f"the system refused the key (HTTP {status_code})", False
        return f"the system answered HTTP {status_code}", False
    except (OSError, TimeoutError, websockets.WebSocketException) as exc:
        return f"can't connect ({type(exc).__name__})", False


async def _run(link_id: str) -> None:
    """Keep the link's connection open until it stops being outbound (or is gone)."""
    from captain_claw.flight_deck.auth import get_db

    attempt = 0
    quick = 0  # connections in a row that closed within UP_A_WHILE_S
    while True:
        link = await get_db().get_agent_link(link_id)
        if link is None or link.get("direction") != "outbound":
            _status.pop(link_id, None)
            return
        if al._link_agent(str(link["user_id"]), str(link.get("agent_slug") or "")) is None:
            slug = link.get("agent_slug") or ""
            _set(link_id, "waiting", f'agent "{slug}" isn\'t running' if slug else "no agent is running",
                 retry_in=AGENT_POLL_S)
            await asyncio.sleep(AGENT_POLL_S)
            continue
        opened = time.monotonic()
        try:
            why, was_up = await _connect_once(link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the connector die
            log.warning("agent link %s: connector error: %s", link_id, exc)
            why, was_up = f"error ({type(exc).__name__})", False
        if was_up:
            # A connection that worked: dial again at once — unless they keep
            # closing within seconds, then the waits grow (1, 2, then 5 s).
            quick = quick + 1 if time.monotonic() - opened < UP_A_WHILE_S else 0
            attempt = min(max(quick - 1, 0), 2)
        delay = RETRY_S[min(attempt, len(RETRY_S) - 1)]
        attempt += 1
        _set(link_id, "waiting", why, retry_in=delay)
        log.info("agent link %s: %s; trying again in %ss", link_id, why, delay)
        await asyncio.sleep(delay)


# ── Starting and stopping ─────────────────────────────────────────────


def ensure(link_id: str) -> None:
    """Keep this outbound link connected (a no-op when it already is)."""
    task = _tasks.get(link_id)
    if task is None or task.done():
        _set(link_id, "connecting")
        _tasks[link_id] = asyncio.create_task(_run(link_id))


async def stop(link_id: str) -> None:
    task = _tasks.pop(link_id, None)
    _status.pop(link_id, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass


async def restart(link_id: str) -> None:
    """After a change to its address, key or direction: dial afresh (or stop)."""
    from captain_claw.flight_deck.auth import get_db

    await stop(link_id)
    link = await get_db().get_agent_link(link_id)
    if link is not None and link.get("direction") == "outbound":
        ensure(link_id)


async def start_all() -> int:
    """At start-up: connect every outbound link; how many."""
    from captain_claw.flight_deck.auth import get_db

    links = await get_db().list_outbound_links()
    for link in links:
        ensure(link["id"])
    return len(links)


async def stop_all() -> None:
    for link_id in list(_tasks):
        await stop(link_id)
