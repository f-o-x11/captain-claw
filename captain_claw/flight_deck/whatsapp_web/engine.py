"""The service's links: one per Flight Deck user, each with its own client.

Every operation on one user's link runs under that link's lock, so a double
click on Link or an unlink racing a reconnect can't leave two clients on one
session (WhatsApp would answer with "stream replaced").

The client comes from an *adapter factory* (:func:`.neonize_adapter.make_adapter`
in the service, a fake in tests), so everything here runs without WhatsApp.

Bells: :meth:`Engine.events_after` hands Flight Deck the notices
:func:`.link_state.apply_event` raised (unlinked on the phone, stream
replaced, temporary ban, outdated client); Flight Deck rings them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from captain_claw.flight_deck.whatsapp_web.link_state import (
    NOT_RESUMED,
    LinkState,
    apply_event,
    normalize_phone,
    now_iso,
)
from captain_claw.flight_deck.whatsapp_web.store import RETENTION_CHOICES, ChatNotFound, Store

log = logging.getLogger("whatsapp_web.engine")

# How long a QR / pairing code may wait for the phone before the link is dropped.
PAIRING_TTL = 180.0
# History asked for at link time ("keep everything" → ten years; see the plan).
HISTORY_DAYS = 3650
# Bells kept for Flight Deck to collect.
_MAX_EVENTS = 500
_LOGOUT_TIMEOUT = 15.0
_STOP_TIMEOUT = 10.0
# A folder name under the data dir: no "." / ".." / hidden names, nothing that climbs out.
_USER_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}$")
_SESSION_FILES = ("session.sqlite3", "session.sqlite3-wal", "session.sqlite3-shm", "session.sqlite3-journal")
_STORE_FILES = ("store.sqlite3", "store.sqlite3-wal", "store.sqlite3-shm", "store.sqlite3-journal")
# Events that only feed the store: state.json isn't rewritten for each.
_STORE_ONLY = frozenset({"message", "directory"})
_PURGE_EVERY = 3600.0
# Bigger attachments are refused rather than pulled into memory.
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
# What WhatsApp's servers say when a file is gone (media expires there).
_GONE_MARKERS = ("404", "410", "not found", "expired", "gone", "no longer")


class Adapter(Protocol):
    async def connect(self) -> None: ...
    async def pair_code(self, phone: str) -> str: ...
    async def download(self, field: str, raw_hex: str) -> bytes: ...
    async def logout(self) -> None: ...
    async def stop(self) -> None: ...


EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
AdapterFactory = Callable[..., Adapter]


class LinkError(Exception):
    """A request the engine refuses; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class _Link:
    user_id: str
    dir: Path
    state: LinkState
    adapter: Adapter | None = None
    store: Store | None = None
    # Bumped for every client started, so a stopped client's late events are ignored.
    gen: int = 0
    watchdog: asyncio.Task | None = None
    pair_code_at: float = 0.0
    started_at: float = 0.0

    @property
    def session(self) -> Path:
        return self.dir / "session.sqlite3"

    @property
    def state_file(self) -> Path:
        return self.dir / "state.json"

    @property
    def store_file(self) -> Path:
        return self.dir / "store.sqlite3"


def _qr_svg(code: str) -> str:
    """The QR as an SVG data URI, or "" when segno (a neonize dependency) is missing."""
    try:
        import segno
    except ImportError:
        return ""
    return segno.make_qr(code).svg_data_uri(scale=6, border=2)


