"""Key/secret authentication for BotPort."""

from __future__ import annotations

import hmac
import time

from botport.config import AuthKeyEntry, get_config


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def match_credentials(key: str, secret: str) -> AuthKeyEntry | None:
    """The key entry these credentials match, or None.

    Every entry is compared (no early exit) in constant time so a caller
    cannot learn which keys exist from response timing.
    """
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key:
        return None
    found: AuthKeyEntry | None = None
    for entry in get_config().auth.keys:
        ok_key = _eq(entry.key.strip(), key)
        ok_secret = _eq(entry.secret.strip(), secret)
        if ok_key and ok_secret and entry.secret.strip() and found is None:
            found = entry
    return found


def validate_credentials(key: str, secret: str) -> bool:
    """``True`` if auth is disabled or the credentials match an entry."""
    if not get_config().auth.enabled:
        return True
    return match_credentials(key, secret) is not None


def get_instance_name_for_key(key: str) -> str | None:
    """Return the configured instance name for a given key, or None."""
    for entry in get_config().auth.keys:
        if _eq(entry.key.strip(), (key or "").strip()):
            return entry.instance or None
    return None


class FailureLimiter:
    """Refuse an address after too many failed registrations.

    In-memory and per process: enough to stop a credential-guessing loop
    against an internet-facing hub without ever locking out a correct key for
    long (a block expires after ``block_seconds``).
    """

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, addr: str) -> bool:
        until = self._blocked_until.get(addr, 0.0)
        if until and until > time.monotonic():
            return True
        if until:
            self._blocked_until.pop(addr, None)
        return False

    def record_failure(self, addr: str) -> None:
        cfg = get_config().auth
        now = time.monotonic()
        window = [t for t in self._fails.get(addr, []) if now - t < 60.0]
        window.append(now)
        self._fails[addr] = window
        if cfg.max_failures_per_minute > 0 and len(window) >= cfg.max_failures_per_minute:
            self._blocked_until[addr] = now + max(1, cfg.block_seconds)
            self._fails.pop(addr, None)

    def record_success(self, addr: str) -> None:
        self._fails.pop(addr, None)

    def forget_key(self, key: str) -> None:
        """Clear the failures and blocks counted against ``key`` (from any
        address) — after the hub itself changed that key."""
        suffix = f"|{key}"
        for d in (self._fails, self._blocked_until):
            for bucket in [b for b in d if b.endswith(suffix)]:
                d.pop(bucket, None)

    def prune(self) -> None:
        now = time.monotonic()
        for addr in [a for a, ts in self._fails.items() if not ts or now - ts[-1] > 60.0]:
            self._fails.pop(addr, None)
        for addr in [a for a, u in self._blocked_until.items() if u <= now]:
            self._blocked_until.pop(addr, None)
