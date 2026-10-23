"""Users' agents in Telegram groups, through the deck's shared bot.

A bot can't follow a group invite link — someone in the group adds it. The
agent (on ``/group``, or when sent an invite link) and Connections → Telegram
offer the shared bot's add-to-group link, ``t.me/<bot>?startgroup=agent``:
Telegram adds the bot to the group the person picks, then posts
``/start@<bot> agent`` there from them — the cue that they want their agent
there, not their Telegram inbox (telegram_inbox).

One group, one Flight Deck user: the linked, approved person who brought the
bot in. Their link names the agent (moving the link to another agent moves
their groups too). Flight Deck relays the group's messages to that agent — it
sees the conversation it's asked about and decides when to answer
(web/telegram_group_chat.py) — counts only the messages addressed to the bot
against that person's plan, and lets the agent post in and leave only its own
groups (telegram_shared.check_call).

When the bot joins a group:

* someone with no approved link added it → the inbox's rules (it leaves);
* a linked person whose Telegram inbox is off → their agent, at once;
* a linked person whose inbox is on → the join waits :data:`JOIN_WAIT`
  seconds for the /start cue: with it, their agent; without, their inbox as
  before.

``/start@<bot> agent`` sent later in a group the bot already reads for an inbox
brings the person's agent in as well. Removing the bot, the agent leaving
(``/leave``), Connections → Remove, or the person's link going away ends it; the
bot stays while someone still keeps the group in their inbox.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from captain_claw import telegram_groups
from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import telegram_shared as ts
from captain_claw.logging import get_logger

log = get_logger(__name__)

# How long a join by someone whose inbox is on waits for the add-to-group
# link's "/start@<bot> agent" (it follows the join at once). 0: no waiting —
# such a join goes to the inbox, and only a later /start brings the agent.
JOIN_WAIT = 5.0

_PRESENT = ("member", "administrator", "creator", "restricted")
_GONE = ("left", "kicked")

_TAKEN = "This group already has {name}'s agent here — one agent per group."
_REMOVED = "👋 {agent} isn't available in this group any more."
_OVER_LIMIT = "I can't answer more today — try again tomorrow."
_OFFLINE = "⏸ {agent} isn't running right now — it answers once it's back."

# A join waiting for the /start cue: chat id → (my_chat_member update, timer).
_pending: dict[str, tuple[dict[str, Any], asyncio.Task | None]] = {}


# ── who ─────────────────────────────────────────────────────────────


def _linked_user(tg_user_id: Any) -> str:
    """The Flight Deck user an approved link (with an agent) binds this Telegram account to."""
    link = ts._state.links_by_tg.get(str(tg_user_id or ""))
    if link and link.get("status") == "approved" and link.get("agent_slug"):
        return str(link["user_id"])
    return ""


def _agent_of(user_id: str) -> tuple[str, str]:
    """``(slug, name)`` of the agent the user's approved link names (``("", "")`` if none)."""
    link = ts._state.links_by_user.get(str(user_id or ""))
    if link and link.get("status") == "approved" and link.get("agent_slug"):
        return str(link["agent_slug"]), str(link.get("agent_name") or link["agent_slug"])
    return "", ""


def bound_to(chat_id: Any) -> str:
    """The user whose agent is in this group ("" if none)."""
    return ts._state.groups.get(str(chat_id), "")


def _seal(title: str) -> str:
    try:
        return fd_crypto.seal(title) if title and fd_crypto.available() else ""
    except Exception:
        return ""


def _open(sealed: str) -> str:
    try:
        value = fd_crypto.unseal(sealed) if sealed else None
    except Exception:
        value = None
    return value if isinstance(value, str) else ""


async def groups_of(db, user_id: str) -> list[dict[str, Any]]:
    """The groups a user brought their agent into (Connections → Telegram)."""
    return [{"chat_id": r["chat_id"], "title": _open(r["title"]) or "A group",
             "added_at": r["added_at"]} for r in await db.list_tg_agent_groups(user_id)]


async def _say(chat_id: Any, text: str) -> None:
    try:
        await ts.send_text(chat_id, text)
    except Exception:
        pass


