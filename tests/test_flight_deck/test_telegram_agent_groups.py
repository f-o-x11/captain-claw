"""Users' agents in Telegram groups through the shared bot (telegram_agent_groups.py).

* a bot can't use an invite link, so the add-to-group link's "/start@bot agent"
  (or a linked person without an inbox adding the bot) brings that person's
  agent into the group; an inbox user's plain add still goes to the inbox;
* the group's messages then reach that agent — and only it — while only the
  messages addressed to the bot count against the person's plan;
* the agent may post in and leave only its own groups; removing the bot,
  Connections → Remove or disconnecting ends it (the bot stays for an inbox);
* strangers adding the bot make it leave.

Telegram is never contacted: ``telegram_shared._http`` gets a mock transport.
"""

from __future__ import annotations

import asyncio
import json
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import captain_claw.flight_deck.telegram_agent_groups as tag
import captain_claw.flight_deck.telegram_inbox as ti
import captain_claw.flight_deck.telegram_shared as ts
import captain_claw.flight_deck.telegram_shared_routes as tsr
from captain_claw.flight_deck import agent_secret, auth, fd_crypto, usage_quota
from captain_claw.flight_deck.db import FlightDeckDB

BOB = "user-bob"
CAROL = "user-carol"
BOT_TOKEN = "123456:SHARED-bot-token-xyz"
TG_BOB = 5001
TG_CAROL = 5002
TG_ANA = 5003
TG_STRANGER = 5999
GROUP = -100777
OTHER_GROUP = -100888


@pytest.fixture()
async def db(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET", "FD_DATA_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAPTAIN_CLAW_FD_HOME", str(tmp_path / "fd-home"))
    agent_secret.reset_cache_for_tests()
    fd_crypto.reset_for_tests()
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev_db = auth._db
    auth.set_auth_db(d)
    now = "2026-01-01T00:00:00Z"
    for uid, email in [(BOB, "bob@x.co"), (CAROL, "carol@x.co")]:
        await d._db.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'user', ?, ?)",
            (uid, email, "h", uid.split("-")[1].title(), now, now),
        )
    await d._db.commit()
    monkeypatch.setattr(ts, "_state", ts._State())
    monkeypatch.setattr(ti, "_unknown", {})
    monkeypatch.setattr(ti, "_last_purge", 0.0)
    monkeypatch.setattr(tag, "_pending", {})
    counted: list[tuple[str, str]] = []

    async def admit(user, content=""):
        counted.append((user, content))
        return True, {}

    monkeypatch.setattr(usage_quota, "admit_message", admit)
    d.counted = counted
    try:
        yield d
    finally:
        for _update, task in tag._pending.values():
            if task is not None:
                task.cancel()
        fd_crypto.reset_for_tests()
        agent_secret.reset_cache_for_tests()
        auth.set_auth_db(prev_db)
        await d.close()


@pytest.fixture()
def tg(monkeypatch):
    state = types.SimpleNamespace(calls=[])

    def handler(request: httpx.Request) -> httpx.Response:
        _, bot, method = request.url.path.split("/", 2)
        body = json.loads(request.content) if request.content else None
        state.calls.append({"method": method, "body": body})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 42, "is_bot": True, "first_name": "Team", "username": "team_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(ts, "_http", lambda: httpx.AsyncClient(transport=transport))

    async def idle_poll(db, token):
        await asyncio.Event().wait()

    monkeypatch.setattr(ts, "_poll_loop", idle_poll)
    return state


@pytest.fixture()
def agents(monkeypatch):
    import captain_claw.flight_deck.server as srv

    registry = {"tok-bob": (BOB, "bob-bot"), "tok-carol": (CAROL, "carol-bot")}

    def find(token):
        if token in registry:
            owner, slug = registry[token]
            return True, owner, slug
        return False, "", ""

    monkeypatch.setattr(srv, "_find_agent_by_auth", find)
    monkeypatch.setattr(srv, "_resolve_agent_identity_by_auth", lambda t: find(t)[:2])
    monkeypatch.setattr(tsr, "own_agents", lambda uid: [
        {"slug": slug, "name": slug.title(), "kind": "process", "status": "running"}
        for owner, slug in registry.values() if owner == uid])
    return registry


