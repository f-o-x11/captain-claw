"""The Flight Decks a hub knows by name: keys made on the dashboard, and the
label and description its operator gives each deck.

Keys in botport.yaml (``auth.keys``) keep working exactly as before and stay
read-only here. A deck added from the dashboard gets its key from this
registry, which lives in the hub's database, so adding, rotating or removing
one takes effect at once: no restart, no file to edit.

Secrets are stored as SHA-256 digests. Each is 32 random bytes, so a plain
digest is as strong as a slow hash would be, and the database never holds a
secret anyone could connect with. The dashboard shows a new secret once.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from botport.config import AuthKeyEntry, get_config

if TYPE_CHECKING:
    from botport.store import BotPortStore

DECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
LABEL_MAX = 80
DESCRIPTION_MAX = 500
_NOTES_MAX = 200


class DeckError(Exception):
    """A request the registry refuses, with the HTTP status that fits it."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class DeckRecord:
    name: str
    label: str = ""
    description: str = ""
    key: str = ""
    secret_hash: str = ""
    created_at: str = ""
    updated_at: str = ""
    rotated_at: str = ""

    @property
    def has_key(self) -> bool:
        return bool(self.key and self.secret_hash)

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "description": self.description,
            "key": self.key, "secret_hash": self.secret_hash, "created_at": self.created_at,
            "updated_at": self.updated_at, "rotated_at": self.rotated_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def clean_label(value: Any) -> str:
    return " ".join(str(value or "").split())[:LABEL_MAX]