async def _leave(chat_id: Any) -> None:
    token = ts.token()
    if token:
        try:
            await ts.call(token, "leaveChat", {"chat_id": int(chat_id)})
        except Exception:
            pass


# ── updates ─────────────────────────────────────────────────────────


async def handle(db, update: dict[str, Any]) -> str | None:
    """Take a group update that's an agent's; ``None`` leaves it to the inbox."""
    member = update.get("my_chat_member")
    if isinstance(member, dict):
        return await _on_membership(db, update, member)
    msg = update.get("message")
    if isinstance(msg, dict):
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        if chat.get("type") in telegram_groups.GROUP_KINDS:
            return await _on_message(db, update, msg, chat)
    return None


async def _on_membership(db, update: dict[str, Any], member: dict[str, Any]) -> str | None:
    from captain_claw.flight_deck import telegram_inbox

    chat = member.get("chat") if isinstance(member.get("chat"), dict) else {}
    if chat.get("type") not in telegram_groups.GROUP_KINDS:
        return None
    chat_id = str(chat.get("id") or "")
    new = str((member.get("new_chat_member") or {}).get("status") or "")
    old = str((member.get("old_chat_member") or {}).get("status") or "")
    if new in _GONE:
        _drop_pending(chat_id)
        user_id = bound_to(chat_id)
        if user_id:
            slug, _name = _agent_of(user_id)
            if slug:
                ts.enqueue(slug, update)  # the agent forgets the group
            await _unbind(db, chat_id)
        return None  # the inbox stops keeping it too
    if not (new in _PRESENT and (old in _GONE or not old)) or bound_to(chat_id):
        return None
    actor = member.get("from") if isinstance(member.get("from"), dict) else {}
    user_id = _linked_user(actor.get("id"))
    if not user_id:
        if telegram_inbox.allowed():
            return None  # the inbox turns them away
        await _say(chat_id, telegram_inbox._REFUSED)
        await _leave(chat_id)
        return "group:refused"
    if await telegram_inbox.collecting(db, user_id):
        if JOIN_WAIT <= 0:
            return None  # the inbox keeps it
        _hold(db, chat_id, update)
        return "group:pending"
    await _bind(db, user_id, chat, update)
    return "group:joined"


async def _on_message(db, update: dict[str, Any], msg: dict[str, Any], chat: dict[str, Any]) -> str | None:
    from captain_claw.flight_deck import telegram_inbox

    chat_id = str(chat.get("id") or "")
    if msg.get("migrate_to_chat_id"):
        user_id = bound_to(chat_id)
        if user_id:
            await db.move_tg_agent_group(chat_id, str(msg["migrate_to_chat_id"]))
            await ts.reload_groups(db)
            slug, _name = _agent_of(user_id)
            if slug:
                ts.enqueue(slug, update)  # the agent follows the group to its new id
        return None  # the inbox moves its copy too
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    text = str(msg.get("text") or "").strip()
    cmd = telegram_groups.command_of(text, ts.bot_username()) if text.startswith("/") else None
    if cmd is not None and cmd.name == "start" and cmd.args.lower() == telegram_groups.START_PAYLOAD:
        return await _on_start(db, update, chat, sender)
    user_id = bound_to(chat_id)
    if not user_id:
        return None  # not an agent's group: the inbox's
    kept = await telegram_inbox.handle(db, update)  # its copy, or its /inbox command
    slug, name = _agent_of(user_id)
    if not slug or sender.get("is_bot") or (cmd is not None and cmd.name == "inbox"):
        return kept or "group:ignored"
    addressed = telegram_groups.addressed(msg, ts.bot_id(), ts.bot_username())
    if addressed:
        # What's asked counts against the plan of whoever brought the agent in.
        from captain_claw.flight_deck import usage_quota

        allowed, _quota = await usage_quota.admit_message(user_id, text)
        if not allowed:
            await ts._notice(chat_id, f"group:{chat_id}", "limit", _OVER_LIMIT)
            return "group:over_limit"
    box = ts.enqueue(slug, update)
    if addressed and not box.online():
        await ts._notice(chat_id, f"group:{chat_id}", "offline", _OFFLINE.format(agent=name))
    return "group:queued"