def _client():
    app = FastAPI()
    app.include_router(tsr.router)
    return TestClient(app, client=("127.0.0.1", 50123))


def _bearer(uid):
    return {"Authorization": f"Bearer {auth.create_access_token(uid, 'user')}"}


async def _bot_on(db, **extra):
    await ts.save_config(db, {"token": BOT_TOKEN, "enabled": True, "bot_username": "team_bot",
                              "bot_id": "42", **extra})
    await ts.start(db)


async def _link(db, user, slug, tg_user):
    await db.create_telegram_link(user, slug, slug.title(), ts.new_link_code())
    await db.update_telegram_link(user, status="approved", tg_user_id=str(tg_user), link_code="")
    await ts.reload_links(db)


_uid = iter(range(1000, 100000))


def _added(by, *, chat=GROUP, new="member", old="left"):
    return {"update_id": next(_uid), "my_chat_member": {
        "chat": {"id": chat, "type": "supergroup", "title": "Family"},
        "from": {"id": by, "is_bot": False, "first_name": f"U{by}"}, "date": 1,
        "old_chat_member": {"status": old, "user": {"id": 42, "is_bot": True}},
        "new_chat_member": {"status": new, "user": {"id": 42, "is_bot": True}}}}


def _say(frm, text, *, chat=GROUP, **extra):
    uid = next(_uid)
    return {"update_id": uid, "message": {
        "message_id": uid, "date": 1, "text": text,
        "from": {"id": frm, "is_bot": False, "first_name": f"U{frm}"},
        "chat": {"id": chat, "type": "supergroup", "title": "Family"}, **extra}}


def _sent(tg, method="sendMessage"):
    return [c for c in tg.calls if c["method"] == method]


def _queued(slug):
    box = ts._state.boxes.get(slug)
    return [u for _t, u in box.queue] if box else []


async def _poll(slug, owner):
    return await ts.relay_get_updates(slug, owner, None, 0)


