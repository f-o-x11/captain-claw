"""Agent links without a socket, and the agent's own messages to a linked system.

A system that can't keep /link/ws open (a serverless function, a cron job, a
SaaS that only does webhooks) talks HTTP with the same key — ``Authorization:
Bearer <key>``, or ``?key=``:

  POST /link/messages[?wait=<s>]     a message, as on the socket ({"id","text","data","files"})
                                     → 202 {"id","status":"queued","queued":n}; with ``wait``
                                     (≤ 120 s) its answer when it comes in time: 200
                                     {"id","status":"done","event":{…}}. An id seen before →
                                     200 {"id","status":"duplicate","event":{…}} (when answered)
  GET  /link/events?after=<seq>[&wait=<s>]   what Flight Deck has for the system, oldest first
                                     → {"events":[…],"next":<seq to ask after next time>};
                                     ``wait`` (≤ 60 s) holds the request until there is one
  GET  /link/files/<file>            a file an event names, from the agent that made it

An event — the answer to a message that came over HTTP, or a message the agent
sent on its own (the ``linked_system`` tool, /fd/agent-links/send):

  {"seq":n,"type":"reply|error|push","re":"<message id>"|null,"text":"…",
   "files":[{"file":"<id>","name":"…","kind":"image|audio|file","mime":"…",
             "url":"/link/files/<id>"}],"created_at":"…"}

Events are kept a week (the newest 500 per link). Each goes out as soon as it
can: a push on the system's socket when it is connected ({"type":"push",
"seq","text","files":k} and its files as file / file_part frames with
"push": seq, and a socket that connects gets the pushes it missed), else to
the link's webhook, else it waits for /link/events. A webhook POST is the event
plus ``"link"``, signed — ``X-Captain-Signature: sha256=<hex HMAC-SHA256 of
"<X-Captain-Timestamp>.<body>" with the link's signing secret>`` — with
``X-Captain-Event`` (its type) and ``X-Captain-Delivery`` (its seq); a failed
POST is tried again three times (2, 10, 30 s), then the event waits for
/link/events. Webhooks go to public https addresses only, unless
``FD_LINK_WEBHOOKS_PRIVATE=1`` (an on-premises deck) also allows http and
private ones; redirects aren't followed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import socket
import time
import urllib.parse
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from captain_claw.flight_deck import agent_link as al

log = logging.getLogger(__name__)

router = APIRouter()

MAX_WAIT_S = 120.0               # POST /link/messages?wait=
MAX_POLL_WAIT_S = 60.0           # GET /link/events?wait=
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
WEBHOOK_TIMEOUT_S = 10.0
WEBHOOK_RETRIES_S = (0, 2, 10, 30)  # the wait before each try

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}

_tasks: set[asyncio.Task] = set()  # webhook deliveries in flight


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _db():
    from captain_claw.flight_deck.auth import get_db

    return get_db()


# ── Small pure helpers (tested) ───────────────────────────────────────


def kind_of(name: str) -> str:
    ext = PurePosixPath(name).suffix.lower()
    return "image" if ext in _IMAGE_EXTS else "audio" if ext in _AUDIO_EXTS else "file"


def mime_of(name: str) -> str:
    return (mimetypes.guess_type(name)[0] or al._MIME_FALLBACK.get(PurePosixPath(name).suffix.lower())
            or "application/octet-stream")


def file_entry(file_id: str, name: str, kind: str) -> dict:
    return {"file": file_id, "name": name, "kind": kind, "mime": mime_of(name), "url": f"/link/files/{file_id}"}


def public_event(event: dict) -> dict:
    """An event as the system gets it."""
    body = event.get("body") or {}
    return {
        "seq": event["seq"], "type": event["kind"], "re": event.get("re") or None,
        "text": str(body.get("text") or ""), "files": list(body.get("files") or []),
        "created_at": event.get("created_at"),
    }


def sign(secret: str, timestamp: str, body: str) -> str:
    """The webhook signature: hex HMAC-SHA256 of ``"<timestamp>.<body>"``."""
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def new_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def private_webhooks_allowed() -> bool:
    return os.environ.get("FD_LINK_WEBHOOKS_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")


async def webhook_url_problem(url: str) -> str:
    """"" when Flight Deck may post to *url*, else why not: a public https
    address (http and private ones too with FD_LINK_WEBHOOKS_PRIVATE)."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "That isn't a web address."
    private_ok = private_webhooks_allowed()
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "Use an https:// address."
    if parts.scheme == "http" and not private_ok:
        return "Use an https:// address."
    if parts.username or parts.password:
        return "Leave the user name and password out of the address."
    if private_ok:
        return ""
    host = parts.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return f"{host} can't be found."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return f"{host} is a private address ({ip}); Flight Deck posts only to public ones."
    return ""