async def _on_start(db, update: dict[str, Any], chat: dict[str, Any], sender: dict[str, Any]) -> str:
    """``/start@<bot> agent`` (the add-to-group link's cue): this person's agent joins."""
    chat_id = str(chat.get("id") or "")
    user_id = _linked_user(sender.get("id"))
    if not user_id:
        return "group:start-ignored"  # a waiting join settles as it would have
    current = bound_to(chat_id)
    if current == user_id:
        _drop_pending(chat_id)
        return "group:already"
    if current:
        other = ts._state.links_by_user.get(current) or {}
        await _say(chat_id, _TAKEN.format(name=other.get("tg_first_name") or "someone else"))
        return "group:taken"
    held = _pending.pop(chat_id, None)
    if held is not None and held[1] is not None and not held[1].done():
        held[1].cancel()
    joined = held[0] if held is not None else _joined_update(chat, sender)
    # The /start itself never reaches the agent: its update id goes to the join.
    await _bind(db, user_id, chat, dict(joined, update_id=int(update.get("update_id") or 0)))
    return "group:joined"


def _joined_update(chat: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    """A join for a group the bot was already in (for someone's inbox)."""
    bot = {"id": int(ts.bot_id() or 0), "is_bot": True, "username": ts.bot_username()}
    return {"update_id": 0, "my_chat_member": {
        "chat": chat, "from": sender, "date": int(time.time()),
        "old_chat_member": {"status": "left", "user": bot},
        "new_chat_member": {"status": "member", "user": bot}}}


# ── binding ─────────────────────────────────────────────────────────


async def _bind(db, user_id: str, chat: dict[str, Any], joined: dict[str, Any]) -> None:
    chat_id = str(chat.get("id") or "")
    await db.set_tg_agent_group(chat_id, user_id, _seal(str(chat.get("title") or "")))
    ts._state.groups[chat_id] = user_id
    slug, _name = _agent_of(user_id)
    if slug:
        ts.enqueue(slug, joined)  # the agent says hello
    log.info("Shared Telegram bot: an agent joined a group", agent=slug)


async def _unbind(db, chat_id: str) -> None:
    await db.delete_tg_agent_group(chat_id)
    ts._state.groups.pop(chat_id, None)


async def release(db, chat_id: Any, *, say: str = "") -> bool:
    """The agent leaves the group: the bot too, unless someone keeps the group
    in their inbox. False when no agent was there."""
    chat_id = str(chat_id)
    if not bound_to(chat_id):
        return False
    await _unbind(db, chat_id)
    if say:
        await _say(chat_id, say)
    if not await db.tg_inbox_group_subscribers(chat_id):
        await _leave(chat_id)
    return True


async def forget_user(db, user_id: str) -> None:
    """The user's link is going away: their agent leaves their groups."""
    _slug, name = _agent_of(user_id)
    for chat_id, owner in list(ts._state.groups.items()):
        if owner == user_id:
            await release(db, chat_id, say=_REMOVED.format(agent=name or "This agent"))


# ── a join waiting for its cue ──────────────────────────────────────


def _hold(db, chat_id: str, update: dict[str, Any]) -> None:
    _drop_pending(chat_id)
    _pending[chat_id] = (update, asyncio.create_task(_settle_later(db, chat_id)))


async def _settle_later(db, chat_id: str) -> None:
    try:
        await asyncio.sleep(JOIN_WAIT)
    except asyncio.CancelledError:
        return
    await settle(db, chat_id)


async def settle(db, chat_id: str) -> str | None:
    """No /start cue came: the waiting join goes to the inbox, as before."""
    from captain_claw.flight_deck import telegram_inbox

    held = _pending.pop(str(chat_id), None)
    if held is None:
        return None
    try:
        return await telegram_inbox.handle(db, held[0])
    except Exception as exc:
        log.warning("Shared Telegram bot: a waiting group join failed: %s", type(exc).__name__)
        return None


def _drop_pending(chat_id: str) -> None:
    held = _pending.pop(chat_id, None)
    if held is not None and held[1] is not None and not held[1].done() \
            and held[1] is not asyncio.current_task():
        held[1].cancel()
