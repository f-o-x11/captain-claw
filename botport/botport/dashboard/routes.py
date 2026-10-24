"""BotPort dashboard REST API and static file serving."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from botport import __version__

if TYPE_CHECKING:
    from botport.server import BotPortServer

STATIC_DIR = Path(__file__).parent / "static"


def setup_dashboard_routes(app: web.Application, server: BotPortServer) -> None:
    """Register dashboard routes on the aiohttp application."""

    async def index(request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return web.FileResponse(index_path)
        return web.Response(text="Dashboard not found", status=404)

    async def dashboard_css(request: web.Request) -> web.Response:
        css_path = STATIC_DIR / "dashboard.css"
        if css_path.is_file():
            return web.FileResponse(css_path, headers={"Content-Type": "text/css"})
        return web.Response(text="", content_type="text/css")

    async def dashboard_js(request: web.Request) -> web.Response:
        js_path = STATIC_DIR / "dashboard.js"
        if js_path.is_file():
            return web.FileResponse(js_path, headers={"Content-Type": "application/javascript"})
        return web.Response(text="", content_type="application/javascript")

    async def auth_js(request: web.Request) -> web.Response:
        js_path = STATIC_DIR / "auth.js"
        if js_path.is_file():
            return web.FileResponse(js_path, headers={"Content-Type": "application/javascript"})
        return web.Response(text="", content_type="application/javascript")

    async def swarm_js(request: web.Request) -> web.Response:
        js_path = STATIC_DIR / "swarm.js"
        if js_path.is_file():
            return web.FileResponse(js_path, headers={"Content-Type": "application/javascript"})
        return web.Response(text="", content_type="application/javascript")

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
        concern = await server.store.load_concern(concern_id)
        if concern is None:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response(concern.to_dict())

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
                "name": d.name, "status": d.status, "version": d.version,
                "connected_at": d.connected_at, "disconnected_at": d.disconnected_at,
                "last_heartbeat": d.last_heartbeat, "remote_addr": d.remote_addr,
                "active_concerns": d.active_concerns, "max_concurrent": d.max_concurrent,
                "agents": [a.to_dict() for a in d.agents],
            }
            for d in server.connections.list_decks(connected_only=False)
        ]
        return web.json_response(snap)

    async def dashboard_ws(request: web.Request) -> web.StreamResponse:
        """Live events for dashboards (instance_connected/_disconnected,
        activity, concern_created, concern_result). Read-only."""
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
    app.router.add_get("/dashboard.css", dashboard_css)
    app.router.add_get("/dashboard.js", dashboard_js)
    app.router.add_get("/auth.js", auth_js)
    app.router.add_get("/swarm.js", swarm_js)
    app.router.add_get("/ws/dashboard", dashboard_ws)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/instances", api_instances)
    app.router.add_get("/api/concerns", api_concerns)
    app.router.add_get("/api/concerns/{id}", api_concern_detail)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/registry", api_registry)
    app.router.add_patch("/api/instances/{id}", api_patch_instance)