# ── Delivering events ─────────────────────────────────────────────────


def _webhook_secret(link: dict) -> str:
    from captain_claw.flight_deck import fd_crypto

    try:
        return str(fd_crypto.unseal(str(link.get("webhook_secret") or "")) or "")
    except fd_crypto.CryptoUnavailable:
        return ""


async def post_webhook(link: dict, event: dict) -> tuple[bool, str, bool]:
    """One signed POST of *event* to the link's webhook: (delivered, why not,
    worth trying again)."""
    import httpx

    url = str(link.get("webhook_url") or "")
    problem = await webhook_url_problem(url) if url else "no webhook"
    if problem:
        return False, problem, False
    secret = _webhook_secret(link)
    if not secret:
        return False, "no signing secret", False
    body = json.dumps({"link": link["id"], **public_event(event)}, ensure_ascii=False, separators=(",", ":"))
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json", "User-Agent": "Captain-Agent-Link/1",
        "X-Captain-Event": event["kind"], "X-Captain-Delivery": str(event["seq"]),
        "X-Captain-Timestamp": stamp, "X-Captain-Signature": "sha256=" + sign(secret, stamp, body),
    }
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
    except httpx.HTTPError as exc:
        return False, type(exc).__name__, True
    return 200 <= resp.status_code < 300, f"HTTP {resp.status_code}", True


async def _webhook(link: dict, event: dict) -> None:
    why = ""
    for delay in WEBHOOK_RETRIES_S:
        if delay:
            await asyncio.sleep(delay)
        ok, why, again = await post_webhook(link, event)
        if ok:
            await _db().mark_link_event(event["seq"], "webhook")
            return
        if not again:
            break
    log.warning("agent link %s: webhook for event %s not delivered (%s); kept for /link/events",
                link["id"], event["seq"], why)


async def _push_files(event: dict) -> list[dict]:
    """A push's files as the socket sends them: path, name, kind and the agent that has them."""
    out = []
    for f in (event.get("body") or {}).get("files") or []:
        row = await _db().get_link_file(str(f.get("file") or ""))
        if row:
            out.append({"path": row["path"], "name": row["name"], "kind": row["kind"], "agent": row["agent_slug"]})
    return out


async def deliver(link: dict, event: dict) -> str:
    """Hand *event* to the system now if it can take it — a push on its socket,
    else its webhook (in the background); "" when it waits for /link/events.
    Returns which: "socket", "webhook" or ""."""
    if event["kind"] == "push":
        conn = al._live.get(al._channel_id(str(link["user_id"]), link["id"]))
        if conn is not None and not conn.closed:
            try:
                await conn.send_push(event, await _push_files(event))
                await _db().mark_link_event(event["seq"], "socket")
                return "socket"
            except Exception as exc:
                log.warning("agent link %s: push %s not sent on the socket: %s", link["id"], event["seq"], exc)
    if link.get("webhook_url"):
        _spawn(_webhook(link, event))
        return "webhook"
    return ""


async def send_backlog(conn: al._LinkConn) -> None:
    """A socket that just connected gets the pushes it missed."""
    try:
        for event in await _db().undelivered_link_events(conn.link["id"], "push"):
            if conn.closed:
                return
            await conn.send_push(event, await _push_files(event))
            await _db().mark_link_event(event["seq"], "socket")
    except Exception as exc:
        log.warning("agent link %s: backlog not sent: %s", conn.link.get("id"), exc)


# ── A message that came over HTTP: its answer ─────────────────────────


async def http_frame(conn: al._LinkConn, obj: dict) -> None:
    """A frame about a message that came over HTTP (agent_link routes them
    here): its answer, its error, and its "ready" — which make its event."""
    entry = al._http_waits.get(conn.channel_id, {}).get(obj.get("re"))
    if entry is None or entry.get("finishing"):
        return
    kind = obj.get("type")
    if kind == "reply":
        entry["text"] = str(obj.get("text") or "")
    elif kind == "error":
        entry["error"] = str(obj.get("text") or "Agent error")
        await _finish(conn, obj["re"], entry)
    elif kind == "state" and obj.get("state") == "ready":
        await _finish(conn, obj["re"], entry)


