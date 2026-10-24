"""HTTP access control, config precedence and filesystem safety."""

from __future__ import annotations

import base64
import gzip

import pytest

from botport.config import BotPortConfig
from botport.swarm.file_manager import FileManager, decode_file


async def test_api_requires_admin_token_when_set(make_hub):
    hub = await make_hub(server={"admin_token": "t0ken-xyz"})
    async with hub.session.get(hub.url("/api/instances")) as r:
        assert r.status == 401
    async with hub.session.get(hub.url("/api/instances"), headers={"Authorization": "Bearer t0ken-xyz"}) as r:
        assert r.status == 200
    async with hub.session.get(hub.url("/api/status?token=t0ken-xyz")) as r:
        assert r.status == 200 and (await r.json())["protocol"] == 2
    async with hub.session.get(hub.url("/healthz")) as r:
        assert r.status == 200 and (await r.json())["ok"] is True
    async with hub.session.get(hub.url("/")) as r:
        assert r.status == 200  # the page itself carries no data
    async with hub.session.ws_connect(hub.url("/ws/dashboard?token=t0ken-xyz")) as ws:
        assert not ws.closed


async def test_cookie_token_only_for_reads(make_hub):
    hub = await make_hub(server={"admin_token": "t0ken-xyz"})
    cookies = {"botport_token": "t0ken-xyz"}
    async with hub.session.get(hub.url("/api/stats"), cookies=cookies) as r:
        assert r.status == 200
    async with hub.session.post(hub.url("/api/swarm/projects"), cookies=cookies, json={"name": "x"}) as r:
        assert r.status == 401


async def test_auth_on_without_token_means_loopback_only(make_hub):
    hub = await make_hub(auth=[{"instance": "a", "key": "k", "secret": "s" * 20}])
    async with hub.session.get(hub.url("/api/instances")) as r:
        assert r.status == 200  # the test client is loopback
    hub.server.config.server.behind_proxy = True
    async with hub.session.get(hub.url("/api/instances")) as r:
        assert r.status == 401


async def test_cors_only_for_listed_origins(make_hub):
    hub = await make_hub(server={"cors_origins": ["https://deck.example"]})
    async with hub.session.options(hub.url("/api/instances"), headers={
        "Origin": "https://deck.example", "Access-Control-Request-Method": "GET"}) as r:
        assert r.status == 204
        assert r.headers["Access-Control-Allow-Origin"] == "https://deck.example"
    async with hub.session.get(hub.url("/api/instances"), headers={"Origin": "https://evil.example"}) as r:
        assert "Access-Control-Allow-Origin" not in r.headers


async def test_swarm_file_route_cannot_escape(make_hub):
    hub = await make_hub()
    async with hub.session.get(hub.url("/api/swarm/swarms/%2F/files/etc/hostname")) as r:
        assert r.status == 404
    async with hub.session.get(hub.url("/api/swarm/swarms/..%2F..%2F..%2F/files/etc/passwd")) as r:
        assert r.status == 404


def test_file_manager_rejects_hostile_components(tmp_path):
    fm = FileManager(tmp_path / "root")
    with pytest.raises(ValueError):
        fm.store_file(swarm_id="/etc", agent_name="a", filename="x", data=b"1")
    with pytest.raises(ValueError):
        fm.store_file(swarm_id="../up", agent_name="a", filename="x", data=b"1")
    meta = fm.store_file(swarm_id="s1", agent_name="..", filename="..", data=b"1", subfolder="..")
    stored = (tmp_path / "root" / "swarm" / "s1" / meta["path"]).resolve()
    assert str(stored).startswith(str((tmp_path / "root" / "swarm" / "s1").resolve()))
    assert fm.get_file("/", "etc/hostname") is None
    assert fm.list_files("../..") == []


def test_gzip_bomb_is_bounded():
    bomb = base64.b64encode(gzip.compress(b"\0" * (2 * 1024 * 1024))).decode()
    with pytest.raises(ValueError):
        decode_file(bomb, True, max_size=1024 * 1024)
    assert decode_file(base64.b64encode(gzip.compress(b"hello")).decode(), True) == b"hello"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("server:\n  port: 1111\nauth:\n  enabled: false\n")
    monkeypatch.setenv("BOTPORT_SERVER__PORT", "2222")
    monkeypatch.setenv("BOTPORT_AUTH__ENABLED", "true")
    cfg = BotPortConfig.from_yaml(p)
    assert cfg.server.port == 2222 and cfg.auth.enabled is True
    assert cfg.source == str(p.resolve())


