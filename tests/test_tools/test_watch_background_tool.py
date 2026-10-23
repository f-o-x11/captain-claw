"""The agent-side ``watch_background`` tool — "make this my watch background".

The picture is found the way the read tool finds files and goes to Flight
Deck's agent route as base64 with the agent's own identity headers; only
pictures go, and ``clear`` sends no picture at all.

No FD, no network: httpx is routed to a MockTransport that records requests.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from captain_claw import fd_client
from captain_claw.tools import watch_background as wb
from captain_claw.tools.watch_background import WatchBackgroundTool

FD = "http://fd.test"


@pytest.fixture()
def fd(monkeypatch, tmp_path):
    seen: list[httpx.Request] = []
    answer = {"status": 200, "body": {"ok": True, "id": "abc", "sent_to": 1}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(answer["status"], json=answer["body"])

    real = httpx.AsyncClient
    monkeypatch.setattr(wb.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(fd_client, "flight_deck_base", lambda: FD)
    monkeypatch.setattr(fd_client, "flight_deck_headers",
                        lambda: {"X-Agent-Auth": "my-web-auth", "X-Agent-Secret": "s"})
    saved = tmp_path / "saved"
    (saved / "downloads" / "sess-1").mkdir(parents=True)
    photo = saved / "downloads" / "sess-1" / "beach.jpg"
    photo.write_bytes(b"\xff\xd8 a picture")
    (saved / "downloads" / "sess-1" / "notes.txt").write_text("not a picture")
    runtime = {"_saved_base_path": str(saved), "_runtime_base_path": str(tmp_path)}
    return seen, answer, runtime, photo


def test_set_sends_the_picture_to_flight_deck(fd):
    seen, _, runtime, photo = fd
    res = asyncio.run(WatchBackgroundTool().execute(action="set", path="beach.jpg", **runtime))
    assert res.success, res.error
    assert "watch background now" in res.content and "on the watch already" in res.content
    req = seen[-1]
    assert str(req.url) == f"{FD}/fd/device/agent/background"
    assert req.headers["X-Agent-Auth"] == "my-web-auth"
    body = json.loads(req.content)
    assert body["filename"] == "beach.jpg"
    assert base64.b64decode(body["image_b64"]) == photo.read_bytes()


def test_a_watch_that_is_away_gets_it_on_its_next_connect(fd):
    seen, answer, runtime, _ = fd
    answer["body"] = {"ok": True, "id": "abc", "sent_to": 0}
    res = asyncio.run(WatchBackgroundTool().execute(action="set", path="beach.jpg", **runtime))
    assert res.success and "when it next connects" in res.content and "0.13.0" in res.content


def test_clear_sends_no_picture(fd):
    seen, _, runtime, _ = fd
    res = asyncio.run(WatchBackgroundTool().execute(action="clear", **runtime))
    assert res.success and "black" in res.content
    assert json.loads(seen[-1].content) == {"clear": True}


def test_only_pictures_that_exist_are_sent(fd):
    seen, _, runtime, _ = fd
    res = asyncio.run(WatchBackgroundTool().execute(action="set", path="notes.txt", **runtime))
    assert not res.success and "isn't a picture" in res.error
    res = asyncio.run(WatchBackgroundTool().execute(action="set", path="nope.png", **runtime))
    assert not res.success and "No file" in res.error
    res = asyncio.run(WatchBackgroundTool().execute(action="set", **runtime))
    assert not res.success and "path" in res.error
    assert seen == []


def test_flight_deck_refusals_reach_the_agent(fd):
    seen, answer, runtime, _ = fd
    answer.update(status=400, body={"detail": "beach.jpg: not a picture Flight Deck can read"})
    res = asyncio.run(WatchBackgroundTool().execute(action="set", path="beach.jpg", **runtime))
    assert not res.success and "can read" in res.error


def test_without_flight_deck_it_says_so(monkeypatch):
    monkeypatch.setattr(fd_client, "flight_deck_base", lambda: "")
    monkeypatch.setattr(fd_client, "flight_deck_headers", lambda: {})
    res = asyncio.run(WatchBackgroundTool().execute(action="clear"))
    assert not res.success and "Flight Deck" in res.error
