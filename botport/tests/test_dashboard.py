"""The dashboard's data: /api/overview, live events with routes, static files."""

from __future__ import annotations

import asyncio
import json
import uuid

import aiohttp

RESEARCHER = {"name": "Researcher", "slug": "researcher", "description": "Finds things",
              "expertise_tags": ["research"], "status": "running"}


async def _events(ws: aiohttp.ClientWebSocketResponse, until: str, concern_id: str, timeout: float = 5.0) -> list[dict]:
    """Dashboard events up to and including the first ``until`` for ``concern_id``."""
    seen: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        msg = await ws.receive(timeout=max(0.05, deadline - loop.time()))
        assert msg.type == aiohttp.WSMsgType.TEXT, msg
        ev = json.loads(msg.data)
        seen.append(ev)
        if ev.get("type") == until and ev.get("concern_id") == concern_id:
            return seen


async def test_overview_and_live_events_carry_the_route(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1", [])
    b = await hub.deck("pi", [RESEARCHER])
    async with hub.session.ws_connect(hub.url("/ws/dashboard")) as dash:
        cid = uuid.uuid4().hex
        await a.send({"type": "concern", "concern_id": cid, "task": "find X", "target_deck": "pi",
                      "target_agent": "researcher", "from_agent": "Planner", "timeout_seconds": 60})
        created = (await _events(dash, "concern_created", cid))[-1]
        assert created["from"] == "deck:vps1" and created["to"] == "deck:pi"
        assert created["from_name"] == "vps1" and created["to_name"] == "Researcher@pi"
        assert created["from_agent"] == "Planner" and created["mode"] == "consult"

        await b.expect("dispatch", concern_id=cid)
        await b.send({"type": "progress", "concern_id": cid, "event": "tool", "data": {"tool_name": "web_fetch"}})
        prog = (await _events(dash, "concern_progress", cid))[-1]
        assert prog["text"] == "using web_fetch" and prog["to"] == "deck:pi"

        async with hub.session.get(hub.url("/api/overview")) as r:
            ov = await r.json()
        assert ov["hub"]["protocol"] == 2 and ov["now"]
        assert {i["name"] for i in ov["instances"]} == {"vps1", "pi"}
        [running] = ov["active"]
        assert running["id"] == cid and running["live"]["text"] == "using web_fetch"
        assert running["to_name"] == "Researcher@pi" and "messages" in running
        assert all(c["id"] != cid for c in ov["recent"]), "a running concern is listed once"

        await b.send({"type": "result", "concern_id": cid, "ok": True, "response": "X is 42"})
        done = (await _events(dash, "concern_result", cid))[-1]
        assert done["status"] == "closed" and done["from"] == "deck:vps1" and done["answered"] is True

    async with hub.session.get(hub.url("/api/overview")) as r:
        ov = await r.json()
    assert not ov["active"] and ov["recent"][0]["id"] == cid
    assert ov["recent"][0]["end_reason"] == "completed"
    # The store's list carries no message bodies; the counts come from one query.
    assert ov["recent"][0]["answered"] is True and ov["recent"][0]["messages"] >= 2
    assert cid not in hub.server.live_progress, "progress bookkeeping is dropped with the concern"
    hub.server.live_progress["gone"] = {"text": "stale"}
    await hub.server.maintenance_tick()
    assert "gone" not in hub.server.live_progress, "maintenance sweeps what no running concern owns"


async def test_a_failure_keeps_its_reason_for_the_dashboard(make_hub):
    hub = await make_hub()
    a = await hub.deck("vps1", [])
    b = await hub.deck("pi", [RESEARCHER])
    cid = uuid.uuid4().hex
    await a.send({"type": "concern", "concern_id": cid, "task": "find X", "target_deck": "pi",
                  "target_agent": "researcher", "timeout_seconds": 60})
    await b.expect("dispatch", concern_id=cid)
    await b.send({"type": "result", "concern_id": cid, "ok": False, "error": "Researcher failed: no network"})
    await a.expect("concern_result", concern_id=cid)
    async with hub.session.get(hub.url(f"/api/concerns/{cid}")) as r:
        detail = await r.json()
    assert detail["status"] == "failed" and detail["metadata"]["error"] == "Researcher failed: no network"
    async with hub.session.get(hub.url("/api/overview?limit=5")) as r:
        [row] = (await r.json())["recent"]
    assert row["error"] == "Researcher failed: no network"


async def test_only_the_dashboard_files_are_served(make_hub):
    hub = await make_hub()
    for name, ctype in [("app.css", "text/css"), ("dashboard.js", "application/javascript"),
                        ("auth.js", "application/javascript"), ("icon.svg", "image/svg+xml")]:
        async with hub.session.get(hub.url("/" + name)) as r:
            assert r.status == 200 and r.headers["Content-Type"].startswith(ctype), name
            assert r.headers.get("Cache-Control") == "no-cache"
    for path in ("/routes.js", "/secret.js", "/..%2Fconfig.js", "/index.html.js"):
        async with hub.session.get(hub.url(path)) as r:
            assert r.status == 404, path
    async with hub.session.get(hub.url("/api/overview?limit=abc")) as r:
        assert r.status == 200  # a bad limit falls back to the default
