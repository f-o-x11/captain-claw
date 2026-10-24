"""HTTP access control for the BotPort dashboard and REST API.

The agent/deck WebSocket (``/ws``) authenticates in its own handshake. Every
``/api/*`` route and the dashboard live-event socket (``/ws/dashboard``) go
through :func:`security_middleware`:

* ``server.admin_token`` set   → a matching token is required (Authorization:
  Bearer, X-BotPort-Token, ``?token=``; or the dashboard cookie for GETs).
* no token, auth enabled      → loopback clients only (and with
  ``server.behind_proxy`` not even those: a local reverse proxy would launder
  every remote caller into 127.0.0.1).
* no token, auth disabled     → open, as before (development / trusted LAN).

The static dashboard pages carry no data and stay reachable; their scripts
send the token.
"""

from __future__ import annotations

import hmac
from typing import Awaitable, Callable

from aiohttp import web

from botport.config import BotPortConfig, get_config

_OPEN_PATHS = frozenset({"/healthz", "/ws"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def client_addr(request: web.BaseRequest, config: BotPortConfig | None = None) -> str:
    """The caller's address. Behind a proxy, for a request that came from one
    of ``server.trusted_proxies``: the LAST X-Forwarded-For hop — the one our
    proxy appended; anything to its left came from the client and can be
    forged, and an X-Forwarded-For from anyone else is ignored."""
    cfg = config or get_config()
    remote = request.remote or ""
    if cfg.server.behind_proxy and remote in cfg.server.trusted_proxies:
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return remote


def is_loopback(request: web.BaseRequest) -> bool:
    return (request.remote or "") in _LOOPBACK


def _presented_token(request: web.BaseRequest) -> str:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    tok = request.headers.get("X-BotPort-Token", "").strip()
    if tok:
        return tok
    # The query string and the dashboard cookie serve reads only (a link, a
    # download, the live-events socket); anything that changes state must
    # send the token in a header.
    if request.method in ("GET", "HEAD"):
        tok = request.query.get("token", "").strip()
        if tok:
            return tok
        return request.cookies.get("botport_token", "").strip()
    return ""


def admin_allowed(request: web.BaseRequest, config: BotPortConfig | None = None) -> bool:
    """May this request use the dashboard API?"""
    cfg = config or get_config()
    token = cfg.server.admin_token.strip()
    if token:
        presented = _presented_token(request)
        return bool(presented) and hmac.compare_digest(presented.encode(), token.encode())
    if not cfg.auth.enabled:
        return True
    return not cfg.server.behind_proxy and is_loopback(request)


def _needs_admin(path: str) -> bool:
    return path.startswith("/api/") or path == "/api" or path == "/ws/dashboard"


def _cors_headers(request: web.BaseRequest, cfg: BotPortConfig) -> dict[str, str]:
    origin = request.headers.get("Origin", "")
    allowed = [o.strip().rstrip("/") for o in cfg.server.cors_origins if o.strip()]
    if not origin or not allowed:
        return {}
    if origin.rstrip("/") not in allowed and "*" not in allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-BotPort-Token",
        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Max-Age": "600",
    }


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def security_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    cfg = get_config()
    path = request.path
    cors = _cors_headers(request, cfg) if _needs_admin(path) else {}

    if request.method == "OPTIONS" and cors:
        return web.Response(status=204, headers=cors)

    if path not in _OPEN_PATHS and _needs_admin(path) and not admin_allowed(request, cfg):
        hint = (
            "admin token required (server.admin_token)"
            if cfg.server.admin_token
            else "the BotPort API answers only local requests when auth is enabled; "
                 "set server.admin_token to use it remotely"
        )
        resp: web.StreamResponse = web.json_response({"error": "unauthorized", "detail": hint}, status=401)
    else:
        resp = await handler(request)

    # A WebSocket response is already prepared — its headers are sent.
    if not resp.prepared:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        for k, v in cors.items():
            resp.headers[k] = v
    return resp