def clean_description(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    # Keep line breaks (a description may be a short list); drop the rest of
    # the control characters.
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    return re.sub(r"\n{3,}", "\n\n", text)[:DESCRIPTION_MAX]


def config_entry_for(name: str) -> AuthKeyEntry | None:
    """The botport.yaml key bound to deck ``name``, if there is one."""
    low = name.strip().lower()
    for k in get_config().auth.keys:
        if k.instance.strip().lower() == low and k.key.strip():
            return k
    return None


class DeckRegistry:
    """Dashboard-managed deck keys and every deck's label and description.

    Loaded from the database once, then served from memory, so registration
    never waits on the disk; every change is written through.
    """

    def __init__(self, store: BotPortStore) -> None:
        self._store = store
        self._records: dict[str, DeckRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        # key -> why it stopped working (shown to a deck that still uses it).
        self._notes: dict[str, str] = {}

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            for row in await self._store.list_deck_records():
                rec = DeckRecord(**{k: str(row.get(k) or "") for k in DeckRecord.__dataclass_fields__})
                if rec.name:
                    self._records[rec.name.lower()] = rec
            self._loaded = True

    # ── Reading ──────────────────────────────────────────────────

    def get(self, name: str) -> DeckRecord | None:
        return self._records.get((name or "").strip().lower())

    def records(self) -> list[DeckRecord]:
        return sorted(self._records.values(), key=lambda r: r.name.lower())

    def meta(self, name: str) -> dict[str, str]:
        rec = self.get(name)
        return {"label": rec.label, "description": rec.description} if rec else {"label": "", "description": ""}

    def knows_key(self, key: str) -> bool:
        key = (key or "").strip()
        return bool(key) and any(r.key == key for r in self._records.values() if r.has_key)

    def match(self, key: str, secret: str) -> AuthKeyEntry | None:
        """The deck these credentials belong to, or None. Every key is
        compared, in constant time, so timing tells nothing about which exist."""
        key = (key or "").strip()
        secret = (secret or "").strip()
        if not key or not secret:
            return None
        digest = _digest(secret)
        found: AuthKeyEntry | None = None
        for rec in self._records.values():
            if not rec.has_key:
                continue
            ok_key = _eq(rec.key, key)
            ok_secret = _eq(rec.secret_hash, digest)
            if ok_key and ok_secret and found is None:
                found = AuthKeyEntry(instance=rec.name, key=rec.key, secret="")
        return found

    def note_for(self, key: str) -> str:
        """Why this key no longer works, if the hub changed it (else "")."""
        return self._notes.get((key or "").strip(), "")

    # ── Changing ─────────────────────────────────────────────────

    def _note(self, key: str, text: str) -> None:
        if not key:
            return
        self._notes.pop(key, None)
        self._notes[key] = text
        while len(self._notes) > _NOTES_MAX:
            self._notes.pop(next(iter(self._notes)))

    def _check_new_name(self, name: str) -> str:
        name = (name or "").strip()
        if not DECK_NAME_RE.match(name):
            raise DeckError("Deck name: letters, digits, '-', '_' or '.', up to 63, "
                            "starting with a letter or digit (no spaces or '@')")
        if config_entry_for(name) is not None:
            raise DeckError(f"'{name}' already has a key in botport.yaml. Give it a label and description "
                            "here, or remove it from the file first.", status=409)
        rec = self.get(name)
        if rec is not None and rec.has_key:
            raise DeckError(f"A Flight Deck named '{rec.name}' is already on this hub", status=409)
        return name

    def _new_key(self, name: str) -> str:
        taken = {k.key.strip() for k in get_config().auth.keys} | {r.key for r in self._records.values()}
        while True:
            key = f"{name.lower()}-{secrets.token_hex(4)}"
            if key not in taken:
                return key

    async def create(self, name: str, label: Any = "", description: Any = "") -> tuple[DeckRecord, str]:
        """A new deck with its own key. Returns the record and the secret,
        which is never stored and cannot be shown again."""
        await self.ensure_loaded()
        async with self._lock:
            name = self._check_new_name(name)
            now = _now()
            secret = secrets.token_urlsafe(32)
            old = self.get(name)  # a label given before the deck had a key
            rec = DeckRecord(
                name=old.name if old else name,
                label=clean_label(label) or (old.label if old else ""),
                description=clean_description(description) or (old.description if old else ""),
                key=self._new_key(name),
                secret_hash=_digest(secret),
                created_at=old.created_at if old and old.created_at else now,
                updated_at=now,
                rotated_at="",
            )
            await self._store.save_deck_record(rec.to_row())
            self._records[rec.name.lower()] = rec
            return rec, secret

    async def update(self, name: str, *, label: Any = None, description: Any = None) -> DeckRecord:
        """Set a deck's label and/or description (any deck the caller knows of:
        one from the dashboard, from botport.yaml, or connected with auth off)."""
        await self.ensure_loaded()
        async with self._lock:
            name = (name or "").strip()
            if not DECK_NAME_RE.match(name):
                raise DeckError("Not a deck name", status=404)
            now = _now()
            rec = self.get(name) or DeckRecord(name=name, created_at=now)
            if label is not None:
                rec.label = clean_label(label)
            if description is not None:
                rec.description = clean_description(description)
            rec.updated_at = now
            await self._store.save_deck_record(rec.to_row())
            self._records[rec.name.lower()] = rec
            return rec

    async def rotate(self, name: str) -> tuple[DeckRecord, str]:
        """A new secret for a dashboard deck (its key stays). The old secret
        stops working at once."""
        await self.ensure_loaded()
        async with self._lock:
            rec = self.get(name)
            if rec is None or not rec.has_key:
                raise DeckError("Only a key made on this dashboard can be rotated here", status=404)
            secret = secrets.token_urlsafe(32)
            now = _now()
            rec.secret_hash = _digest(secret)
            rec.rotated_at = now
            rec.updated_at = now
            await self._store.save_deck_record(rec.to_row())
            self._note(rec.key, "this deck's secret was replaced on the hub; "
                                "paste the new one into the deck's Network settings")
            return rec, secret

    async def remove(self, name: str) -> DeckRecord:
        """Forget a deck: its dashboard key stops working at once, and its
        label and description go with it."""
        await self.ensure_loaded()
        async with self._lock:
            rec = self.get(name)
            if rec is None:
                raise DeckError("No such deck on this dashboard", status=404)
            await self._store.delete_deck_record(rec.name)
            self._records.pop(rec.name.lower(), None)
            if rec.has_key:
                self._note(rec.key, "this key was removed on the hub")
            return rec
