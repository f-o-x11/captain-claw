"""The shared-folder routes the kiosk's Folders dialog adds: a file inline
(``/view``), a PDF's first page (``/thumb``) and a search across every folder
the caller can open (``/search``).

Same harness as test_vfs_routes: ``DATA_DIR`` points at a tmp dir, auth is a
fixed user, and the share lookups go to a small fake database.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import vfs_routes


class _FakeDB:
    """Folder shares only: (resource_id, owner_id, grantee_id, permission)."""

    def __init__(self) -> None:
        self.shares: list[tuple[str, str, str, str]] = []

    async def list_shares_for_grantee(self, grantee: str, rtype: str):
        return [{"resource_id": r, "owner_id": o, "permission": p, "owner_email": "", "owner_name": ""}
                for r, o, g, p in self.shares if g == grantee and rtype == "vfs"]

    async def get_share_for_grantee(self, rtype: str, rid: str, grantee: str, owner: str):
        for r, o, g, p in self.shares:
            if rtype == "vfs" and r == rid and g == grantee and o == owner:
                return {"permission": p}
        return None

    async def list_basna_sessions(self, _uid: str):
        return []


@pytest.fixture
def deck(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import captain_claw.flight_deck.server as server

    # Its own dir: a linked folder elsewhere in tmp_path stays outside it.
    data = tmp_path / "fd-data"
    monkeypatch.setattr(server, "DATA_DIR", data, raising=False)
    db = _FakeDB()
    monkeypatch.setattr(vfs_routes, "get_db", lambda: db)
    app = FastAPI()
    app.include_router(vfs_routes.router)
    from captain_claw.flight_deck.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
    return TestClient(app), data / "vfs", db


def _put(root: Path, rel: str, data: str = "x", mtime: float | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ── /view ──


def test_view_serves_inline_and_never_sniffed(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "docs/notes.md", "# hi")
    r = client.get("/fd/vfs/view", params={"project": "docs", "path": "notes.md"})
    assert r.status_code == 200 and r.text == "# hi"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers


@pytest.mark.parametrize("name", ["page.html", "logo.svg", "feed.xml"])
def test_view_sandboxes_what_a_browser_runs(deck, name: str) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", f"docs/{name}", "<script>alert(1)</script>")
    r = client.get("/fd/vfs/view", params={"project": "docs", "path": name})
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox") and "allow-same-origin" not in csp


def test_view_refuses_a_path_out_of_the_folder(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "docs/a.txt")
    _put(vfs / "u2", "secret/key.txt", "nope")
    r = client.get("/fd/vfs/view", params={"project": "docs", "path": "../../u2/secret/key.txt"})
    assert r.status_code == 400


def test_view_of_another_users_folder_needs_a_share(deck) -> None:
    client, vfs, db = deck
    _put(vfs / "u2", "plans/q3.md", "plan")
    params = {"project": "plans", "path": "q3.md", "owner": "u2"}
    assert client.get("/fd/vfs/view", params=params).status_code == 404
    db.shares.append(("plans", "u2", "u1", "view"))
    r = client.get("/fd/vfs/view", params=params)
    assert r.status_code == 200 and r.text == "plan"


# ── /thumb ──


def test_thumb_is_for_pdfs_only(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "docs/a.txt")
    assert client.get("/fd/vfs/thumb", params={"project": "docs", "path": "a.txt"}).status_code == 404
    assert client.get("/fd/vfs/thumb", params={"project": "docs", "path": "gone.pdf"}).status_code == 404


def test_thumb_draws_a_pdfs_first_page(deck) -> None:
    pdfium = pytest.importorskip("pypdfium2")
    client, vfs, _ = deck
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 300)
    target = vfs / "u1" / "docs" / "brief.pdf"
    target.parent.mkdir(parents=True)
    pdf.save(str(target))
    pdf.close()
    r = client.get("/fd/vfs/thumb", params={"project": "docs", "path": "brief.pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


# ── /search ──


def test_search_finds_files_by_any_word_of_their_path_newest_first(deck) -> None:
    client, vfs, _ = deck
    now = time.time()
    _put(vfs / "u1", "research/drafts/Market Report.md", mtime=now - 300)
    _put(vfs / "u1", "research/final/market-report.pdf", mtime=now - 10)
    _put(vfs / "u1", "other/todo.txt", mtime=now)
    hits = client.get("/fd/vfs/search", params={"q": "MARKET report"}).json()
    assert [h["path"] for h in hits["files"]] == ["final/market-report.pdf", "drafts/Market Report.md"]
    assert {h["project"] for h in hits["files"]} == {"research"}
    assert hits["files"][0]["owner"] == "" and hits["files"][0]["name"] == "market-report.pdf"
    # A word of the folder path finds what's inside it.
    assert [h["name"] for h in client.get("/fd/vfs/search", params={"q": "drafts"}).json()["files"]] == ["Market Report.md"]


def test_search_words_may_name_the_folder(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "invoices-2026/october/INV-1043.md")
    _put(vfs / "u1", "invoices-2026/summary.csv")
    _put(vfs / "u1", "other/INV-9.md")
    hits = client.get("/fd/vfs/search", params={"q": "invoices"}).json()["files"]
    assert sorted(h["path"] for h in hits) == ["october/INV-1043.md", "summary.csv"]
    # A word of the folder and a word of the file, together.
    hits = client.get("/fd/vfs/search", params={"q": "invoices 1043"}).json()["files"]
    assert [h["path"] for h in hits] == ["october/INV-1043.md"]


def test_search_with_no_words_lists_the_newest_files(deck) -> None:
    client, vfs, _ = deck
    now = time.time()
    for i in range(5):
        _put(vfs / "u1", f"p/f{i}.txt", mtime=now - i * 60)
    hits = client.get("/fd/vfs/search", params={"limit": 3}).json()
    assert [h["name"] for h in hits["files"]] == ["f0.txt", "f1.txt", "f2.txt"]
    assert hits["total"] == 5 and hits["truncated"] is False


def test_search_skips_hidden_entries_heavy_dirs_and_symlinks(deck, tmp_path: Path) -> None:
    client, vfs, _ = deck
    root = vfs / "u1"
    _put(root, "app/main.py")
    _put(root, "app/.env", "SECRET=1")
    _put(root, "app/.git/config")
    _put(root, "app/node_modules/lib/main.js")
    _put(root, ".drive/mount/main.doc")
    outside = _put(tmp_path / "outside", "main-secret.txt")
    (root / "app" / "escape").symlink_to(outside.parent, target_is_directory=True)
    (root / "app" / "main-link.txt").symlink_to(outside)
    (root / "app" / ".vfs-meta.jsonl").write_text("")
    names = [h["path"] for h in client.get("/fd/vfs/search", params={"q": "main"}).json()["files"]]
    assert names == ["main.py"]


def test_search_reaches_links_and_shares_but_no_one_elses_folders(deck, tmp_path: Path) -> None:
    client, vfs, db = deck
    linked = tmp_path / "repo"
    _put(linked, "src/budget.py")
    (vfs / "u1").mkdir(parents=True)
    (vfs / "u1" / ".vfs-links.json").write_text(json.dumps({"repo": {"path": str(linked), "mode": "ro"}}))
    _put(vfs / "u2", "plans/budget.xlsx")
    _put(vfs / "u2", "private/budget-secret.txt")
    _put(vfs / "u3", "x/budget.md")
    db.shares.append(("plans", "u2", "u1", "view"))
    db.shares.append(("x", "u3", "someone-else", "view"))
    hits = client.get("/fd/vfs/search", params={"q": "budget"}).json()["files"]
    assert sorted((h["project"], h["owner"], h["path"]) for h in hits) == [
        ("plans", "u2", "budget.xlsx"),
        ("repo", "", "src/budget.py"),
    ]


def test_search_in_one_folder_and_its_share_check(deck) -> None:
    client, vfs, db = deck
    _put(vfs / "u1", "a/report.md")
    _put(vfs / "u1", "b/report.md")
    _put(vfs / "u2", "plans/report.md")
    hits = client.get("/fd/vfs/search", params={"q": "report", "project": "b"}).json()["files"]
    assert [(h["project"], h["path"]) for h in hits] == [("b", "report.md")]
    assert client.get("/fd/vfs/search", params={"q": "report", "project": "plans", "owner": "u2"}).status_code == 404
    db.shares.append(("plans", "u2", "u1", "edit"))
    hits = client.get("/fd/vfs/search", params={"q": "report", "project": "plans", "owner": "u2"}).json()["files"]
    assert [(h["project"], h["owner"]) for h in hits] == [("plans", "u2")]


def test_search_names_who_wrote_a_file(deck) -> None:
    client, vfs, _ = deck
    root = vfs / "u1" / "docs"
    _put(vfs / "u1", "docs/brief.md")
    (root / vfs_routes.META_FILENAME).write_text(json.dumps({"path": "brief.md", "agent": "researcher", "ts": 1}) + "\n")
    hit = client.get("/fd/vfs/search", params={"q": "brief"}).json()["files"][0]
    assert hit["author"] == "researcher"


def test_search_stops_at_its_scan_cap(deck, monkeypatch: pytest.MonkeyPatch) -> None:
    client, vfs, _ = deck
    for i in range(12):
        _put(vfs / "u1", f"p{i % 3}/f{i}.txt")
    monkeypatch.setattr(vfs_routes, "_SEARCH_SCAN_CAP", 5)
    hits = client.get("/fd/vfs/search").json()
    assert hits["truncated"] is True and len(hits["files"]) == 5


# ── /rename ──


def test_rename_never_replaces_or_swallows_what_is_there(deck) -> None:
    client, vfs, _ = deck
    root = vfs / "u1" / "docs"
    _put(vfs / "u1", "docs/a.md", "A")
    _put(vfs / "u1", "docs/b.md", "B")
    _put(vfs / "u1", "docs/drafts/old.md", "old")
    r = client.post("/fd/vfs/rename", json={"project": "docs", "path": "a.md", "to": "b.md"})
    assert r.status_code == 409
    assert (root / "b.md").read_text() == "B" and (root / "a.md").read_text() == "A"
    r = client.post("/fd/vfs/rename", json={"project": "docs", "path": "a.md", "to": "drafts"})
    assert r.status_code == 409 and not (root / "drafts" / "a.md").exists()
    r = client.post("/fd/vfs/rename", json={"project": "docs", "path": "a.md", "to": "brief.md"})
    assert r.status_code == 200 and (root / "brief.md").read_text() == "A"


def test_rename_refuses_the_root_and_a_folder_into_itself(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "docs/drafts/old.md")
    assert client.post("/fd/vfs/rename", json={"project": "docs", "path": "", "to": "x"}).status_code == 400
    assert client.post("/fd/vfs/rename", json={"project": "docs", "path": "drafts", "to": "drafts/inner"}).status_code == 400
    assert (vfs / "u1" / "docs" / "drafts" / "old.md").is_file()


def test_rename_may_change_only_the_case(deck) -> None:
    client, vfs, _ = deck
    _put(vfs / "u1", "docs/readme.md", "hi")
    r = client.post("/fd/vfs/rename", json={"project": "docs", "path": "readme.md", "to": "README.md"})
    assert r.status_code == 200
    assert [p.name for p in (vfs / "u1" / "docs").iterdir() if not p.name.startswith(".")] == ["README.md"]
