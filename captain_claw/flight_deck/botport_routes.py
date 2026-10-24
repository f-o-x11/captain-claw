"""BotPort link REST endpoints: status for everyone, configuration for admins.

The link itself lives in :mod:`botport_link`; these routes read its state and
persist its settings (system setting ``fd:botport-link``, secret sealed).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck import botport_link as bl
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import _fd_auth_enabled, get_current_user, get_db

router = APIRouter(prefix="/fd/botport", tags=["botport"])

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


async def link_admin(request: Request, admin: dict = Depends(require_admin)) -> dict:
    """An admin — and on a deck with sign-in off (where everyone is the local
    admin) only from this machine: the link's settings decide which agents
    other decks may drive."""
    if not _fd_auth_enabled():
        host = request.client.host if request.client else ""
        if host not in _LOOPBACK:
            raise HTTPException(403, "BotPort settings can only be changed from this machine while "
                                     "Flight Deck sign-in is off (or set FD_BOTPORT_* environment variables)")
    return admin


class LinkConfigUpdate(BaseModel):
    """Partial update; omitted fields keep their value. ``secret`` replaces the
    stored one only when non-empty (``clear_secret`` empties it)."""

    enabled: bool | None = None
    url: str | None = None
    deck_name: str | None = None
    key: str | None = None
    secret: str | None = None
    clear_secret: bool = False
    expose: list[str] | None = None
    accept_from: list[str] | None = None
    allow_outbound: bool | None = None
    max_concurrent: int | None = None
    max_inbound_timeout: float | None = None
    deny_tools: list[str] | None = None


async def _stored() -> dict[str, Any]:
    return bl.unseal_settings(await get_db().get_system_setting(bl.SETTING_KEY))


def _public_config(stored: dict[str, Any]) -> dict[str, Any]:
    link = bl.get_link()
    eff = link.effective(stored).to_dict()
    secret_set = bool(eff.pop("secret", ""))
    env = bl.env_overrides()
    return {
        **eff,
        "secret_set": secret_set,
        "env_locked": sorted(k for k in env if k in eff or k == "secret"),
        "deck_id": link.deck_id,
        "problems": link.effective(stored).problems(),
        "normalized_url": _safe_normalize(eff.get("url", "")),
    }


def _safe_normalize(url: str) -> str:
    try:
        return bl.normalize_url(url) if url else ""
    except ValueError:
        return ""


@router.get("/status")
async def botport_status(user: dict = Depends(get_current_user)):
    """Link state. Admins get everything; others see what they can consult."""
    link = bl.get_link()
    full = link.status()
    if user.get("role") == "admin":
        return full
    return {
        "enabled": full["enabled"],
        "state": full["state"],
        "deck_name": full["deck_name"],
        "remote_agents": [r["name"] for r in link.remote_agents()],
    }


@router.get("/config")
async def botport_get_config(_admin: dict = Depends(link_admin)):
    return _public_config(await _stored())


@router.put("/config")
async def botport_put_config(body: LinkConfigUpdate, _admin: dict = Depends(link_admin)):
    stored = await _stored()
    updates = body.model_dump(exclude_none=True)
    clear_secret = bool(updates.pop("clear_secret", False))
    secret = updates.pop("secret", None)
    if "url" in updates:
        updates["url"] = updates["url"].strip()
        if updates["url"]:
            try:
                bl.normalize_url(updates["url"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
    if "deck_name" in updates:
        updates["deck_name"] = updates["deck_name"].strip()
        if updates["deck_name"] and not bl.valid_deck_name(updates["deck_name"]):
            raise HTTPException(400, "deck name must be letters, digits, '-', '_' or '.' "
                                     "(max 63, no spaces or '@')")
    stored.update(updates)
    if clear_secret:
        stored["secret"] = ""
    elif secret:
        stored["secret"] = secret.strip()
    # Normalise through the dataclass so only known, well-typed fields persist.
    clean = bl.LinkSettings.from_dict(stored).to_dict()
    await get_db().set_system_setting(bl.SETTING_KEY, bl.seal_settings(clean))
    await bl.get_link().apply(clean)
    return _public_config(clean)


@router.post("/reconnect")
async def botport_reconnect(_admin: dict = Depends(link_admin)):
    await bl.get_link().reconnect()
    return {"ok": True}


@router.get("/agents")
async def botport_local_agents(_admin: dict = Depends(link_admin)):
    """This deck's agents (all owners) with whether each is exposed."""
    return {"agents": await bl.get_link().local_agents()}


@router.get("/probe")
async def botport_probe(url: str = "", _admin: dict = Depends(link_admin)):
    """Is the hub reachable? GETs its /healthz (does not register, so it never
    disturbs the live link)."""
    import httpx

    target = url or bl.get_link().settings.url
    try:
        ws_url = bl.normalize_url(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    health = bl.health_url(ws_url)
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(health)
        if resp.status_code == 404:
            return {"ok": False, "url": health, "error": "no /healthz there — not BotPort, or a BotPort "
                                                         "older than protocol 2 (upgrade it)"}
        if resp.status_code != 200:
            return {"ok": False, "url": health, "error": f"HTTP {resp.status_code} from {health}"}
        data = resp.json()
        if data.get("service") != "botport":
            return {"ok": False, "url": health, "error": "that address answered, but it is not BotPort"}
        proto = int(data.get("protocol") or 1)
        return {
            "ok": proto >= bl.PROTOCOL,
            "url": health,
            "version": data.get("version", ""),
            "protocol": proto,
            "error": "" if proto >= bl.PROTOCOL else
                     f"hub speaks protocol {proto}; Flight Deck links need protocol {bl.PROTOCOL} (upgrade BotPort)",
        }
    except Exception as exc:
        return {"ok": False, "url": health, "error": bl._describe(exc)}
