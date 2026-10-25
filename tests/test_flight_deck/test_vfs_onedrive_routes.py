"""OneDrive mounts through the real FD vfs_routes handlers, with a faked
OneDrive client (the harness of test_vfs_drive_routes): the picker, the mount,
its listing, refresh, Markdown copies, read-through, download and unmount —
and that a OneDrive mount never asks Google for anything."""

import json
import tempfile
from pathlib import Path

import pytest

from captain_claw.drive_client import FOLDER_MIME, DriveFile


class FakeOneDrive:
    def __init__(self):
        self.tree = {
            "root": [
                DriveFile(id="D1", name="Reports", mime_type=FOLDER_MIME),
                DriveFile(id="f1", name="plan.txt", mime_type="text/plain", size=12,
                          modified_time="2026-10-01T10:00:00Z"),
            ],
            "D1": [DriveFile(id="f2", name="q3.md", mime_type="text/markdown", size=20,
                             modified_time="2026-10-01T10:00:00Z")],
            "ana|S1": [DriveFile(id="ana|f9", name="board.txt", mime_type="text/plain", size=5,
                                 modified_time="2026-10-02T10:00:00Z")],
        }
        self.shared = [DriveFile(id="ana|S1", name="Board pack", mime_type=FOLDER_MIME)]
        self.content = {"f1": (b"launch in november", ".txt"), "f2": (b"# Q3\n\nup 41%", ".md"),
                        "ana|f9": (b"board notes", ".txt")}
        self.fetches: list[str] = []

    async def list_folder(self, fid, *, drive_id="", max_files=None, sleep=None, **_):
        return list(self.tree.get(fid, [])), False

    async def list_shared_folders(self, *, sleep=None):
        return list(self.shared)

    async def fetch(self, f, *, sleep=None):
        self.fetches.append(f.id)
        return self.content[f.id]

    async def close(self):
        pass


USER = {"id": "local"}


@pytest.fixture()
async def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    import captain_claw.flight_deck.vfs_routes as vr
    from captain_claw import vfs_drive
    from captain_claw.flight_deck import auth
    from captain_claw.flight_deck.db import FlightDeckDB

    monkeypatch.setattr(vr, "_user_root", lambda uid: tmp / "vfs" / uid)
    monkeypatch.setattr(vfs_drive, "user_root", lambda uid: tmp / "vfs" / uid)
    monkeypatch.setenv("CLAW_VFS_ROOT", str(tmp / "vfs"))
    fake = FakeOneDrive()

    async def _onedrive(user_id=""):
        return fake

    async def _google(user_id=""):
        raise AssertionError("a OneDrive mount must not ask Google")

    monkeypatch.setattr(vr, "_onedrive_client", _onedrive)
    monkeypatch.setattr(vr, "_drive_client", _google)
    db = FlightDeckDB(str(tmp / "fd.db"))
    await db.init()
    auth.set_auth_db(db)
    try:
        yield vr, vfs_drive, tmp, fake
    finally:
        await db.close()


async def test_browse_root_lists_folders_and_what_was_shared(env):
    vr, _, _, _ = env
    out = await vr.onedrive_browse(folder_id="root", drive_id="", user=USER)
    assert out["folders"] == [{"id": "D1", "name": "Reports"}]
    assert out["shared"] == [{"id": "ana|S1", "name": "Board pack"}]
    inner = await vr.onedrive_browse(folder_id="D1", drive_id="", user=USER)
    assert inner["folders"] == [] and inner["shared"] == []