async def _finish(conn: al._LinkConn, re_id: str, entry: dict) -> None:
    entry["finishing"] = True
    db = _db()
    link = conn.link
    files = []
    for item in entry.get("files") or []:
        name = str(item.get("name") or "") or str(item["path"]).rsplit("/", 1)[-1]
        kind = str(item.get("kind") or kind_of(name))
        file_id = await db.add_link_file(link["id"], agent_slug=entry.get("agent_slug") or "",
                                         path=str(item["path"]), name=name, kind=kind)
        files.append(file_entry(file_id, name, kind))
    if entry.get("text") is None and entry.get("error") is not None:
        event = await db.add_link_event(link["id"], "error", re_id, {"text": entry["error"], "files": files})
    else:
        event = await db.add_link_event(link["id"], "reply", re_id, {"text": entry.get("text") or "", "files": files})
    entry["event"] = event  # (the entry stays, finished, until the next message: late frames end here)
    entry["done"].set()
    if not entry.get("waiting"):
        await deliver(link, event)


# ── Routes for the system ─────────────────────────────────────────────


async def _link_from(request: Request) -> tuple[dict, dict]:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("key", "")
    found = await al._link_for_key(key)
    if found is None:
        raise HTTPException(401, "Unknown or missing link key")
    return found


@router.post("/link/messages")
async def post_message(request: Request, wait: float = 0.0):
    link, user = await _link_from(request)
    try:
        obj = await request.json()
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise HTTPException(400, 'Send a JSON object: {"id", "text", "data", "files"}')
    msg_id = al.message_id(obj.get("id")) or "m_" + secrets.token_hex(6)
    text = al.link_message_text(obj.get("text"), obj.get("data"))
    files, why = al.message_files(obj.get("files"))
    if why or not (text or files):
        return JSONResponse({"id": msg_id, "error": why or "The message has no text, data or files."}, 400)
    cid = al._channel_id(str(user["id"]), link["id"])
    frame = al.enqueue(cid, msg_id, text, files, via="http")
    if frame.get("duplicate"):
        event = await _db().link_event_for(link["id"], msg_id)
        return {"id": msg_id, "status": "duplicate", **({"event": public_event(event)} if event else {})}
    if frame["type"] == "error":
        return JSONResponse({"id": msg_id, "error": frame["text"]}, 429)
    entry: dict[str, Any] = {"done": asyncio.Event(), "text": None, "error": None, "files": [],
                             "agent_slug": "", "event": None, "waiting": wait > 0}
    al._http_waits.setdefault(cid, {})[msg_id] = entry
    al._start_worker(cid)
    if wait > 0:
        try:
            await asyncio.wait_for(entry["done"].wait(), min(wait, MAX_WAIT_S))
        except TimeoutError:
            entry["waiting"] = False  # its event goes to the webhook / /link/events when it comes
        if entry["event"] is not None:
            await _db().mark_link_event(entry["event"]["seq"], "response")
            return {"id": msg_id, "status": "done", "event": public_event(entry["event"])}
        if entry["done"].is_set():
            return JSONResponse({"id": msg_id, "error": "The link was removed or given a new key."}, 410)
    return JSONResponse({"id": msg_id, "status": "queued", "queued": frame["queued"]}, 202)


@router.get("/link/events")
async def get_events(request: Request, after: int = 0, wait: float = 0.0, limit: int = 50):
    link, _user = await _link_from(request)
    db = _db()
    deadline = time.monotonic() + max(0.0, min(wait, MAX_POLL_WAIT_S))
    while True:
        events = await db.link_events_after(link["id"], max(0, after), max(1, min(limit, 100)))
        if events or time.monotonic() >= deadline:
            break
        await asyncio.sleep(1.0)
    for event in events:
        if not event["delivered"]:
            await db.mark_link_event(event["seq"], "poll")
    return {"events": [public_event(e) for e in events], "next": events[-1]["seq"] if events else max(0, after)}


