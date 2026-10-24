"""BotPort server - agent-to-agent task routing hub.

Two kinds of connection share one WebSocket endpoint (``/ws``):

* ``agent`` (protocol 1, legacy) — one Captain Claw agent per socket.
* ``deck`` (protocol 2) — a Flight Deck that exposes some of its agents and
  proxies concerns to them. Decks keep a stable identity (``deck:<name>``)
  across reconnects, so a network blip neither loses a result nor fails work
  in progress: results for an offline requester wait in an outbox, and a deck
  that drops mid-task gets ``concerns.reconnect_grace_seconds`` to come back.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import re
import signal
import ssl
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from botport import __version__
from botport.auth import FailureLimiter, match_credentials
from botport.concern_manager import ConcernManager
from botport.config import BotPortConfig, set_config
from botport.connection_manager import CLOSE_SUPERSEDED, ConnectionManager
from botport.models import Concern
from botport.protocol import (
    ActivityMessage,
    BaseMessage,
    CloseConcernMessage,
    ConcernAckMessage,
    ConcernClosedMessage,
    ConcernResultMessage,
    ConcernSubmitMessage,
    ContextReplyMessage,
    ContextRequestMessage,
    DeckAgentsMessage,
    DispatchMessage,
    FileListMessage,
    FileListResponseMessage,
    FileRequestMessage,
    FileResponseMessage,
    FileUploadAckMessage,
    FileUploadMessage,
    FollowUpMessage,
    HeartbeatAckMessage,
    HeartbeatMessage,
    NetworkMessage,
    ProgressMessage,
    RegisteredMessage,
    RegisterMessage,
    ResultAckMessage,
    ResultMessage,
    TimeoutNoticeMessage,
    parse_raw,
)
from botport.registry import Registry
from botport.router import RouteError, Router
from botport.store import BotPortStore
from botport.swarm.file_manager import FileManager, decode_file, encode_file
from botport.web_security import client_addr, security_middleware

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 2

# Deck names travel in "Agent@deck" addresses and URLs.
DECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

# Close codes (4000-4999 are application-defined).
CLOSE_AUTH_FAILED = 4401
CLOSE_REGISTER_TIMEOUT = 4408
CLOSE_REJECTED = 4409

# Relay at most this many progress frames per concern per second.
_PROGRESS_MIN_INTERVAL = 0.25
_SWARM_ORIGIN = "__swarm__"


class BotPortServer:
    """Main BotPort server orchestrating all components."""

    def __init__(self, config: BotPortConfig) -> None:
        self.config = config
        self.store = BotPortStore(config.data_dir / "botport.db")
        self.connections = ConnectionManager()
        self.registry = Registry(self.connections)
        self.router = Router(self.registry)
        self.concerns = ConcernManager(self.store)
        self.concerns.on_expired = self._on_concern_expired
        self.file_manager = FileManager(_workspace_root(config))
        self.auth_limiter = FailureLimiter()
        self._swarm_store_initialized = False
        self.swarm_engine: Any = None
        self.swarm_scheduler: Any = None
        self.started_at = time.time()
        # Results waiting for an offline (or not yet acknowledging) deck:
        # originator instance_id -> concern_id -> (message, expires_monotonic)
        self._outbox: dict[str, dict[str, tuple[ConcernResultMessage, float]]] = {}
        # Dispatches not yet acknowledged by progress/result (resent once if
        # the assignee reconnects without having seen them).
        self._pending_dispatch: dict[str, DispatchMessage] = {}
        self._last_progress: dict[str, float] = {}
        self._pending_progress: dict[str, ProgressMessage] = {}
        self._network_task: asyncio.Task[None] | None = None
        self._roster_gen = 0
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self.dashboard_sockets: set[web.WebSocketResponse] = set()
        self.counters: dict[str, int] = {
            "registrations": 0, "auth_failures": 0, "concerns_routed": 0,
            "concerns_rejected": 0, "results_relayed": 0, "results_queued": 0,
            "timeouts": 0, "superseded": 0,
        }

    def create_app(self) -> web.Application:
        max_bytes = max(1, self.config.server.max_message_mb) * 1024 * 1024
        app = web.Application(middlewares=[security_middleware], client_max_size=max_bytes)

        # Agent / deck WebSocket endpoint.
        app.router.add_get("/ws", self._ws_handler)
        # Liveness probe (no data beyond version and uptime).
        app.router.add_get("/healthz", self._healthz)

        # Dashboard.
        if self.config.server.dashboard_enabled:
            from botport.dashboard.routes import setup_dashboard_routes
            from botport.dashboard.swarm_routes import setup_swarm_routes
            setup_dashboard_routes(app, self)
            setup_swarm_routes(app, self)

        return app

    @property
    def swarm_store(self):
        """Lazy accessor for the swarm store (initialized with DB)."""
        return self.store.swarm

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── Health ───────────────────────────────────────────────────

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "service": "botport",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "uptime_seconds": int(time.time() - self.started_at),
        })

    # ── WebSocket handler ────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        cfg = self.config.server
        addr = client_addr(request, self.config)

        ws = web.WebSocketResponse(
            heartbeat=cfg.ws_heartbeat_seconds if cfg.ws_heartbeat_seconds > 0 else None,
            max_msg_size=max(1, cfg.max_message_mb) * 1024 * 1024,
        )
        await ws.prepare(request)

        instance_id: str | None = None
        try:
            # The time limit covers waiting for the register frame only — not
            # what follows (reconcile, re-dispatch, delivering queued results).
            try:
                reg = await asyncio.wait_for(
                    self._await_register(ws), timeout=max(1.0, cfg.register_timeout_seconds),
                )
            except asyncio.TimeoutError:
                await self.connections.send_on(ws, RegisteredMessage(
                    ok=False, error="No 'register' message received in time",
                ))
                await ws.close(code=CLOSE_REGISTER_TIMEOUT, message=b"register timeout")
                return ws
            if reg is None:
                return ws

            instance_id = await self._handle_register(ws, reg, addr)
            if not instance_id:
                if not ws.closed:
                    await ws.close(code=CLOSE_REJECTED, message=b"registration rejected")
                return ws
            if reg.kind == "deck":
                await self._after_deck_register(instance_id, reg)

            # Main message loop.
            async for raw_msg in ws:
                if raw_msg.type == web.WSMsgType.TEXT:
                    if not self.connections.is_current(instance_id, ws):
                        break  # superseded by a newer socket of the same deck
                    try:
                        msg = parse_raw(raw_msg.data)
                    except ValueError as exc:
                        log.warning("Invalid message from %s: %s", self._name(instance_id), exc)
                        continue
                    try:
                        await self._handle_message(instance_id, msg)
                    except Exception:
                        log.exception("Handler error for %s (%s)", self._name(instance_id), msg.type)

                elif raw_msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("WebSocket error from %s: %s", addr, exc)
        finally:
            if instance_id:
                await self._handle_disconnect(instance_id, ws)
            else:
                self.connections.forget_socket(ws)

        return ws

    async def _await_register(self, ws: web.WebSocketResponse) -> RegisterMessage | None:
        """Read until a ``register`` frame (None if the socket closes first)."""
        async for raw_msg in ws:
            if raw_msg.type == web.WSMsgType.TEXT:
                try:
                    msg = parse_raw(raw_msg.data)
                except ValueError as exc:
                    await self.connections.send_on(ws, RegisteredMessage(ok=False, error=str(exc)))
                    continue
                if not isinstance(msg, RegisterMessage):
                    await self.connections.send_on(ws, RegisteredMessage(
                        ok=False, error="First message must be 'register'",
                    ))
                    continue
                return msg
            if raw_msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                return None
        return None

    async def _after_deck_register(self, instance_id: str, msg: RegisterMessage) -> None:
        """A deck is registered (and owns its socket): line up state, send its
        roster, then deliver queued results without blocking its read loop."""
        try:
            await self._reconcile_deck(instance_id, msg)
        except Exception:
            log.exception("Reconcile failed for %s", self._name(instance_id))
        await self.connections.send_to(instance_id, self.network_for(instance_id))
        self._spawn(self._flush_outbox(instance_id))

    # ── Registration ─────────────────────────────────────────────

    async def _reject(self, ws: web.WebSocketResponse, error: str, *, retry: bool, code: int) -> None:
        await self.connections.send_on(ws, RegisteredMessage(ok=False, error=error, retry=retry))
        try:
            await ws.close(code=code, message=error[:120].encode())
        except Exception:
            pass

    async def _handle_register(
        self, ws: web.WebSocketResponse, msg: RegisterMessage, addr: str,
    ) -> str | None:
        """Handle registration. Returns instance_id or None."""
        entry = None
        if self.config.auth.enabled:
            # Failures count per address AND key: guessing one key's secret
            # gets that key blocked from that address, while another deck
            # behind the same NAT (its own key) is unaffected. Unknown keys
            # share one bucket per address.
            known = any(k.key.strip() == (msg.key or "").strip() for k in self.config.auth.keys)
            bucket = f"{addr}|{(msg.key or '').strip() if known else '?'}"
            if self.auth_limiter.blocked(bucket):
                await self._reject(ws, "Too many failed registrations from this address; try again later",
                                   retry=True, code=CLOSE_AUTH_FAILED)
                return None
            entry = match_credentials(msg.key, msg.secret)
            if entry is None:
                self.counters["auth_failures"] += 1
                self.auth_limiter.record_failure(bucket)
                log.warning("Auth failed for %s %r from %s", msg.kind or "agent", msg.instance_name, addr)
                await self._reject(ws, "Authentication failed", retry=False, code=CLOSE_AUTH_FAILED)
                return None
            self.auth_limiter.record_success(bucket)

        bound = entry.instance.strip() if entry else ""
        if msg.kind == "deck":
            if self.config.auth.enabled and not bound:
                await self._reject(
                    ws, "This key is not bound to a deck name: set `instance:` for it in the hub's auth.keys",
                    retry=False, code=CLOSE_REJECTED,
                )
                return None
            return await self._register_deck(ws, msg, addr, bound)
        return await self._register_agent(ws, msg, addr, bound)

    async def _register_agent(
        self, ws: web.WebSocketResponse, msg: RegisterMessage, addr: str, bound_name: str = "",
    ) -> str | None:
        # A key that names an instance names it for agents too; and no agent
        # may take a deck's name (decks trust each other by name).
        name = (bound_name or msg.instance_name or "").strip()
        if name and self.connections.find_deck(name) is not None:
            await self._reject(ws, f"'{name}' is the name of a Flight Deck on this hub",
                               retry=False, code=CLOSE_REJECTED)
            return None
        instance_id = str(uuid.uuid4())
        info, _ = await self.connections.register(ws, msg, instance_id, name=name, remote_addr=addr)
        info.max_concurrent = min(info.max_concurrent, max(1, self.config.concerns.max_concurrent_per_instance))
        await self.connections.send_on(ws, RegisteredMessage(
            instance_id=instance_id,
            botport_version=__version__,
            ok=True,
            protocol=PROTOCOL_VERSION,
            heartbeat_seconds=self.config.server.ws_heartbeat_seconds,
        ))
        self.counters["registrations"] += 1
        log.info(
            "Agent registered: %s (id=%s, personas=%d, tools=%d) from %s",
            info.name, instance_id[:8], len(info.personas), len(info.tools), addr,
        )
        self._dashboard_event({"type": "instance_connected", "instance": info.to_dict()})
        self._schedule_network()
        return instance_id

    async def _register_deck(
        self, ws: web.WebSocketResponse, msg: RegisterMessage, addr: str, bound_name: str,
    ) -> str | None:
        name = bound_name or msg.instance_name.strip()
        if not DECK_NAME_RE.match(name):
            await self._reject(
                ws,
                f"Invalid deck name {name!r}: use letters, digits, '-', '_' or '.' (max 63, no spaces or '@')",
                retry=False, code=CLOSE_REJECTED,
            )
            return None
        if bound_name and msg.instance_name.strip() and msg.instance_name.strip().lower() != bound_name.lower():
            log.info("Deck asked for name %r; its key binds it to %r", msg.instance_name, bound_name)

        instance_id = f"deck:{name.lower()}"
        existing = self.connections.get_instance(instance_id)
        live_ws = self.connections.get_ws(instance_id)
        if (
            live_ws is not None and not live_ws.closed
            and existing is not None and existing.deck_id
            and existing.deck_id != (msg.deck_id or "")
        ):
            # Two different Flight Decks claim one name. Refuse the newcomer
            # (it retries); if the old socket is really dead, the WS heartbeat
            # drops it within a couple of intervals and the retry succeeds.
            await self._reject(
                ws, f"A different Flight Deck is already connected as '{name}'",
                retry=True, code=CLOSE_REJECTED,
            )
            return None

        resumed = existing is not None
        info, old_ws = await self.connections.register(ws, msg, instance_id, name=name, remote_addr=addr)
        info.max_concurrent = min(info.max_concurrent, max(1, self.config.concerns.max_concurrent_per_instance))
        # The load is the hub's own bookkeeping: what is really assigned there.
        info.active_concerns = self._counted_for(instance_id)
        if old_ws is not None and not old_ws.closed:
            self.counters["superseded"] += 1
            self._spawn(old_ws.close(code=CLOSE_SUPERSEDED, message=b"superseded by a new connection"))

        await self.connections.send_on(ws, RegisteredMessage(
            instance_id=instance_id,
            botport_version=__version__,
            ok=True,
            protocol=PROTOCOL_VERSION,
            deck_name=name,
            resumed=resumed,
            heartbeat_seconds=self.config.server.ws_heartbeat_seconds,
        ))
        self.counters["registrations"] += 1
        log.info(
            "Deck %s: %s (agents=%d, version=%s) from %s",
            "reconnected" if resumed else "registered", name, len(info.agents), info.version or "?", addr,
        )

        self._dashboard_event({"type": "instance_connected", "instance": info.to_dict()})
        self._schedule_network()
        return instance_id

    def _counted_for(self, instance_id: str) -> int:
        return sum(1 for c in self.concerns.get_concerns_for_instance(instance_id) if c.counted_active)

    async def _reconcile_deck(self, instance_id: str, msg: RegisterMessage) -> None:
        """Line the hub's view of a (re)connecting deck up with the deck's own.

        ``in_flight``: concerns the deck is still working on (or finished, with
        the result not yet acknowledged). ``awaiting``: concerns it submitted,
        had acked, and still waits for. ``submitted``: ones it sent but never
        saw acked — it resubmits them right after (submission is idempotent).
        """
        if int(msg.protocol or 1) < 2:
            return
        in_flight = set(msg.in_flight or [])
        awaiting = set(msg.awaiting or [])
        submitted = set(msg.submitted or [])
        keep = awaiting | submitted

        # Concerns assigned to this deck.
        known_assigned: set[str] = set()
        for concern in self.concerns.get_concerns_for_instance(instance_id):
            known_assigned.add(concern.id)
            concern.assignee_offline_since = 0.0
            if concern.id in in_flight or concern.status == "responded":
                continue  # still working on it — or already answered
            pending = self._pending_dispatch.get(concern.id)
            if pending is not None and concern.status in ("pending", "assigned"):
                # The dispatch was lost in the drop: send it again.
                log.info("Re-dispatching concern %s to %s", concern.id[:8], self._name(instance_id))
                await self.connections.send_to(instance_id, pending)
                continue
            await self._finish_failed(
                concern, f"'{self._name(instance_id)}' restarted and lost this task",
                reason="assignee_restarted", notify_assignee=False,
            )

        # Work the deck still does that the hub no longer knows about.
        for cid in in_flight - known_assigned:
            await self.connections.send_to(instance_id, ConcernClosedMessage(
                concern_id=cid, reason="unknown_concern",
            ))

        # Concerns this deck originated.
        outbox = self._outbox.get(instance_id, {})
        for concern in self.concerns.get_concerns_from_instance(instance_id):
            if concern.id in keep:
                continue
            # Nobody on the deck waits for it any more (it restarted).
            await self._close_and_release(concern, reason="originator_restarted", notify_assignee=True)
        for cid in awaiting - submitted:
            concern = self.concerns.get_concern(cid)
            if concern is None and cid not in outbox:
                await self.connections.send_to(instance_id, ConcernResultMessage(
                    concern_id=cid, ok=False,
                    error="BotPort no longer knows this request (the hub restarted). Please retry.",
                ))
        # Results it already has (it stopped waiting for them) need no resend.
        for cid in [c for c in outbox if c not in keep]:
            outbox.pop(cid, None)
        if not outbox:
            self._outbox.pop(instance_id, None)

    async def _handle_disconnect(self, instance_id: str, ws: web.WebSocketResponse) -> None:
        """Handle a socket closing (no-op for a socket that was superseded)."""
        info = await self.connections.unregister(instance_id, ws)
        if info is None:
            return
        name = info.name

        if info.is_deck:
            # Give it time to come back before failing what it was doing
            # (an already answered concern is not "in progress" any more).
            now = time.monotonic()
            assigned = [c for c in self.concerns.get_concerns_for_instance(instance_id)
                        if c.status != "responded"]
            for concern in assigned:
                concern.assignee_offline_since = now
            log.warning(
                "Deck %s disconnected (%d concern(s) in progress; %ds grace)",
                name, len(assigned), self.config.concerns.reconnect_grace_seconds,
            )
        else:
            failed = 0
            for concern in self.concerns.get_concerns_for_instance(instance_id):
                await self._finish_failed(
                    concern, f"Assigned instance '{name}' disconnected",
                    reason=f"instance {instance_id} disconnected", notify_assignee=False,
                )
                failed += 1
            # An agent never resumes its id: nobody waits for its concerns now.
            for concern in self.concerns.get_concerns_from_instance(instance_id):
                await self._close_and_release(concern, reason="originator_disconnected", notify_assignee=True)
            if failed:
                log.warning("Instance %s disconnected, failed %d concerns", name, failed)
            else:
                log.info("Instance %s disconnected", name)

        self._dashboard_event({"type": "instance_disconnected", "instance_id": instance_id})
        self._schedule_network()

    # ── Network roster ───────────────────────────────────────────

    def _schedule_network(self) -> None:
        """Push the roster to every deck soon, coalescing bursts of changes —
        and never losing one: a change while a push is under way (or waiting)
        causes one more push."""
        self._roster_gen += 1
        if self._network_task is not None and not self._network_task.done():
            return

        async def _loop() -> None:
            while True:
                gen = self._roster_gen
                await asyncio.sleep(0.3)
                await self.broadcast_network()
                if gen == self._roster_gen:
                    return

        try:
            self._network_task = asyncio.ensure_future(_loop())
        except RuntimeError:
            self._network_task = None

    def network_for(self, instance_id: str) -> NetworkMessage:
        decks: list[dict[str, Any]] = []
        agents: list[dict[str, Any]] = []
        for info in self.connections.list_instances():
            if info.id == instance_id:
                continue
            if info.is_deck:
                decks.append({
                    "name": info.name,
                    "status": info.status,
                    "version": info.version,
                    "connected_at": info.connected_at,
                    "active_concerns": info.active_concerns,
                    "max_concurrent": info.max_concurrent,
                    "agents": [a.to_dict() for a in info.agents] if info.status == "connected" else [],
                })
            elif info.status == "connected":
                agents.append({
                    "name": info.name,
                    "personas": [{"name": p.name, "description": p.description,
                                  "expertise_tags": p.expertise_tags} for p in info.personas],
                    "active_concerns": info.active_concerns,
                    "max_concurrent": info.max_concurrent,
                })
        decks.sort(key=lambda d: d["name"].lower())
        agents.sort(key=lambda a: a["name"].lower())
        return NetworkMessage(decks=decks, agents=agents)

    async def broadcast_network(self) -> None:
        for info in self.connections.list_decks():
            await self.connections.send_to(info.id, self.network_for(info.id))

    # ── Message dispatch ─────────────────────────────────────────

    async def _handle_message(self, instance_id: str, msg: BaseMessage) -> None:
        if isinstance(msg, HeartbeatMessage):
            await self._handle_heartbeat(instance_id, msg)
        elif isinstance(msg, ActivityMessage):
            self._handle_activity(instance_id, msg)
        elif isinstance(msg, ConcernSubmitMessage):
            await self._handle_concern(instance_id, msg)
        elif isinstance(msg, ProgressMessage):
            await self._handle_progress(instance_id, msg)
        elif isinstance(msg, ResultMessage):
            await self._handle_result(instance_id, msg)
        elif isinstance(msg, ResultAckMessage):
            self._handle_result_ack(instance_id, msg)
        elif isinstance(msg, DeckAgentsMessage):
            self._handle_deck_agents(instance_id, msg)
        elif isinstance(msg, FollowUpMessage):
            await self._handle_follow_up(instance_id, msg)
        elif isinstance(msg, ContextRequestMessage):
            await self._handle_context_request(instance_id, msg)
        elif isinstance(msg, ContextReplyMessage):
            await self._handle_context_reply(instance_id, msg)
        elif isinstance(msg, CloseConcernMessage):
            await self._handle_close_concern(instance_id, msg)
        elif isinstance(msg, (FileUploadMessage, FileListMessage, FileRequestMessage)):
            await self._handle_file_frame(instance_id, msg)
        else:
            log.warning("Unhandled message type from %s: %s", self._name(instance_id), msg.type)

    def _name(self, instance_id: str) -> str:
        info = self.connections.get_instance(instance_id)
        return info.name if info else instance_id[:8]

    def _is_deck_id(self, instance_id: str) -> bool:
        return instance_id.startswith("deck:")

    # ── Activity ─────────────────────────────────────────────────

    def _handle_activity(self, instance_id: str, msg: ActivityMessage) -> None:
        """Handle live activity update from an agent. Ephemeral, in-memory only."""
        self.connections.update_activity(instance_id, msg.step_type, dict(msg.data))
        self._dashboard_event({
            "type": "activity", "instance_id": instance_id,
            "step_type": msg.step_type, "data": dict(msg.data),
        })

    # ── Heartbeat ────────────────────────────────────────────────

    async def _handle_heartbeat(self, instance_id: str, msg: HeartbeatMessage) -> None:
        self.connections.update_heartbeat(instance_id, msg.active_concerns)
        await self.connections.send_to(instance_id, HeartbeatAckMessage(
            connected_instances=self.connections.connected_count,
        ))

    def _handle_deck_agents(self, instance_id: str, msg: DeckAgentsMessage) -> None:
        if self.connections.update_agents(instance_id, msg.agents):
            info = self.connections.get_instance(instance_id)
            log.info("Deck %s now exposes %d agent(s)", self._name(instance_id), len(info.agents) if info else 0)
            self._schedule_network()

    # ── Concern flow ─────────────────────────────────────────────

    async def _ack(self, instance_id: str, concern_id: str, *, ok: bool, error: str = "",
                   assigned_to_name: str = "", target_deck: str = "", target_agent: str = "") -> None:
        await self.connections.send_to(instance_id, ConcernAckMessage(
            concern_id=concern_id, ok=ok, error=error, assigned_to_name=assigned_to_name,
            target_deck=target_deck, target_agent=target_agent,
        ))

    async def _handle_concern(self, instance_id: str, msg: ConcernSubmitMessage) -> None:
        """An originator submits a concern -> route it to the best target."""
        from_info = self.connections.get_instance(instance_id)
        from_name = from_info.name if from_info else instance_id[:8]
        from_is_deck = bool(from_info and from_info.is_deck)

        # Idempotent resubmission (a deck resends after a reconnect).
        if msg.concern_id:
            if msg.concern_id in self._outbox.get(instance_id, {}):
                await self._flush_outbox(instance_id, only=msg.concern_id)
                return
            existing = self.concerns.get_concern(msg.concern_id)
            if existing is None and from_is_deck:
                # Finished and evicted from memory? The store remembers it.
                stored = await self.store.load_concern(msg.concern_id)
                if stored is not None and (stored.is_terminal or stored.from_instance != instance_id):
                    existing = stored
                # (still "active" in the store but unknown in memory: the hub
                # restarted — run it again as a new request)
            if existing is not None:
                if existing.from_instance != instance_id:
                    await self._ack(instance_id, msg.concern_id, ok=False, error="concern_id already in use")
                    return
                if existing.is_active:
                    await self._ack(
                        instance_id, existing.id, ok=True,
                        assigned_to_name=existing.assigned_instance_name,
                        target_deck=existing.target_deck, target_agent=existing.target_agent,
                    )
                    return
                await self._ack(instance_id, existing.id, ok=False,
                                error="This request already finished and its result is no longer held")
                return

        if not msg.task.strip():
            await self._ack(instance_id, msg.concern_id, ok=False, error="Empty task")
            return

        # Legacy agents: reject an identical active task (a model re-asking).
        if not from_is_deck:
            for existing in self.concerns.get_concerns_from_instance(instance_id):
                if existing.task == msg.task:
                    await self._ack(
                        instance_id, msg.concern_id or existing.id, ok=False,
                        error="Duplicate concern — identical task already active",
                    )
                    return

        cfg = self.config.concerns

        def _clamp(t: float) -> float:
            return max(10.0, min(t, float(cfg.max_timeout_seconds)))

        requested = float(msg.timeout_seconds or 0)
        # How long the assignee may work (sent with the dispatch) …
        timeout = _clamp(requested or float(cfg.default_timeout_seconds))
        # … and a hard deadline only when one was asked for or a deck asks.
        # Legacy agents keep idle-only timing: their follow-ups refresh it.
        deadline = _clamp(requested) if requested else (timeout if from_is_deck else 0.0)

        concern = await self.concerns.create_concern(
            from_instance=instance_id,
            task=msg.task,
            context=dict(msg.context),
            expertise_tags=[str(t) for t in msg.expertise_tags][:50],
            from_session=msg.from_session,
            concern_id=msg.concern_id,
            from_instance_name=from_name,
            target_deck=msg.target_deck.strip(),
            target_agent=msg.target_agent.strip(),
            from_agent=msg.from_agent.strip()[:128],
            mode=msg.mode,
            timeout_seconds=deadline,
        )
        if msg.concern_id and concern.id != msg.concern_id:
            log.debug("Ignoring malformed client concern id %r", msg.concern_id[:40])

        try:
            route_result = await self.router.route(concern, exclude_instance=instance_id)
            error = "No available instance to handle this concern"
        except RouteError as exc:
            route_result = None
            error = str(exc)

        if concern.is_terminal:
            # Closed while it was being created / routed (its deck reconnected
            # and no longer waits for it, or it was cancelled).
            await self._ack(instance_id, concern.id, ok=False, error="The request was closed")
            return
        if route_result is None:
            self.counters["concerns_rejected"] += 1
            await self.concerns.fail_concern(concern.id, reason=error)
            await self._ack(instance_id, concern.id, ok=False, error=error)
            log.info("Concern %s from %s rejected: %s", concern.id[:8], from_name, error)
            return

        target = route_result.instance
        persona_hint = route_result.persona_name
        dispatch_context = dict(concern.context)
        if not target.is_deck:
            # Override with user-selected active persona/model if set.
            if target.active_persona:
                persona_hint = target.active_persona
            if target.active_model:
                dispatch_context["model_id"] = target.active_model

        concern.target_agent = route_result.target_agent
        if target.is_deck:
            concern.target_deck = target.name
        assigned_label = route_result.label
        concern.assigned_instance_name = assigned_label

        dispatch = DispatchMessage(
            concern_id=concern.id,
            from_instance_name=from_name,
            from_deck=from_name if from_is_deck else "",
            from_agent=concern.from_agent,
            target_agent=concern.target_agent,
            task=concern.task,
            context=dispatch_context,
            persona_hint=persona_hint,
            mode=concern.mode,
            timeout_seconds=timeout,
        )
        # One synchronous step (no await): assigned, counted and — for a deck —
        # remembered as an unacknowledged dispatch. Whatever ends the concern
        # from here on releases exactly that slot, and a reconnect of the
        # assignee in the meantime re-sends the dispatch instead of failing it.
        self.concerns.mark_assigned(concern.id, target.id)
        self.connections.increment_active(target.id)
        concern.counted_active = True
        if target.is_deck:
            self._pending_dispatch[concern.id] = dispatch
        await self.concerns.persist(concern)
        if concern.is_terminal:
            return

        agent_name = route_result.agent.name if route_result.agent else ""
        await self._ack(
            instance_id, concern.id, ok=True, assigned_to_name=assigned_label,
            target_deck=target.name if target.is_deck else "", target_agent=agent_name,
        )
        if concern.is_terminal:
            return
        sent = await self.connections.send_to(target.id, dispatch)
        if not sent:
            if target.is_deck:
                # It just dropped: the reconnect re-sends the pending dispatch,
                # or the grace period fails the concern.
                concern.assignee_offline_since = concern.assignee_offline_since or time.monotonic()
            else:
                await self._finish_failed(concern, f"'{target.name}' disconnected before the task was delivered",
                                          reason="dispatch_failed", notify_assignee=False)
                return

        self.counters["concerns_routed"] += 1
        self._dashboard_event({"type": "concern_created", "concern_id": concern.id})
        log.info(
            "Concern %s routed: %s%s -> %s (persona: %s, reason: %s)",
            concern.id[:8], f"{concern.from_agent}@" if concern.from_agent else "", from_name,
            assigned_label, persona_hint or "auto", route_result.reason,
        )

    async def _handle_progress(self, instance_id: str, msg: ProgressMessage) -> None:
        """Assignee -> originator live progress (ephemeral; keeps the concern alive)."""
        concern = self.concerns.get_concern(msg.concern_id)
        if concern is None:
            await self.connections.send_to(instance_id, ConcernClosedMessage(
                concern_id=msg.concern_id, reason="unknown_concern",
            ))
            return
        if concern.is_terminal:
            await self.connections.send_to(instance_id, ConcernClosedMessage(
                concern_id=concern.id, reason=concern.status,
            ))
            return
        if concern.assigned_instance != instance_id:
            return
        self._pending_dispatch.pop(concern.id, None)
        await self.concerns.note_progress(concern.id)

        frame = ProgressMessage(concern_id=concern.id, event=msg.event[:32], data=_clip_data(msg.data))
        now = time.monotonic()
        last = self._last_progress.get(concern.id, 0.0)
        wait = _PROGRESS_MIN_INTERVAL - (now - last)
        if msg.event != "accepted" and wait > 0:
            # Too soon: keep only the newest frame and send it when the
            # interval is up (trailing edge), so the latest status always lands.
            first = concern.id not in self._pending_progress
            self._pending_progress[concern.id] = frame
            if first:
                self._spawn(self._flush_progress_later(concern.id, wait))
            return
        self._pending_progress.pop(concern.id, None)
        await self._relay_progress(concern, frame)

    async def _relay_progress(self, concern: Concern, frame: ProgressMessage) -> None:
        self._last_progress[concern.id] = time.monotonic()
        if self.connections.is_connected(concern.from_instance):
            await self.connections.send_to(concern.from_instance, frame)

    async def _flush_progress_later(self, concern_id: str, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        frame = self._pending_progress.pop(concern_id, None)
        concern = self.concerns.get_concern(concern_id)
        if frame is not None and concern is not None and concern.is_active:
            await self._relay_progress(concern, frame)

    async def _handle_result(self, instance_id: str, msg: ResultMessage) -> None:
        """Assignee sends a result -> relay it to the originator."""
        sender = self.connections.get_instance(instance_id)
        sender_is_deck = bool(sender and sender.is_deck)
        concern = self.concerns.get_concern(msg.concern_id)
        if concern is None:
            if sender_is_deck:
                # Stop it resending; there is nobody to deliver to.
                await self.connections.send_to(instance_id, ResultAckMessage(concern_id=msg.concern_id))
            log.warning("Result for unknown or finished concern %s from %s",
                        msg.concern_id[:8], self._name(instance_id))
            return

        if concern.assigned_instance != instance_id:
            if concern.is_terminal and sender_is_deck:
                # Nothing to deliver any more; let it drop the frame.
                await self.connections.send_to(instance_id, ResultAckMessage(concern_id=concern.id))
            log.warning("Ignoring result for concern %s from %s (not its assignee)",
                        concern.id[:8], self._name(instance_id))
            return

        if sender_is_deck:
            await self.connections.send_to(instance_id, ResultAckMessage(concern_id=concern.id))
        if concern.is_terminal:
            return  # timed out / closed meanwhile, or a resend after reconnect

        self._release(concern)
        self._pending_dispatch.pop(concern.id, None)
        self._last_progress.pop(concern.id, None)
        self._pending_progress.pop(concern.id, None)

        if not msg.ok:
            await self.concerns.fail_concern(concern.id, reason=msg.error or "agent_error")
            await self._deliver_result(concern, ConcernResultMessage(
                concern_id=concern.id, ok=False, error=msg.error or "The remote agent failed",
                from_instance_name=concern.assigned_instance_name, target_agent=concern.target_agent,
            ))
            self._dashboard_event({"type": "concern_result", "concern_id": concern.id})
            return

        # Store persona name in concern metadata for dashboard.
        result_metadata = _clip_data(dict(msg.metadata), limit=50_000)
        if msg.persona_name:
            result_metadata["persona_name"] = msg.persona_name
            concern.metadata["persona_name"] = msg.persona_name

        await self.concerns.record_result(
            concern.id, msg.response, result_metadata, from_instance=instance_id,
        )

        await self._deliver_result(concern, ConcernResultMessage(
            concern_id=concern.id,
            response=msg.response,
            from_instance_name=concern.assigned_instance_name or self._name(instance_id),
            persona_name=msg.persona_name,
            metadata=result_metadata,
            ok=True,
            target_agent=concern.target_agent,
        ))

        # A deck's consult is one exchange: done as soon as it is answered.
        # (Legacy agents may still follow up, so theirs stay "responded".)
        if self._is_deck_id(concern.from_instance):
            await self.concerns.close_concern(concern.id, reason="completed")

        self.counters["results_relayed"] += 1
        self._dashboard_event({"type": "concern_result", "concern_id": concern.id})
        log.info(
            "Result relayed: concern=%s %s -> %s (%d chars)",
            concern.id[:8], concern.assigned_instance_name, concern.from_instance_name,
            len(msg.response or ""),
        )

    def _handle_result_ack(self, instance_id: str, msg: ResultAckMessage) -> None:
        box = self._outbox.get(instance_id)
        if box is not None:
            box.pop(msg.concern_id, None)
            if not box:
                self._outbox.pop(instance_id, None)

    async def _deliver_result(self, concern: Concern, result: ConcernResultMessage) -> None:
        """Send a result to the originator; a deck's is kept until it acknowledges."""
        origin = concern.from_instance
        if origin == _SWARM_ORIGIN:
            return  # the swarm engine reads the concern itself
        if self._is_deck_id(origin):
            ttl = float(self.config.concerns.result_retention_seconds)
            self._outbox.setdefault(origin, {})[concern.id] = (result, time.monotonic() + ttl)
            if not await self.connections.send_to(origin, result):
                self.counters["results_queued"] += 1
                log.info("Requester %s offline: result for %s held for %ds",
                         self._name(origin), concern.id[:8], int(ttl))
            return
        if not await self.connections.send_to(origin, result):
            log.info("Requester %s gone: result for %s dropped", origin[:8], concern.id[:8])

    async def _flush_outbox(self, instance_id: str, only: str = "") -> None:
        box = self._outbox.get(instance_id) or {}
        now = time.monotonic()
        for cid, (result, expires) in list(box.items()):
            if expires < now:
                box.pop(cid, None)
                continue
            if only and cid != only:
                continue
            if not await self.connections.send_to(instance_id, result):
                break

    def _prune_outbox(self) -> None:
        now = time.monotonic()
        for iid in list(self._outbox):
            box = self._outbox[iid]
            for cid in [c for c, (_, exp) in box.items() if exp < now]:
                box.pop(cid, None)
            if not box:
                self._outbox.pop(iid, None)

    def _release(self, concern: Concern) -> None:
        """Give the assignee's capacity slot back (exactly once)."""
        if concern.counted_active and concern.assigned_instance:
            self.connections.decrement_active(concern.assigned_instance)
        concern.counted_active = False

    async def _finish_failed(self, concern: Concern, error: str, *, reason: str,
                             notify_assignee: bool) -> None:
        """Fail a concern and tell the originator (and optionally the assignee)."""
        self._release(concern)
        self._pending_dispatch.pop(concern.id, None)
        self._last_progress.pop(concern.id, None)
        self._pending_progress.pop(concern.id, None)
        await self.concerns.fail_concern(concern.id, reason=reason)
        await self._deliver_result(concern, ConcernResultMessage(
            concern_id=concern.id, ok=False, error=error,
            from_instance_name=concern.assigned_instance_name, target_agent=concern.target_agent,
        ))
        if notify_assignee and concern.assigned_instance:
            await self.connections.send_to(concern.assigned_instance, ConcernClosedMessage(
                concern_id=concern.id, reason=reason,
            ))
        self._dashboard_event({"type": "concern_result", "concern_id": concern.id})

    async def _close_and_release(self, concern: Concern, *, reason: str, notify_assignee: bool) -> None:
        self._release(concern)
        self._pending_dispatch.pop(concern.id, None)
        self._last_progress.pop(concern.id, None)
        self._pending_progress.pop(concern.id, None)
        await self.concerns.close_concern(concern.id, reason=reason)
        if notify_assignee and concern.assigned_instance:
            await self.connections.send_to(concern.assigned_instance, ConcernClosedMessage(
                concern_id=concern.id, reason=reason,
            ))

    async def _on_concern_expired(self, concern: Concern, reason: str) -> None:
        """The concern manager ended a concern (idle, deadline, assignee gone)."""
        self.counters["timeouts"] += 1
        self._release(concern)
        self._pending_dispatch.pop(concern.id, None)
        self._last_progress.pop(concern.id, None)
        self._pending_progress.pop(concern.id, None)
        cfg = self.config.concerns
        target = concern.assigned_instance_name or "the remote agent"
        if reason == "assignee_disconnected":
            error = f"{target} went offline and did not reconnect within {cfg.reconnect_grace_seconds}s"
        elif reason == "deadline":
            error = f"{target} did not finish in time"
        else:
            error = f"No activity from {target} for {cfg.idle_timeout_seconds}s — gave up"
        await self._deliver_result(concern, ConcernResultMessage(
            concern_id=concern.id, ok=False, error=error,
            from_instance_name=concern.assigned_instance_name, target_agent=concern.target_agent,
        ))
        origin = self.connections.get_instance(concern.from_instance)
        if origin is not None and not origin.is_deck:
            await self.connections.send_to(concern.from_instance, TimeoutNoticeMessage(
                concern_id=concern.id, reason=reason,
            ))
        if concern.assigned_instance:
            await self.connections.send_to(concern.assigned_instance, ConcernClosedMessage(
                concern_id=concern.id, reason=reason,
            ))
        self._dashboard_event({"type": "concern_result", "concern_id": concern.id})
        log.warning("Concern %s ended (%s): %s", concern.id[:8], reason, error)

    # ── Follow-ups ───────────────────────────────────────────────

    async def _handle_follow_up(self, instance_id: str, msg: FollowUpMessage) -> None:
        """Route follow-up from originator to assignee or vice versa."""
        concern = self.concerns.get_concern(msg.concern_id)
        if not concern or concern.is_terminal:
            log.warning("Follow-up for invalid concern %s", msg.concern_id[:8])
            return

        # Determine target: if from CC-A, send to CC-B; if from CC-B, send to CC-A.
        if instance_id == concern.from_instance and concern.assigned_instance:
            target_id = concern.assigned_instance
        elif instance_id == concern.assigned_instance:
            target_id = concern.from_instance
        else:
            log.warning("Follow-up from unrelated instance %s", self._name(instance_id))
            return

        await self.concerns.add_follow_up(
            concern.id, msg.message, from_instance=instance_id,
            additional_context=dict(msg.additional_context),
        )

        await self.connections.send_to(target_id, FollowUpMessage(
            concern_id=concern.id,
            message=msg.message,
            additional_context=msg.additional_context,
        ))

    # ── Context negotiation ──────────────────────────────────────

    async def _handle_context_request(
        self, instance_id: str, msg: ContextRequestMessage,
    ) -> None:
        """Assignee asks for more context -> forward to the originator."""
        concern = self.concerns.get_concern(msg.concern_id)
        if not concern or concern.is_terminal or concern.assigned_instance != instance_id:
            return

        await self.concerns.add_context_request(
            concern.id, msg.questions, from_instance=instance_id,
        )

        await self.connections.send_to(concern.from_instance, ContextRequestMessage(
            concern_id=concern.id,
            questions=msg.questions,
        ))

    async def _handle_context_reply(
        self, instance_id: str, msg: ContextReplyMessage,
    ) -> None:
        """Originator replies with context -> forward to the assignee."""
        concern = self.concerns.get_concern(msg.concern_id)
        if (not concern or concern.is_terminal or not concern.assigned_instance
                or concern.from_instance != instance_id):
            return

        await self.concerns.add_context_reply(
            concern.id, msg.answers, from_instance=instance_id,
        )

        await self.connections.send_to(concern.assigned_instance, ContextReplyMessage(
            concern_id=concern.id,
            answers=msg.answers,
        ))

    # ── Lifecycle ────────────────────────────────────────────────

    async def _handle_close_concern(
        self, instance_id: str, msg: CloseConcernMessage,
    ) -> None:
        """The originator closes (or cancels) a concern."""
        concern = self.concerns.get_concern(msg.concern_id)
        if not concern or concern.is_terminal:
            return
        if concern.from_instance != instance_id:
            log.warning("Ignoring close of concern %s from %s (not its originator)",
                        concern.id[:8], self._name(instance_id))
            return
        await self._close_and_release(concern, reason="closed_by_originator", notify_assignee=True)

    # ── File transfer ──────────────────────────────────────────

    async def _handle_file_frame(self, instance_id: str, msg: BaseMessage) -> None:
        """Swarm file transfer — for legacy agents working on an existing swarm."""
        info = self.connections.get_instance(instance_id)
        swarm_id = str(getattr(msg, "swarm_id", "") or "")
        refusal = ""
        if info is not None and info.is_deck:
            refusal = "File transfer is for swarm agents; Flight Decks do not use it"
        elif swarm_id:
            try:
                swarm = await self.swarm_store.get_swarm(swarm_id)
            except Exception:
                swarm = None
            if swarm is None:
                refusal = "Unknown swarm"
        if refusal:
            if isinstance(msg, FileUploadMessage):
                await self.connections.send_to(instance_id, FileUploadAckMessage(ok=False, error=refusal))
            elif isinstance(msg, FileListMessage):
                await self.connections.send_to(instance_id, FileListResponseMessage(swarm_id=swarm_id, files=[]))
            else:
                await self.connections.send_to(instance_id, FileResponseMessage(
                    swarm_id=swarm_id, ok=False, error=refusal))
            return
        if isinstance(msg, FileUploadMessage):
            await self._handle_file_upload(instance_id, msg)
        elif isinstance(msg, FileListMessage):
            await self._handle_file_list(instance_id, msg)
        else:
            await self._handle_file_request(instance_id, msg)

    async def _handle_file_upload(
        self, instance_id: str, msg: FileUploadMessage,
    ) -> None:
        """Handle file upload from an agent."""
        info = self.connections.get_instance(instance_id)
        agent_name = info.name if info else instance_id[:8]

        if not msg.swarm_id or not msg.filename or not msg.data:
            await self.connections.send_to(instance_id, FileUploadAckMessage(
                ok=False, error="Missing swarm_id, filename, or data",
            ))
            return

        try:
            mb = 1024 * 1024
            used = self.file_manager.workspace_size(msg.swarm_id)
            free = _free_bytes(self.file_manager._root) - max(0, self.config.server.min_free_disk_mb) * mb
            remaining = min(max(1, self.config.server.swarm_workspace_mb) * mb - used, free)
            if remaining <= 0:
                raise ValueError("No room for more files (swarm quota reached or the disk is nearly full)")
            from botport.swarm.file_manager import MAX_FILE_SIZE
            file_data = decode_file(msg.data, msg.compressed, max_size=min(MAX_FILE_SIZE, remaining))
            meta = self.file_manager.store_file(
                swarm_id=msg.swarm_id,
                agent_name=agent_name,
                filename=msg.filename,
                data=file_data,
                subfolder=msg.subfolder,
            )

            await self.connections.send_to(instance_id, FileUploadAckMessage(
                file_id=meta["file_id"],
                filename=meta["filename"],
                path=meta["path"],
                size=meta["size"],
                ok=True,
            ))

            log.info(
                "File uploaded: %s (%d bytes) from %s for swarm %s",
                meta["filename"], meta["size"], agent_name, msg.swarm_id[:8],
            )

        except Exception as exc:
            log.error("File upload failed: %s", exc)
            await self.connections.send_to(instance_id, FileUploadAckMessage(
                ok=False, error=str(exc),
            ))

    async def _handle_file_list(
        self, instance_id: str, msg: FileListMessage,
    ) -> None:
        """Handle file list request."""
        if not msg.swarm_id:
            await self.connections.send_to(instance_id, FileListResponseMessage(
                swarm_id="", files=[],
            ))
            return

        files = self.file_manager.list_files(
            msg.swarm_id, agent_name=msg.agent_filter,
        )

        # Strip modified_at (float) for JSON serialization — not needed by client.
        for f in files:
            f.pop("modified_at", None)

        await self.connections.send_to(instance_id, FileListResponseMessage(
            swarm_id=msg.swarm_id,
            files=files,
        ))

    async def _handle_file_request(
        self, instance_id: str, msg: FileRequestMessage,
    ) -> None:
        """Handle file download request."""
        if not msg.swarm_id:
            await self.connections.send_to(instance_id, FileResponseMessage(
                ok=False, error="Missing swarm_id",
            ))
            return

        file_data: bytes | None = None
        filename = ""
        rel_path = ""
        mime = ""

        if msg.file_path:
            file_data = self.file_manager.get_file(msg.swarm_id, msg.file_path)
            rel_path = msg.file_path
            filename = Path(msg.file_path).name
        elif msg.file_id:
            file_data, meta = self.file_manager.get_file_by_id(
                msg.swarm_id, msg.file_id,
            )
            if meta:
                filename = meta.get("filename", "")
                rel_path = meta.get("path", "")
                mime = meta.get("mime_type", "")

        if file_data is None:
            await self.connections.send_to(instance_id, FileResponseMessage(
                swarm_id=msg.swarm_id,
                ok=False,
                error="File not found",
            ))
            return

        from botport.swarm.file_manager import guess_mime_type
        if not mime:
            mime = guess_mime_type(filename)

        b64_data, compressed = encode_file(file_data, filename)

        await self.connections.send_to(instance_id, FileResponseMessage(
            swarm_id=msg.swarm_id,
            filename=filename,
            path=rel_path,
            data=b64_data,
            compressed=compressed,
            mime_type=mime,
            size=len(file_data),
            ok=True,
        ))

        log.info(
            "File served: %s (%d bytes, compressed=%s) for swarm %s",
            filename, len(file_data), compressed, msg.swarm_id[:8],
        )

    # ── Dashboard live events ────────────────────────────────────

    def _dashboard_event(self, event: dict[str, Any]) -> None:
        if not self.dashboard_sockets:
            return
        import json
        text = json.dumps(event, default=str)
        for ws in list(self.dashboard_sockets):
            if ws.closed:
                self.dashboard_sockets.discard(ws)
                continue
            self._spawn(_safe_send(ws, text))

    # ── Maintenance ──────────────────────────────────────────────

    async def maintenance_tick(self) -> None:
        """Periodic housekeeping (every ~15s)."""
        grace = float(self.config.concerns.reconnect_grace_seconds)
        purged = self.connections.purge_stale(max_age_seconds=60.0, deck_max_age_seconds=grace + 120.0)
        for iid in purged:
            log.info("Purged stale instance %s", iid[:12])
        if purged:
            self._schedule_network()
        self._prune_outbox()
        self.auth_limiter.prune()
        for deck in self.connections.list_decks(connected_only=False):
            deck.active_concerns = self._counted_for(deck.id)  # self-healing load figure
        live = {c.id for c in self.concerns.get_active_concerns()}
        for cid in [c for c in self._pending_dispatch if c not in live]:
            self._pending_dispatch.pop(cid, None)
        for cid in [c for c in self._last_progress if c not in live]:
            self._last_progress.pop(cid, None)
        for cid in [c for c in self._pending_progress if c not in live]:
            self._pending_progress.pop(cid, None)

    def status_snapshot(self) -> dict[str, Any]:
        decks = self.connections.list_decks(connected_only=False)
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "uptime_seconds": int(time.time() - self.started_at),
            "decks_connected": sum(1 for d in decks if d.status == "connected"),
            "agents_connected": sum(1 for i in self.connections.list_connected() if not i.is_deck),
            "active_concerns": len(self.concerns.get_active_concerns()),
            "queued_results": sum(len(b) for b in self._outbox.values()),
            "auth_enabled": self.config.auth.enabled,
            "counters": dict(self.counters),
        }

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        if self.swarm_scheduler:
            self.swarm_scheduler.stop()
            await self.swarm_scheduler.wait_stopped()
        if self.swarm_engine:
            self.swarm_engine.stop()
            await self.swarm_engine.wait_stopped()
        self.concerns.stop_timeout_checker()
        if self._network_task is not None:
            self._network_task.cancel()
        # "Going away": clients reconnect with backoff once the hub is back.
        await self.connections.close_all(code=1001, message=b"BotPort shutting down")
        for ws in list(self.dashboard_sockets):
            try:
                await ws.close()
            except Exception:
                pass
        for t in list(self._bg_tasks):
            t.cancel()
        await self.router.close()
        await self.store.close()
        log.info("BotPort server shut down.")


