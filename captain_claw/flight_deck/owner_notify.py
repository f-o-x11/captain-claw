"""Telling a user something is waiting for them, on the channels they chose.

Autonomous Work reaches out two ways: a nudge (what the agent said) and a
proposal waiting for the user's approval. Besides the agent's own chat, each
goes to every channel the user switched on in their autonomy config:

  * WhatsApp — ``nudge_to_whatsapp``: the text, to ``notify_waid`` (the
    bridge allowlist and /mute still apply, see ``push_to_waid``).
  * Telegram — ``notify_telegram``: the text, to the user's own chat with
    the deck's shared bot.
  * Devices  — ``notify_devices``: the user's connected handhelds beep and
    show how many proposals wait; the text itself stays in Flight Deck. A
    device that was asleep beeps for what came meanwhile when it next
    connects (``device_connected``), once.

The devices' count follows the ledger: every status change calls
``waiting_may_have_changed`` (autonomy.py), which sends a silent update
when the number moved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from captain_claw.flight_deck.autonomy import _norm_user, get_store, resolve_config

_log = logging.getLogger(__name__)

# A beep a device missed while asleep is still worth one when it connects
# for this long; after that it only shows the count.
UNSEEN_KEEP_S = 24 * 3600.0
DEVICE_LINE_MAX = 120

# The newest thing that beeped for each user (wall time, its line), when each
# device last beeped (keyed by user + device id), and the waiting count each
# user's devices last got. In memory: after a restart a device shows the
# count when it connects, without a beep for what came before.
_last_event: dict[str, tuple[float, str]] = {}
_device_heard: dict[str, float] = {}
_sent_count: dict[str, int] = {}


def waiting_count(user_id: str) -> int:
    """How many of this user's proposals wait for their approval."""
    return get_store().count_status(user_id, "awaiting_approval")


def _quiet(cfg: dict[str, Any]) -> bool:
    from captain_claw.flight_deck.arbiter import _in_quiet_hours

    return _in_quiet_hours(int(cfg.get("quiet_hours_start", 22)), int(cfg.get("quiet_hours_end", 8)))


def device_line(text: str) -> str:
    """The text as a device shows it: its first line, plain, no emoji."""
    from captain_claw.flight_deck.bus_client import activity_text
    from captain_claw.flight_deck.meta_webhook_bridge import strip_markdown

    first = next((ln for ln in strip_markdown(str(text or "")).splitlines() if ln.strip()), "")
    line = activity_text(first, DEVICE_LINE_MAX + 1)
    return line if len(line) <= DEVICE_LINE_MAX else line[:DEVICE_LINE_MAX - 1].rstrip() + "…"


def proposal_text(action: dict[str, Any], waiting: int) -> str:
    """What WhatsApp and Telegram say about a new proposal."""
    title = str(action.get("title") or "").strip() or "a new proposal"
    more = f" ({waiting} waiting)" if waiting > 1 else ""
    url = os.environ.get("FD_PUBLIC_URL", "").strip()
    return f"Waiting for your approval in Flight Deck{more}: {title}" + (f"\n{url}" if url else "")


def _device_key(user_id: str, device_id: str) -> str:
    return f"{_norm_user(user_id)}:{device_id}"


