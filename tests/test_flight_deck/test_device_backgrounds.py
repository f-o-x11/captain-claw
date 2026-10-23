"""The StopWatch's background picture: Flight Deck prepares it (fills the
circle, small enough for the device), keeps it per owner and device kind, and
the device bridge sends it to watches that name another one — as it is to
firmware that darkens it itself (0.13.1), darkened to 0.13.0."""

from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from captain_claw.flight_deck import device_backgrounds as bgs
from captain_claw.flight_deck import device_bridge as db


def _png(size, color=(200, 120, 40), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color if mode == "RGB" else color + (255,)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(bgs, "root", lambda: tmp_path / "device_backgrounds")
    return tmp_path


def test_a_picture_becomes_a_round_screen_background():
    wide = io.BytesIO()
    im = Image.new("RGB", (1200, 600), (0, 0, 255))
    im.paste((255, 0, 0), (450, 0, 750, 600))  # a red band in the middle
    im.save(wide, "JPEG")
    jpeg = bgs.prepare(wide.getvalue())
    out = Image.open(io.BytesIO(jpeg))
    assert out.format == "JPEG" and out.size == (bgs.SIZE, bgs.SIZE)
    assert not out.info.get("progressive")       # the device's decoder takes baseline only
    assert len(jpeg) <= bgs.MAX_BYTES
    r, g, b = out.convert("RGB").getpixel((bgs.SIZE // 2, bgs.SIZE // 2))
    assert r > 200 and b < 60                    # the middle kept (centre crop), as bright
    dark = Image.open(io.BytesIO(bgs.darken(jpeg))).convert("RGB").getpixel((bgs.SIZE // 2, bgs.SIZE // 2))
    assert 80 < dark[0] < 130                    # for 0.13.0: darkened here (0.42)


def test_transparent_and_noisy_pictures_fit_too():
    import os

    assert Image.open(io.BytesIO(bgs.prepare(_png((300, 300), mode="RGBA")))).size == (466, 466)
    noisy = io.BytesIO()
    Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)).save(noisy, "PNG")
    assert len(bgs.prepare(noisy.getvalue())) <= bgs.MAX_BYTES


def test_something_that_is_not_a_picture_is_refused():
    with pytest.raises(ValueError):
        bgs.prepare(b"%PDF-1.7 not a picture")


def test_kept_per_owner_and_kind(store):
    jpeg = bgs.prepare(_png((500, 500)))
    pid = bgs.save("u1", "stopwatch", jpeg)
    assert bgs.current("u1", "stopwatch") == (pid, jpeg)
    assert bgs.current("u2", "stopwatch") is None and bgs.current("u1", "sticks3") is None
    assert bgs.clear("u1", "stopwatch") and bgs.current("u1", "stopwatch") is None
    with pytest.raises(ValueError):
        bgs.save("../..", "stopwatch", jpeg)


# ── the device bridge ────────────────────────────────────────────────


class _WS:
    def __init__(self):
        self.sent: list = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(bytes(data))


def _watch(user="u1", hw="stopwatch", bg="", fw="0.13.1"):
    conn = db._DeviceConn(_WS(), {"id": user}, "watch", lang="", speak=False, screen=(466, 466),
                          round_screen=True, chunked=True, hw=hw, fw=fw)
    conn.bg_id = bg
    return conn


@pytest.mark.asyncio
async def test_a_watch_with_another_background_gets_the_owners(store):
    jpeg = bgs.prepare(_png((800, 800)))
    pid = bgs.save("u1", "stopwatch", jpeg)
    conn = _watch()
    assert await conn.sync_background() == 1
    head, frames = conn.ws.sent[0], conn.ws.sent[1:]
    assert head == {"type": "background", "id": pid, "size": len(jpeg)}
    assert [f[0] for f in frames] == [db.BIN_BG_PART] * (len(frames) - 1) + [db.BIN_BG]
    assert b"".join(f[1:] for f in frames) == jpeg
    assert conn.bg_id == pid
    assert await conn.sync_background() == 0  # it has it now


@pytest.mark.asyncio
async def test_firmware_that_shows_it_as_it_comes_gets_it_darkened(store):
    jpeg = bgs.prepare(_png((800, 800), color=(240, 240, 240)))
    pid = bgs.save("u1", "stopwatch", jpeg)
    old = _watch(fw="0.13.0")
    assert await old.sync_background() == 1
    head = old.ws.sent[0]
    assert head["id"] != pid and old.bg_id == head["id"]  # its own id: an update fetches it again
    sent = b"".join(f[1:] for f in old.ws.sent[1:])
    assert len(sent) == head["size"]
    assert Image.open(io.BytesIO(sent)).convert("RGB").getpixel((233, 233))[0] < 120
    assert await old.sync_background() == 0
    upgraded = _watch(fw="0.13.1", bg=head["id"])
    assert await upgraded.sync_background() == 1 and upgraded.ws.sent[0]["id"] == pid


@pytest.mark.asyncio
async def test_a_cleared_background_goes_away_and_old_firmware_gets_nothing(store):
    conn = _watch(bg="0123456789abcdef")
    assert await conn.sync_background() == 1
    assert conn.ws.sent == [{"type": "background", "id": "", "size": 0}]
    bgs.save("u1", "stopwatch", bgs.prepare(_png((300, 300))))
    old = _watch()
    old.bg_id = None  # never named one in its prefs
    assert await old.sync_background() == 0 and old.ws.sent == []


@pytest.mark.asyncio
async def test_a_new_background_reaches_the_owners_watches_only(store, monkeypatch):
    mine, theirs, stick = _watch(), _watch(user="u2"), _watch(hw="sticks3")
    monkeypatch.setattr(db, "_live", {mine, theirs, stick})
    bgs.save("u1", "stopwatch", bgs.prepare(_png((300, 300))))
    assert await db.push_background("u1", "stopwatch") == 1
    assert mine.ws.sent and not theirs.ws.sent and not stick.ws.sent


@pytest.mark.asyncio
async def test_the_watch_names_its_background_in_prefs(store, monkeypatch):
    from captain_claw.flight_deck import device_firmware

    bgs.save("u1", "stopwatch", bgs.prepare(_png((300, 300))))

    class _Socket(_WS):
        query_params = {"token": "t", "device": "watch-1", "hw": "stopwatch", "fw": "0.13.0"}

        def __init__(self):
            super().__init__()
            self.inbox = [{"type": "websocket.receive", "text": json.dumps({"type": "prefs", "bg": ""})}]

        async def accept(self):
            pass

        async def receive(self):
            await asyncio.sleep(0.05)
            return self.inbox.pop(0) if self.inbox else {"type": "websocket.disconnect", "code": 1000}

    async def _user(token):
        return {"id": "u1"}

    monkeypatch.setattr(db, "_device_user", _user)
    monkeypatch.setattr(db, "_pick_agent", lambda user_id, selector: None)
    monkeypatch.setattr(device_firmware, "offer_for", lambda hw, fw: None)
    sock = _Socket()
    await db.device_ws(sock)
    kinds = [m["type"] if isinstance(m, dict) else m[0] for m in sock.sent]
    assert "background" in kinds and kinds[-1] == db.BIN_BG


# ── the agent's route ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_agent_route_keeps_and_sends_the_picture(store, monkeypatch):
    from fastapi import HTTPException

    pushed: list = []

    async def _push(user, hw):
        pushed.append((user, hw))
        return 1

    monkeypatch.setattr(bgs, "_owner", lambda request: "u1")
    monkeypatch.setattr(db, "push_background", _push)
    body = bgs.BackgroundRequest(image_b64=base64.b64encode(_png((640, 480))).decode(), filename="a.png")
    out = await bgs.agent_background(body, request=None)
    assert out["ok"] and out["sent_to"] == 1 and bgs.current("u1", "stopwatch")[0] == out["id"]
    out = await bgs.agent_background(bgs.BackgroundRequest(clear=True), request=None)
    assert out["cleared"] and bgs.current("u1", "stopwatch") is None
    assert pushed == [("u1", "stopwatch"), ("u1", "stopwatch")]
    for bad in (bgs.BackgroundRequest(image_b64="not base64!"),
                bgs.BackgroundRequest(image_b64=base64.b64encode(b"plain text").decode(), filename="x.txt")):
        with pytest.raises(HTTPException) as exc:
            await bgs.agent_background(bad, request=None)
        assert exc.value.status_code == 400
