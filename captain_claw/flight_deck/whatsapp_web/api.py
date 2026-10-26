"""The service's loopback HTTP API — Flight Deck is its only caller.

Bound to 127.0.0.1 on a port the OS picks (``service.json`` says which), and
every route needs ``Authorization: Bearer <service.secret>``. The user id in
the path is Flight Deck's to vouch for: it has already authenticated the
person and only ever passes their own id.

* ``GET    /health``
* ``GET    /links/{user_id}``
* ``POST   /links/{user_id}/start``      ``{"consented_at": "…"}`` → pairing (QR)
* ``POST   /links/{user_id}/pair-code``  ``{"phone": "…"}`` → an 8-character code
* ``POST   /links/{user_id}/reconnect``  start again on the kept session
* ``POST   /links/{user_id}/cancel``     forget a link that never paired
* ``DELETE /links/{user_id}``            log out, stop, delete
* ``GET    /links/{user_id}/chats``      the chats kept, latest first
* ``POST   /links/{user_id}/messages``   ``{chat, query, since, until, limit, agent, mark_seen}``
* ``POST   /links/{user_id}/contacts``   ``{query, limit}``
* ``POST   /links/{user_id}/news``       ``{agent}`` → what's new for that agent (moves its cursor)
* ``PUT    /links/{user_id}/retention``  ``{days}`` (null = keep everything)
* ``GET    /events?after=<seq>``         bells for Flight Deck to ring
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck.whatsapp_web.engine import Engine, LinkError


class StartBody(BaseModel):
    consented_at: str


class PairCodeBody(BaseModel):
    phone: str


class MessagesBody(BaseModel):
    chat: str = ""
    query: str = ""
    since: int | None = None
    until: int | None = None
    limit: int = 50
    # The agent reading (its news cursor), and whether this look counts as seen.
    agent: str = ""
    mark_seen: bool = False


class ContactsBody(BaseModel):
    query: str = ""
    limit: int = 20


class NewsBody(BaseModel):
    agent: str


class RetentionBody(BaseModel):
    # None = keep everything.
    days: int | None = None


def build_app(engine: Engine, secret: str, *, version: str = "", lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="WhatsApp Web service", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    def authorized(request: Request) -> None:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    def answer(fn):
        async def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await fn(*args, **kwargs)
            except LinkError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return run

    auth = [Depends(authorized)]

    @app.get("/health", dependencies=auth)
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "available": not engine.unavailable,
            "reason": engine.unavailable,
            "neonize": version,
            "links": engine.counts(),
        }

    @app.get("/links/{user_id}", dependencies=auth)
    async def get_link(user_id: str) -> dict[str, Any]:
        try:
            return engine.view(user_id)
        except LinkError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None

    @app.post("/links/{user_id}/start", dependencies=auth)
    async def start(user_id: str, body: StartBody) -> dict[str, Any]:
        return await answer(engine.link)(user_id, consented_at=body.consented_at)

    @app.post("/links/{user_id}/pair-code", dependencies=auth)
    async def pair_code(user_id: str, body: PairCodeBody) -> dict[str, Any]:
        return await answer(engine.pair_code)(user_id, body.phone)

    @app.post("/links/{user_id}/reconnect", dependencies=auth)
    async def reconnect(user_id: str) -> dict[str, Any]:
        return await answer(engine.reconnect)(user_id)

    @app.post("/links/{user_id}/cancel", dependencies=auth)
    async def cancel(user_id: str) -> dict[str, Any]:
        return await answer(engine.cancel)(user_id)

    @app.delete("/links/{user_id}", dependencies=auth)
    async def unlink(user_id: str) -> dict[str, Any]:
        return await answer(engine.unlink)(user_id)

    @app.get("/links/{user_id}/chats", dependencies=auth)
    async def chats(user_id: str) -> dict[str, Any]:
        return await answer(engine.chats)(user_id)

    @app.post("/links/{user_id}/messages", dependencies=auth)
    async def messages(user_id: str, body: MessagesBody) -> dict[str, Any]:
        return await answer(engine.messages)(
            user_id, chat=body.chat, query=body.query, since=body.since, until=body.until,
            limit=body.limit, agent=body.agent, mark_seen=body.mark_seen)

    @app.post("/links/{user_id}/contacts", dependencies=auth)
    async def contacts(user_id: str, body: ContactsBody) -> dict[str, Any]:
        return await answer(engine.contacts)(user_id, query=body.query, limit=body.limit)

    @app.post("/links/{user_id}/news", dependencies=auth)
    async def news(user_id: str, body: NewsBody) -> dict[str, Any]:
        return await answer(engine.news)(user_id, agent=body.agent)

    @app.put("/links/{user_id}/retention", dependencies=auth)
    async def retention(user_id: str, body: RetentionBody) -> dict[str, Any]:
        return await answer(engine.set_retention)(user_id, body.days)

    @app.get("/events", dependencies=auth)
    async def events(after: int = 0) -> dict[str, Any]:
        return {"events": engine.events_after(after)}

    return app
