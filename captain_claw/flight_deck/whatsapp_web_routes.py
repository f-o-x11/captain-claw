"""HTTP side of WhatsApp Web (service: :mod:`.whatsapp_web`, Flight Deck's
end of it: :mod:`.whatsapp_web_supervisor`).

* ``/fd/whatsapp-web`` — the user's own link (Connections → WhatsApp Web):
  link with consent (a QR follows), a pairing code instead of the QR,
  reconnect, cancel, disconnect.
  Also the retention choice and a peek at what's kept.
* ``/fd/whatsapp-web/agent/*`` — what the user's agents read: owner only,
  identified like the Google / Telegram routes (``X-Agent-Auth``); a shared
  agent's member never gets here (GrantGuardMiddleware refuses member markers).
* ``/fd/admin/whatsapp-web`` — the deck's switch, off by default.

Read-only: nothing here sends, reacts or marks anything read in WhatsApp.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import whatsapp_web_supervisor as sup
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.flight_deck.whatsapp_web.link_state import now_iso
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["whatsapp-web"])


async def _unavailable_reason(db: Any) -> str:
    """Why this deck can't link WhatsApp Web right now ("" when it can)."""
    from captain_claw.flight_deck import google_oauth_routes as _g

    if sup.kill_switch():
        return "WhatsApp Web is switched off on this deck (FD_WHATSAPP_WEB=off)."
    if _g._fd_desktop_deck():
        return "WhatsApp Web isn't available in the desktop app yet."
    if not _g._fd_auth_enabled():
        # Agents read through their owner; a deck that can't tell its users
        # apart has no owner to read for.
        return "WhatsApp Web needs Flight Deck sign-in, which is off on this deck (FD_AUTH_ENABLED)."
    if not fd_crypto.available():
        return ("This Flight Deck can't encrypt kept messages: it needs the 'cryptography' "
                "package (pip install cryptography) and a restart.")
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


# ── the user's peek and retention ───────────────────────────────────


@router.get("/fd/whatsapp-web/recent")
async def recent(limit: int = 20, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """The newest kept messages — what the user's agents see."""
    s = sup.get()
    if s is None or not s.user_has_link(user["id"]) or await _unavailable_reason(get_db()):
        return {"messages": []}
    try:
        data = await s.request("POST", f"/links/{user['id']}/messages",
                               {"limit": max(1, min(int(limit or 20), 100))})
    except (sup.ServiceDown, sup.ServiceError) as exc:
        raise _service_error(exc) from None
    return {"messages": data.get("messages") or []}


class RetentionBody(BaseModel):
    # None = keep everything.
    days: int | None = None


@router.put("/fd/whatsapp-web/retention")
async def set_retention(body: RetentionBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _call(user, "PUT", "/retention", {"days": body.days})


# ── agents (owner only) ─────────────────────────────────────────────

_NOT_LINKED = ("Your user hasn't linked WhatsApp — they can in Flight Deck → Connections → "
               "WhatsApp Web")
# Link states whose kept messages an agent may read.
_READABLE = ("connected", "disconnected", "connecting", "stream_replaced", "temporary_ban", "outdated")


async def _agent_owner(request: Request) -> str:
    from captain_claw.flight_deck import google_oauth_routes as _g

    _g._authorize_agent_call(request)
    return await _g._agent_owner(request, service="WhatsApp Web")


def _agent_slug(request: Request) -> str:
    """The calling agent's slug on this deck (its news cursor's key)."""
    try:
        from captain_claw.flight_deck.server import _find_agent_by_auth

        matched, _owner, slug = _find_agent_by_auth(request.headers.get("X-Agent-Auth", ""))
    except Exception:
        return ""
    return str(slug) if matched and slug else ""


def _readable(s: sup.Supervisor, owner: str) -> bool:
    link = s.offline_view(owner) if s.user_has_link(owner) else {"state": "off"}
    return link.get("state") in _READABLE and bool(link.get("linked_at"))


async def _agent_call(request: Request, path: str, body: dict[str, Any] | None = None) -> tuple[str, dict]:
    reason = await _unavailable_reason(get_db())
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    owner = await _agent_owner(request)
    s = _supervisor()
    if not _readable(s, owner):
        raise HTTPException(status_code=409, detail=_NOT_LINKED)
    try:
        return owner, await s.request("GET" if body is None else "POST", f"/links/{owner}{path}", body)
    except (sup.ServiceDown, sup.ServiceError) as exc:
        raise _service_error(exc) from None


@router.get("/fd/whatsapp-web/agent/status")
async def agent_status(request: Request) -> dict[str, Any]:
    """Whether the calling agent's owner has WhatsApp linked (no network)."""
    s = sup.get()
    if s is None or await _unavailable_reason(get_db()):
        return {"connected": False}  # before asking whose agent: off decks have no owners
    return {"connected": _readable(s, await _agent_owner(request))}


@router.post("/fd/whatsapp-web/agent/chats")
async def agent_chats(request: Request) -> dict[str, Any]:
    _owner, data = await _agent_call(request, "/chats")
    return data


class AgentMessagesBody(BaseModel):
    chat: str = ""
    query: str = ""
    since: int | None = None
    until: int | None = None
    limit: int = 50


@router.post("/fd/whatsapp-web/agent/messages")
async def agent_messages(body: AgentMessagesBody, request: Request) -> dict[str, Any]:
    """Messages, newest first — of one chat (id, number or name) or all of them;
    ``query`` keeps those containing every word. A look at the newest ones
    (no ``until``, no ``query``) counts as seen for this agent's news line."""
    _owner, data = await _agent_call(request, "/messages", {
        **body.model_dump(), "agent": _agent_slug(request), "mark_seen": True,
    })
    return data


class AgentContactsBody(BaseModel):
    query: str = ""
    limit: int = 20


@router.post("/fd/whatsapp-web/agent/contacts")
async def agent_contacts(body: AgentContactsBody, request: Request) -> dict[str, Any]:
    _owner, data = await _agent_call(request, "/contacts", body.model_dump())
    return data


@router.post("/fd/whatsapp-web/agent/news")
async def agent_news(request: Request) -> dict[str, Any]:
    """What others wrote since this agent last looked — counts per chat, no
    text — and the look itself moves the agent's cursor (an owner's own turn
    calls it once)."""
    s = sup.get()
    if s is None or await _unavailable_reason(get_db()):
        return {"connected": False}
    owner = await _agent_owner(request)
    if not _readable(s, owner):
        return {"connected": False}
    try:
        return await s.request("POST", f"/links/{owner}/news", {"agent": _agent_slug(request) or "_"})
    except (sup.ServiceDown, sup.ServiceError):
        return {"connected": False}