class TestJoining:
    async def test_a_linked_person_without_an_inbox_brings_their_agent(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        assert await ts.route_update(db, _added(TG_BOB)) == "group:joined"
        assert tag.bound_to(GROUP) == BOB
        (joined,) = await _poll("bob-bot", BOB)
        assert joined["my_chat_member"]["chat"]["id"] == GROUP
        assert not _sent(tg, "leaveChat")

    async def test_the_add_to_group_cue_wins_over_the_inbox(self, db, tg, monkeypatch):
        monkeypatch.setattr(tag, "JOIN_WAIT", 60)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ti.set_enabled(db, BOB, enabled=True)
        assert await ts.route_update(db, _added(TG_BOB)) == "group:pending"
        start = _say(TG_BOB, "/start@team_bot agent")
        assert await ts.route_update(db, start) == "group:joined"
        assert tag.bound_to(GROUP) == BOB and not tag._pending
        # The agent hears of the join (under the /start's update id); the inbox
        # doesn't keep the group, and nobody was told the bot "won't post".
        (joined,) = await _poll("bob-bot", BOB)
        assert "my_chat_member" in joined and joined["update_id"] == start["update_id"]
        assert await ti.chats(db, BOB) == []
        assert not any("won't post" in (c["body"] or {}).get("text", "") for c in _sent(tg))

    async def test_without_the_cue_an_inbox_user_gets_the_inbox_as_before(self, db, tg, monkeypatch):
        monkeypatch.setattr(tag, "JOIN_WAIT", 0.01)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ti.set_enabled(db, BOB, enabled=True)
        assert await ts.route_update(db, _added(TG_BOB)) == "group:pending"
        await asyncio.sleep(0.1)
        assert not tag.bound_to(GROUP) and not tag._pending
        assert [c["kind"] for c in await ti.chats(db, BOB)] == ["group"]
        assert "won't post" in _sent(tg)[-1]["body"]["text"]
        assert _queued("bob-bot") == []

    async def test_the_cue_later_adds_the_agent_to_an_inbox_group(self, db, tg, monkeypatch):
        monkeypatch.setattr(tag, "JOIN_WAIT", 0)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ti.set_enabled(db, BOB, enabled=True)
        assert await ts.route_update(db, _added(TG_BOB)) == "inbox:group-joined"
        assert await ts.route_update(db, _say(TG_BOB, "/start@team_bot agent")) == "group:joined"
        (joined,) = await _poll("bob-bot", BOB)
        assert joined["my_chat_member"]["from"]["id"] == TG_BOB
        # Both now: the inbox keeps a copy, the agent sees it.
        assert await ts.route_update(db, _say(TG_ANA, "dinner at 8?")) == "group:queued"
        assert [m["text"] for m in await ti.messages(db, BOB)] == ["dinner at 8?"]

    async def test_strangers_are_turned_away(self, db, tg):
        await _bot_on(db, inbox=False)
        assert await ts.route_update(db, _added(TG_STRANGER)) == "group:refused"
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP
        # A stranger's cue in some group does nothing.
        assert await ts.route_update(db, _say(TG_STRANGER, "/start@team_bot agent", chat=OTHER_GROUP)) \
            == "group:start-ignored"
        assert not ts._state.groups

    async def test_one_agent_per_group(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL)
        await ts.route_update(db, _added(TG_BOB))
        assert await ts.route_update(db, _say(TG_CAROL, "/start@team_bot agent")) == "group:taken"
        assert tag.bound_to(GROUP) == BOB
        assert await ts.route_update(db, _say(TG_BOB, "/start@team_bot agent")) == "group:already"


class TestMessages:
    async def test_the_agent_sees_its_group_and_only_questions_count(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await _link(db, CAROL, "carol-bot", TG_CAROL)
        await ts.route_update(db, _added(TG_BOB))
        assert await ts.route_update(db, _say(TG_ANA, "who's cooking?")) == "group:queued"
        asked = _say(TG_ANA, "@team_bot any good recipes?",
                     entities=[{"type": "mention", "offset": 0, "length": 9}])
        assert await ts.route_update(db, asked) == "group:queued"
        assert await ts.route_update(db, _say(TG_ANA, "/ask what about dessert")) == "group:queued"
        assert db.counted == [(BOB, "@team_bot any good recipes?"), (BOB, "/ask what about dessert")]
        texts = [u["message"]["text"] for u in await _poll("bob-bot", BOB) if "message" in u]
        assert texts == ["who's cooking?", "@team_bot any good recipes?", "/ask what about dessert"]
        assert await _poll("carol-bot", CAROL) == []
        # Another group's messages go nowhere.
        assert await ts.route_update(db, _say(TG_ANA, "hi", chat=OTHER_GROUP)) == "ignored:group"

    async def test_an_exhausted_plan_stops_questions(self, db, tg, monkeypatch):
        async def spent(user, content=""):
            return False, {}

        monkeypatch.setattr(usage_quota, "admit_message", spent)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        await _poll("bob-bot", BOB)
        assert await ts.route_update(db, _say(TG_ANA, "/ask hi")) == "group:over_limit"
        assert _sent(tg)[-1]["body"]["chat_id"] == GROUP
        assert all("message" not in u for _t, u in ts._state.boxes["bob-bot"].queue)


class TestRelayAndLeaving:
    async def test_the_agent_posts_and_leaves_only_its_own_groups(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        c, h = _client(), {"X-Agent-Auth": "tok-bob"}
        assert c.post("/fd/telegram-relay/botshared/sendMessage", headers=h,
                      json={"chat_id": GROUP, "text": "hi all"}).status_code == 200
        assert c.post("/fd/telegram-relay/botshared/sendMessage", headers=h,
                      json={"chat_id": OTHER_GROUP, "text": "x"}).status_code == 403
        assert c.post("/fd/telegram-relay/botshared/leaveChat", headers=h,
                      json={"chat_id": OTHER_GROUP}).status_code == 403
        assert c.post("/fd/telegram-relay/botshared/sendMessage", headers={"X-Agent-Auth": "tok-carol"},
                      json={"chat_id": GROUP, "text": "x"}).status_code == 403
        r = c.post("/fd/telegram-relay/botshared/leaveChat", headers=h, json={"chat_id": GROUP})
        assert r.status_code == 200 and r.json()["result"] is True
        assert not tag.bound_to(GROUP)
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_leaving_keeps_the_bot_for_an_inbox(self, db, tg, agents, monkeypatch):
        monkeypatch.setattr(tag, "JOIN_WAIT", 0)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ti.set_enabled(db, BOB, enabled=True)
        await ts.route_update(db, _added(TG_BOB))
        await ts.route_update(db, _say(TG_BOB, "/start@team_bot agent"))
        r = _client().post("/fd/telegram-relay/botshared/leaveChat", headers={"X-Agent-Auth": "tok-bob"},
                           json={"chat_id": GROUP})
        assert r.status_code == 200 and not tag.bound_to(GROUP)
        assert not _sent(tg, "leaveChat")
        # …and the inbox turning off later doesn't keep the bot either.
        await ti.set_enabled(db, BOB, enabled=False)
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_the_inbox_turning_off_doesnt_pull_the_agent_out(self, db, tg, monkeypatch):
        monkeypatch.setattr(tag, "JOIN_WAIT", 0)
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ti.set_enabled(db, BOB, enabled=True)
        await ts.route_update(db, _added(TG_BOB))
        await ts.route_update(db, _say(TG_BOB, "/start@team_bot agent"))
        await ti.set_enabled(db, BOB, enabled=False)
        assert not _sent(tg, "leaveChat") and tag.bound_to(GROUP) == BOB

    async def test_removing_the_bot_tells_the_agent(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        await _poll("bob-bot", BOB)
        await ts.route_update(db, _added(TG_ANA, new="kicked", old="member"))
        assert not tag.bound_to(GROUP)
        (gone,) = await _poll("bob-bot", BOB)
        assert gone["my_chat_member"]["new_chat_member"]["status"] == "kicked"
        assert await ts.route_update(db, _say(TG_ANA, "still there?")) == "ignored:group"

    async def test_a_supergroup_upgrade_keeps_the_agent(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB, chat=-55))
        await _poll("bob-bot", BOB)
        await ts.route_update(db, _say(TG_ANA, "", chat=-55, migrate_to_chat_id=GROUP))
        assert tag.bound_to(GROUP) == BOB and not tag.bound_to(-55)
        (moved,) = await _poll("bob-bot", BOB)
        assert moved["message"]["migrate_to_chat_id"] == GROUP
        assert await ts.route_update(db, _say(TG_ANA, "after")) == "group:queued"


class TestConnections:
    async def test_lists_groups_offers_the_link_and_removes_one(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        c = _client()
        me = c.get("/fd/telegram-shared", headers=_bearer(BOB)).json()
        assert me["group_link"] == "https://t.me/team_bot?startgroup=agent"
        assert [g["title"] for g in me["groups"]] == ["Family"]
        assert c.get("/fd/telegram-shared", headers=_bearer(CAROL)).json()["groups"] == []
        assert c.delete(f"/fd/telegram-shared/groups/{GROUP}", headers=_bearer(CAROL)).status_code == 404
        me = c.delete(f"/fd/telegram-shared/groups/{GROUP}", headers=_bearer(BOB)).json()
        assert me["groups"] == [] and not tag.bound_to(GROUP)
        assert "isn't available" in _sent(tg)[-1]["body"]["text"]
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_titles_are_sealed_at_rest(self, db, tg, tmp_path):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        raw = (tmp_path / "fd.db").read_bytes() + b"".join(p.read_bytes() for p in tmp_path.glob("fd.db-wal"))
        assert b"Family" not in raw

    async def test_disconnecting_takes_the_agent_out(self, db, tg, agents):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        assert _client().delete("/fd/telegram-shared", headers=_bearer(BOB)).status_code == 200
        assert not ts._state.groups
        assert _sent(tg, "leaveChat")[-1]["body"]["chat_id"] == GROUP

    async def test_groups_survive_a_restart(self, db, tg):
        await _bot_on(db)
        await _link(db, BOB, "bob-bot", TG_BOB)
        await ts.route_update(db, _added(TG_BOB))
        ts._state.groups.clear()
        await ts.start(db)
        assert tag.bound_to(GROUP) == BOB
