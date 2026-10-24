"""WebSocket connection pool for connected instances (agents and decks)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from botport.models import DeckAgent, InstanceInfo, PersonaInfo, _utcnow_iso
from botport.protocol import BaseMessage, RegisterMessage, serialize_message

log = logging.getLogger(__name__)

# Close code sent to a socket whose deck reconnected on a new socket.
CLOSE_SUPERSEDED = 4000


class ConnectionManager:
    """Manages persistent WebSocket connections from agents and decks.

    An agent gets a fresh id per connection. A deck keeps one stable id
    (``deck:<name>``) across reconnects, so concerns it originated or works on
    survive a dropped socket; a newer socket for the same deck takes over and
    the stale one is closed.
    """

    def __init__(self) -> None:
        self._connections: dict[str, web.WebSocketResponse] = {}  # instance_id -> ws
        self._instances: dict[str, InstanceInfo] = {}  # instance_id -> info
        self._ws_to_instance: dict[int, str] = {}  # id(ws) -> instance_id
        self._send_locks: dict[int, asyncio.Lock] = {}  # id(ws) -> lock

    async def register(
        self,
        ws: web.WebSocketResponse,
        msg: RegisterMessage,
        instance_id: str,
        *,
        name: str = "",
        remote_addr: str = "",
    ) -> tuple[InstanceInfo, web.WebSocketResponse | None]:
        """Register a connection under ``instance_id``.

        Returns ``(info, superseded_ws)`` — the previous socket of the same
        instance (decks), which the caller should close, or None.
        """
        capabilities = msg.capabilities or {}

        personas: list[PersonaInfo] = []
        for p in capabilities.get("personas", []) or []:
            personas.append(PersonaInfo.from_dict(p) if isinstance(p, dict) else PersonaInfo(name=str(p)))

        try:
            max_concurrent = int(capabilities.get("max_concurrent", 5))
        except (TypeError, ValueError):
            max_concurrent = 5
        max_concurrent = max(1, min(max_concurrent, 1000))

        previous = self._instances.get(instance_id)
        info = InstanceInfo(
            id=instance_id,
            name=name or msg.instance_name or instance_id,
            personas=personas,
            tools=[str(t) for t in capabilities.get("tools", []) or []],
            models=list(capabilities.get("models", []) or []),
            max_concurrent=max_concurrent,
            active_concerns=0,
            status="connected",
            connected_at=_utcnow_iso(),
            last_heartbeat=_utcnow_iso(),
            kind="deck" if msg.kind == "deck" else "agent",
            protocol=int(msg.protocol or 1),
            deck_id=msg.deck_id,
            version=str(capabilities.get("version", "") or "")[:64],
            remote_addr=remote_addr,
            agents=_parse_agents(capabilities.get("agents", [])),
        )
        if previous is not None:
            # Same deck again: keep the user's routing choices and the count
            # of concerns it is still working on.
            info.active_persona = previous.active_persona
            info.active_model = previous.active_model
            info.active_concerns = previous.active_concerns

        old_ws = self._connections.get(instance_id)
        if old_ws is ws:
            old_ws = None
        if old_ws is not None:
            self._ws_to_instance.pop(id(old_ws), None)

        self._connections[instance_id] = ws
        self._instances[instance_id] = info
        self._ws_to_instance[id(ws)] = instance_id
        self._send_locks.setdefault(id(ws), asyncio.Lock())

        log.debug("Instance registered: %s (%s)", info.name, instance_id)
        return info, old_ws

    def is_current(self, instance_id: str, ws: web.WebSocketResponse) -> bool:
        """True while ``ws`` is the live socket of ``instance_id``."""
        return self._connections.get(instance_id) is ws

    async def unregister(
        self, instance_id: str, ws: web.WebSocketResponse | None = None,
    ) -> InstanceInfo | None:
        """Mark an instance disconnected.

        With ``ws`` given, only that socket's registration is undone — a
        socket that was already superseded leaves the new one alone.
        """
        if ws is not None:
            self._send_locks.pop(id(ws), None)
            if not self.is_current(instance_id, ws):
                return None

        info = self._instances.get(instance_id)
        if info:
            info.status = "disconnected"
            info.disconnected_at = _utcnow_iso()
            info.activity.clear()

        old = self._connections.pop(instance_id, None)
        if old is not None:
            self._ws_to_instance.pop(id(old), None)

        if info:
            log.debug("Instance unregistered: %s (%s)", info.name, instance_id)
        return info

    def purge_stale(
        self, max_age_seconds: float = 60.0, deck_max_age_seconds: float | None = None,
    ) -> list[str]:
        """Remove instances that have been disconnected longer than *max_age_seconds*
        (*deck_max_age_seconds* for decks, which may still come back).

        Returns a list of purged instance IDs.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        purged: list[str] = []
        for iid, info in list(self._instances.items()):
            if info.status != "disconnected":
                continue
            limit = max_age_seconds
            if info.is_deck and deck_max_age_seconds is not None:
                limit = deck_max_age_seconds
            disc_at = getattr(info, "disconnected_at", "")
            if not disc_at:
                # No timestamp — remove immediately.
                del self._instances[iid]
                purged.append(iid)
                continue
            try:
                dt = datetime.fromisoformat(disc_at)
                if (now - dt).total_seconds() >= limit:
                    del self._instances[iid]
                    purged.append(iid)
            except (ValueError, TypeError):
                del self._instances[iid]
                purged.append(iid)
        return purged

    def get_instance_id_for_ws(self, ws: web.WebSocketResponse) -> str | None:
        """Look up instance_id by WebSocket object."""
        return self._ws_to_instance.get(id(ws))

    def get_ws(self, instance_id: str) -> web.WebSocketResponse | None:
        return self._connections.get(instance_id)

    def is_connected(self, instance_id: str) -> bool:
        ws = self._connections.get(instance_id)
        return ws is not None and not ws.closed

    async def _send_text(self, instance_id: str, text: str) -> bool:
        ws = self._connections.get(instance_id)
        if ws is None or ws.closed:
            return False
        lock = self._send_locks.setdefault(id(ws), asyncio.Lock())
        try:
            async with lock:
                await ws.send_str(text)
            return True
        except Exception as exc:
            log.warning("Failed to send to %s: %s", instance_id, exc)
            return False

    async def send_to(self, instance_id: str, message: BaseMessage) -> bool:
        """Send a message to a specific instance. Returns False if not connected."""
        return await self._send_text(instance_id, serialize_message(message))

    async def send_json_to(self, instance_id: str, data: dict[str, Any]) -> bool:
        """Send raw JSON dict to a specific instance."""
        return await self._send_text(instance_id, json.dumps(data, default=str))

    async def send_on(self, ws: web.WebSocketResponse, message: BaseMessage) -> bool:
        """Send on a specific socket (before or outside registration)."""
        if ws.closed:
            return False
        lock = self._send_locks.setdefault(id(ws), asyncio.Lock())
        try:
            async with lock:
                await ws.send_str(serialize_message(message))
            return True
        except Exception as exc:
            log.debug("Failed to send on socket: %s", exc)
            return False

    def forget_socket(self, ws: web.WebSocketResponse) -> None:
        """Drop per-socket state for a socket that never registered."""
        self._send_locks.pop(id(ws), None)

    def get_instance(self, instance_id: str) -> InstanceInfo | None:
        return self._instances.get(instance_id)

    def list_instances(self) -> list[InstanceInfo]:
        return list(self._instances.values())

    def list_connected(self) -> list[InstanceInfo]:
        return [i for i in self._instances.values() if i.status == "connected"]

    def list_decks(self, connected_only: bool = True) -> list[InstanceInfo]:
        return [
            i for i in self._instances.values()
            if i.is_deck and (i.status == "connected" or not connected_only)
        ]

    def find_deck(self, name: str) -> InstanceInfo | None:
        """A deck by name (case-insensitive), connected or not."""
        n = (name or "").strip().lower()
        if not n:
            return None
        return self._instances.get(f"deck:{n}")

    def list_available(self, exclude: str | None = None) -> list[InstanceInfo]:
        """List instances that are connected and have capacity."""
        return [
            i for i in self._instances.values()
            if i.has_capacity and (exclude is None or i.id != exclude)
        ]

    def update_heartbeat(self, instance_id: str, active_concerns: int = 0) -> None:
        """Update heartbeat timestamp and load info.

        A deck's active count is the hub's own bookkeeping (concerns it
        dispatched there); an agent reports its own.
        """
        info = self._instances.get(instance_id)
        if info:
            info.last_heartbeat = _utcnow_iso()
            if not info.is_deck:
                info.active_concerns = max(0, int(active_concerns or 0))

    def update_agents(self, instance_id: str, agents: list[Any]) -> bool:
        """Replace a deck's advertised agents. Returns True if they changed."""
        info = self._instances.get(instance_id)
        if not info or not info.is_deck:
            return False
        new = _parse_agents(agents)
        changed = [a.to_dict() for a in new] != [a.to_dict() for a in info.agents]
        info.agents = new
        return changed

    @property
    def connected_count(self) -> int:
        return sum(1 for i in self._instances.values() if i.status == "connected")

    def update_activity(self, instance_id: str, step_type: str, data: dict) -> None:
        """Update live activity for an instance. Each step_type replaces the previous."""
        info = self._instances.get(instance_id)
        if info:
            if step_type == "thinking" and data.get("phase") == "done":
                # Clear thinking slot when agent signals done.
                info.activity.pop("thinking", None)
            else:
                info.activity[step_type] = {
                    **data,
                    "updated_at": _utcnow_iso(),
                }

    def clear_activity(self, instance_id: str) -> None:
        """Clear all activity for an instance (e.g. on disconnect)."""
        info = self._instances.get(instance_id)
        if info:
            info.activity.clear()

    def increment_active(self, instance_id: str) -> None:
        info = self._instances.get(instance_id)
        if info:
            info.active_concerns += 1

    def decrement_active(self, instance_id: str) -> None:
        info = self._instances.get(instance_id)
        if info and info.active_concerns > 0:
            info.active_concerns -= 1

    async def close_all(self, code: int = 1001, message: bytes = b"server shutdown") -> None:
        for ws in list(self._connections.values()):
            if not ws.closed:
                try:
                    await asyncio.wait_for(ws.close(code=code, message=message), timeout=2.0)
                except Exception:
                    pass


def _parse_agents(raw: Any) -> list[DeckAgent]:
    agents: list[DeckAgent] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return agents
    for item in raw[:500]:
        a = DeckAgent.from_dict(item)
        if a is None:
            continue
        key = (a.slug or a.name).lower()
        if key in seen:
            continue
        seen.add(key)
        agents.append(a)
    return agents
