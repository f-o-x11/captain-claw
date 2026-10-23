"""Minimal Telegram Bot API bridge (long polling + send message)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from captain_claw import telegram_groups


def _md_to_telegram_html(text: str) -> str:
    """Convert common Markdown formatting to Telegram-compatible HTML.

    Supports: **bold**, *italic*, `code`, ```code blocks```, [text](url),
    ~~strikethrough~~.  Unsupported or nested patterns are left as-is.
    HTML entities in the source text are escaped first so the output is safe
    to send with ``parse_mode="HTML"``.
    """
    # Escape HTML entities FIRST so user content doesn't break tags.
    text = html.escape(text, quote=False)

    # Fenced code blocks: ```lang\n...\n``` → <pre>...</pre>
    text = re.sub(
        r"```(?:\w*)\n(.*?)```",
        lambda m: f"<pre>{m.group(1).rstrip()}</pre>",
        text,
        flags=re.DOTALL,
    )
    # Inline code: `...` → <code>...</code>
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)

    # Bold: **text** → <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # Italic: *text* → <i>text</i> (but not inside <b> tags from bold)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    # Strikethrough: ~~text~~ → <s>text</s>
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Links: [text](url) → <a href="url">text</a>
    text = re.sub(
        r"\[([^\]]+?)\]\(([^)]+?)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # Strip heading markers: ### Heading → <b>Heading</b>
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    return text


@dataclass
class TelegramMessage:
    """Normalized incoming Telegram message payload."""

    update_id: int
    message_id: int
    chat_id: int
    user_id: int
    username: str
    first_name: str
    text: str
    photo_file_id: str = ""
    document_file_id: str = ""
    document_file_name: str = ""
    contact_phone: str = ""
    contact_first_name: str = ""
    contact_last_name: str = ""
    contact_vcard: str = ""
    business_connection_id: str = ""
    # A voice note, audio file, video, round video message or GIF ("voice",
    # "audio", "video", "video_note", "animation"): Telegram sends each as its
    # own type, never as a document.
    media_kind: str = ""
    media_file_id: str = ""
    media_file_name: str = ""
    media_mime: str = ""
    media_size: int = 0
    media_duration: int = 0
    # Forwarded from someone else (a forwarded voice note isn't the user speaking).
    forwarded: bool = False
    # "private", or "group" / "supergroup" (web/telegram_group_chat.py).
    chat_type: str = "private"
    chat_title: str = ""
    # In a group: meant for this bot (telegram_groups.addressed) — an @mention,
    # a reply to one of its messages, /ask or /command@bot.
    addressed: bool = False
    # The message this one replies to: its text and who wrote it.
    reply_to_text: str = ""
    reply_to_from: str = ""


@dataclass
class TelegramGroupEvent:
    """The bot joined or left a group, or the group became a supergroup."""

    update_id: int
    kind: str  # "joined" | "left" | "migrated"
    chat_id: int
    chat_type: str = ""
    chat_title: str = ""
    # Who added or removed the bot (nobody for "migrated").
    user_id: int = 0
    username: str = ""
    first_name: str = ""
    # "migrated": the supergroup's new id.
    new_chat_id: int = 0


# A chat member's status: in the chat, or not.
_PRESENT = ("member", "administrator", "creator", "restricted")
_GONE = ("left", "kicked")


# Telegram's own types for sound and moving pictures, in the order a message
# is checked (a GIF also carries a ``document``; the animation wins).
MEDIA_KINDS = ("voice", "audio", "video", "video_note", "animation")


@dataclass
class TelegramCallbackQuery:
    """Normalized incoming Telegram callback query (inline keyboard button press)."""

    update_id: int
    callback_query_id: str
    chat_id: int
    user_id: int
    username: str
    first_name: str
    data: str  # callback_data from the button
    message_id: int = 0  # message the button was attached to


def _group_membership(update_id: int, member: dict[str, Any]) -> TelegramGroupEvent | None:
    """A ``my_chat_member`` update as a joined / left event (groups only;
    promotions and other changes are None)."""
    chat = member.get("chat") if isinstance(member.get("chat"), dict) else {}
    if chat.get("type") not in telegram_groups.GROUP_KINDS:
        return None
    new = str((member.get("new_chat_member") or {}).get("status") or "")
    old = str((member.get("old_chat_member") or {}).get("status") or "")
    if new in _GONE:
        kind = "left"
    elif new in _PRESENT and (old in _GONE or not old):
        kind = "joined"
    else:
        return None
    actor = member.get("from") if isinstance(member.get("from"), dict) else {}
    try:
        return TelegramGroupEvent(
            update_id=update_id, kind=kind, chat_id=int(chat.get("id", 0)),
            chat_type=str(chat.get("type") or ""), chat_title=str(chat.get("title") or ""),
            user_id=int(actor.get("id", 0) or 0), username=str(actor.get("username") or "").strip(),
            first_name=str(actor.get("first_name") or "").strip(),
        )
    except (TypeError, ValueError):
        return None


class TelegramBridge:
    """Telegram Bot API helper."""

    def __init__(
        self,
        token: str,
        api_base_url: str = "https://api.telegram.org",
        *,
        headers: dict[str, str] | None = None,
    ):
        self.token = token.strip()
        self.api_base_url = (api_base_url or "https://api.telegram.org").rstrip("/")
        # ``headers``: Flight Deck's agent headers when this bridge talks to the
        # deck's shared-bot relay instead of Telegram (see web/telegram.py).
        self._client = httpx.AsyncClient(timeout=40.0, headers=headers or None)
        self._me: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def get_me(self) -> dict[str, Any]:
        """The bot itself (``getMe``: id, username, can_read_all_group_messages);
        ``{}`` while Telegram can't be asked. Kept once known."""
        if self._me is None:
            try:
                response = await self._client.get(self._url("getMe"))
                data = response.json()
            except Exception:
                return {}
            if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                return {}
            self._me = data["result"]
        return self._me

    async def leave_chat(self, chat_id: int) -> None:
        response = await self._client.post(self._url("leaveChat"), json={"chat_id": int(chat_id)})
        response.raise_for_status()

    def _url(self, method: str) -> str:
        return f"{self.api_base_url}/bot{self.token}/{method}"

    async def close(self) -> None:
        await self._client.aclose()

    async def get_updates(
        self, offset: int | None = None, timeout: int = 25,
    ) -> list[TelegramMessage | TelegramCallbackQuery | TelegramGroupEvent]:
        """Poll Telegram updates and normalize messages, callback queries and
        the bot's own group membership."""
        params: dict[str, Any] = {"timeout": max(1, int(timeout))}
        if offset is not None:
            params["offset"] = int(offset)
        response = await self._client.get(self._url("getUpdates"), params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return []
        results = payload.get("result")
        if not isinstance(results, list):
            return []

        updates: list[TelegramMessage | TelegramCallbackQuery | TelegramGroupEvent] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            update_id = int(item.get("update_id", 0))

            # The bot added to / removed from a group.
            member = item.get("my_chat_member")
            if isinstance(member, dict):
                event = _group_membership(update_id, member)
                if event is not None:
                    updates.append(event)
                continue

            # Handle callback queries (inline keyboard button presses).
            cbq = item.get("callback_query")
            if isinstance(cbq, dict):
                cb_from = cbq.get("from")
                cb_msg = cbq.get("message")
                if isinstance(cb_from, dict):
                    cb_chat_id = 0
                    cb_message_id = 0
                    if isinstance(cb_msg, dict):
                        cb_chat = cb_msg.get("chat")
                        if isinstance(cb_chat, dict):
                            cb_chat_id = int(cb_chat.get("id", 0))
                        cb_message_id = int(cb_msg.get("message_id", 0))
                    updates.append(
                        TelegramCallbackQuery(
                            update_id=update_id,
                            callback_query_id=str(cbq.get("id", "")),
                            chat_id=cb_chat_id or int(cb_from.get("id", 0)),
                            user_id=int(cb_from.get("id", 0)),
                            username=str(cb_from.get("username", "")).strip(),
                            first_name=str(cb_from.get("first_name", "")).strip(),
                            data=str(cbq.get("data", "")).strip(),
                            message_id=cb_message_id,
                        )
                    )
                continue

            # Handle regular messages.
            msg = item.get("message")
            if not isinstance(msg, dict):
                msg = item.get("business_message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
            chat_type = str(chat.get("type") or "private")
            if chat_type in telegram_groups.GROUP_KINDS and msg.get("migrate_to_chat_id"):
                # The group became a supergroup, with a new id.
                try:
                    updates.append(TelegramGroupEvent(
                        update_id=update_id, kind="migrated", chat_id=int(chat.get("id", 0)),
                        chat_type=chat_type, chat_title=str(chat.get("title") or ""),
                        new_chat_id=int(msg["migrate_to_chat_id"]),
                    ))
                except (TypeError, ValueError):
                    pass
                continue
            # Extract text, or caption + photo/document for media messages.
            text = str(msg.get("text", "")).strip()
            photo_file_id = ""
            document_file_id = ""
            document_file_name = ""
            contact_phone = ""
            contact_first_name = ""
            contact_last_name = ""
            contact_vcard = ""

            photo_array = msg.get("photo")
            if isinstance(photo_array, list) and photo_array:
                # Telegram sends multiple sizes; pick the largest (last).
                largest = photo_array[-1]
                if isinstance(largest, dict):
                    photo_file_id = str(largest.get("file_id", "")).strip()
                # Use caption as text for photo messages.
                if not text:
                    text = str(msg.get("caption", "")).strip()

            # Document attachments (Word, PDF, Excel, etc.).
            doc = msg.get("document")
            if isinstance(doc, dict):
                document_file_id = str(doc.get("file_id", "")).strip()
                document_file_name = str(doc.get("file_name", "")).strip()
                if not text:
                    text = str(msg.get("caption", "")).strip()

            # Voice notes, audio files, videos, round video messages, GIFs.
            media_kind = media_file_id = media_file_name = media_mime = ""
            media_size = media_duration = 0
            for kind in MEDIA_KINDS:
                media = msg.get(kind)
                if isinstance(media, dict) and str(media.get("file_id", "")).strip():
                    media_kind = kind
                    media_file_id = str(media.get("file_id", "")).strip()
                    media_file_name = str(media.get("file_name", "") or "").strip()
                    media_mime = str(media.get("mime_type", "") or "").strip()
                    try:
                        media_size = int(media.get("file_size") or 0)
                        media_duration = int(media.get("duration") or 0)
                    except (TypeError, ValueError):
                        media_size = media_duration = 0
                    if kind == "animation":
                        document_file_id = document_file_name = ""  # same file, sent twice
                    if not text:
                        text = str(msg.get("caption", "")).strip()
                    break

            # Contact messages.
            contact = msg.get("contact")
            if isinstance(contact, dict):
                contact_phone = str(contact.get("phone_number", "")).strip()
                contact_first_name = str(contact.get("first_name", "")).strip()
                contact_last_name = str(contact.get("last_name", "")).strip()
                contact_vcard = str(contact.get("vcard", "")).strip()

            if (not text and not photo_file_id and not document_file_id and not contact_phone
                    and not media_file_id):
                continue
            from_user = msg.get("from")
            if not isinstance(from_user, dict) or not chat:
                continue
            user_id = int(from_user.get("id", 0))
            chat_id = int(chat.get("id", 0))
            if user_id == 0 or chat_id == 0:
                continue
            is_group = chat_type in telegram_groups.GROUP_KINDS
            if is_group and from_user.get("is_bot"):
                continue  # another bot's message
            addressed = False
            if is_group:
                me = await self.get_me()
                addressed = telegram_groups.addressed(msg, me.get("id"), str(me.get("username") or ""))
            reply = msg.get("reply_to_message") if isinstance(msg.get("reply_to_message"), dict) else {}
            reply_text = str(reply.get("text") or reply.get("caption") or "").strip()
            updates.append(
                TelegramMessage(
                    update_id=update_id,
                    message_id=int(msg.get("message_id", 0)),
                    chat_id=chat_id,
                    user_id=user_id,
                    username=str(from_user.get("username", "")).strip(),
                    first_name=str(from_user.get("first_name", "")).strip(),
                    text=text,
                    photo_file_id=photo_file_id,
                    document_file_id=document_file_id,
                    document_file_name=document_file_name,
                    contact_phone=contact_phone,
                    contact_first_name=contact_first_name,
                    contact_last_name=contact_last_name,
                    contact_vcard=contact_vcard,
                    business_connection_id=str(msg.get("business_connection_id", "")).strip(),
                    media_kind=media_kind,
                    media_file_id=media_file_id,
                    media_file_name=media_file_name,
                    media_mime=media_mime,
                    media_size=media_size,
                    media_duration=media_duration,
                    forwarded=isinstance(msg.get("forward_origin"), dict),
                    chat_type=chat_type,
                    chat_title=str(chat.get("title") or "") if is_group else "",
                    addressed=addressed,
                    reply_to_text=reply_text,
                    reply_to_from=telegram_groups.person(reply.get("from")) if reply_text else "",
                )
            )
        return updates

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send text to Telegram, splitting at 4096 chars when needed.

        Markdown in *text* is converted to Telegram HTML so formatting
        renders natively in the Telegram app.  If the API rejects the
        HTML (malformed tags etc.), the message is re-sent as plain text.
        If reply_to_message_id is stale, the message is re-sent without it.
        """
        raw = str(text or "").strip()
        if not raw:
            return
        max_len = 3800
        chunks: list[str] = []
        while raw:
            if len(raw) <= max_len:
                chunks.append(raw)
                break
            split_at = raw.rfind("\n", 0, max_len)
            if split_at < 800:
                split_at = max_len
            chunks.append(raw[:split_at].rstrip())
            raw = raw[split_at:].lstrip()

        for idx, chunk in enumerate(chunks):
            html_chunk = _md_to_telegram_html(chunk)
            payload: dict[str, Any] = {
                "chat_id": int(chat_id),
                "text": html_chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to_message_id and idx == 0:
                payload["reply_to_message_id"] = int(reply_to_message_id)
            response = await self._client.post(self._url("sendMessage"), json=payload)
            # Fallback 1: if Telegram rejects the HTML, resend as plain text.
            if response.status_code == 400:
                payload["text"] = chunk
                payload.pop("parse_mode", None)
                response = await self._client.post(self._url("sendMessage"), json=payload)
            # Fallback 2: if reply_to target is stale/deleted, drop it and retry.
            if response.status_code == 400 and "reply_to_message_id" in payload:
                payload.pop("reply_to_message_id", None)
                response = await self._client.post(self._url("sendMessage"), json=payload)
            response.raise_for_status()

    async def send_message_with_inline_keyboard(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict[str, str]]],
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send a message with inline keyboard buttons.

        *buttons* is a list of rows, each row is a list of button dicts
        with keys ``text`` and ``callback_data``.
        """
        raw = str(text or "").strip() or "\u200b"  # zero-width space fallback
        html_text = _md_to_telegram_html(raw)
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons},
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        response = await self._client.post(self._url("sendMessage"), json=payload)
        if response.status_code == 400:
            payload["text"] = raw
            del payload["parse_mode"]
            response = await self._client.post(self._url("sendMessage"), json=payload)
        response.raise_for_status()

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Acknowledge a callback query (inline keyboard button press)."""
        payload: dict[str, Any] = {
            "callback_query_id": str(callback_query_id),
        }
        if text:
            payload["text"] = str(text)[:200]
        response = await self._client.post(self._url("answerCallbackQuery"), json=payload)
        response.raise_for_status()

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Send transient chat action status (e.g., `typing`)."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "action": str(action or "typing").strip() or "typing",
        }
        response = await self._client.post(self._url("sendChatAction"), json=payload)
        response.raise_for_status()

    async def send_audio_file(
        self,
        chat_id: int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        """Upload audio file to Telegram using sendAudio."""
        audio_path = Path(file_path).expanduser().resolve()
        if not audio_path.exists() or not audio_path.is_file():
            raise FileNotFoundError(f"Telegram audio file not found: {audio_path}")
        payload: dict[str, Any] = {"chat_id": int(chat_id)}
        caption_text = str(caption or "").strip()
        if caption_text:
            payload["caption"] = caption_text
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        with audio_path.open("rb") as handle:
            files = {
                "audio": (
                    audio_path.name,
                    handle,
                    "audio/mpeg",
                )
            }
            response = await self._client.post(
                self._url("sendAudio"),
                data=payload,
                files=files,
            )
        response.raise_for_status()

    async def send_photo(
        self,
        chat_id: int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        """Upload an image file to Telegram using sendPhoto."""
        photo_path = Path(file_path).expanduser().resolve()
        if not photo_path.exists() or not photo_path.is_file():
            raise FileNotFoundError(f"Telegram photo file not found: {photo_path}")
        payload: dict[str, Any] = {"chat_id": int(chat_id)}
        caption_text = str(caption or "").strip()
        if caption_text:
            payload["caption"] = caption_text
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime = mime_types.get(photo_path.suffix.lower(), "image/png")
        with photo_path.open("rb") as handle:
            files = {
                "photo": (
                    photo_path.name,
                    handle,
                    mime,
                )
            }
            response = await self._client.post(
                self._url("sendPhoto"),
                data=payload,
                files=files,
            )
        response.raise_for_status()

    async def send_document(
        self,
        chat_id: int,
        file_path: str | Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        """Upload a generic document file to Telegram using sendDocument.

        Suitable for Word, PowerPoint, Excel, PDF, TXT, MD and similar files.
        """
        doc_path = Path(file_path).expanduser().resolve()
        if not doc_path.exists() or not doc_path.is_file():
            raise FileNotFoundError(f"Telegram document file not found: {doc_path}")
        payload: dict[str, Any] = {"chat_id": int(chat_id)}
        caption_text = str(caption or "").strip()
        if caption_text:
            payload["caption"] = caption_text
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        mime_types: dict[str, str] = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".csv": "text/csv",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".vcf": "text/vcard",
        }
        mime = mime_types.get(doc_path.suffix.lower(), "application/octet-stream")
        with doc_path.open("rb") as handle:
            files = {
                "document": (
                    doc_path.name,
                    handle,
                    mime,
                )
            }
            response = await self._client.post(
                self._url("sendDocument"),
                data=payload,
                files=files,
            )
        response.raise_for_status()

    async def send_contact(
        self,
        chat_id: int,
        phone_number: str,
        first_name: str,
        *,
        last_name: str = "",
        vcard: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send a contact to Telegram using sendContact."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "phone_number": str(phone_number or "").strip(),
            "first_name": str(first_name or "").strip(),
        }
        if last_name:
            payload["last_name"] = str(last_name).strip()
        if vcard:
            payload["vcard"] = str(vcard).strip()
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        response = await self._client.post(self._url("sendContact"), json=payload)
        response.raise_for_status()

    async def download_file(self, file_id: str, dest: Path) -> Path:
        """Download a Telegram file by file_id to a local path."""
        # Step 1: getFile to get file_path on Telegram servers.
        resp = await self._client.post(
            self._url("getFile"), json={"file_id": file_id},
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
            # Keep Telegram's reason ("Bad Request: file is too big" past the
            # Bot API's 20 MB download limit) — a bare HTTP 400 tells no one anything.
            reason = data.get("description") if isinstance(data, dict) else ""
            raise RuntimeError(f"getFile failed: {reason or f'HTTP {resp.status_code}'}")
        file_path = data.get("result", {}).get("file_path", "")
        if not file_path:
            raise RuntimeError("getFile returned no file_path")
        # Step 2: Download the file bytes.
        download_url = f"{self.api_base_url}/file/bot{self.token}/{file_path}"
        dl_resp = await self._client.get(download_url)
        dl_resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(dl_resp.content)
        return dest

    async def read_business_message(
        self,
        business_connection_id: str,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Mark incoming business message as read (Business API only)."""
        payload: dict[str, Any] = {
            "business_connection_id": str(business_connection_id or "").strip(),
            "chat_id": int(chat_id),
            "message_id": int(message_id),
        }
        if not payload["business_connection_id"]:
            return
        response = await self._client.post(self._url("readBusinessMessage"), json=payload)
        response.raise_for_status()

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register slash commands shown in Telegram command picker."""
        payload_commands: list[dict[str, str]] = []
        for name, description in commands:
            command = str(name or "").strip().lower().lstrip("/")
            desc = str(description or "").strip()
            if not command or not desc:
                continue
            payload_commands.append({"command": command, "description": desc})
        if not payload_commands:
            return
        payload: dict[str, Any] = {"commands": payload_commands}
        response = await self._client.post(self._url("setMyCommands"), json=payload)
        response.raise_for_status()

    async def set_chat_menu_button_commands(self) -> None:
        """Configure chat menu button to open command list."""
        payload: dict[str, Any] = {"menu_button": {"type": "commands"}}
        response = await self._client.post(self._url("setChatMenuButton"), json=payload)
        response.raise_for_status()