def test_broken_config_is_an_error_not_defaults(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("server: [unclosed\n")
    monkeypatch.setenv("BOTPORT_CONFIG", str(p))
    with pytest.raises(Exception):
        BotPortConfig.load()


def test_config_problems_and_warnings():
    cfg = BotPortConfig()
    cfg.auth.enabled = True
    assert any("keys is empty" in p for p in cfg.problems())
    cfg2 = BotPortConfig()
    assert any("DISABLED" in w for w in cfg2.warnings())


def test_cli_gen_key_and_check(tmp_path, capsys):
    from botport import cli

    assert cli.main(["gen-key", "vps1"]) == 0
    out = capsys.readouterr().out
    assert "instance: vps1" in out and "secret:" in out
    assert cli.main(["gen-key", "bad name"]) == 1

    p = tmp_path / "c.yaml"
    p.write_text("auth:\n  enabled: true\n  keys: []\n")
    assert cli.main(["check", "-c", str(p)]) == 1


def _req_from(peer: str, xff: str):
    from unittest import mock

    from aiohttp.test_utils import make_mocked_request

    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: (peer, 40000) if k == "peername" else d
    return make_mocked_request("GET", "/ws", headers={"X-Forwarded-For": xff}, transport=transport)


def test_behind_proxy_uses_the_hop_our_proxy_added():
    from botport.web_security import client_addr

    cfg = BotPortConfig()
    cfg.server.behind_proxy = True
    via_proxy = _req_from("127.0.0.1", "6.6.6.6, 203.0.113.7")
    assert client_addr(via_proxy, cfg) == "203.0.113.7"
    # Straight to the port, not through the proxy: the header is not believed.
    direct = _req_from("198.51.100.4", "203.0.113.99")
    assert client_addr(direct, cfg) == "198.51.100.4"
    cfg.server.behind_proxy = False
    assert client_addr(via_proxy, cfg) == "127.0.0.1"


async def test_failed_logins_block_only_that_key_from_that_address(make_hub):
    hub = await make_hub(auth=[
        {"instance": "vps1", "key": "k1", "secret": "s" * 20},
        {"instance": "vps2", "key": "k2", "secret": "t" * 20},
    ])
    hub.server.config.auth.max_failures_per_minute = 3
    for _ in range(3):
        p = hub.peer()
        reg = await p.connect(kind="deck", protocol=2, instance_name="vps1", key="k1", secret="wrong")
        assert not reg["ok"]
    # k1 is now blocked from this address, even with the right secret...
    p = hub.peer()
    reg = await p.connect(kind="deck", protocol=2, instance_name="vps1", key="k1", secret="s" * 20)
    assert not reg["ok"] and "Too many" in reg["error"] and reg["retry"] is True
    # ...but another deck behind the same address is not.
    ok = await hub.deck("vps2", key="k2", secret="t" * 20)
    assert ok.registered["deck_name"] == "vps2"


async def test_query_token_is_for_reads_only(make_hub):
    hub = await make_hub(server={"admin_token": "t0ken-xyz"})
    async with hub.session.post(hub.url("/api/swarm/projects?token=t0ken-xyz"), json={"name": "x"}) as r:
        assert r.status == 401
    async with hub.session.post(hub.url("/api/swarm/projects"), json={"name": "x"},
                                headers={"Authorization": "Bearer t0ken-xyz"}) as r:
        assert r.status in (200, 201)


def test_captain_claws_config_yaml_is_not_mistaken_for_ours(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOTPORT_CONFIG", raising=False)
    monkeypatch.setattr("botport.config.DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    (tmp_path / "config.yaml").write_text("model:\n  provider: openai\nweb:\n  port: 23080\n")
    cfg = BotPortConfig.load()
    assert cfg.source == "" and cfg.server.port == 23180
    (tmp_path / "botport.yaml").write_text("server:\n  port: 24000\n")
    assert BotPortConfig.load().server.port == 24000
    (tmp_path / "botport.yaml").unlink()
    (tmp_path / "config.yaml").write_text("server:\n  port: 24001\nauth:\n  enabled: false\n")
    assert BotPortConfig.load().server.port == 24001


def test_short_tags_do_not_match_everything():
    from botport.models import PersonaInfo
    from botport.registry import Registry

    assert Registry._count_fuzzy_matches(["a"], ["legal", "research"]) == 0
    assert Registry._count_fuzzy_matches(["m&a"], ["m&a / deal advisory"]) == 1
    assert Registry._count_fuzzy_matches(["valuation"], ["valuation support"]) == 1
    assert PersonaInfo.from_dict({"name": "x", "expertise_tags": "legal"}).expertise_tags == ["legal"]


async def test_decks_cannot_use_swarm_file_frames(make_hub):
    hub = await make_hub()
    d = await hub.deck("vps1")
    await d.send({"type": "file_upload", "swarm_id": "s1", "filename": "x.txt", "data": "aGk="})
    ack = await d.expect("file_upload_ack")
    assert not ack["ok"] and "Flight Decks" in ack["error"]
    agent = hub.peer()
    await agent.connect(instance_name="old-claw", capabilities={})
    await agent.send({"type": "file_upload", "swarm_id": "nope", "filename": "x.txt", "data": "aGk="})
    ack = await agent.expect("file_upload_ack")
    assert not ack["ok"] and "Unknown swarm" in ack["error"]
