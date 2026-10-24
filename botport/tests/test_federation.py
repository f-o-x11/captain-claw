"""Deck federation: Flight Deck A -> BotPort -> Flight Deck B, end to end over real sockets."""

from __future__ import annotations

import asyncio
import time
import uuid

RESEARCHER = {"name": "Researcher", "slug": "researcher", "description": "Finds things",
              "expertise_tags": ["research", "web"], "status": "running"}
CODER = {"name": "Coder", "slug": "coder", "description": "Writes code",
         "expertise_tags": ["python"], "status": "running"}


def _cid() -> str:
    return uuid.uuid4().hex


async def _consult(a, cid: str, **fields):
    await a.send({"type": "concern", "concern_id": cid, "task": fields.pop("task", "find X"),
                  "target_deck": fields.pop("target_deck", "vps2"),
                  "target_agent": fields.pop("target_agent", "researcher"),
                  "from_agent": fields.pop("from_agent", "Planner"), "timeout_seconds": 120, **fields})


async def test_roster_lists_other_decks(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1", [CODER])
    b = await hub.deck("vps2", [RESEARCHER])
    net = await a.expect("network")
    while not any(d["name"] == "vps2" for d in net["decks"]):
        net = await a.expect("network")
    vps2 = next(d for d in net["decks"] if d["name"] == "vps2")
    assert [x["name"] for x in vps2["agents"]] == ["Researcher"]
    assert all(d["name"] != "vps1" for d in net["decks"]), "a deck is not listed to itself"

    await b.send({"type": "deck_agents", "agents": [RESEARCHER, CODER]})
    while True:
        net = await a.expect("network")
        vps2 = next((d for d in net["decks"] if d["name"] == "vps2"), None)
        if vps2 and len(vps2["agents"]) == 2:
            break


async def test_consult_roundtrip_with_progress_and_acks(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1", [CODER])
    b = await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid)

    ack = await a.expect("concern_ack", concern_id=cid)
    assert ack["ok"] and ack["target_deck"] == "vps2" and ack["target_agent"] == "Researcher"

    d = await b.expect("dispatch", concern_id=cid)
    assert d["from_deck"] == "vps1" and d["from_agent"] == "Planner"
    assert d["target_agent"] == "researcher" and d["task"] == "find X"
    assert d["timeout_seconds"] == 120

    await b.send({"type": "progress", "concern_id": cid, "event": "accepted", "data": {}})
    await b.send({"type": "progress", "concern_id": cid, "event": "status", "data": {"text": "searching"}})
    p = await a.expect("progress", concern_id=cid)
    assert p["event"] in ("accepted", "status")

    await b.send({"type": "result", "concern_id": cid, "ok": True, "response": "X is 42"})
    await b.expect("result_ack", concern_id=cid)
    res = await a.expect("concern_result", concern_id=cid)
    assert res["ok"] and res["response"] == "X is 42"
    assert res["from_instance_name"] == "Researcher@vps2"
    assert hub.server._outbox.get("deck:vps1", {}).get(cid) is not None
    await a.send({"type": "result_ack", "concern_id": cid})
    await asyncio.sleep(0.1)
    assert cid not in hub.server._outbox.get("deck:vps1", {})
    concern = hub.server.concerns.get_concern(cid)
    assert concern.status == "closed"
    assert hub.server.connections.get_instance("deck:vps2").active_concerns == 0


async def test_unknown_agent_and_offline_deck_fail_fast(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid, target_agent="nobody")
    ack = await a.expect("concern_ack", concern_id=cid)
    assert not ack["ok"] and "Researcher" in ack["error"]

    cid2 = _cid()
    await _consult(a, cid2, target_deck="ghost")
    ack = await a.expect("concern_ack", concern_id=cid2)
    assert not ack["ok"] and "not connected" in ack["error"]


async def test_agent_without_deck_resolves_when_unambiguous(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    c = await hub.deck("pi", [CODER])
    cid = _cid()
    await _consult(a, cid, target_deck="", target_agent="Coder")
    assert (await a.expect("concern_ack", concern_id=cid))["ok"]
    await c.expect("dispatch", concern_id=cid)
    await b.nothing("dispatch")

    await hub.deck("pi2", [RESEARCHER])
    cid2 = _cid()
    await _consult(a, cid2, target_deck="", target_agent="researcher")
    ack = await a.expect("concern_ack", concern_id=cid2)
    assert not ack["ok"] and "ambiguous" in ack["error"]


async def test_only_the_assignee_can_answer_and_only_the_originator_can_close(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    c = await hub.deck("pi")
    cid = _cid()
    await _consult(a, cid)
    await b.expect("dispatch", concern_id=cid)

    await c.send({"type": "result", "concern_id": cid, "ok": True, "response": "forged"})
    await c.send({"type": "close_concern", "concern_id": cid})
    await a.nothing("concern_result")
    await b.nothing("concern_closed")
    assert hub.server.concerns.get_concern(cid).is_active

    await a.send({"type": "close_concern", "concern_id": cid})
    closed = await b.expect("concern_closed", concern_id=cid)
    assert closed["reason"] == "closed_by_originator"
    assert hub.server.connections.get_instance("deck:vps2").active_concerns == 0


async def test_resubmission_is_idempotent_and_ids_cannot_be_hijacked(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    c = await hub.deck("pi")
    cid = _cid()
    await _consult(a, cid)
    await a.expect("concern_ack", concern_id=cid)
    await b.expect("dispatch", concern_id=cid)

    await _consult(a, cid)
    assert (await a.expect("concern_ack", concern_id=cid))["ok"]
    await b.nothing("dispatch")

    await _consult(c, cid, task="hijack")
    ack = await c.expect("concern_ack", concern_id=cid)
    assert not ack["ok"] and "in use" in ack["error"]


async def test_result_waits_for_an_offline_requester(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid)
    await a.expect("concern_ack", concern_id=cid)
    await b.expect("dispatch", concern_id=cid)

    await a.close()  # requester's link drops while the work runs
    await asyncio.sleep(0.1)
    await b.send({"type": "result", "concern_id": cid, "ok": True, "response": "done late"})
    await b.expect("result_ack", concern_id=cid)

    a2 = await hub.deck("vps1", awaiting=[cid])
    assert a2.registered["resumed"] is True
    res = await a2.expect("concern_result", concern_id=cid)
    assert res["response"] == "done late"


async def test_assignee_blip_keeps_work_alive_but_restart_fails_it(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid)
    await b.expect("dispatch", concern_id=cid)
    await b.send({"type": "progress", "concern_id": cid, "event": "accepted", "data": {}})
    await a.expect("progress", concern_id=cid)

    await b.close()
    await asyncio.sleep(0.1)
    concern = hub.server.concerns.get_concern(cid)
    assert concern.is_active and concern.assignee_offline_since > 0

    b2 = await hub.deck("vps2", [RESEARCHER], in_flight=[cid])
    await b2.send({"type": "result", "concern_id": cid, "ok": True, "response": "survived"})
    assert (await a.expect("concern_result", concern_id=cid))["response"] == "survived"

    cid2 = _cid()
    await _consult(a, cid2)
    await b2.expect("dispatch", concern_id=cid2)
    await b2.send({"type": "progress", "concern_id": cid2, "event": "accepted", "data": {}})
    await b2.close()
    await asyncio.sleep(0.1)
    await hub.deck("vps2", [RESEARCHER], in_flight=[])  # restarted: lost it
    res = await a.expect("concern_result", concern_id=cid2)
    assert not res["ok"] and "restarted" in res["error"]


async def test_lost_dispatch_is_resent_after_reconnect(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    await b.close()
    await asyncio.sleep(0.1)
    # The deck is offline but still in the roster: explicit routing refuses.
    cid = _cid()
    await _consult(a, cid)
    ack = await a.expect("concern_ack", concern_id=cid)
    assert not ack["ok"]

    b = await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid)
    await b.expect("dispatch", concern_id=cid)
    # Never acknowledged (no progress) and the socket dies: on reconnect the
    # hub sends the dispatch again rather than failing it.
    await b.close()
    await asyncio.sleep(0.1)
    b2 = await hub.deck("vps2", [RESEARCHER], in_flight=[])
    again = await b2.expect("dispatch", concern_id=cid)
    assert again["task"] == "find X"


async def test_grace_expiry_and_deadline_notify_both_sides(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    b = await hub.deck("vps2", [RESEARCHER])
    cid = _cid()
    await _consult(a, cid)
    await b.expect("dispatch", concern_id=cid)
    await b.send({"type": "progress", "concern_id": cid, "event": "accepted", "data": {}})
    await b.close()
    await asyncio.sleep(0.1)
    hub.server.concerns.get_concern(cid).assignee_offline_since = time.monotonic() - 10_000
    await hub.server.concerns._check_timeouts()
    res = await a.expect("concern_result", concern_id=cid)
    assert not res["ok"] and "did not reconnect" in res["error"]

    b2 = await hub.deck("vps2", [RESEARCHER])
    cid2 = _cid()
    await _consult(a, cid2)
    await b2.expect("dispatch", concern_id=cid2)
    hub.server.concerns.get_concern(cid2).deadline_at = time.monotonic() - 1
    await hub.server.concerns._check_timeouts()
    res = await a.expect("concern_result", concern_id=cid2)
    assert not res["ok"] and "in time" in res["error"]
    closed = await b2.expect("concern_closed", concern_id=cid2)
    assert closed["reason"] == "deadline"
    assert hub.server.connections.get_instance("deck:vps2").active_concerns == 0


async def test_supersede_and_name_collision(make_hub):
    hub = await make_hub()
    first = await hub.deck("vps1", deck_id="same")
    second = await hub.deck("vps1", deck_id="same")
    closed = await first.expect("_closed")
    assert closed["code"] == 4000
    assert second.registered["resumed"] is True
    assert hub.server.connections.is_current("deck:vps1", hub.server.connections.get_ws("deck:vps1"))

    intruder = hub.peer()
    reg = await intruder.connect(kind="deck", protocol=2, instance_name="vps1", deck_id="other",
                                 capabilities={"agents": []})
    assert not reg["ok"] and reg["retry"] is True
    # The live one is untouched.
    assert hub.server.connections.get_instance("deck:vps1").status == "connected"


async def test_auth_binds_name_and_rejects_bad_secret(make_hub):
    hub = await make_hub(auth=[{"instance": "vps1", "key": "k1", "secret": "s1-long-enough-secret"}])
    bad = hub.peer()
    reg = await bad.connect(kind="deck", protocol=2, instance_name="vps1", key="k1", secret="nope")
    assert not reg["ok"] and reg["retry"] is False

    good = hub.peer()
    reg = await good.connect(kind="deck", protocol=2, instance_name="pretend-other", deck_id="x",
                             key="k1", secret="s1-long-enough-secret", capabilities={"agents": []})
    assert reg["ok"] and reg["deck_name"] == "vps1"
    assert hub.server.connections.get_instance("deck:vps1") is not None


async def test_invalid_deck_name_is_refused(make_hub):
    hub = await make_hub()
    p = hub.peer()
    reg = await p.connect(kind="deck", protocol=2, instance_name="bad name@x", capabilities={})
    assert not reg["ok"] and reg["retry"] is False


async def test_legacy_agent_interop(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1")
    agent = hub.peer()
    reg = await agent.connect(instance_name="old-claw", capabilities={
        "personas": [{"name": "Lawyer", "expertise_tags": ["legal"]}], "tools": [], "max_concurrent": 2})
    assert reg["ok"] and reg["instance_id"]
    await asyncio.sleep(0.4)
    net = await a.expect("network")
    while not net["agents"]:
        net = await a.expect("network")
    assert net["agents"][0]["name"] == "old-claw"

    cid = _cid()
    await _consult(a, cid, target_deck="", target_agent="old-claw")
    assert (await a.expect("concern_ack", concern_id=cid))["ok"]
    d = await agent.expect("dispatch", concern_id=cid)
    assert d["from_instance_name"] == "vps1"
    await agent.send({"type": "result", "concern_id": cid, "ok": True, "response": "legal opinion"})
    assert (await a.expect("concern_result", concern_id=cid))["response"] == "legal opinion"

    # A legacy agent's own concern can reach a deck agent by tags.
    b = await hub.deck("vps2", [RESEARCHER])
    await agent.send({"type": "concern", "task": "research the web", "expertise_tags": ["research"]})
    d = await b.expect("dispatch")
    assert d["target_agent"] == "researcher" and d["from_instance_name"] == "old-claw"


async def test_unknown_concern_progress_tells_the_deck_to_stop(make_hub):
    hub = await make_hub()
    b = await hub.deck("vps2", [RESEARCHER])
    await b.send({"type": "progress", "concern_id": "deadbeefdeadbeef", "event": "status", "data": {}})
    closed = await b.expect("concern_closed", concern_id="deadbeefdeadbeef")
    assert closed["reason"] == "unknown_concern"


async def test_hub_restart_tells_the_requester_its_concern_is_gone(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1", awaiting=["0123456789abcdef"])
    res = await a.expect("concern_result", concern_id="0123456789abcdef")
    assert not res["ok"] and "no longer knows" in res["error"]