async def notify_owner(
    user_id: str, kind: str, text: str, *, device_text: str = "", cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send ``text`` to each channel ``user_id`` switched on. ``kind`` ("nudge",
    "proposal", "test") names the log lines; ``device_text`` is the devices'
    line (default: the text's first). Returns what each channel did, None
    where it is off: WhatsApp ``[sent, total]``, Telegram ``True/False``,
    devices how many beeped. Never raises."""
    out: dict[str, Any] = {"whatsapp": None, "telegram": None, "devices": None}
    text = str(text or "").strip()
    if not text:
        return out
    try:
        cfg = cfg or resolve_config(user_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("notify: no config for %s: %s", user_id, exc)
        return out
    store = get_store()

    if cfg.get("nudge_to_whatsapp", True):
        try:
            from captain_claw.flight_deck.fd_dispatch import _nudge_waids
            from captain_claw.flight_deck.whatsapp_bridge import push_to_waid

            waids, skipped = _nudge_waids(cfg)
            sent = 0
            for waid in waids:
                if await push_to_waid(waid, text):
                    sent += 1
            out["whatsapp"] = [sent, len(waids)]
            if waids:
                store.log(user_id, f"{kind} → whatsapp", f"sent to {sent}/{len(waids)} recipient(s)")
            elif skipped:
                store.log(user_id, f"{kind} → whatsapp skipped", skipped)
        except Exception as exc:  # noqa: BLE001
            _log.debug("%s whatsapp delivery failed: %s", kind, exc)

    if cfg.get("notify_telegram", False):
        try:
            from captain_claw.flight_deck.fd_scheduler import _deliver_telegram_owner

            res = await _deliver_telegram_owner(user_id, text)
            out["telegram"] = bool(res.get("ok"))
            store.log(user_id, f"{kind} → telegram" + ("" if res.get("ok") else " skipped"),
                      str(res.get("note") or ""))
        except Exception as exc:  # noqa: BLE001
            _log.debug("%s telegram delivery failed: %s", kind, exc)

    if cfg.get("notify_devices", True):
        try:
            line = device_line(device_text or text)
            count = waiting_count(user_id)
            beep = not _quiet(cfg)
            now = time.time()
            if kind != "test":
                _last_event[_norm_user(user_id)] = (now, line)
            reached = await _push_devices(user_id, count, line, beep)
            for device_id in reached:
                _device_heard[_device_key(user_id, device_id)] = now
            out["devices"] = len(reached)
            if reached:
                store.log(user_id, f"{kind} → devices",
                          f"{'beeped' if beep else 'shown quietly (quiet hours) on'} {len(reached)} device(s)")
        except Exception as exc:  # noqa: BLE001
            _log.debug("%s device delivery failed: %s", kind, exc)
    return out


async def _push_devices(user_id: str, count: int, text: str, beep: bool) -> list[str]:
    from captain_claw.flight_deck.device_bridge import push_waiting

    reached = await push_waiting(user_id, count, text, beep)
    _sent_count[_norm_user(user_id)] = count
    return reached


async def notify_proposal(user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """A proposal now waits for this user's approval: tell them. Never raises."""
    try:
        waiting = waiting_count(user_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("proposal notify: no count for %s: %s", user_id, exc)
        waiting = 1
    title = str(action.get("title") or "").strip()
    return await notify_owner(
        user_id, "proposal", proposal_text(action, waiting),
        device_text=f"Approve in Flight Deck: {title}" if title else "A proposal waits in Flight Deck",
    )


def waiting_may_have_changed(user_id: str) -> None:
    """From the ledger (sync): bring the devices' count up to date, later on
    this event loop. No loop (a script, a test): nothing to do."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_sync_count(user_id))


async def _sync_count(user_id: str) -> None:
    try:
        cfg = resolve_config(user_id)
        if not cfg.get("notify_devices", True):
            return
        count = waiting_count(user_id)
        if _sent_count.get(_norm_user(user_id)) == count:
            return
        await _push_devices(user_id, count, "", False)
    except Exception as exc:  # noqa: BLE001
        _log.debug("waiting count sync failed for %s: %s", user_id, exc)


async def devices_switched_off(user_id: str) -> None:
    """Notifications to devices went off: clear the count they show."""
    try:
        await _push_devices(user_id, 0, "", False)
    except Exception as exc:  # noqa: BLE001
        _log.debug("clearing device counts failed for %s: %s", user_id, exc)


def device_connected(user_id: str, device_id: str) -> dict[str, Any] | None:
    """The ``waiting`` frame for a device that just connected: the count (0
    clears one it kept), and a beep for what came while it was away — once,
    and only while recent. None when its owner has devices switched off."""
    try:
        cfg = resolve_config(user_id)
        if not cfg.get("notify_devices", True):
            return None
        count = waiting_count(user_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("waiting frame for %s failed: %s", device_id, exc)
        return None
    now = time.time()
    key = _device_key(user_id, device_id)
    at, line = _last_event.get(_norm_user(user_id), (0.0, ""))
    unseen = at > _device_heard.get(key, 0.0) and now - at < UNSEEN_KEEP_S
    if unseen:
        _device_heard[key] = now
    _sent_count[_norm_user(user_id)] = count
    return {"type": "waiting", "count": count, "text": line if unseen else "",
            "beep": bool(unseen and not _quiet(cfg))}