@router.get("/link/files/{file_id}")
async def get_file(file_id: str, request: Request):
    import httpx

    link, user = await _link_from(request)
    row = await _db().get_link_file(file_id)
    if row is None or row["link_id"] != link["id"]:
        raise HTTPException(404, "No such file for this link")
    try:
        base, params = await al._agent_endpoint(str(user["id"]), row["agent_slug"])
    except RuntimeError:
        raise HTTPException(503, "The agent that has this file isn't running") from None
    client = httpx.AsyncClient(timeout=120.0)
    try:
        resp = await client.send(client.build_request(
            "GET", f"{base}/api/files/download", params={"path": row["path"], **params}), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "The agent couldn't be reached") from None
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(404 if resp.status_code in (403, 404) else 502, "The agent no longer has this file")

    async def stream():
        sent = 0
        try:
            async for chunk in resp.aiter_bytes():
                sent += len(chunk)
                if sent > MAX_DOWNLOAD_BYTES:
                    break
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    disposition = "attachment; filename*=UTF-8''" + urllib.parse.quote(row["name"])
    return StreamingResponse(stream(), media_type=mime_of(row["name"]), headers={"Content-Disposition": disposition})


# ── Routes for the agent (the linked_system tool) ─────────────────────


class AgentSendBody(BaseModel):
    link: str = ""
    text: str = ""
    files: list[str] = Field(default_factory=list)


def _calling_agent(request: Request) -> tuple[str, str]:
    """(owner, slug) of the agent making this request."""
    from captain_claw.flight_deck.agent_identity import require_agent_caller
    from captain_claw.flight_deck.auth import _fd_auth_enabled

    caller = require_agent_caller(request, what="linked systems")
    owner = caller.owner or ("" if _fd_auth_enabled() else "local")
    if not owner:
        raise HTTPException(403, "This agent has no owner on record, so it has no linked systems")
    return owner, caller.slug


def reachable(links: list[dict], slug: str) -> list[dict]:
    """The links agent *slug* may message on its own: its own (or ones bound
    to no agent in particular) that allow it."""
    return [ln for ln in links if ln.get("agent_may_send") and (ln.get("agent_slug") or "") in ("", slug)]


@router.post("/fd/agent-links/list")
async def agent_list(request: Request):
    owner, slug = _calling_agent(request)
    links = reachable(await _db().list_agent_links(owner), slug)
    return {"links": [{"id": ln["id"], "name": ln["name"], "connected": al._channel_id(owner, ln["id"]) in al._live,
                       "webhook": bool(ln.get("webhook_url"))} for ln in links]}


@router.post("/fd/agent-links/send")
async def agent_send(request: Request, body: AgentSendBody = Body(default=AgentSendBody())):
    import httpx

    owner, slug = _calling_agent(request)
    db = _db()
    links = reachable(await db.list_agent_links(owner), slug)
    want = body.link.strip()
    link = (next((ln for ln in links if ln["id"] == want), None)
            or next((ln for ln in links if ln["name"].casefold() == want.casefold()), None))
    if link is None:
        names = ", ".join(f'"{ln["name"]}"' for ln in links)
        raise HTTPException(404, f'No linked system "{want}" this agent can message.'
                                 + (f" It can message {names}." if names else " It can message none."))
    text = body.text.strip()[:al.MAX_TEXT_CHARS]
    paths = [p.strip() for p in body.files if p and p.strip()]
    if len(paths) > al.MAX_FILES:
        raise HTTPException(400, f"At most {al.MAX_FILES} files in one message")
    if not (text or paths):
        raise HTTPException(400, "Give the message's text, its files, or both")
    files = []
    if paths:
        base, params = await al._agent_endpoint(owner, slug)
        async with httpx.AsyncClient(timeout=30.0) as client:
            for path in paths:  # the agent hands out only saved/, output/ and registered files
                async with client.stream("GET", f"{base}/api/files/download",
                                         params={"path": path, **params}) as resp:
                    if resp.status_code != 200:
                        raise HTTPException(400, f'"{path}" isn\'t a file this agent can send (it must be one '
                                                 "of its files: saved/, output/ or its Files list)")
                name = PurePosixPath(path).name
                file_id = await db.add_link_file(link["id"], agent_slug=slug, path=path, name=name, kind=kind_of(name))
                files.append(file_entry(file_id, name, kind_of(name)))
    event = await db.add_link_event(link["id"], "push", "", {"text": text, "files": files})
    how = await deliver(link, event)
    log.info("agent link %s: agent %s sent push %s (%s)", link["id"], slug, event["seq"], how or "kept")
    return {"seq": event["seq"], "link": link["name"], "delivered": how or "kept"}
