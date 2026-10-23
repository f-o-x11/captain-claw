"""An agent's Telegram bot in group chats (web/telegram_group_chat.py).

* bots can't follow invite links: /group or a pasted t.me/+… link gets the
  add-to-group button (t.me/<bot>?startgroup=agent);
* on the agent's own bot only someone it works for may bring it into a group —
  a stranger's add makes it leave; through Flight Deck's relay, FD decided;
* in a group it answers @mentions, replies and /ask from anyone, with what was
  said before as context, under the group's principal: the web only;
* /new and /leave are for whoever brought it in (or its owner); bare commands
  may be another bot's.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest

from captain_claw import speaker, telegram_groups
from captain_claw.exceptions import ToolBlockedError
from captain_claw.session import Session
from captain_claw.telegram_bridge import TelegramBridge, TelegramGroupEvent, TelegramMessage
from captain_claw.tools.registry import ToolRegistry, ToolResult
from captain_claw.web import telegram as tg
from captain_claw.web import telegram_group_chat as gc

OWNER = 7001
STRANGER = 7999
ANA = 7003
GROUP = -100555
ME = {"id": 42, "is_bot": True, "first_name": "Claw", "username": "claw_bot",
      "can_read_all_group_messages": True}


# ── shared helpers ───────────────────────────────────────────────────


class TestHelpers:
    def test_what_is_addressed_to_the_bot(self):
        def msg(text, **extra):
            return {"text": text, **extra}

        assert telegram_groups.addressed(
            msg("hey @Claw_Bot", entities=[{"type": "mention", "offset": 4, "length": 9}]), 42, "claw_bot")
        assert telegram_groups.addressed(  # offsets count UTF-16 units
            msg("🙂 @claw_bot hi", entities=[{"type": "mention", "offset": 3, "length": 9}]), 42, "claw_bot")
        assert not telegram_groups.addressed(
            msg("hey @other_bot", entities=[{"type": "mention", "offset": 4, "length": 10}]), 42, "claw_bot")
        assert telegram_groups.addressed(msg("sure", reply_to_message={"from": {"id": 42}}), 42, "claw_bot")
        assert not telegram_groups.addressed(msg("sure", reply_to_message={"from": {"id": 9}}), 42, "claw_bot")
        assert telegram_groups.addressed(msg("/ask what now"), 42, "claw_bot")
        assert telegram_groups.addressed(msg("/help@claw_bot"), 42, "claw_bot")
        assert not telegram_groups.addressed(msg("/help"), 42, "claw_bot")  # maybe another bot's
        assert not telegram_groups.addressed(msg("/ask@other_bot hi"), 42, "claw_bot")
        assert telegram_groups.addressed(
            msg("hi", entities=[{"type": "text_mention", "offset": 0, "length": 2, "user": {"id": 42}}]),
            42, "claw_bot")

    def test_invite_links_and_the_add_to_group_link(self):
        assert telegram_groups.has_invite_link("join us https://t.me/+PtW5t3rFsWphMWY0")
        assert telegram_groups.has_invite_link("t.me/joinchat/AAAAAEkk2WdoDrB4")
        assert telegram_groups.has_invite_link("tg://join?invite=PtW5t3rFsWphMWY0")
        assert not telegram_groups.has_invite_link("https://t.me/claw_bot")
        assert telegram_groups.add_to_group_url("@claw_bot") == "https://t.me/claw_bot?startgroup=agent"
        assert telegram_groups.add_to_group_url("") == ""
        assert telegram_groups.strip_mention("@claw_bot, what's up", "claw_bot") == "what's up"
        assert telegram_groups.command_of("/start@claw_bot agent", "claw_bot") == ("start", "agent", True)
        assert telegram_groups.command_of("/inbox@other_bot", "claw_bot") is None


# ── the bridge ───────────────────────────────────────────────────────


async def test_bridge_reads_group_messages_and_the_bots_membership():
    chat = {"id": GROUP, "type": "supergroup", "title": "Family"}
    updates = [
        {"update_id": 1, "my_chat_member": {
            "chat": chat, "from": {"id": OWNER, "first_name": "Bob", "username": "bob"},
            "old_chat_member": {"status": "left"}, "new_chat_member": {"status": "member"}}},
        {"update_id": 2, "message": {"message_id": 5, "text": "@claw_bot hi",
                                     "entities": [{"type": "mention", "offset": 0, "length": 9}],
                                     "from": {"id": ANA, "first_name": "Ana"}, "chat": chat}},
        {"update_id": 3, "message": {"message_id": 6, "text": "dinner?",
                                     "reply_to_message": {"text": "Here's a recipe", "from": {"id": 42, "is_bot": True, "first_name": "Claw", "username": "claw_bot"}},
                                     "from": {"id": ANA, "first_name": "Ana"}, "chat": chat}},
        {"update_id": 4, "message": {"message_id": 7, "text": "beep", "chat": chat,
                                     "from": {"id": 99, "is_bot": True, "first_name": "Other"}}},
        {"update_id": 5, "message": {"message_id": 8, "chat": chat, "from": {"id": ANA},
                                     "migrate_to_chat_id": -100999}},
        {"update_id": 6, "my_chat_member": {
            "chat": chat, "from": {"id": ANA}, "old_chat_member": {"status": "member"},
            "new_chat_member": {"status": "administrator"}}},  # a promotion: nothing
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": ME})
        return httpx.Response(200, json={"ok": True, "result": updates})

    bridge = TelegramBridge("123:abc")
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = await bridge.get_updates()
    joined, mention, reply, migrated = got
    assert isinstance(joined, TelegramGroupEvent) and joined.kind == "joined" \
        and joined.user_id == OWNER and joined.chat_title == "Family"
    assert mention.chat_type == "supergroup" and mention.addressed
    assert reply.addressed and reply.reply_to_text == "Here's a recipe" \
        and reply.reply_to_from == "Claw (@claw_bot)"
    assert migrated.kind == "migrated" and migrated.new_chat_id == -100999


# ── the agent in a group ─────────────────────────────────────────────


class _Bridge:
    def __init__(self, me=ME):
        self.me = dict(me)
        self.sent: list[dict] = []
        self.keyboards: list[dict] = []
        self.left: list[int] = []

    async def get_me(self):
        return self.me

    async def send_message(self, chat_id, text, *, reply_to_message_id=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id})

    async def send_message_with_inline_keyboard(self, chat_id, text, buttons, *, reply_to_message_id=None):
        self.keyboards.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def send_chat_action(self, chat_id, action="typing"):
        pass


class _GroupAgent:
    """Records each turn: the prompt and what the turn ran as."""

    def __init__(self, session):
        self.session = session
        self.turns: list[dict] = []
        self.answer = "Try a risotto."

    async def complete(self, text):
        p = speaker.current()
        self.turns.append({"text": text, "principal": p, "tools": speaker.allowed_tools(p)})
        return self.answer


class _Sessions:
    def __init__(self):
        self.made: list[Session] = []

    async def create_session(self, name="", **kw):
        s = Session(id=f"s{len(self.made) + 1}", name=name)
        self.made.append(s)
        return s

    async def load_session(self, sid):
        return next((s for s in self.made if s.id == sid), None)


@pytest.fixture
def env(monkeypatch):
    state: dict[str, str] = {}

    async def get_state(key):
        return state.get(key)

    async def set_state(key, value):
        state[key] = value

    sessions = _Sessions()
    monkeypatch.setattr(gc, "get_session_manager", lambda: sessions)
    monkeypatch.setattr(speaker, "_IN_FLIGHT", 0)
    built: list = []

    async def build(session, send, **kw):
        assert kw == {"register_tools": False, "warm_owner_caches": False}
        agent = _GroupAgent(session)
        built.append(agent)
        return agent

    server = types.SimpleNamespace(
        config=types.SimpleNamespace(telegram=types.SimpleNamespace(groups=True, group_turns_per_hour=30)),
        _telegram_bridge=_Bridge(),
        _telegram_relay=False,
        _approved_telegram_users={str(OWNER): {"username": "bob"}},
        agent=types.SimpleNamespace(
            session_manager=types.SimpleNamespace(get_app_state=get_state, set_app_state=set_state),
            _fleet_identity={"name": "Chef"}, tools=None),
        _build_scoped_agent=build,
        _register_speaker_keys=lambda agent: None,
        broadcast=[],
    )
    server._broadcast = server.broadcast.append
    return types.SimpleNamespace(server=server, state=state, built=built, bridge=server._telegram_bridge)


def _msg(frm, text, *, chat=GROUP, addressed=False, mid=1, **kw):
    return TelegramMessage(
        update_id=mid, message_id=mid, chat_id=chat, user_id=frm, username="",
        first_name={OWNER: "Bob", ANA: "Ana"}.get(frm, "Stranger"), text=text,
        chat_type="supergroup" if chat < 0 else "private", chat_title="Family" if chat < 0 else "",
        addressed=addressed, **kw)


def _joined(by):
    return TelegramGroupEvent(update_id=1, kind="joined", chat_id=GROUP, chat_type="supergroup",
                              chat_title="Family", user_id=by, first_name="Bob")


def _texts(env, chat=GROUP):
    return [m["text"] for m in env.bridge.sent if m["chat_id"] == chat]


class TestJoining:
    async def test_someone_it_works_for_brings_it_in(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        assert str(GROUP) in json.loads(env.state[gc.STATE_KEY])
        (intro,) = _texts(env)
        assert "I'm Chef — Bob added me here" in intro and "@claw_bot" in intro
        assert "Group Privacy" not in intro
        # The add-to-group link's /start right after isn't answered twice.
        await gc.handle_message(env.server, _msg(OWNER, "/start@claw_bot agent", addressed=True))
        assert len(_texts(env)) == 1

    async def test_a_stranger_adding_it_makes_it_leave(self, env):
        await gc.handle_event(env.server, _joined(STRANGER))
        assert env.bridge.left == [GROUP] and not gc.chats_of(env.server).groups
        assert "only join groups when someone I work for adds me" in _texts(env)[0]
        # …and nothing said there reaches a turn.
        await gc.handle_message(env.server, _msg(STRANGER, "@claw_bot hi", addressed=True))
        assert env.built == []

    async def test_through_the_relay_flight_deck_decided(self, env):
        env.server._telegram_relay = True
        await gc.handle_event(env.server, _joined(STRANGER))
        assert str(GROUP) in gc.chats_of(env.server).groups and env.bridge.left == []

    async def test_groups_off_means_it_leaves(self, env):
        env.server.config.telegram.groups = False
        await gc.handle_event(env.server, _joined(OWNER))
        assert env.bridge.left == [GROUP]

    async def test_privacy_mode_is_explained(self, env):
        env.bridge.me["can_read_all_group_messages"] = False
        await gc.handle_event(env.server, _joined(OWNER))
        assert "/ask@claw_bot" in _texts(env)[0] and "Group Privacy" in _texts(env)[0]

    async def test_bot_removed_and_supergroup_upgrade(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        await gc.handle_event(env.server, TelegramGroupEvent(
            update_id=2, kind="migrated", chat_id=GROUP, new_chat_id=-100999))
        assert list(gc.chats_of(env.server).groups) == ["-100999"]
        await gc.handle_event(env.server, TelegramGroupEvent(update_id=3, kind="left", chat_id=-100999))
        assert not gc.chats_of(env.server).groups


class TestAnswering:
    async def test_answers_a_mention_with_context_and_the_web_only(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        await gc.handle_message(env.server, _msg(ANA, "who's cooking tonight?", mid=2))
        await gc.handle_message(env.server, _msg(OWNER, "not me", mid=3))
        assert env.built == []  # chatter isn't a question
        await gc.handle_message(env.server, _msg(ANA, "@claw_bot any idea what to make?",
                                                 addressed=True, mid=4))
        (agent,) = env.built
        (turn,) = agent.turns
        assert turn["principal"].speaker_id == f"tg-group:{GROUP}"
        assert turn["tools"] == frozenset({"web_search", "web_fetch"})
        assert "Ana: who's cooking tonight?" in turn["text"] and "Bob: not me" in turn["text"]
        assert turn["text"].endswith("Ana: any idea what to make?")
        assert "@claw_bot" not in turn["text"].splitlines()[-1]
        assert env.bridge.sent[-1] == {"chat_id": GROUP, "text": "Try a risotto.", "reply_to": 4}
        assert speaker.current() is None and speaker.turns_in_flight() == 0
        # The context went with that answer; the next one starts fresh.
        await gc.handle_message(env.server, _msg(ANA, "/ask and dessert?", mid=5))
        assert "cooking" not in agent.turns[1]["text"] and agent.turns[1]["text"].endswith("Ana: and dessert?")

    async def test_a_reply_to_the_bot_carries_what_it_replies_to(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        await gc.handle_message(env.server, _msg(
            ANA, "how long?", addressed=True, mid=2,
            reply_to_text="Try a risotto.", reply_to_from="Claw (@claw_bot)"))
        assert '[Ana is replying to Claw (@claw_bot): "Try a risotto."]' in env.built[0].turns[0]["text"]

    async def test_answers_are_capped_per_hour(self, env):
        env.server.config.telegram.group_turns_per_hour = 1
        await gc.handle_event(env.server, _joined(OWNER))
        for mid in (2, 3, 4):
            await gc.handle_message(env.server, _msg(ANA, "/ask hi", mid=mid))
        assert len(env.built[0].turns) == 1
        assert _texts(env).count(gc.CAPPED) == 1

    async def test_an_unknown_group_is_adopted_only_for_someone_it_works_for(self, env):
        await gc.handle_message(env.server, _msg(STRANGER, "@claw_bot hi", addressed=True))
        assert not gc.chats_of(env.server).groups and env.built == []
        await gc.handle_message(env.server, _msg(OWNER, "@claw_bot hi", addressed=True, mid=2))
        assert str(GROUP) in gc.chats_of(env.server).groups
        assert "I'm Chef" in _texts(env)[0] and len(env.built[0].turns) == 1


class TestCommands:
    async def test_new_and_leave_are_for_whoever_brought_it_in(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        await gc.handle_message(env.server, _msg(ANA, "/leave@claw_bot", addressed=True, mid=2))
        assert env.bridge.left == [] and "Only Bob or my owner" in _texts(env)[-1]
        await gc.handle_message(env.server, _msg(ANA, "/ask hi", mid=3))
        first = gc.chats_of(env.server).groups[str(GROUP)]["session_id"]
        await gc.handle_message(env.server, _msg(OWNER, "/new@claw_bot", addressed=True, mid=4))
        assert gc.chats_of(env.server).groups[str(GROUP)]["session_id"] != first
        await gc.handle_message(env.server, _msg(OWNER, "/leave@claw_bot", addressed=True, mid=5))
        assert env.bridge.left == [GROUP] and not gc.chats_of(env.server).groups

    async def test_bare_commands_may_be_another_bots(self, env):
        await gc.handle_event(env.server, _joined(OWNER))
        n = len(env.bridge.sent)
        await gc.handle_message(env.server, _msg(ANA, "/help", mid=2))
        await gc.handle_message(env.server, _msg(ANA, "/config", mid=3))
        assert len(env.bridge.sent) == n
        await gc.handle_message(env.server, _msg(ANA, "/config@claw_bot", addressed=True, mid=4))
        assert _texts(env)[-1] == gc.ONLY_QUESTIONS
        await gc.handle_message(env.server, _msg(ANA, "/ask", mid=5))
        assert _texts(env)[-1] == gc.ASK_USAGE


class TestPrivateChat:
    async def test_an_invite_link_gets_the_add_to_group_button(self, env):
        msg = _msg(OWNER, "join https://t.me/+PtW5t3rFsWphMWY0", chat=OWNER)
        assert gc.wants_group_link(msg.text) and gc.wants_group_link("/group")
        assert not gc.wants_group_link("hello")
        await gc.offer_add_link(env.server, msg)
        (kb,) = env.bridge.keyboards
        assert "doesn't let bots join through invite links" in kb["text"]
        assert kb["buttons"] == [[{"text": "➕ Add me to a group",
                                   "url": "https://t.me/claw_bot?startgroup=agent"}]]

    async def test_the_private_handler_offers_it_before_any_turn(self, env, monkeypatch):
        async def no_turn(*a, **kw):
            raise AssertionError("no agent turn for an invite link")

        async def agent_for(server, message):
            return types.SimpleNamespace(session=None)

        monkeypatch.setattr(tg, "_tg_process_with_typing", no_turn)
        monkeypatch.setattr(tg, "_tg_get_or_create_agent", agent_for)
        await tg._handle_telegram_message(env.server, _msg(OWNER, "https://t.me/+PtW5t3rFsWphMWY0", chat=OWNER))
        assert len(env.bridge.keyboards) == 1


# ── what a group turn may run ────────────────────────────────────────


class _Rec:
    def __init__(self, name):
        self.name, self.description = name, name
        self.parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="ran")


async def test_a_group_turn_lists_and_runs_only_the_web(monkeypatch):
    monkeypatch.setattr(speaker, "_IN_FLIGHT", 0)
    reg = ToolRegistry()
    for name in ("shell", "insights", "playbooks", "topics", "read", "web_search", "web_fetch"):
        reg.register(_Rec(name))
    tok = speaker.bind(speaker.group_principal(GROUP, "Family"))
    try:
        assert set(reg.list_tools(session_id="s1")) == {"web_search", "web_fetch"}
        for name in ("shell", "insights", "read"):
            with pytest.raises(ToolBlockedError):
                await reg.execute(name, {"action": "list"}, session_id="s1")
    finally:
        speaker.reset(tok)
    assert speaker.speaker_mode_note(speaker.group_principal(GROUP)) == speaker.GROUP_MODE_NOTE
