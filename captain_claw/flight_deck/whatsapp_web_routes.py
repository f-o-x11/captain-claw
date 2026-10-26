"""HTTP side of WhatsApp Web (service: :mod:`.whatsapp_web`, Flight Deck's
end of it: :mod:`.whatsapp_web_supervisor`).

* ``/fd/whatsapp-web`` — the user's own link (Connections → WhatsApp Web):
  link with consent (a QR follows), a pairing code instead of the QR,
  reconnect, cancel, disconnect.
* ``/fd/admin/whatsapp-web`` — the deck's switch, off by default.

Phase 0: linking only. Nothing here reads, sends or marks anything in WhatsApp;
agents get no route yet.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from captain_claw.flight_deck import whatsapp_web_supervisor as sup
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.flight_deck.whatsapp_web.link_state import now_iso
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["whatsapp-web"])


async def _unavailable_reason(db: Any) -> str:
    """Why this deck can't link WhatsApp Web right now ("" when it can)."""
    if sup.kill_switch():
        return "WhatsApp Web is switched off on this deck (FD_WHATSAPP_WEB=off)."
    if not sup.installed():
        return ("WhatsApp Web isn't installed on this deck: pip install "
                "'captain-claw[whatsapp-web]' and restart Flight Deck.")
    if not (await load_cfg(db))["allowed"]:
        return "An admin hasn't allowed WhatsApp Web on this deck."
    if sup.get() is None:
        return "WhatsApp Web hasn't started on this deck: restart Flight Deck."
    return ""


async def load_cfg(db: Any) -> dict[str, Any]:
    return await sup.load_config(db)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, sup.ServiceError):
        return HTTPException(status_code=exc.status, detail=exc.detail)
    return HTTPException(status_code=503, detail=str(exc) or "The WhatsApp Web service isn't running")


async def _view(user: dict, *, link: dict[str, Any] | None = None) -> dict[str, Any]:
    db = get_db()
    uid = user["id"]
    reason = await _unavailable_reason(db)
    s = sup.get()
    out: dict[str, Any] = {
        "available": not reason,
        "reason": reason,
        "allowed": (await load_cfg(db))["allowed"],
        "can_admin": user.get("role") == "admin",
        "link": {"state": "off", "running": False},
    }
    if link is not None:
        out["link"] = link
    elif s is not None and s.user_has_link(uid):
        if reason:
            out["link"] = s.offline_view(uid)
        else:
            try:
                out["link"] = await s.request("GET", f"/links/{uid}")
            except (sup.ServiceDown, sup.ServiceError) as exc:
                out["link"] = {**s.offline_view(uid), "detail": str(exc)}
    return out


def _supervisor() -> sup.Supervisor:
    s = sup.get()
    if s is None:
        raise HTTPException(status_code=503, detail="WhatsApp Web hasn't started on this deck: restart Flight Deck.")
    return s


async def _call(user: dict, method: str, suffix: str = "", body: dict[str, Any] | None = None) -> dict[str, Any]:
    reason = await _unavailable_reason(get_db())
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    try:
        link = await _supervisor().request(method, f"/links/{user['id']}{suffix}", body)
    except (sup.ServiceDown, sup.ServiceError) as exc:
        raise _service_error(exc) from None
    return await _view(user, link=link)


# ── the user's own link ─────────────────────────────────────────────


@router.get("/fd/whatsapp-web")
async def my_link(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _view(user)


class LinkBody(BaseModel):
    # Every new link needs it: the user read that this is an unofficial client
    # (against WhatsApp's terms, bans happen) and that agents will read it.
    consent: bool = False


@router.post("/fd/whatsapp-web/link")
async def start_link(body: LinkBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    if not body.consent:
        raise HTTPException(status_code=422, detail="Linking WhatsApp needs your consent")
    return await _call(user, "POST", "/start", {"consented_at": now_iso()})


class PairCodeBody(BaseModel):
    phone: str


@router.post("/fd/whatsapp-web/pair-code")
async def pair_code(body: PairCodeBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _call(user, "POST", "/pair-code", {"phone": body.phone})


@router.post("/fd/whatsapp-web/reconnect")
async def reconnect(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _call(user, "POST", "/reconnect")


@router.post("/fd/whatsapp-web/cancel")
async def cancel(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _call(user, "POST", "/cancel")


@router.delete("/fd/whatsapp-web")
async def unlink(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Disconnect: log the device out (it leaves the phone's Linked devices) and
    delete everything kept. With the service off, the files go and the phone
    still lists the device until it's removed there."""
    s = _supervisor()
    uid = user["id"]
    if not s.user_has_link(uid):
        out = await _view(user)
        out["removed_from_phone"] = False
        return out
    reason = await _unavailable_reason(get_db())
    if reason:
        s.forget_local(uid)
        out = await _view(user)
        out["removed_from_phone"] = False
        return out
    try:
        link = await s.request("DELETE", f"/links/{uid}")
    except (sup.ServiceDown, sup.ServiceError) as exc:
        log.warning("WhatsApp Web unlink for %s via the service failed (%s); deleting locally", uid, exc)
        s.forget_local(uid)
        link = {"state": "off", "running": False, "removed_from_phone": False}
    out = await _view(user, link={k: v for k, v in link.items() if k != "removed_from_phone"})
    out["removed_from_phone"] = bool(link.get("removed_from_phone"))
    return out


# ── the deck's switch ───────────────────────────────────────────────


async def _admin_view() -> dict[str, Any]:
    db = get_db()
    s = sup.get()
    service = None
    if s is not None and s.running():
        try:
            service = await s.request("GET", "/health")
        except (sup.ServiceDown, sup.ServiceError):
            service = None
    return {
        "allowed": (await load_cfg(db))["allowed"],
        "installed": sup.installed(),
        "kill_switch": sup.kill_switch(),
        "service": service,
    }


@router.get("/fd/admin/whatsapp-web")
async def admin_get(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    return await _admin_view()


class AdminBody(BaseModel):
    allowed: bool


@router.put("/fd/admin/whatsapp-web")
async def admin_put(body: AdminBody, _admin: dict = Depends(require_admin)) -> dict[str, Any]:
    db = get_db()
    await sup.save_config(db, allowed=body.allowed)
    s = sup.get()
    if s is not None:
        if not body.allowed:
            await s.stop()
        elif s.any_links() and not sup.kill_switch() and sup.installed():
            try:
                await s.ensure_running()
            except sup.ServiceDown as exc:
                log.warning("WhatsApp Web service not started: %s", exc)
    return await _admin_view()
