"""Telegram groups: what an agent's bot and Flight Deck's shared bot agree on.

A bot can't follow a group's invite link (``t.me/+…``) — the Bot API has no
way to join a chat. Only a member of the group can add it. So the way in is the
bot's own add-to-group link, :func:`add_to_group_url`: Telegram asks which group,
adds the bot, and posts ``/start@<bot> agent`` there from the person who added
it (``agent`` = :data:`START_PAYLOAD`).

In a group the bot answers only what is addressed to it (:func:`addressed`): an
@mention, a reply to one of its messages, a ``/command@<bot>``, or ``/ask``.
With Group Privacy on (BotFather's default) Telegram delivers only the last
three; @mentions need Group Privacy off, or the bot made a group admin.

Stdlib only: the agent (web/telegram_group_chat.py) and Flight Deck
(flight_deck/telegram_agent_groups.py) both import it.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

GROUP_KINDS = ("group", "supergroup")
START_PAYLOAD = "agent"
# The one command anyone may send without naming the bot (/ask, not /ask@bot).
ASK_COMMAND = "ask"

# t.me/+HASH, t.me/joinchat/HASH (also telegram.me / telegram.dog), tg://join?invite=HASH
_INVITE_RE = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)"
    r"|tg://join\?invite=)[A-Za-z0-9_-]{6,}",
    re.IGNORECASE,
)
_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,64}))?(?=\s|$)([\s\S]*)$")


class Command(NamedTuple):
    name: str       # lower case, without the slash
    args: str
    explicit: bool  # /name@<this bot>, not a bare /name


def add_to_group_url(bot_username: str) -> str:
    """The link that adds the bot to a group the opener picks ("" without a username)."""
    bot = str(bot_username or "").lstrip("@").strip()
    return f"https://t.me/{bot}?startgroup={START_PAYLOAD}" if bot else ""


def has_invite_link(text: str) -> bool:
    """Whether *text* carries a group invite link (which a bot can't use)."""
    return bool(_INVITE_RE.search(str(text or "")))


def command_of(text: str, bot_username: str) -> Command | None:
    """A ``/command`` meant for this bot, or None (not a command, or another bot's)."""
    m = _COMMAND_RE.match(str(text or "").strip())
    if not m:
        return None
    at = m.group(2) or ""
    bot = str(bot_username or "").lstrip("@").lower()
    if at and at.lower() != bot:
        return None
    return Command(m.group(1).lower(), m.group(3).strip(), bool(at))


def _utf16_slice(text: str, offset: Any, length: Any) -> str:
    """Telegram counts entity offsets in UTF-16 code units."""
    try:
        start, size = int(offset), int(length)
    except (TypeError, ValueError):
        return ""
    raw = text.encode("utf-16-le")
    return raw[start * 2:(start + size) * 2].decode("utf-16-le", errors="ignore")


def addressed(msg: Any, bot_id: Any, bot_username: str) -> bool:
    """Whether a group message (a Bot API ``message`` dict) is for this bot."""
    if not isinstance(msg, dict):
        return False
    bot_id = str(bot_id or "")
    bot = str(bot_username or "").lstrip("@").lower()
    reply = msg.get("reply_to_message")
    if isinstance(reply, dict) and isinstance(reply.get("from"), dict):
        author = reply["from"]
        if bot_id and str(author.get("id") or "") == bot_id:
            return True
        if bot and author.get("is_bot") and str(author.get("username") or "").lower() == bot:
            return True
    text = str(msg.get("text") or "")
    entities = msg.get("entities")
    if not text:
        text = str(msg.get("caption") or "")
        entities = msg.get("caption_entities")
    cmd = command_of(text, bot)
    if cmd is not None and (cmd.explicit or cmd.name == ASK_COMMAND):
        return True
    entities = entities if isinstance(entities, list) else []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if ent.get("type") == "text_mention":
            user = ent.get("user") if isinstance(ent.get("user"), dict) else {}
            if bot_id and str(user.get("id") or "") == bot_id:
                return True
        elif ent.get("type") == "mention" and bot:
            if _utf16_slice(text, ent.get("offset"), ent.get("length")).lstrip("@").lower() == bot:
                return True
    # No entities at all (a hand-made payload): a plain @mention in the text.
    if not entities and bot:
        return bool(re.search(rf"(?<![\w@])@{re.escape(bot)}(?!\w)", text, re.IGNORECASE))
    return False


def strip_mention(text: str, bot_username: str) -> str:
    """*text* without ``@<bot>`` (and the comma or colon that follows it)."""
    bot = str(bot_username or "").lstrip("@")
    if not bot:
        return str(text or "").strip()
    out = re.sub(rf"(?<![\w@])@{re.escape(bot)}(?!\w)[,:]?", " ", str(text or ""), flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def person(obj: Any) -> str:
    """A Telegram user's name for others to read: "Ana (@ana)" / "@ana" / "Ana"."""
    if not isinstance(obj, dict):
        return ""
    name = " ".join(str(p) for p in (obj.get("first_name"), obj.get("last_name")) if p).strip()
    username = str(obj.get("username") or "").strip()
    if username:
        return f"{name} (@{username})" if name else f"@{username}"
    return name or str(obj.get("title") or obj.get("id") or "")
