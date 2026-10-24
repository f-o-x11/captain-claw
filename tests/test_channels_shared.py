"""The channels share one conversation and the model knows which said what:
user messages from anywhere but the web chat are marked with where they came
from, the turn's note names the current message's source, and the owner's
own Telegram chat joins the main conversation (lane A) instead of a session
of its own — other Telegram users and groups stay apart."""

from __future__ import annotations

import asyncio
import types

import pytest

from captain_claw import garbled_reply
from captain_claw.agent_context_mixin import AgentContextMixin
from captain_claw.web import telegram as tg

mark = AgentContextMixin._with_source_mark


# ── the model sees where messages came from ─────────────────────────


@pytest.mark.parametrize("msg,expected", [
    ({"role": "user", "content": "hi", "source": "whatsapp"}, "[via WhatsApp] hi"),
    ({"role": "user", "content": "hi", "source": "device:sticks3", "source_label": "M5Stack StickS3 (desk)"},
     "[via M5Stack StickS3 (desk)] hi"),
    ({"role": "user", "content": "hi", "channel": "telegram"}, "[via Telegram] hi"),  # older message
    ({"role": "user", "content": "hi", "source": "web"}, "hi"),
    ({"role": "user", "content": "hi"}, "hi"),
    ({"role": "user", "content": "hi", "source": "member"}, "hi"),
    ({"role": "user", "content": "[via WhatsApp] hi", "source": "whatsapp"}, "[via WhatsApp] hi"),
])
def test_history_marks_the_source(msg, expected):
    assert mark(msg)["content"] == expected


def test_marking_never_touches_the_stored_message():
    stored = {"role": "user", "content": "hi", "source": "whatsapp"}
    mark(stored)
    assert stored["content"] == "hi"


def test_turn_note_names_the_current_source():
    obj = types.SimpleNamespace(_turn_source=("device:sticks3", "M5Stack StickS3 (desk)"))
    line = AgentContextMixin._turn_source_line(obj)
    assert "M5Stack StickS3 (desk)" in line and "[via" in line
    assert AgentContextMixin._turn_source_line(types.SimpleNamespace()) == ""


# ── who the owner is ─────────────────────────────────────────────────


def _server(*, relay=False, owner_in_main=True, approved=None):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(telegram=types.SimpleNamespace(owner_in_main=owner_in_main)),
        _telegram_relay=relay,
        _approved_telegram_users=approved or {},
    )


def test_shared_bot_sender_is_the_owner():
    s = _server(relay=True, approved={"42": {"user_id": 42, "via": "shared_bot"}})
    assert tg._tg_is_owner(s, "42")
    assert not tg._tg_is_owner(s, "42", "supergroup")  # groups stay apart
    assert not tg._tg_is_owner(_server(relay=True, owner_in_main=False,
                                       approved={"42": {"via": "shared_bot"}}), "42")


def test_own_bot_needs_the_owner_mark():
    s = _server(approved={"1": {"user_id": 1}, "2": {"user_id": 2, "owner": True}})
    assert not tg._tg_is_owner(s, "1")
    assert tg._tg_is_owner(s, "2")
    assert not tg._tg_is_owner(s, "3")  # not approved at all


async def test_approve_with_owner(monkeypatch):
    async def _no_save(server):
        return None

    monkeypatch.setattr(tg, "_tg_save_state", _no_save)
    monkeypatch.setattr(tg, "_tg_cleanup_expired", lambda server: None)
    s = _server(approved={"7": {"user_id": 7, "username": "Stevica"}})
    s._pending_telegram_pairings = {"ABC123": {"user_id": 9, "chat_id": 9, "username": "kid"}}
    s._telegram_bridge = None
    out = await tg.handle_approve_command(s, "user telegram @stevica owner")
    assert "owner" in out and s._approved_telegram_users["7"]["owner"] is True
    out = await tg.handle_approve_command(s, "user telegram ABC123")
    assert "owner" not in out and "owner" not in s._approved_telegram_users["9"]
    out = await tg.handle_approve_command(s, "user telegram @nobody owner")
    assert out.startswith("No approved Telegram user")


# ── the owner's turn holds the main conversation ─────────────────────


class _Bridge:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, *, reply_to_message_id=None):
        self.sent.append(text)

    async def send_chat_action(self, chat_id, action):
        pass


class _MainAgent:
    """The main agent, as far as a Telegram turn touches it."""

    def __init__(self, server):
        self.server = server
        self.session = None
        self.provider = None
        self.last_usage = self.total_usage = {}
        self.web_narration = object()
        self.narration_callback = self.web_narration
        self.during: dict = {}

    async def complete(self, text):
        self.during = {
            "busy": self.server._busy,
            "chat_id": getattr(self, "_telegram_chat_id", None),
            "user_id": getattr(self, "_telegram_user_id", None),
            "text": text,
        }
        await asyncio.sleep(0)
        return "Done."


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setenv("STREAM_NARRATION", "off")
    broadcasts: list[dict] = []
    server = types.SimpleNamespace(
        _telegram_bridge=_Bridge(), _telegram_user_locks={}, _orchestrator=None,
        _broadcast=broadcasts.append, _busy=False,
    )
    server.agent = _MainAgent(server)

    async def _not_garbled(reply, message=""):
        return False

    async def _no_steps(provider, response):
        return []

    monkeypatch.setattr(garbled_reply, "is_garbled", _not_garbled)
    monkeypatch.setattr(tg, "extract_next_steps", _no_steps)
    return types.SimpleNamespace(server=server, broadcasts=broadcasts)


async def test_owner_turn_runs_on_the_main_conversation(owner_env):
    s = owner_env.server
    await tg._tg_process_with_typing(s, 4242, "hello from telegram", 1,
                                     user_agent=s.agent, user_id="42", owner=True)
    agent = s.agent
    assert agent.during == {"busy": True, "chat_id": 4242, "user_id": "42", "text": "hello from telegram"}
    # Afterwards: lane A free, nothing left on the main agent, its own hook back.
    assert s._busy is False
    assert not hasattr(agent, "_telegram_chat_id") and not hasattr(agent, "_telegram_user_id")
    assert agent.narration_callback is agent.web_narration
    kinds = [(b.get("type"), b.get("status") or b.get("role")) for b in owner_env.broadcasts]
    assert kinds[0] == ("status", "thinking")
    assert ("chat_message", "user") in kinds and ("status", "ready") in kinds
    user_frame = next(b for b in owner_env.broadcasts if b.get("role") == "user")
    assert user_frame["via"] == "telegram" and user_frame["source"] == "telegram"
    assert s._telegram_bridge.sent == ["Done."]


async def test_owner_turn_waits_for_a_running_turn(owner_env):
    s = owner_env.server
    s._busy = True

    async def _finish_other_turn():
        await asyncio.sleep(0.6)
        s._busy = False

    other = asyncio.create_task(_finish_other_turn())
    await tg._tg_process_with_typing(s, 4242, "queued", 1, user_agent=s.agent, user_id="42", owner=True)
    await other
    assert s._telegram_bridge.sent[0].startswith("Still answering another message")
    assert s.agent.during["busy"] is True and s._busy is False


async def test_someone_elses_turn_leaves_lane_a_alone(owner_env):
    s = owner_env.server
    other = _MainAgent(s)
    await tg._tg_process_with_typing(s, 9, "hi", 1, user_agent=other, user_id="9")
    assert other.during["busy"] is False  # never held the main conversation
    assert not any(b.get("type") == "status" and b.get("status") == "thinking" for b in owner_env.broadcasts)
