"""Where a user message came from (turn_source) and the agent's reasoning
effort per source: the source each entry point records, how the settings
file is read, which level a turn gets, and Flight Deck's settings route."""

from __future__ import annotations

import json
import types
from pathlib import Path

import httpx
import pytest
import yaml

from captain_claw import mail_authority, msg_origin, turn_source
from captain_claw.agent import Agent
from captain_claw.agent_guard_mixin import AgentGuardMixin
from captain_claw.llm import reasoning
from captain_claw.session import Session


# ── sources ──────────────────────────────────────────────────────────


def test_normalize_keeps_known_channels_and_kinds():
    assert turn_source.normalize("Device:StickS3") == "device:sticks3"
    assert turn_source.normalize("whatsapp") == "whatsapp"
    assert turn_source.normalize("device:../etc") == "device"
    assert turn_source.normalize("carrier-pigeon") == ""


def test_labels():
    assert turn_source.label_for("device:sticks3") == "M5Stack StickS3"
    assert turn_source.label_for("device:sticks3", "M5Stack StickS3 (desk)") == "M5Stack StickS3 (desk)"
    assert turn_source.label_for("device:unknownkind") == "Device"
    assert turn_source.label_for("scheduled") == "Scheduled tasks"


@pytest.mark.parametrize("kw,expected", [
    ({}, "web"),
    ({"surface": "whatsapp"}, "whatsapp"),
    ({"whatsapp": True}, "whatsapp"),
    ({"surface": "glasses", "frame_source": "app:android"}, "app:android"),
    ({"surface": "device", "frame_source": "device:papercolor"}, "device:papercolor"),
    ({"surface": "device"}, "device"),
    ({"automation_kind": "fd_scheduler", "whatsapp": True}, "scheduled"),
    ({"automation_kind": "peer_relay"}, "agent"),
    ({"public": True}, "public"),
])
def test_for_frame(kw, expected):
    assert turn_source.for_frame(**kw) == expected


def test_resolve_from_hint_automation_channel_and_origin():
    assert turn_source.resolve(hinted="device:sticks3", channel="device") == "device:sticks3"
    assert turn_source.resolve(channel="telegram") == "telegram"
    assert turn_source.resolve(channel="mcp") == "api"
    assert turn_source.resolve(origin="cron") == "scheduled"
    assert turn_source.resolve(origin_detail="flow", channel="web") == "flow"
    assert turn_source.resolve() == ""


def test_catalog_has_no_web_chat():
    keys = [s["key"] for g in turn_source.settings_catalog() for s in g["sources"]]
    assert "web" not in keys and "device:sticks3" in keys and "telegram" in keys


# ── settings ─────────────────────────────────────────────────────────


def test_clean_settings_drops_junk():
    cleaned = reasoning.clean_settings({
        "default": "HIGH", "channels": {"whatsapp": "low", "web": "max", "device:sticks3": "auto",
                                        "nope": "high", "telegram": "loud"},
    })
    assert cleaned == {"default": "high", "channels": {"whatsapp": "low", "device:sticks3": "auto"}}


def test_configured_effort_most_specific_first():
    cfg = {"default": "medium", "channels": {"device": "low", "device:papercolor": "off", "telegram": "auto"}}
    assert reasoning.configured_effort("device:papercolor", cfg) == "off"
    assert reasoning.configured_effort("device:sticks3", cfg) == "low"
    assert reasoning.configured_effort("telegram", cfg) is None  # "auto": the model's own
    assert reasoning.configured_effort("whatsapp", cfg) == "medium"
    assert reasoning.configured_effort("", cfg) == "medium"  # unknown source: the default
    assert reasoning.configured_effort("web", cfg) is None  # the chat box decides


def test_load_settings_rereads_on_change(tmp_path, monkeypatch):
    path = tmp_path / "reasoning_effort.json"
    monkeypatch.setattr(reasoning, "SETTINGS_PATH", path)
    monkeypatch.setattr(reasoning, "_settings_cache", {"mtime": None, "value": None})
    assert reasoning.load_settings() == {"channels": {}}
    path.write_text(json.dumps({"default": "low"}))
    assert reasoning.load_settings()["default"] == "low"
    path.write_text(json.dumps({"default": "max"}))
    import os
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert reasoning.load_settings()["default"] == "max"


# ── the agent ────────────────────────────────────────────────────────


def _turn(**hint):
    """An object carrying just what _set_turn_origin and the guard read."""
    obj = types.SimpleNamespace()
    if hint:
        msg_origin.hint_turn_provenance(obj, **hint)
    Agent._set_turn_origin(obj, "hello", None, None, None)
    return obj


def test_set_turn_origin_records_source_and_label():
    obj = _turn(channel="device", source="device:sticks3", source_label="M5Stack StickS3 (desk)")
    assert obj._turn_source == ("device:sticks3", "M5Stack StickS3 (desk)")
    assert _turn(channel="telegram")._turn_source == ("telegram", "Telegram")
    assert _turn(channel="web")._turn_source == ("web", "Web chat")


def test_automated_turn_is_its_job():
    obj = types.SimpleNamespace()
    msg_origin.hint_turn_provenance(obj, channel="web")
    with mail_authority.bound(mail_authority.automated("fd_scheduler", "brief")):
        Agent._set_turn_origin(obj, "brief", None, None, None)
    assert obj._turn_source[0] == "scheduled"


