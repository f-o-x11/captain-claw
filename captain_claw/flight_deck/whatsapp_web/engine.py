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


class Adapter(Protocol):
    async def connect(self) -> None: ...
    async def pair_code(self, phone: str) -> str: ...
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
        for name in _SESSION_FILES:
            try:
                (link.dir / name).unlink()
            except FileNotFoundError:
                pass

    def _remove(self, link: _Link) -> None:
        self._links.pop(link.user_id, None)
        shutil.rmtree(link.dir, ignore_errors=True)

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
        self._save(link)
        if notice is not None:
            self._seq += 1
            self._events.append({
                "seq": self._seq, "user_id": link.user_id, "title": notice.title,
                "body": notice.body, "at": now_iso(self._clock()),
            })
            del self._events[:-_MAX_EVENTS]

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

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["seq"] > seq]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for link in self._links.values():
            out[link.state.state] = out.get(link.state.state, 0) + 1
        return out
