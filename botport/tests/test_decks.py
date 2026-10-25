"""Flight Decks added, named, rotated and removed from the dashboard: each
change applies at once, without restarting the hub."""

from __future__ import annotations

import hashlib
import sqlite3

from botport.config import BotPortConfig

FILE_KEY = {"instance": "vps1", "key": "vps1-key", "secret": "s" * 32}
RESEARCHER = {"name": "Researcher", "slug": "researcher", "description": "Finds things",
              "expertise_tags": ["research"], "status": "running"}


async def _json(resp):
    return resp.status, await resp.json()


async def _add(hub, **body):
    async with hub.session.post(hub.url("/api/decks"), json=body) as r:
        return await _json(r)


async def _connect(hub, creds, name="whatever", **extra):
    """A deck registering with these credentials; returns (peer, registered)."""
    p = hub.peer()
    reg = await p.connect(
        kind="deck", protocol=2, instance_name=name, deck_id=extra.pop("deck_id", "id-new"),
        key=creds["key"], secret=creds["secret"],
        capabilities={"agents": extra.pop("agents", []), "max_concurrent": 4, "version": "test"},
    )
    return p, reg


async def test_a_deck_added_on_the_dashboard_connects_without_a_restart(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    old = await hub.deck("vps1", [], key=FILE_KEY["key"], secret=FILE_KEY["secret"])
    status, body = await _add(hub, name="pi", label="Kitchen Pi", description="Home automation\nand notes")
    assert status == 201, body
    creds = body["credentials"]
    assert creds["deck_name"] == "pi" and creds["key"].startswith("pi-") and len(creds["secret"]) >= 40
    assert body["deck"]["managed"] and body["deck"]["status"] == "never"

    pi, reg = await _connect(hub, creds, agents=[RESEARCHER])
    # The key decides the name, whatever the deck asked for.
    assert reg["ok"] and reg["deck_name"] == "pi", reg

    roster = await old.expect("network")
    while not any(d["name"] == "pi" for d in roster["decks"]):
        roster = await old.expect("network")
    [deck] = [d for d in roster["decks"] if d["name"] == "pi"]
    assert deck["label"] == "Kitchen Pi" and deck["description"] == "Home automation\nand notes"

    async with hub.session.get(hub.url("/api/overview")) as r:
        ov = await r.json()
    [inst] = [i for i in ov["instances"] if i["name"] == "pi"]
    assert inst["label"] == "Kitchen Pi"

    async with hub.session.get(hub.url("/api/decks")) as r:
        listing = await r.json()
    assert listing["auth_enabled"] is True
    views = {d["name"]: d for d in listing["decks"]}
    assert views["pi"]["status"] == "online" and views["pi"]["via"] == "dashboard"
    assert views["pi"]["agents"] == [{"name": "Researcher", "available": True}]
    assert views["vps1"]["in_file"] and not views["vps1"]["managed"] and views["vps1"]["via"] == "file"
    assert views["vps1"]["key"] == "vps1-key" and "secret" not in views["vps1"]

    # Only a digest is kept: the database holds nothing a deck could connect with.
    db = sqlite3.connect(hub.server.config.data_dir / "botport.db")
    row = db.execute("SELECT key, secret_hash FROM decks WHERE name = 'pi'").fetchone()
    db.close()
    assert row == (creds["key"], hashlib.sha256(creds["secret"].encode()).hexdigest())
    assert creds["secret"].encode() not in (hub.server.config.data_dir / "botport.db").read_bytes()


async def test_label_edits_reach_every_deck(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    _, body = await _add(hub, name="pi")
    pi, _ = await _connect(hub, body["credentials"])
    vps = await hub.deck("vps1", [], key=FILE_KEY["key"], secret=FILE_KEY["secret"])

    # A deck keyed in botport.yaml gets a label too.
    async with hub.session.patch(hub.url("/api/decks/VPS1"), json={"label": "  Frankfurt   VPS ",
                                                                    "description": "Public site"}) as r:
        status, edited = await _json(r)
    assert status == 200 and edited["deck"]["label"] == "Frankfurt VPS" and edited["deck"]["in_file"]
    roster = await pi.expect("network")
    while not any(d.get("label") == "Frankfurt VPS" for d in roster["decks"]):
        roster = await pi.expect("network")
    async with hub.session.patch(hub.url("/api/decks/pi"), json={"label": "x" * 500}) as r:
        assert (await r.json())["deck"]["label"] == "x" * 80
    async with hub.session.patch(hub.url("/api/decks/nobody"), json={"label": "Ghost"}) as r:
        assert r.status == 404
    await vps.close()


async def test_rotate_drops_the_deck_until_it_has_the_new_secret(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    _, body = await _add(hub, name="pi", label="Pi")
    old_creds = body["credentials"]
    pi, _ = await _connect(hub, old_creds)
    # Someone guessing at this key got it blocked from this address...
    for _ in range(12):
        await _connect(hub, {**old_creds, "secret": "guess"}, deck_id="id-guess")
    _, reg = await _connect(hub, old_creds, deck_id="id-guess")
    assert "Too many failed" in reg["error"]

    async with hub.session.post(hub.url("/api/decks/pi/rotate"), json={}) as r:
        status, rotated = await _json(r)
    assert status == 200 and rotated["disconnected"] == ["pi"]
    new_creds = rotated["credentials"]
    assert new_creds["key"] == old_creds["key"] and new_creds["secret"] != old_creds["secret"]
    assert (await pi.expect("_closed"))["code"] == 4401

    # ...the rotation starts it over: the old secret is told why it fails,
    # and the new one gets in.
    _, reg = await _connect(hub, old_creds, deck_id="id-retry")
    assert not reg["ok"] and reg["retry"] is False
    assert "secret was replaced on the hub" in reg["error"]
    _, reg = await _connect(hub, new_creds, deck_id="id-retry")
    assert reg["ok"] and reg["deck_name"] == "pi", reg


async def test_remove_disconnects_and_forgets(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    _, body = await _add(hub, name="pi", label="Pi")
    pi, _ = await _connect(hub, body["credentials"])
    async with hub.session.delete(hub.url("/api/decks/pi")) as r:
        status, out = await _json(r)
    assert status == 200 and out["disconnected"] == ["pi"]
    assert (await pi.expect("_closed"))["code"] == 4401
    _, reg = await _connect(hub, body["credentials"], deck_id="id-2")
    assert not reg["ok"] and "removed on the hub" in reg["error"]
    async with hub.session.get(hub.url("/api/decks")) as r:
        assert [d["name"] for d in (await r.json())["decks"]] == ["vps1"]


async def test_what_the_dashboard_refuses(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    for bad in ("", "has space", "a@b", "-lead", "x" * 64):
        status, body = await _add(hub, name=bad)
        assert status == 400 and "Deck name" in body["error"], bad
    status, body = await _add(hub, name="VPS1")
    assert status == 409 and "botport.yaml" in body["error"]
    assert (await _add(hub, name="pi"))[0] == 201
    assert (await _add(hub, name="PI"))[0] == 409
    async with hub.session.post(hub.url("/api/decks"), data='{"name": "x"}',
                                headers={"Content-Type": "text/plain"}) as r:
        assert r.status == 415, "a cross-site form post cannot add a deck"
    async with hub.session.post(hub.url("/api/decks/vps1/rotate"), json={}) as r:
        assert r.status == 409 and "botport.yaml" in (await r.json())["error"]
    async with hub.session.delete(hub.url("/api/decks/vps1")) as r:
        assert r.status == 409
    async with hub.session.delete(hub.url("/api/decks/nope")) as r:
        assert r.status == 404


async def test_deck_admin_needs_the_admin_token(make_hub):
    hub = await make_hub(auth=[FILE_KEY], server={"admin_token": "t0ken-xyz"})
    assert (await _add(hub, name="pi"))[0] == 401
    async with hub.session.get(hub.url("/api/decks")) as r:
        assert r.status == 401
    async with hub.session.post(hub.url("/api/decks"), json={"name": "pi"},
                                headers={"Authorization": "Bearer t0ken-xyz"}) as r:
        assert r.status == 201


async def test_dashboard_keys_survive_a_restart(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    _, body = await _add(hub, name="pi", label="Kitchen Pi")
    await hub.server.store.close()
    again = await make_hub(auth=[FILE_KEY])  # same data directory
    _, reg = await _connect(again, body["credentials"])
    assert reg["ok"] and reg["deck_name"] == "pi"
    assert again.server.decks.meta("PI")["label"] == "Kitchen Pi"


async def test_labels_for_decks_on_an_open_hub(make_hub):
    hub = await make_hub()  # auth off: any deck may connect, and still gets a label
    await hub.deck("laptop", [])
    async with hub.session.patch(hub.url("/api/decks/laptop"), json={"label": "Ana's laptop"}) as r:
        assert (await r.json())["deck"]["label"] == "Ana's laptop"
    async with hub.session.get(hub.url("/api/decks")) as r:
        listing = await r.json()
    assert listing["auth_enabled"] is False
    [d] = listing["decks"]
    assert d["via"] == "open" and not d["managed"]


def test_no_file_keys_is_fine_when_the_dashboard_can_add_decks():
    cfg = BotPortConfig()
    cfg.auth.enabled = True
    assert not cfg.problems()
    assert any("added on the dashboard" in w for w in cfg.warnings())
    cfg.server.dashboard_enabled = False
    assert any("gen-key" in w for w in cfg.warnings())


async def test_a_named_deck_without_a_key_gets_one_and_keeps_its_name(make_hub):
    hub = await make_hub(auth=[FILE_KEY])
    # Named while the hub ran with auth off (or keyed in a file since emptied).
    await hub.server.decks.ensure_loaded()
    await hub.server.decks.update("lab", label="Lab bench", description="The test rig")
    async with hub.session.get(hub.url("/api/decks")) as r:
        [lab] = [d for d in (await r.json())["decks"] if d["name"] == "lab"]
    assert lab["has_record"] and not lab["managed"] and not lab["in_file"]
    status, body = await _add(hub, name="lab")
    assert status == 201 and body["deck"]["label"] == "Lab bench" and body["deck"]["description"] == "The test rig"
    _, reg = await _connect(hub, body["credentials"])
    assert reg["ok"] and reg["deck_name"] == "lab"