def test_turn_effort_precedence(tmp_path, monkeypatch):
    path = tmp_path / "reasoning_effort.json"
    path.write_text(json.dumps({"default": "low", "channels": {"whatsapp": "high"}}))
    monkeypatch.setattr(reasoning, "SETTINGS_PATH", path)
    monkeypatch.setattr(reasoning, "_settings_cache", {"mtime": None, "value": None})

    wa, web, dev = _turn(channel="whatsapp"), _turn(channel="web"), _turn(channel="device", source="device:sticks3")
    effort = AgentGuardMixin._turn_reasoning_effort
    assert effort(wa) == "high"
    assert effort(dev) == "low"  # the default
    assert effort(web) is None  # web chat: no pick → the model's own
    with reasoning.turn_effort("max"):
        assert effort(web) == "max"  # the chat box's pick wins
        assert effort(wa) == "max"


def test_session_stores_source():
    s = Session(id="s1", name="t")
    s.add_message("user", "hi", channel="device", source="device:sticks3", source_label="M5Stack StickS3 (desk)")
    s.add_message("user", "plain")
    assert s.messages[0]["source"] == "device:sticks3" and s.messages[0]["source_label"] == "M5Stack StickS3 (desk)"
    assert "source" not in s.messages[1]


def test_user_turn_frame_carries_source():
    from captain_claw.web.chat_handler import user_turn_frame

    frame = user_turn_frame("hi", "device", "c1", "device:sticks3", "M5Stack StickS3 (desk)")
    assert frame["source"] == "device:sticks3" and frame["source_label"] == "M5Stack StickS3 (desk)"
    assert "source" not in user_turn_frame("hi", "web")


def test_device_bridge_names_the_device():
    from captain_claw.flight_deck.device_bridge import _DeviceConn as DeviceConn

    conn = types.SimpleNamespace(hw="sticks3", device_id="desk")
    assert DeviceConn.source_fields(conn) == {"source": "device:sticks3", "source_label": "M5Stack StickS3 (desk)"}
    conn = types.SimpleNamespace(hw="", device_id="stick")
    assert DeviceConn.source_fields(conn) == {"source": "device", "source_label": "Device (stick)"}


# ── Flight Deck route ────────────────────────────────────────────────


@pytest.fixture
async def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from captain_claw.flight_deck import auth as fd_auth
    from captain_claw.flight_deck import rate_limiter, server
    from captain_claw.flight_deck.auth import set_auth_db
    from captain_claw.flight_deck.db import FlightDeckDB

    prev = fd_auth._db
    db = FlightDeckDB(tmp_path / "fd.db")
    await db.init()
    set_auth_db(db)
    await db._db.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
        " VALUES ('bob', 'bob@x.co', 'h', 'bob', 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    await db._db.commit()
    data = tmp_path / "fd-data"
    data.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "PROCESS_REGISTRY_FILE", data / ".processes.json")
    monkeypatch.setattr(server, "_processes", {})
    monkeypatch.setattr(server, "AUTH_ENABLED", True)
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    monkeypatch.setenv("FD_ALLOWED_HOSTS", "fd.test")
    monkeypatch.delenv("FD_LOCKDOWN", raising=False)
    monkeypatch.setattr(rate_limiter, "_api_limiter", rate_limiter._SlidingWindow())
    server._save_process_registry({"bob-agent": {"slug": "bob-agent", "name": "Bob's", "web_port": 24101,
                                                 "web_auth": "t", "owner": "bob", "pid": None}})
    cc = data / "bob-agent" / "data" / "home-config-parent" / ".captain-claw"
    cc.mkdir(parents=True)
    cfg = yaml.dump({"model": {"provider": "anthropic", "model": "claude-opus-5-5"}})
    (data / "bob-agent" / "config.yaml").write_text(cfg)
    (cc / "config.yaml").write_text(cfg)
    try:
        yield cc
    finally:
        await db.close()
        fd_auth._db = prev


def _client():
    from captain_claw.flight_deck import server

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=("203.0.113.9", 40001)),
                             base_url="http://fd.test")


def _hdr(uid: str = "bob") -> dict:
    from captain_claw.flight_deck.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_route_reads_profile_and_writes_the_agents_file(deck):
    async with _client() as c:
        r = await c.get("/fd/agent-reasoning/process/bob-agent", headers=_hdr())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["settings"] == {"channels": {}}
        assert body["profile"]["levels"] == ["low", "medium", "high", "xhigh", "max"]
        assert body["catalog"][0]["sources"][0]["key"] == "whatsapp"

        r = await c.put("/fd/agent-reasoning/process/bob-agent", headers=_hdr(),
                        json={"default": "low", "channels": {"device:sticks3": "off", "telegram": "auto"}})
        assert r.status_code == 200, r.text
        saved = json.loads((deck / "reasoning_effort.json").read_text())
        assert saved == {"default": "low", "channels": {"device:sticks3": "off", "telegram": "auto"}}

        r = await c.put("/fd/agent-reasoning/process/bob-agent", headers=_hdr(),
                        json={"clear_default": True, "channels": {"telegram": None}})
        assert r.json()["settings"] == {"channels": {"device:sticks3": "off"}}

        assert (await c.put("/fd/agent-reasoning/process/bob-agent", headers=_hdr(),
                            json={"channels": {"web": "high"}})).status_code == 400
        assert (await c.put("/fd/agent-reasoning/process/bob-agent", headers=_hdr(),
                            json={"default": "loud"})).status_code == 400


@pytest.mark.asyncio
async def test_route_is_owner_only(deck):
    from captain_claw.flight_deck import auth as fd_auth

    db = fd_auth._db
    await db._db.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, created_at, updated_at)"
        " VALUES ('eve', 'eve@x.co', 'h', 'eve', 'user', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    await db._db.commit()
    async with _client() as c:
        r = await c.put("/fd/agent-reasoning/process/bob-agent", headers=_hdr("eve"), json={"default": "max"})
    assert r.status_code in (403, 404)
    assert not (deck / "reasoning_effort.json").exists()