async def test_mount_lists_as_onedrive_and_reads_through(env):
    vr, vfs_drive, tmp, fake = env
    summary = await vr.mount_onedrive(vr.DriveMountBody(name="work", folder_id="root", path="My files"), USER)
    assert summary["files"] == 2 and summary["dirs"] == 1
    mount = tmp / "vfs" / "local" / ".drive" / "work"
    marker = (mount / "plan.txt").read_text()
    assert "OneDrive" in marker and "Google" not in marker and "microsoft_onedrive" in marker
    assert vfs_drive.Manifest.load(mount).provider == "onedrive"

    work = next(p for p in (await vr.list_projects(USER))["projects"] if p["name"] == "work")
    assert work["kind"] == "onedrive" and work["mode"] == "ro"
    assert work["drive"]["source_path"] == "My files" and work["drive"]["total"] == 2

    # The panel reads the real text through the user's own OneDrive.
    r = await vr.read_file(project="work", path="Reports/q3.md", owner="", user=USER)
    assert r["text"] == "# Q3\n\nup 41%" and fake.fetches == ["f2"]
    # A download is the original file, fetched once and cached.
    resp = await vr.download_file(project="work", path="plan.txt", user=USER)
    assert Path(resp.path).read_bytes() == b"launch in november"


async def test_a_folder_shared_with_the_user_mounts_by_its_drive_qualified_id(env):
    vr, _, tmp, fake = env
    await vr.mount_onedrive(vr.DriveMountBody(name="board", folder_id="ana|S1"), USER)
    r = await vr.read_file(project="board", path="board.txt", owner="", user=USER)
    assert r["text"] == "board notes" and fake.fetches == ["ana|f9"]
    # The cache file name stays one safe component.
    cache = tmp / "vfs" / "local" / ".drive" / "board" / ".drive-cache"
    assert [p.name for p in cache.iterdir()] == ["ana_f9"]


async def test_refresh_markdown_copies_and_unmount_follow_the_provider(env):
    vr, vfs_drive, tmp, fake = env
    await vr.mount_onedrive(vr.DriveMountBody(name="work", folder_id="root"), USER)
    fake.tree["root"].append(DriveFile(id="f3", name="new.txt", mime_type="text/plain", size=3,
                                       modified_time="2026-10-03T00:00:00Z"))
    fake.content["f3"] = (b"new", ".txt")
    r = await vr.refresh_drive("work", USER)
    assert r["files"] == 3
    r = await vr.toggle_clonemd("work", vr.DriveToggleBody(clonemd=True), USER)
    mount = tmp / "vfs" / "local" / ".drive" / "work"
    assert r["clonemd"] is True and (mount / "new.txt").read_text() == "new"

    out = await vr.unmount_drive("work", keep_cloned=False, user=USER)
    assert out["ok"]
    assert "work" not in {p["name"] for p in (await vr.list_projects(USER))["projects"]}
    links = json.loads((tmp / "vfs" / "local" / ".vfs-links.json").read_text())
    assert "work" not in links


async def test_a_name_taken_by_a_google_mount_is_refused(env, monkeypatch):
    vr, _, tmp, _ = env
    root = tmp / "vfs" / "local"
    root.mkdir(parents=True)
    (root / ".vfs-links.json").write_text(json.dumps({"docs": {"path": str(root / ".drive" / "docs"),
                                                              "mode": "ro", "kind": "gdrive"}}))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await vr.mount_onedrive(vr.DriveMountBody(name="docs", folder_id="root"), USER)
    assert exc.value.status_code == 409


async def test_not_connected_says_so():
    from fastapi import HTTPException

    import captain_claw.flight_deck.vfs_routes as vr

    async def no(_uid):
        return False

    import captain_claw.flight_deck.microsoft_oauth_routes as mo

    orig = mo.is_microsoft_connected
    mo.is_microsoft_connected = no
    try:
        with pytest.raises(HTTPException) as exc:
            await vr._onedrive_client("someone")
        assert exc.value.status_code == 400 and "Microsoft" in exc.value.detail
    finally:
        mo.is_microsoft_connected = orig


async def test_agent_side_read_through_uses_the_onedrive_client(env, monkeypatch):
    vr, vfs_drive, tmp, fake = env
    await vr.mount_onedrive(vr.DriveMountBody(name="work", folder_id="root"), USER)
    monkeypatch.setattr("captain_claw.onedrive_client.make_client", lambda: fake)
    monkeypatch.setattr("captain_claw.drive_client.make_client",
                        lambda: (_ for _ in ()).throw(AssertionError("not Google")))
    text = await vfs_drive.read_through(tmp / "vfs" / "local" / ".drive" / "work" / "plan.txt")
    assert text == "launch in november"
