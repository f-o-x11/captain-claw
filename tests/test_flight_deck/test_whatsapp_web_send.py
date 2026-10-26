"""WhatsApp Web, Phase 3 (docs/whatsapp-web-plan.md): sending, automatic like
Gmail, only to whitelisted chats.

The plan's "done when", one test each:

* a send to a whitelisted chat works and carries the signature;
* a send to a chat that isn't whitelisted is refused, and so is every send
  while the whitelist is empty or sending is off;
* a number with no conversation is refused even if someone typed it into the
  whitelist (it never even gets onto it);
* the daily limit, the per-chat minute limit and the kill switch refuse;
* an unknown outcome (WhatsApp didn't confirm) is recorded, rings a bell and
  catches the blind retry; a definite failure doesn't block a retry;
* the routes: owner-only, the kill switch, the user's Sending settings;
* the tool's first line is what fabrication_guard takes as proof, and the guard
  turns down a WhatsApp send claim with no send behind it.

WhatsApp is never contacted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw import fabrication_guard as fg
from captain_claw import whatsapp_web_client as wwc
from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import whatsapp_web_routes as routes
from captain_claw.flight_deck import whatsapp_web_supervisor as sup
from captain_claw.flight_deck.whatsapp_web.api import build_app
from captain_claw.flight_deck.whatsapp_web.engine import SEND_PER_CHAT_MINUTE, Engine, LinkError
from captain_claw.flight_deck.whatsapp_web.store import DEFAULT_SIGNATURE, Store
from captain_claw.tools.whatsapp_web import WhatsAppWebTool

ANA = "385911111111@s.whatsapp.net"
IVO = "385922222222@s.whatsapp.net"
STRANGER = "385933333333@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
ALICE = "user-alice"


@pytest.fixture(autouse=True)
def data_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FD_DATA_KEY_FILE", str(tmp_path / "data_key"))
    fd_crypto.reset_for_tests()
    yield
    fd_crypto.reset_for_tests()


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def __call__(self):
        return self.t


class FakeAdapter:
    def __init__(self, client_id, session_path, sink, *, history_days):
        self.sink, self.session_path = sink, Path(session_path)
        self.sent: list[tuple[str, str]] = []
        self.fail: Exception | None = None
        FakeAdapter.last = self

    async def connect(self):
        self.session_path.write_bytes(b"x")

    async def pair_code(self, phone):
        return "ABCD-EFGH"

    async def download(self, field, raw):
        return b""

    async def send_text(self, jid, text):
        if self.fail:
            raise self.fail
        self.sent.append((jid, text))
        return f"3EB0{len(self.sent):04d}"

    async def logout(self):
        pass

    async def stop(self):
        pass


def _row(mid, chat, text="hi", ts=1_789_999_000):
    return {"op": "message", "chat": chat, "id": mid, "sender": chat if not chat.endswith("@g.us") else IVO,
            "from_me": False, "is_group": chat.endswith("@g.us"), "ts": ts, "push_name": "Ana", "kind": "text",
            "text": text, "media": None, "reply_to": ""}


async def _engine(tmp_path, clock=None):
    eng = Engine(tmp_path / "waw", adapter_factory=FakeAdapter, clock=clock or Clock())
    await eng.link(ALICE, consented_at="t")
    a = FakeAdapter.last
    await a.sink("paired", {"jid": "385976707736:56@s.whatsapp.net"})
    await a.sink("connected", {})
    await a.sink("message", _row("A1", ANA))
    await a.sink("message", _row("G1", GROUP, "dinner?"))
    await a.sink("directory", {"contacts": [], "groups": [{"jid": GROUP, "name": "Obitelj"}]})
    # Ivo: known as a contact, but no conversation.
    await a.sink("directory", {"contacts": [{"jid": IVO, "full_name": "Ivo Ivić"}], "groups": []})
    return eng, a


async def _status(coro) -> int:
    with pytest.raises(LinkError) as e:
        await coro
    return e.value.status


async def test_off_and_an_empty_whitelist_send_nothing(tmp_path):
    eng, a = await _engine(tmp_path)
    assert await _status(eng.send(ALICE, chat="Ana", text="hello")) == 403  # sending is off
    await eng.set_send_policy(ALICE, {"enabled": True})
    assert await _status(eng.send(ALICE, chat="Ana", text="hello")) == 403  # nobody whitelisted
    assert a.sent == []


async def test_a_whitelisted_chat_gets_the_message_with_the_signature(tmp_path):
    eng, a = await _engine(tmp_path)
    policy = await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA]})
    assert policy["allowed"] == [ANA] and policy["signature"] == DEFAULT_SIGNATURE and policy["signature_on"]
    out = await eng.send(ALICE, chat="Ana", text="Running 10 minutes late", agent="ana-bot")
    assert out["status"] == "sent" and out["signed"] and out["chat"] == "Ana"
    assert a.sent == [(ANA, f"Running 10 minutes late\n\n{DEFAULT_SIGNATURE}")]
    bells = eng.events_after(0)
    assert bells[-1]["title"] == "ana-bot sent a WhatsApp message" and "To Ana: Running" in bells[-1]["body"]
    sends = (await eng.send_policy(ALICE))["sends"]
    assert sends[0]["status"] == "sent" and sends[0]["agent"] == "ana-bot" and sends[0]["chat"] == "Ana"
    # The group isn't whitelisted.
    assert await _status(eng.send(ALICE, chat="Obitelj", text="hi all")) == 403
    # Signature off sends the text alone.
    await eng.set_send_policy(ALICE, {"signature_on": False})
    out = await eng.send(ALICE, chat="Ana", text="see you")
    assert a.sent[-1] == (ANA, "see you") and not out["signed"]


async def test_a_number_without_a_conversation_never_gets_on_the_list(tmp_path):
    eng, a = await _engine(tmp_path)
    policy = await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA, IVO, STRANGER, GROUP]})
    assert set(policy["allowed"]) == {ANA, GROUP} and set(policy["dropped"]) == {IVO, STRANGER}
    # A chat row with no messages (history listed it, nothing was said) isn't a
    # conversation either: it can't be whitelisted, and forced into the stored
    # list it still sends nothing.
    store = eng._links[ALICE].store
    store.put_chats([{"jid": IVO, "name": "Ivo", "is_group": False}])
    assert (await eng.set_send_policy(ALICE, {"allowed": [ANA, IVO]}))["dropped"] == [IVO]
    store.set_send_policy({**store.send_policy(), "allowed": [ANA, IVO]})
    assert await _status(eng.send(ALICE, chat="Ivo", text="hello Ivo")) == 403
    assert await _status(eng.send(ALICE, chat="+385 93 333 3333", text="hi")) == 404  # not even a chat
    assert a.sent == []


async def test_repeats_and_limits(tmp_path):
    clock = Clock()
    eng, a = await _engine(tmp_path, clock)
    await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA, GROUP], "daily_limit": 200})
    await eng.send(ALICE, chat="Ana", text="ok")
    assert await _status(eng.send(ALICE, chat="Ana", text="  OK ")) == 409  # the same text, folded
    await eng.send(ALICE, chat="Ana", text="ok", allow_repeat=True)
    for n in range(SEND_PER_CHAT_MINUTE - 2):
        await eng.send(ALICE, chat="Ana", text=f"line {n}")
    assert await _status(eng.send(ALICE, chat="Ana", text="one too many")) == 429  # per chat, per minute
    await eng.send(ALICE, chat="Obitelj", text="other chats aren't held up")
    clock.t += 61
    await eng.send(ALICE, chat="Ana", text="a minute later")
    await eng.set_send_policy(ALICE, {"daily_limit": len(a.sent)})
    assert await _status(eng.send(ALICE, chat="Obitelj", text="past the day's limit")) == 429
    clock.t += 86401
    await eng.send(ALICE, chat="Obitelj", text="a day later")


async def test_an_unknown_outcome_catches_the_blind_retry(tmp_path):
    eng, a = await _engine(tmp_path)
    await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA]})
    a.fail = asyncio.TimeoutError()
    assert await _status(eng.send(ALICE, chat="Ana", text="are you there?", agent="bot")) == 504
    a.fail = None
    with pytest.raises(LinkError) as e:
        await eng.send(ALICE, chat="Ana", text="are you there?")
    assert e.value.status == 409 and "may already have gone out" in e.value.detail
    assert eng.events_after(0)[-1]["title"] == "bot may have sent a WhatsApp message"
    assert (await eng.send_policy(ALICE))["sends"][0]["status"] == "unknown"
    # A definite refusal doesn't count as sent: the retry goes.
    a.fail = RuntimeError("server returned error 479")
    assert await _status(eng.send(ALICE, chat="Ana", text="second try")) == 502
    a.fail = None
    assert (await eng.send(ALICE, chat="Ana", text="second try"))["status"] == "sent"


async def test_not_connected_or_banned_sends_nothing(tmp_path):
    eng, a = await _engine(tmp_path)
    await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA]})
    await a.sink("disconnected", {})
    assert await _status(eng.send(ALICE, chat="Ana", text="x")) == 409
    await a.sink("connected", {})
    await a.sink("temporary_ban", {"expires_at": 1_790_100_000.0})
    assert await _status(eng.send(ALICE, chat="Ana", text="x")) == 409
    assert a.sent == []


async def test_policy_validation(tmp_path):
    eng, _a = await _engine(tmp_path)
    assert await _status(eng.set_send_policy(ALICE, {"daily_limit": 0})) == 422
    assert await _status(eng.set_send_policy(ALICE, {"daily_limit": 201})) == 422
    assert await _status(eng.set_send_policy(ALICE, {"signature": "x" * 121})) == 422
    p = await eng.set_send_policy(ALICE, {"signature": "  — Alice's   bot  "})
    assert p["signature"] == "— Alice's bot"
    await eng.set_send_policy(ALICE, {"enabled": True, "allowed": [ANA]})
    assert await _status(eng.send(ALICE, chat="Ana", text="   ")) == 422
    assert await _status(eng.send(ALICE, chat="Ana", text="x" * 4097)) == 422


def test_the_log_keeps_text_sealed(tmp_path):
    s = Store(tmp_path / "store.sqlite3")
    rid = s.start_send(ANA, "bot", "the secret plan", now=1.0)
    s.finish_send(rid, "sent", wa_id="3EB0")
    assert s.sends()[0]["text"] == "the secret plan"
    assert s.text_hash(ANA, "Hello  World") == s.text_hash(ANA, "hello world")
    s.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("store.sqlite3*"))
    assert b"secret plan" not in raw


async def test_service_routes(tmp_path):
    eng, a = await _engine(tmp_path)
    c = TestClient(build_app(eng, "s"))
    h = {"Authorization": "Bearer s"}
    r = c.put(f"/links/{ALICE}/send-policy", json={"enabled": True, "allowed": [ANA, STRANGER]}, headers=h).json()
    assert r["allowed"] == [ANA] and r["dropped"] == [STRANGER]
    assert c.post(f"/links/{ALICE}/send", json={"chat": "Ana", "text": "hi", "agent": "bot"}, headers=h).json()["status"] == "sent"
    assert c.post(f"/links/{ALICE}/send", json={"chat": "Obitelj", "text": "hi"}, headers=h).status_code == 403


# ── Flight Deck ─────────────────────────────────────────────────────


@pytest.fixture()
async def deck(monkeypatch, tmp_path):
    import captain_claw.flight_deck.server as srv
    from captain_claw.flight_deck import auth
    from captain_claw.flight_deck.db import FlightDeckDB
    from captain_claw.flight_deck.whatsapp_web.link_state import LinkState

    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    for var in ("FD_WHATSAPP_WEB", "FD_WHATSAPP_WEB_SEND", "FD_LOCKDOWN", "FD_AGENT_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    d = FlightDeckDB(str(tmp_path / "fd.db"))
    await d.init()
    prev = auth._db
    auth.set_auth_db(d)
    await d._db.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", (ALICE, "a@x.co", "h", "Alice", "user", "t", "t"))
    await d._db.commit()
    await sup.save_config(d, allowed=True)
    monkeypatch.setattr(srv, "_find_agent_by_auth",
                        lambda t: (True, ALICE, "alice-bot") if t == "tok" else (False, "", ""))
    monkeypatch.setattr(srv, "_resolve_agent_identity_by_auth",
                        lambda t: (True, ALICE) if t == "tok" else (False, ""))
    root = tmp_path / "waw"
    (root / ALICE).mkdir(parents=True)
    LinkState(state="connected", jid="385976707736@s.whatsapp.net", linked_at="t").save(root / ALICE / "state.json")

    class Svc:
        calls: list = []

        def user_has_link(self, uid):
            return (root / uid / "state.json").exists()

        def offline_view(self, uid):
            return sup.Supervisor.offline_view(type("S", (), {"root": root})(), uid)

        async def request(self, method, path, body=None, **kw):
            Svc.calls.append((method, path, body, kw))
            if path.endswith("/send"):
                return {"status": "sent", "id": "3EB0", "chat": "Ana", "text": body["text"], "signed": False}
            if path.endswith("/chats"):
                return {"chats": [{"jid": ANA, "title": "Ana", "kind": "direct", "number": "+385911111111",
                                   "messages": 3, "last_ts": 1, "pinned": False}]}
            return {"enabled": True, "allowed": [ANA], "daily_limit": 30, "signature": "—", "signature_on": True,
                    "sends": [], "sent_today": 0}

    monkeypatch.setattr(sup, "get", lambda: Svc())
    monkeypatch.setattr(sup, "installed", lambda: True)

    async def no_agents(db, owner):  # the auto-reply picker; never lists Docker here
        return []

    monkeypatch.setattr(routes.writer, "list_eligible_agents", no_agents)
    app = FastAPI()
    app.include_router(routes.router)
    try:
        yield Svc, TestClient(app, client=("127.0.0.1", 50123))
    finally:
        auth.set_auth_db(prev)
        await d.close()


def test_agent_send_route(deck, monkeypatch):
    svc, c = deck
    r = c.post("/fd/whatsapp-web/agent/send", json={"chat": "Ana", "text": "hi"}, headers={"X-Agent-Auth": "tok"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    method, path, body, kw = svc.calls[-1]
    assert path == f"/links/{ALICE}/send" and body["agent"] == "alice-bot" and kw["timeout"] >= 60
    assert c.post("/fd/whatsapp-web/agent/send", json={"chat": "Ana", "text": "hi"},
                  headers={"X-Agent-Auth": "nope"}).status_code == 403
    monkeypatch.setenv("FD_WHATSAPP_WEB_SEND", "off")
    n = len(svc.calls)
    r = c.post("/fd/whatsapp-web/agent/send", json={"chat": "Ana", "text": "hi"}, headers={"X-Agent-Auth": "tok"})
    assert r.status_code == 403 and "FD_WHATSAPP_WEB_SEND" in r.json()["detail"] and len(svc.calls) == n


def test_user_sending_settings(deck, monkeypatch):
    from captain_claw.flight_deck import auth

    svc, c = deck
    h = {"Authorization": f"Bearer {auth.create_access_token(ALICE, 'user')}"}
    v = c.get("/fd/whatsapp-web/sending", headers=h).json()
    assert v["available"] and v["allowed"] == [ANA] and v["chats"][0]["title"] == "Ana" and v["kill_switch"] is False
    c.put("/fd/whatsapp-web/sending", json={"enabled": False}, headers=h)
    put = [call for call in svc.calls if call[0] == "PUT"][-1]
    assert put[1] == f"/links/{ALICE}/send-policy" and put[2]["enabled"] is False
    monkeypatch.setenv("FD_WHATSAPP_WEB_SEND", "off")
    assert c.get("/fd/whatsapp-web/sending", headers=h).json()["kill_switch"] is True


# ── the tool and the guard ──────────────────────────────────────────


@pytest.fixture()
def fd(monkeypatch):
    monkeypatch.setattr(wwc, "_fd_base", lambda: "http://fd.test")
    monkeypatch.setattr(wwc, "_fd_headers", lambda: {"X-Agent-Auth": "tok"})
    monkeypatch.setattr(wwc, "_member_call", lambda: False)
    state = type("S", (), {"answer": (200, {}), "bodies": []})()

    def handler(r):
        state.bodies.append(json.loads(r.content))
        status, body = state.answer
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
        **kw, "transport": httpx.MockTransport(handler)}))
    return state


async def test_tool_send(fd):
    fd.answer = (200, {"status": "sent", "id": "3EB0X", "chat": "Ana", "text": f"hi\n\n{DEFAULT_SIGNATURE}",
                       "signed": True})
    out = await WhatsAppWebTool().execute(action="send", chat="Ana", text="hi")
    assert out.success and out.content.splitlines()[0] == "Sent to Ana on WhatsApp (message id 3EB0X)."
    assert DEFAULT_SIGNATURE in out.content and fd.bodies[-1] == {"chat": "Ana", "text": "hi", "allow_repeat": False}
    fd.answer = (403, {"detail": "Ana isn't on the user's sending whitelist"})
    out = await WhatsAppWebTool().execute(action="send", chat="Ana", text="hi", allow_repeat="true")
    assert not out.success and "whitelist" in out.error and fd.bodies[-1]["allow_repeat"] is True
    assert not (await WhatsAppWebTool().execute(action="send", chat="Ana")).success


def _verdict(reply: str, steps=()):
    facts = fg.TurnFacts(request="tell Ana", turn_steps=tuple(steps), session_steps=tuple(steps),
                         whatsapp_tools=fg.WHATSAPP_TOOLS)
    return fg.judge_reply(reply, reply, facts)


def test_the_guard_turns_down_a_send_nobody_made():
    sent = fg.ToolStep(name="whatsapp_web", action="send",
                       content="Sent to Ana on WhatsApp (message id 3EB0).\nText as sent:\nhi")
    for false in ("I messaged Ana on WhatsApp that you'll be late.", "Sent to Ana on WhatsApp.",
                  "Poslao sam Ani poruku na WhatsApp.", "Your reply to Ana has been sent on WhatsApp."):
        v = _verdict(false)
        assert v.kinds == (fg.WHATSAPP_CLAIM,), false
        assert "whatsapp_web" in v.corrective and v.backstop == fg.WHATSAPP_BACKSTOP
        assert _verdict(false, (sent,)).kinds == (), false
    for fine in ("I sent you the report on WhatsApp.", "I've sent the summary here on WhatsApp.",
                 "Ana sent you a WhatsApp message at 9.", "I haven't sent anything on WhatsApp.",
                 "Should I message Ana on WhatsApp?", "I messaged Ana."):
        assert _verdict(fine).kinds == (), fine
    # Not judged when the turn doesn't offer the tool, or a file went to the user's chat.
    facts = fg.TurnFacts(request="x", whatsapp_tools=frozenset())
    assert fg.judge_reply("I messaged Ana on WhatsApp.", "I messaged Ana on WhatsApp.", facts).kinds == ()
    delivered = fg.ToolStep(name="whatsapp_send_file", content="Sent report.pdf")
    assert _verdict("I sent the report to Ana on WhatsApp.", (delivered,)).kinds == ()
