"""An agent's way to its owner's WhatsApp (WhatsApp Web, linked as a device).

Flight Deck's WhatsApp Web service keeps the owner's chats
(``flight_deck/whatsapp_web/``); the agent reads them, and sends where the owner
allows it, through ``/fd/whatsapp-web/agent/*`` — owner only, so a shared
agent's member never reaches them.

:func:`usable` is the synchronous answer the tool registry needs (the
``whatsapp_web`` tool shows only while it is True); :func:`refresh_connected`
refreshes it and runs once per turn. There is no standalone mode: without
Flight Deck there is no linked WhatsApp.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

MEMBER_REFUSED = (
    "The user's WhatsApp isn't available during a shared-agent member's turn — it "
    "belongs to the agent's owner."
)
_OLD_DECK = "this Flight Deck has no WhatsApp Web yet — it needs an update and a restart"

# Whether the owner has WhatsApp linked: (connected, checked_at).
_CONNECTED: tuple[bool, float] = (False, 0.0)
_LAST_CHECK_AT = 0.0
# While connected, re-check at most this often (a turn may refresh many times).
_REFRESH_MIN_GAP = 20.0
# How long a positive check keeps the tool listed without a fresh one.
_USABLE_MAX_AGE = 900.0


class WhatsAppWebError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _fd_base() -> str:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._flight_deck_base()


def _member_call() -> bool:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._member_call()


def _fd_headers() -> dict[str, str]:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    # Owner-only: never the member grant — /fd/whatsapp-web/* is not grant-aware.
    return GoogleOAuthManager._flight_deck_headers(as_speaker=False)


def _mark(connected: bool) -> None:
    global _CONNECTED
    _CONNECTED = (connected, time.time())


def usable() -> bool:
    """Whether the whatsapp_web tool should be offered right now (sync)."""
    connected, at = _CONNECTED
    return connected and at > 0 and time.time() - at <= _USABLE_MAX_AGE


async def refresh_connected(force: bool = False) -> bool:
    """Re-check whether the owner has WhatsApp linked. A member's turn reports
    not connected and leaves the owner's flag alone; an unreachable Flight Deck
    keeps the previous flag."""
    global _LAST_CHECK_AT
    if not _fd_base() or _member_call():
        return False
    connected, _at = _CONNECTED
    if not force and connected and time.time() - _LAST_CHECK_AT < _REFRESH_MIN_GAP:
        _mark(True)
        return True
    _LAST_CHECK_AT = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_fd_base()}/fd/whatsapp-web/agent/status", headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck WhatsApp Web status check failed: %s", exc)
        return usable()
    try:
        connected = resp.status_code == 200 and bool((resp.json() or {}).get("connected"))
    except Exception:
        connected = False
    _mark(connected)
    return connected


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else ""
    return detail if isinstance(detail, str) else ""


async def _post(path: str, body: dict[str, Any], *, timeout: float = 30) -> dict[str, Any]:
    if _member_call():
        raise WhatsAppWebError(0, MEMBER_REFUSED)
    if not _fd_base():
        raise WhatsAppWebError(0, "The user's WhatsApp lives in Flight Deck, and this agent isn't running under one")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{_fd_base()}{path}", json=body, headers=_fd_headers())
    except Exception as exc:
        raise WhatsAppWebError(0, f"Could not reach Flight Deck for WhatsApp: {type(exc).__name__}") from None
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            raise WhatsAppWebError(200, "Flight Deck returned a non-JSON answer") from None
        return data if isinstance(data, dict) else {}
    detail = _detail(resp)
    if resp.status_code in (404, 405) and detail in ("", "Not Found", "Method Not Allowed"):
        detail = _OLD_DECK
    if resp.status_code in (401, 403, 409):
        _mark(False)
    raise WhatsAppWebError(resp.status_code, detail or f"Flight Deck refused (HTTP {resp.status_code})")


# ── what's new, once per owner turn ─────────────────────────────────

_NEWS_CHATS = 5
_TITLE_MAX = 40


def _clean_title(title: Any) -> str:
    """A chat name for the context line: other people chose it, so one line,
    no brackets or quotes, short."""
    text = " ".join(str(title or "").split())
    text = "".join(ch for ch in text if ch.isprintable() and ch not in "[]{}<>\"“”")
    return (text[:_TITLE_MAX - 1] + "…") if len(text) > _TITLE_MAX else (text or "a chat")


def news_line(data: dict[str, Any] | None, *, detail: str = "normal") -> str:
    """One context line from Flight Deck's news ("" when nothing is new):
    counts and chat names only, never message text."""
    if not isinstance(data, dict) or not data.get("connected"):
        return ""
    chats = [c for c in (data.get("chats") or []) if isinstance(c, dict) and int(c.get("new") or 0) > 0]
    if not chats or detail == "nano":
        return ""
    parts = []
    for c in chats[:_NEWS_CHATS]:
        n, name = int(c["new"]), _clean_title(c.get("title"))
        if detail == "micro":
            parts.append(f"\"{name}\" {n}")
        elif c.get("kind") == "group":
            parts.append(f"{n} new in \"{name}\" (group)")
        else:
            parts.append(f"{n} new from \"{name}\"")
    more = len(chats) - _NEWS_CHATS
    if more > 0:
        parts.append(f"+{more} more chat{'' if more == 1 else 's'}")
    when = "in the last day" if data.get("first_look") else "since your last turn"
    if detail == "micro":
        return f"WhatsApp new ({when}): {', '.join(parts)} — whatsapp_web reads them."
    return (
        f"WhatsApp {when}: {', '.join(parts)}. Read them with whatsapp_web if they bear on "
        "what the user is doing or look urgent; otherwise don't bring them up."
    )


async def news() -> dict[str, Any] | None:
    """What's new on the owner's WhatsApp since this agent's last look — once
    per owner turn (the look moves the agent's cursor in Flight Deck). Also
    refreshes the tool's "connected" flag. None when it can't be asked."""
    if not _fd_base() or _member_call():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_fd_base()}/fd/whatsapp-web/agent/news",
                                     json={}, headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck WhatsApp news failed: %s", exc)
        return None
    if resp.status_code != 200:
        if resp.status_code in (401, 403, 404):
            _mark(False)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    _mark(bool(data.get("connected")))
    return data


async def chats() -> dict[str, Any]:
    return await _post("/fd/whatsapp-web/agent/chats", {})


async def messages(
    *, chat: str = "", query: str = "", since: int | None = None, until: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _post("/fd/whatsapp-web/agent/messages", {
        "chat": chat, "query": query, "since": since, "until": until, "limit": limit,
    })


async def attachment(*, chat: str, message_id: str) -> tuple[bytes, dict[str, Any]]:
    """One message's file (downloaded by Flight Deck from WhatsApp's servers)
    and its details: kind, mimetype, file_name, seconds, size, chat, ts."""
    if _member_call():
        raise WhatsAppWebError(0, MEMBER_REFUSED)
    if not _fd_base():
        raise WhatsAppWebError(0, "The user's WhatsApp lives in Flight Deck, and this agent isn't running under one")
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            resp = await client.post(f"{_fd_base()}/fd/whatsapp-web/agent/attachment",
                                     json={"chat": chat, "id": message_id}, headers=_fd_headers())
    except Exception as exc:
        raise WhatsAppWebError(0, f"Could not reach Flight Deck for WhatsApp: {type(exc).__name__}") from None
    if resp.status_code != 200:
        detail = _detail(resp)
        if resp.status_code in (404, 405) and detail in ("", "Not Found", "Method Not Allowed"):
            detail = _OLD_DECK
        if resp.status_code in (401, 403):
            _mark(False)
        raise WhatsAppWebError(resp.status_code, detail or f"Flight Deck refused (HTTP {resp.status_code})")
    try:
        meta = json.loads(resp.headers.get("x-waw-meta") or "{}")
    except ValueError:
        meta = {}
    return resp.content, meta if isinstance(meta, dict) else {}


async def send(*, chat: str, text: str, allow_repeat: bool = False) -> dict[str, Any]:
    """Send one text message through Flight Deck's gate (sending on, the chat
    whitelisted, within the limits). Raises WhatsAppWebError with the reason."""
    return await _post("/fd/whatsapp-web/agent/send",
                       {"chat": chat, "text": text, "allow_repeat": bool(allow_repeat)}, timeout=75)


async def contacts(*, query: str = "", limit: int = 20) -> dict[str, Any]:
    return await _post("/fd/whatsapp-web/agent/contacts", {"query": query, "limit": limit})
