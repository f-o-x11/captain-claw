"""BotPort dashboard REST API and static file serving."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from botport import __version__

if TYPE_CHECKING:
    from botport.models import Concern, InstanceInfo
    from botport.server import BotPortServer

STATIC_DIR = Path(__file__).parent / "static"

# The dashboard's files (no build step). Anything else is not served.
_STATIC_FILES = {
    "app.css": "text/css",
    "dashboard.css": "text/css",
    "dashboard.js": "application/javascript",
    "auth.js": "application/javascript",
    "decks.js": "application/javascript",
    "swarm.js": "application/javascript",
    "icon.svg": "image/svg+xml",
}


def _concern_view(c: Concern, live: dict[str, Any] | None = None,
                  counts: dict[str, int] | None = None) -> dict[str, Any]:
    """A concern without its message bodies — what lists and the map need.
    ``counts`` (messages by direction) stands in for messages the store did
    not load."""
    meta = c.metadata or {}
    if counts is None:
        counts = {}
        for m in c.messages:
            counts[m.direction] = counts.get(m.direction, 0) + 1
    return {
        "id": c.id,
        "status": c.status,
        "mode": c.mode,
        "from": c.from_instance,
        "from_name": c.from_instance_name,
        "from_agent": c.from_agent,
        "to": c.assigned_instance or "",
        "to_name": c.assigned_instance_name,
        "target_deck": c.target_deck,
        "target_agent": c.target_agent,
        "task": (c.task or "")[:600],
        "expertise_tags": list(c.expertise_tags)[:8],
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "messages": sum(counts.values()),
        "answered": counts.get("response", 0) > 0,
        "persona_name": meta.get("persona_name", ""),
        "error": str(meta.get("error") or meta.get("fail_reason") or "")[:500],
        "end_reason": str(meta.get("close_reason") or meta.get("timeout_reason") or "")[:80],
        "live": live or None,
    }


def _instance_view(i: InstanceInfo, server: BotPortServer) -> dict[str, Any]:
    d = i.to_dict()
    d["remote_addr"] = getattr(i, "remote_addr", "") or ""
    if i.is_deck:
        d.update(server.decks.meta(i.name))
    return d


def setup_dashboard_routes(app: web.Application, server: BotPortServer) -> None:
    """Register dashboard routes on the aiohttp application."""

    async def index(request: web.Request) -> web.StreamResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return web.FileResponse(index_path, headers={"Cache-Control": "no-cache"})
        return web.Response(text="Dashboard not found", status=404)

    async def static_file(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        ctype = _STATIC_FILES.get(name)
        path = STATIC_DIR / name
        if ctype is None or not path.is_file():
            raise web.HTTPNotFound()
        # no-cache: revalidate (cheap 304s), so an upgraded hub never runs old JS.
        return web.FileResponse(path, headers={"Content-Type": ctype, "Cache-Control": "no-cache"})

    async def api_instances(request: web.Request) -> web.Response:
        instances = server.connections.list_instances()
        return web.json_response([i.to_dict() for i in instances])

    async def api_concerns(request: web.Request) -> web.Response:
        active_only = request.query.get("active", "").lower() in ("1", "true", "yes")
        if active_only:
            concerns = server.concerns.get_active_concerns()
        else:
            concerns = await server.store.list_concerns(limit=100)
        return web.json_response([c.to_dict() for c in concerns])

    async def api_concern_detail(request: web.Request) -> web.Response:
        concern_id = request.match_info.get("id", "")
        # A running concern's in-memory copy is the newest one.
        concern = server.concerns.get_concern(concern_id) or await server.store.load_concern(concern_id)
        if concern is None:
            return web.json_response({"error": "Not found"}, status=404)
        data = concern.to_dict()
        data["live"] = server.live_progress.get(concern.id)
        return web.json_response(data)

    async def api_stats(request: web.Request) -> web.Response:
        stats = await server.concerns.get_stats()
        stats["connected_instances"] = server.connections.connected_count
        stats["connected_decks"] = len(server.connections.list_decks())
        stats["botport_version"] = __version__
        return web.json_response(stats)

    async def api_status(request: web.Request) -> web.Response:
        """Operational snapshot: decks, counters, queued results."""
        snap = server.status_snapshot()
        snap["decks"] = [
            {
                "name": d.name, **server.decks.meta(d.name), "status": d.status, "version": d.version,
                "connected_at": d.connected_at, "disconnected_at": d.disconnected_at,
                "last_heartbeat": d.last_heartbeat, "remote_addr": d.remote_addr,
                "active_concerns": d.active_concerns, "max_concurrent": d.max_concurrent,
                "agents": [a.to_dict() for a in d.agents],
            }
            for d in server.connections.list_decks(connected_only=False)
        ]
        return web.json_response(snap)

    async def api_overview(request: web.Request) -> web.Response:
        """Everything the live view draws, in one request: the hub, every
        connection, running concerns (with their latest progress) and the
        most recent ones."""
        try:
            limit = max(1, min(int(request.query.get("limit", "60")), 200))
        except ValueError:
            limit = 60
        active = server.concerns.get_active_concerns()
        active_ids = {c.id for c in active}
        recent = [c for c in await server.store.list_concerns(limit=limit) if c.id not in active_ids]
        await server.decks.ensure_loaded()
        counts = await server.store.message_counts([c.id for c in recent])
        return web.json_response({
            "now": datetime.now(timezone.utc).isoformat(),
            "hub": server.status_snapshot(),
            "stats": await server.concerns.get_stats(),
            "instances": [_instance_view(i, server) for i in server.connections.list_instances()],
            "active": [_concern_view(c, server.live_progress.get(c.id)) for c in active],
            "recent": [_concern_view(c, counts=counts.get(c.id, {})) for c in recent],
        })

    async def dashboard_ws(request: web.Request) -> web.StreamResponse:
        """Live events for dashboards (instance_connected/_disconnected,
        activity, concern_created, concern_progress, concern_result). Read-only."""
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=64 * 1024)
        await ws.prepare(request)
        server.dashboard_sockets.add(ws)
        try:
            async for _ in ws:
                pass  # clients do not send anything we act on
        finally:
            server.dashboard_sockets.discard(ws)
        return ws

    async def api_registry(request: web.Request) -> web.Response:
        summary = server.registry.get_summary()
        return web.json_response(summary)

    async def api_patch_instance(request: web.Request) -> web.Response:
        """PATCH /api/instances/{id} — update active_persona and/or active_model."""
        instance_id = request.match_info.get("id", "")
        instance = server.connections.get_instance(instance_id)
        if instance is None:
            return web.json_response({"error": "Instance not found"}, status=404)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        if not isinstance(data, dict):
            return web.json_response({"error": "Expected a JSON object"}, status=400)
        if "active_persona" in data:
            instance.active_persona = str(data["active_persona"] or "")[:128]
        if "active_model" in data:
            instance.active_model = str(data["active_model"] or "")[:256]
        return web.json_response(instance.to_dict())

    # Routes.
    app.router.add_get("/", index)
    app.router.add_get("/{name:[a-z]+\\.(?:css|js|svg)}", static_file)
    app.router.add_get("/ws/dashboard", dashboard_ws)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/instances", api_instances)
    app.router.add_get("/api/concerns", api_concerns)
    app.router.add_get("/api/concerns/{id}", api_concern_detail)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/registry", api_registry)
    app.router.add_patch("/api/instances/{id}", api_patch_instance)
