"""Flight Decks on the dashboard: add one (its key and secret are made here),
name and describe it, rotate its secret, remove it. Every change applies at
once: the hub needs no restart.

All of it sits behind the admin gate (web_security), like the rest of /api.
Requests that change something must be JSON, which a browser will not send
to another origin without a CORS preflight this hub does not grant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from botport.decks import DECK_NAME_RE, DeckError, DeckRecord, config_entry_for

if TYPE_CHECKING:
    from botport.models import InstanceInfo
    from botport.server import BotPortServer

log = logging.getLogger(__name__)


def _connection_view(info: InstanceInfo | None) -> dict[str, Any]:
    if info is None:
        return {"status": "never", "version": "", "connected_at": "", "disconnected_at": "",
                "last_heartbeat": "", "remote_addr": "", "agents": [], "active_concerns": 0,
                "max_concurrent": 0}
    return {
        "status": "online" if info.status == "connected" else "offline",
        "version": info.version,
        "connected_at": info.connected_at,
        "disconnected_at": info.disconnected_at,
        "last_heartbeat": info.last_heartbeat,
        "remote_addr": info.remote_addr,
        "agents": [{"name": a.name, "available": a.available} for a in info.agents],
        "active_concerns": info.active_concerns,
        "max_concurrent": info.max_concurrent,
    }


def deck_views(server: BotPortServer) -> list[dict[str, Any]]:
    """Every deck the hub knows of — added here, keyed in botport.yaml, or
    seen connecting — with what the dashboard shows for it."""
    names: dict[str, str] = {}
    for rec in server.decks.records():
        names.setdefault(rec.name.lower(), rec.name)
    for k in server.config.auth.keys:
        if k.instance.strip() and k.key.strip():
            names.setdefault(k.instance.strip().lower(), k.instance.strip())
    conns: dict[str, InstanceInfo] = {}
    for info in server.connections.list_decks(connected_only=False):
        low = info.name.lower()
        if low not in names and info.status != "connected" and server.config.auth.enabled:
            continue  # its key is gone: it cannot come back
        conns[low] = info
        names[low] = info.name  # the spelling it connects with

    out: list[dict[str, Any]] = []
    for low, name in names.items():
        out.append(_view(server, name, server.decks.get(name), conns.get(low)))
    out.sort(key=lambda d: (d["status"] != "online", (d["label"] or d["name"]).lower()))
    return out


def _view(server: BotPortServer, name: str, rec: DeckRecord | None, info: InstanceInfo | None) -> dict[str, Any]:
    cfg = config_entry_for(name)
    managed = rec is not None and rec.has_key
    via = ""
    if info is not None and info.status == "connected":
        key = getattr(info, "auth_key", "")
        if not key:
            via = "open"
        elif managed and key == rec.key:  # type: ignore[union-attr]
            via = "dashboard"
        else:
            via = "file"
    return {
        "name": name,
        "label": rec.label if rec else "",
        "description": rec.description if rec else "",
        # Key ids name a key; they are not secret (the secret is).
        "key": rec.key if managed else (cfg.key.strip() if cfg else ""),
        "managed": managed,
        "in_file": cfg is not None,
        # A name/description kept on the hub (with or without a key).
        "has_record": rec is not None,
        "via": via,
        "created_at": rec.created_at if rec else "",
        "rotated_at": rec.rotated_at if rec else "",
        **_connection_view(info),
    }


def _find(server: BotPortServer, name: str) -> dict[str, Any] | None:
    low = name.strip().lower()
    return next((d for d in deck_views(server) if d["name"].lower() == low), None)


def setup_deck_routes(app: web.Application, server: BotPortServer) -> None:

    async def _body(request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(
                text='{"error": "send JSON (Content-Type: application/json)"}', content_type="application/json",
            )
        try:
            data = await request.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='{"error": "expected a JSON object"}', content_type="application/json")
        return data

    def _error(exc: Exception) -> web.Response:
        if isinstance(exc, DeckError):
            return web.json_response({"error": str(exc)}, status=exc.status)
        # The registry changes nothing until the database has the change.
        log.exception("Deck change failed")
        return web.json_response({"error": f"The hub could not save that ({exc}); nothing changed. Try again."},
                                 status=500)

    async def list_decks(request: web.Request) -> web.Response:
        await server.decks.ensure_loaded()
        return web.json_response({
            "auth_enabled": server.config.auth.enabled,
            "warnings": server.decks.conflicts(),
            "decks": deck_views(server),
        })

    async def add_deck(request: web.Request) -> web.Response:
        data = await _body(request)
        name = str(data.get("name") or "").strip()
        try:
            rec, secret = await server.decks.create(name, data.get("label"), data.get("description"))
        except Exception as exc:
            return _error(exc)
        server.auth_limiter.forget_key(rec.key)
        server.deck_changed(rec.name)
        return web.json_response({
            "deck": _find(server, rec.name),
            "credentials": {"deck_name": rec.name, "key": rec.key, "secret": secret},
        }, status=201)

    async def edit_deck(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        data = await _body(request)
        if _find(server, name) is None:
            return web.json_response({"error": "No such deck on this hub"}, status=404)
        try:
            rec = await server.decks.update(
                name,
                label=data["label"] if "label" in data else None,
                description=data["description"] if "description" in data else None,
            )
        except Exception as exc:
            return _error(exc)
        server.deck_changed(rec.name)
        return web.json_response({"deck": _find(server, rec.name)})

    async def rotate_deck(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        await _body(request)
        rec = server.decks.get(name)
        if rec is None or not rec.has_key:
            if config_entry_for(name):
                return web.json_response({"error": "This deck's key is in botport.yaml: replace it there "
                                                   "(botport gen-key) and restart the hub"}, status=409)
            return web.json_response({"error": "No key made on this dashboard for that deck"}, status=404)
        try:
            rec, secret = await server.decks.rotate(name)
        except Exception as exc:
            return _error(exc)
        dropped = await server.drop_key(rec.key, "this deck's secret was replaced on the hub")
        server.deck_changed(rec.name)
        return web.json_response({
            "deck": _find(server, rec.name),
            "credentials": {"deck_name": rec.name, "key": rec.key, "secret": secret},
            "disconnected": dropped,
        })

    async def remove_deck(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        rec = server.decks.get(name)
        if rec is None:
            if config_entry_for(name):
                return web.json_response(
                    {"error": "This deck's key is in botport.yaml: remove it there and restart the hub"},
                    status=409,
                )
            return web.json_response({"error": "No such deck on this dashboard"}, status=404)
        try:
            rec = await server.decks.remove(name)
        except Exception as exc:
            return _error(exc)
        dropped = await server.drop_key(rec.key, "this deck was removed on the hub") if rec.has_key else []
        server.deck_changed(rec.name)
        # Its botport.yaml key (if any) still works: say so, not "removed".
        return web.json_response({"ok": True, "disconnected": dropped,
                                  "still_in_file": config_entry_for(rec.name) is not None})

    # The route pattern is DECK_NAME_RE without its anchors.
    name = "{name:" + DECK_NAME_RE.pattern.strip("^$") + "}"
    app.router.add_get("/api/decks", list_decks)
    app.router.add_post("/api/decks", add_deck)
    app.router.add_patch(f"/api/decks/{name}", edit_deck)
    app.router.add_post(f"/api/decks/{name}/rotate", rotate_deck)
    app.router.add_delete(f"/api/decks/{name}", remove_deck)