class Engine:
    def __init__(self, root: Path, *, adapter_factory: AdapterFactory | None,
                 unavailable: str = "", clock: Callable[[], float] = time.time,
                 pairing_ttl: float = PAIRING_TTL) -> None:
        self.root = root
        self.unavailable = unavailable
        self._factory = adapter_factory
        self._clock = clock
        self._pairing_ttl = pairing_ttl
        self._links: dict[str, _Link] = {}
        # One lock per user id, created before the link exists: two Link clicks
        # for a new user must not start two clients on one session.
        self._locks: dict[str, asyncio.Lock] = {}
        self._events: list[dict[str, Any]] = []
        self._seq = 0

    # ── helpers ─────────────────────────────────────────────────────

    def _dir(self, user_id: str) -> Path:
        if not _USER_ID.match(user_id or ""):
            raise LinkError(400, "Bad user id")
        return self.root / user_id

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def _require_runtime(self) -> None:
        if self.unavailable or self._factory is None:
            raise LinkError(503, self.unavailable or "WhatsApp Web isn't available on this deck")

    def _get(self, user_id: str) -> _Link:
        link = self._links.get(user_id)
        if link is None:
            d = self._dir(user_id)
            st = LinkState.load(d / "state.json")
            link = _Link(user_id, d, st or LinkState(state="expired"))
            if st is not None:
                self._links[user_id] = link
        return link

    def _save(self, link: _Link) -> None:
        try:
            link.state.save(link.state_file)
        except OSError as exc:
            log.warning("whatsapp_web: could not save %s: %s", link.state_file, exc)

    def _wipe_session(self, link: _Link) -> None:
        """Drop the session and the messages kept for it (state.json stays, so
        the card can say what happened)."""
        self._close_store(link)
        for name in (*_SESSION_FILES, *_STORE_FILES):
            try:
                (link.dir / name).unlink()
            except FileNotFoundError:
                pass

    def _remove(self, link: _Link) -> None:
        self._close_store(link)
        self._links.pop(link.user_id, None)
        shutil.rmtree(link.dir, ignore_errors=True)

    def _open_store(self, link: _Link, *, create: bool = True) -> Store | None:
        """The link's store, opened on first use. With ``create=False`` a link
        whose folder is gone (unlinked meanwhile) gets None, not a new folder."""
        if link.store is None:
            if not create and not link.dir.is_dir():
                return None
            link.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            link.store = Store(link.store_file)
        return link.store

    def _close_store(self, link: _Link) -> None:
        store, link.store = link.store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:
                log.warning("whatsapp_web: closing %s's store: %s", link.user_id, exc)

    def _sink(self, link: _Link, gen: int) -> EventSink:
        async def sink(name: str, data: dict[str, Any]) -> None:
            if link.gen == gen and self._links.get(link.user_id) is link:
                await self._on_event(link, name, data)
        return sink

    async def _start_client(self, link: _Link) -> None:
        """Start *link*'s client (a fresh session pairs; an existing one resumes)."""
        assert self._factory is not None
        link.gen += 1
        link.adapter = self._factory(
            f"cc-{link.user_id}", link.session, self._sink(link, link.gen), history_days=HISTORY_DAYS)
        link.started_at = self._clock()
        try:
            await link.adapter.connect()
        except Exception as exc:
            log.error("whatsapp_web: %s did not start: %s", link.user_id, exc)
            link.adapter = None
            apply_event(link.state, "connect_failure", {"reason": str(exc)}, now=self._clock())
            link.state.state = "failed"
            self._save(link)
            raise LinkError(502, f"WhatsApp Web could not start: {exc}") from None

    async def _stop_client(self, link: _Link) -> None:
        adapter, link.adapter = link.adapter, None
        link.gen += 1  # whatever it still reports is ignored
        if link.watchdog is not None and link.watchdog is not asyncio.current_task():
            link.watchdog.cancel()
        link.watchdog = None
        if adapter is None:
            return
        try:
            await asyncio.wait_for(adapter.stop(), _STOP_TIMEOUT)
        except Exception as exc:
            log.warning("whatsapp_web: stopping %s: %s", link.user_id, exc)

    def _watch_pairing(self, link: _Link) -> None:
        async def watch() -> None:
            while link.adapter is not None and link.state.state == "pairing":
                await asyncio.sleep(2.0)
                deadline = max(link.started_at, link.pair_code_at) + self._pairing_ttl
                if link.state.state == "pairing" and self._clock() >= deadline:
                    async with self._lock(link.user_id):
                        if link.state.state != "pairing":
                            return
                        await self._stop_client(link)
                        apply_event(link.state, "expired", now=self._clock())
                        self._wipe_session(link)
                        self._save(link)
                    return

        link.watchdog = asyncio.get_running_loop().create_task(watch())

    async def _on_event(self, link: _Link, name: str, data: dict[str, Any]) -> None:
        notice = apply_event(link.state, name, data, now=self._clock())
        if name in ("logged_out", "stream_replaced", "outdated"):
            # Let go of the client from outside its own callback.
            asyncio.get_running_loop().create_task(self._after_terminal(link, name))
        if name in ("history", "message", "directory"):
            try:
                await asyncio.to_thread(self._ingest, link, name, data)
            except Exception as exc:
                log.error("whatsapp_web: storing %s for %s failed: %s", name, link.user_id, exc)
        if name not in _STORE_ONLY:
            self._save(link)
        if notice is not None:
            self._seq += 1
            self._events.append({
                "seq": self._seq, "user_id": link.user_id, "title": notice.title,
                "body": notice.body, "at": now_iso(self._clock()),
            })
            del self._events[:-_MAX_EVENTS]

    def _ingest(self, link: _Link, name: str, data: dict[str, Any]) -> None:
        """Write one event's rows (worker thread; the store serialises itself)."""
        if (not link.state.linked_at or link.state.state == "logged_out"
                or self._links.get(link.user_id) is not link):
            return  # unlinked or logged out while this event was on its way
        store = self._open_store(link, create=False)
        if store is None:
            return
        if name == "history":
            # Mappings first, so this chunk's LID chats land folded already.
            store.put_lid_map(data.get("lid_map") or [])
            store.put_chats(data.get("chats") or [])
            store.put_names(data.get("pushnames") or [])
            store.ingest(data.get("rows") or [], live=False)
        elif name == "message":
            store.ingest([data], live=True)
        elif name == "directory":
            store.put_contacts(data.get("contacts") or [])
            store.put_groups(data.get("groups") or [])

    async def _after_terminal(self, link: _Link, name: str) -> None:
        async with self._lock(link.user_id):
            if self._links.get(link.user_id) is not link:
                return  # unlinked meanwhile
            await self._stop_client(link)
            if name == "logged_out":
                self._wipe_session(link)  # the phone revoked it; it's worthless now
                self._save(link)

    # ── what Flight Deck asks ───────────────────────────────────────

    def view(self, user_id: str) -> dict[str, Any]:
        link = self._get(user_id)
        if user_id not in self._links:
            return {"state": "off", "running": False}
        out = link.state.view()
        out["running"] = link.adapter is not None
        out["has_session"] = link.session.exists()
        if link.state.state == "pairing" and link.state.qr:
            out["qr_svg"] = _qr_svg(link.state.qr)
            out["qr_at"] = now_iso(link.state.qr_at)
        expires = max(link.started_at, link.pair_code_at) + self._pairing_ttl
        if link.state.state == "pairing":
            out["pairing_expires_at"] = now_iso(expires)
        if link.state.linked_at and (link.store is not None or link.store_file.exists()):
            store = self._open_store(link)
            out["stats"] = store.stats()
            out["retention_days"] = store.retention_days()
        else:
            out["stats"] = None
            out["retention_days"] = None
        out["retention_choices"] = list(RETENTION_CHOICES)
        return out

    async def link(self, user_id: str, *, consented_at: str) -> dict[str, Any]:
        """Start pairing a fresh link (a QR follows). A link that is already
        pairing or connected is left alone; a dead one is replaced."""
        self._require_runtime()
        self._dir(user_id)
        async with self._lock(user_id):
            link = self._get(user_id)
            if link.adapter is not None and link.state.state in ("pairing", "connecting", "connected", "disconnected"):
                return self.view(user_id)
            await self._stop_client(link)
            shutil.rmtree(link.dir, ignore_errors=True)
            link.dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            now = self._clock()
            link.state = LinkState(state="pairing", consented_at=consented_at, created_at=now_iso(now))
            link.pair_code_at = 0.0
            self._links[user_id] = link
            self._save(link)
            await self._start_client(link)
            self._watch_pairing(link)
        return self.view(user_id)

    async def reconnect(self, user_id: str) -> dict[str, Any]:
        """Start the client again on the session that's kept (after "stream
        replaced", a failed start or a service stop)."""
        self._require_runtime()
        self._dir(user_id)
        async with self._lock(user_id):
            link = self._get(user_id)
            if user_id not in self._links or not link.session.exists():
                raise LinkError(409, "There's no linked session to reconnect: link again")
            if link.adapter is not None:
                return self.view(user_id)
            link.state.state = "connecting"
            link.state.detail = ""
            self._save(link)
            await self._start_client(link)
        return self.view(user_id)

    async def pair_code(self, user_id: str, phone: str) -> dict[str, Any]:
        """An 8-character code to type on the phone instead of scanning the QR."""
        self._require_runtime()
        try:
            digits = normalize_phone(phone)
        except ValueError as exc:
            raise LinkError(422, str(exc)) from None
        self._dir(user_id)
        async with self._lock(user_id):
            link = self._get(user_id)
            if link.adapter is None or link.state.state != "pairing":
                raise LinkError(409, "Start linking first, then ask for a code")
            try:
                code = await link.adapter.pair_code(digits)
            except Exception as exc:
                raise LinkError(502, f"WhatsApp didn't give a pairing code: {exc}") from None
            link.pair_code_at = self._clock()
            apply_event(link.state, "pair_code", {"code": code}, now=self._clock())
            self._save(link)
        return self.view(user_id)

    async def cancel(self, user_id: str) -> dict[str, Any]:
        """Stop a link that never paired (pairing, expired, failed) and forget it."""
        self._dir(user_id)
        async with self._lock(user_id):
            link = self._get(user_id)
            if user_id not in self._links:
                return self.view(user_id)
            if link.state.linked_at and link.state.state not in ("expired", "failed"):
                raise LinkError(409, "This link is paired: disconnect it instead")
            await self._stop_client(link)
            self._remove(link)
        return self.view(user_id)

    async def unlink(self, user_id: str) -> dict[str, Any]:
        """Log the device out (it leaves the phone's Linked devices), stop the
        client and delete everything kept for this user."""
        self._dir(user_id)
        async with self._lock(user_id):
            link = self._get(user_id)
            if user_id not in self._links:
                return {"state": "off", "running": False, "removed_from_phone": False}
            removed = False
            if link.adapter is not None and link.state.state in ("connected", "disconnected", "temporary_ban"):
                # Our own logout may come back as a "logged out" event: not news.
                link.gen += 1
                try:
                    await asyncio.wait_for(link.adapter.logout(), _LOGOUT_TIMEOUT)
                    removed = True
                except Exception as exc:
                    log.warning("whatsapp_web: logout for %s failed: %s", user_id, exc)
            elif link.state.state == "logged_out":
                removed = True  # the phone already did it
            await self._stop_client(link)
            self._remove(link)
        out = self.view(user_id)
        out["removed_from_phone"] = removed
        return out

    async def resume_all(self) -> int:
        """After a (re)start: reconnect every paired session that should run.
        A link caught mid-pairing is dropped — its QR is long gone."""
        if self.unavailable or self._factory is None or not self.root.is_dir():
            return 0
        started = 0
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if not _USER_ID.match(d.name):
                continue
            link = self._get(d.name)
            if d.name not in self._links:
                continue
            st = link.state
            if st.state == "pairing" or (not st.linked_at and st.state not in NOT_RESUMED):
                self._remove(link)
                continue
            if st.state in NOT_RESUMED or not link.session.exists():
                continue
            try:
                async with self._lock(link.user_id):
                    st.state, st.detail = "connecting", ""
                    self._save(link)
                    await self._start_client(link)
                started += 1
            except LinkError:
                pass
        return started

    async def shutdown(self) -> None:
        """Stop every client (no logout: the links stay on the phone)."""
        for link in list(self._links.values()):
            async with self._lock(link.user_id):
                await self._stop_client(link)
                if link.state.state in ("connected", "disconnected", "connecting"):
                    link.state.state = "connecting"
                    link.state.detail = "Waiting for the WhatsApp Web service"
                self._save(link)
                self._close_store(link)

    # ── reading (what agents and the card see) ──────────────────────

    def _reader(self, user_id: str) -> Store:
        link = self._get(user_id)
        if user_id not in self._links or not link.state.linked_at or link.state.state == "logged_out":
            raise LinkError(409, "WhatsApp isn't linked for this user")
        return self._open_store(link)

    async def chats(self, user_id: str) -> dict[str, Any]:
        store = self._reader(user_id)
        chats = await asyncio.to_thread(store.chats)
        return {"chats": chats, "stats": await asyncio.to_thread(store.stats),
                "state": self._links[user_id].state.state}

    async def messages(self, user_id: str, *, chat: str = "", query: str = "", since: int | None = None,
                       until: int | None = None, limit: int = 50, agent: str = "",
                       mark_seen: bool = False) -> dict[str, Any]:
        """Messages newest first, of one chat or all; a look at a chat's (or all
        chats') newest messages by *agent* counts as seen for its news line."""
        store = self._reader(user_id)
        found = None
        if str(chat or "").strip():
            try:
                found = await asyncio.to_thread(store.find_chat, chat)
            except ChatNotFound as exc:
                options = "; ".join(f"{c['title']} (id {c['jid']})" for c in exc.candidates)
                raise LinkError(404, f"{exc}{': ' + options if options else ''}") from None
        items = await asyncio.to_thread(
            store.messages, chat_jid=found["jid"] if found else None, since=since, until=until,
            query=query, limit=max(1, min(int(limit or 50), 200)))
        if mark_seen and agent and items and until is None and not str(query or "").strip():
            await asyncio.to_thread(store.mark_read, agent, items)
        return {"chat": found, "messages": items}

    async def contacts(self, user_id: str, *, query: str = "", limit: int = 20) -> dict[str, Any]:
        store = self._reader(user_id)
        return {"contacts": await asyncio.to_thread(store.contacts, query, limit)}

    async def news(self, user_id: str, *, agent: str) -> dict[str, Any]:
        link = self._get(user_id)
        if user_id not in self._links or not link.state.linked_at or link.state.state == "logged_out":
            return {"connected": False}
        store = self._open_store(link)
        return {"connected": True, **await asyncio.to_thread(store.news, agent or "_", now=self._clock())}

    async def attachment(self, user_id: str, *, chat: str, message_id: str) -> tuple[bytes, dict[str, Any]]:
        """A message's file (photo, voice note, document …), downloaded now from
        WhatsApp's servers — the link has to be up. Returns (bytes, details)."""
        store = self._reader(user_id)
        link = self._links[user_id]
        try:
            found = await asyncio.to_thread(store.find_chat, chat)
        except ChatNotFound as exc:
            options = "; ".join(f"{c['title']} (id {c['jid']})" for c in exc.candidates)
            raise LinkError(404, f"{exc}{': ' + options if options else ''}") from None
        item = await asyncio.to_thread(store.media, found["jid"], message_id)
        if item is None:
            raise LinkError(404, f"No message #{message_id} in {found['title']}")
        if item["deleted"]:
            raise LinkError(410, "That message was deleted, and its file with it")
        media = item["media"] or {}
        if not media.get("raw") or not media.get("field"):
            raise LinkError(404, f"Message #{message_id} has no file (it's {item['kind']})")
        if int(media.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            raise LinkError(413, f"The file is too large ({int(media['size']) // (1024 * 1024)} MB; "
                                 f"the limit is {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)")
        if link.adapter is None or link.state.state not in ("connected", "disconnected"):
            raise LinkError(409, "WhatsApp isn't connected right now, and files come from WhatsApp's "
                                 "servers: the user can check the link in Flight Deck → Connections")
        try:
            data = await link.adapter.download(str(media["field"]), str(media["raw"]))
        except Exception as exc:
            text = str(exc)
            if any(m in text.lower() for m in _GONE_MARKERS):
                raise LinkError(410, "This file is no longer on WhatsApp's servers (it expired). The "
                                     "phone may still have it.") from None
            raise LinkError(502, f"WhatsApp didn't hand the file over: {text or type(exc).__name__}") from None
        if not data:
            raise LinkError(502, "WhatsApp handed over an empty file")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise LinkError(413, "The file is too large")
        names = await asyncio.to_thread(store.chats)
        title = next((c["title"] for c in names if c["jid"] == found["jid"]), found["title"])
        return data, {
            "id": item["id"], "kind": item["kind"], "chat": title, "chat_jid": found["jid"],
            "ts": item["ts"], "from_me": item["from_me"], "mimetype": str(media.get("mimetype") or ""),
            "file_name": str(media.get("file_name") or ""), "seconds": int(media.get("seconds") or 0),
            "size": len(data),
        }

    async def set_retention(self, user_id: str, days: int | None) -> dict[str, Any]:
        if days is not None and days not in RETENTION_CHOICES:
            raise LinkError(422, "Keep messages for 7, 30, 90 or 365 days, or keep everything")
        store = self._reader(user_id)
        await asyncio.to_thread(store.set_meta, "retention_days", "" if days is None else str(days))
        await asyncio.to_thread(store.purge, now=self._clock())
        return self.view(user_id)

    async def maintain(self, every: float = _PURGE_EVERY) -> None:
        """Forever: drop what's older than each user's retention choice."""
        while True:
            await asyncio.sleep(every)
            for link in list(self._links.values()):
                if link.store is not None or link.store_file.exists():
                    try:
                        await asyncio.to_thread(self._open_store(link).purge, now=self._clock())
                    except Exception as exc:
                        log.warning("whatsapp_web: purge for %s failed: %s", link.user_id, exc)

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["seq"] > seq]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for link in self._links.values():
            out[link.state.state] = out.get(link.state.state, 0) + 1
        return out