def _workspace_root(config: BotPortConfig) -> Path | None:
    """Swarm files live under the data dir (BOTPORT_WORKSPACE overrides). An
    older ./workspace-botport in the working directory keeps being used so
    files stored before stay reachable."""
    if os.environ.get("BOTPORT_WORKSPACE"):
        return None  # FileManager reads it
    legacy = Path("workspace-botport")
    if legacy.is_dir() and not (config.data_dir / "workspace-botport").exists():
        log.warning("Using swarm files in %s (move it to %s to keep them with the data dir)",
                    legacy.resolve(), config.data_dir / "workspace-botport")
        return legacy.resolve()
    return config.data_dir / "workspace-botport"


def _free_bytes(path: Path) -> int:
    import shutil

    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


async def _safe_send(ws: web.WebSocketResponse, text: str) -> None:
    try:
        await asyncio.wait_for(ws.send_str(text), timeout=5.0)
    except Exception:
        pass


def _clip_data(data: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    """Bound relayed free-form dicts (progress detail, result metadata)."""
    out: dict[str, Any] = {}
    for k, v in list(data.items())[:40]:
        if isinstance(v, str) and len(v) > limit:
            v = v[:limit] + "…"
        out[str(k)[:64]] = v
    return out


# ── Logging ──────────────────────────────────────────────────────

# ANSI colors matching Captain Claw's structlog ConsoleRenderer.
_RST = "\033[0m"
_BRIGHT = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"

_LEVEL_COLORS = {
    "DEBUG": _DIM + _GREEN,
    "INFO": _GREEN,
    "WARNING": _YELLOW,
    "ERROR": _RED,
    "CRITICAL": _RED + _BRIGHT,
}


class _ColorFormatter(logging.Formatter):
    """Colored console formatter à la structlog ConsoleRenderer."""

    def __init__(self, color: bool = True) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        lvl = record.levelname
        name = record.name.replace("botport.", "")
        msg = record.getMessage()
        if self._color:
            color = _LEVEL_COLORS.get(lvl, "")
            line = (
                f"{_DIM}{ts}{_RST} "
                f"[{color}{lvl:<8s}{_RST}] "
                f"{_BLUE}{name}{_RST}  "
                f"{_BRIGHT}{msg}{_RST}"
            )
        else:
            line = f"{ts} [{lvl:<8s}] {name}  {msg}"
        if record.exc_info and record.exc_info[1]:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _use_color(setting: str) -> bool:
    s = (setting or "auto").lower()
    if s == "always":
        return True
    if s == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


def _setup_logging(config: BotPortConfig) -> None:
    """Configure colored logging, suppress noisy aiohttp/ws chatter."""
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(color=_use_color(config.logging.color)))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    if config.logging.file:
        path = Path(config.logging.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=max(1, config.logging.file_max_mb) * 1024 * 1024,
            backupCount=max(0, config.logging.file_backups), encoding="utf-8",
        )
        fh.setFormatter(_ColorFormatter(color=False))
        root.addHandler(fh)

    # Suppress noisy aiohttp access logs and websocket internals.
    for noisy in ("aiohttp.access", "aiohttp.server", "aiohttp.web", "aiohttp", "LiteLLM", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Entry point ──────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Start the BotPort server (or run a maintenance command)."""
    from botport import cli

    sys.exit(cli.main(argv))


def run(config: BotPortConfig) -> int:
    """Run the hub until SIGINT/SIGTERM. Returns a process exit code."""
    set_config(config)
    _setup_logging(config)
    for w in config.warnings():
        log.warning("%s", w)
    try:
        return asyncio.run(_run_server(config))
    except KeyboardInterrupt:
        print("\nBotPort stopped.")
        return 0


def _ssl_context(config: BotPortConfig) -> ssl.SSLContext | None:
    if not config.server.tls_enabled:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(
        str(Path(config.server.tls_cert).expanduser()),
        str(Path(config.server.tls_key).expanduser()),
    )
    return ctx


async def _run_server(config: BotPortConfig) -> int:
    """Async server lifecycle."""
    server = BotPortServer(config)
    # The database must be usable before anything connects.
    await server.store._ensure_db()

    app = server.create_app()
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()

    host = config.server.host
    port = config.server.port
    try:
        site = web.TCPSite(runner, host, port, ssl_context=_ssl_context(config),
                           shutdown_timeout=5.0, reuse_address=True)
        await site.start()
    except OSError as exc:
        # Never hop to another port: every deck is configured with this one.
        log.error("Cannot listen on %s:%d — %s. Is another BotPort already running? "
                  "(change server.port or stop the other process)", host, port, exc.strerror or exc)
        await runner.cleanup()
        await server.store.close()
        return 2
    except (ssl.SSLError, FileNotFoundError) as exc:
        log.error("TLS setup failed: %s", exc)
        await runner.cleanup()
        await server.store.close()
        return 2

    # Start background tasks.
    server.concerns.start_timeout_checker()

    # Start swarm engine and scheduler (DB initialized above).
    from botport.swarm.engine import SwarmEngine
    from botport.swarm.scheduler import SwarmScheduler
    server.swarm_engine = SwarmEngine(server)
    server.swarm_engine.start()
    server.swarm_scheduler = SwarmScheduler(server)
    server.swarm_scheduler.start()

    async def _cleanup_loop() -> None:
        while True:
            try:
                await asyncio.sleep(3600)  # hourly
                deleted = await server.store.cleanup_old(config.logging.retention_days)
                if deleted:
                    log.info("Cleaned up %d old concerns", deleted)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Cleanup error: %s", exc)

    async def _maintenance_loop() -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await server.maintenance_tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Maintenance error: %s", exc)

    tasks = [asyncio.create_task(_cleanup_loop()), asyncio.create_task(_maintenance_loop())]

    scheme = "https" if config.server.tls_enabled else "http"
    ws_scheme = "wss" if config.server.tls_enabled else "ws"
    shown = host if host not in ("0.0.0.0", "::", "") else "<this-host>"
    print(f"BotPort v{__version__} (protocol {PROTOCOL_VERSION}) listening on {scheme}://{host}:{port}")
    print(f"  Flight Decks / agents connect to: {ws_scheme}://{shown}:{port}/ws")
    if config.server.dashboard_enabled:
        print(f"  Dashboard: {scheme}://{shown}:{port}/"
              + ("  (admin token required)" if config.server.admin_token else ""))
    print(f"  Health:    {scheme}://{shown}:{port}/healthz")
    llm_info = f"LLM routing: {config.llm.model}" if config.llm.enabled else "LLM routing: disabled"
    print(f"  Routing: explicit -> tags -> {'LLM' if config.llm.enabled else 'skip'} -> fallback  ({llm_info})")
    if config.auth.enabled:
        print(f"  Auth: enabled ({len(config.auth.keys)} keys)")
    else:
        print("  Auth: DISABLED (open) — enable it before exposing this port")
    print(f"  Data: {config.data_dir}" + (f"   Config: {config.source}" if config.source else ""))
    sys.stdout.flush()

    # Wait for shutdown signal.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, OSError, RuntimeError):
            pass

    await stop_event.wait()

    print("\nShutting down BotPort...")
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    await server.shutdown()
    try:
        await asyncio.wait_for(runner.cleanup(), timeout=5.0)
    except asyncio.TimeoutError:
        pass
    return 0


if __name__ == "__main__":
    main()
