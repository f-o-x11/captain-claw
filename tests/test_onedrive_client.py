"""OneDriveClient against a mocked Microsoft Graph (httpx MockTransport): the
listing a mount walks, the bytes it fetches, and the errors it turns into
DriveError / DriveNotConnected. No network."""

from __future__ import annotations

import httpx
import pytest

from captain_claw.drive_client import FOLDER_MIME, DriveError, DriveFile, DriveNotConnected
from captain_claw.onedrive_client import OneDriveClient, join_id, split_id

G = "https://graph.microsoft.com/v1.0"


def _client(handler, *, token="tok", scope="") -> OneDriveClient:
    async def provider():
        return token, scope

    c = OneDriveClient(provider, max_retries=2)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return c


async def _no_sleep(_s):
    pass


def _item(i, name, *, folder=False, size=10, mime="text/plain", **extra):
    d = {"id": i, "name": name, "lastModifiedDateTime": "2026-10-01T10:00:00Z",
         "parentReference": {"driveId": "me-drive"}}
    if folder:
        d["folder"] = {"childCount": 1}
    else:
        d["size"] = size
        d["file"] = {"mimeType": mime, "hashes": {"quickXorHash": f"h-{i}"}}
    d.update(extra)
    return d


async def test_lists_the_root_following_next_links_folders_first():
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        assert req.headers["Authorization"] == "Bearer tok"
        if "skiptoken" not in str(req.url):
            return httpx.Response(200, json={
                "value": [_item("f1", "b.txt"), _item("d1", "Reports", folder=True)],
                "@odata.nextLink": f"{G}/me/drive/root/children?$skiptoken=2",
            })
        return httpx.Response(200, json={"value": [_item("f2", "a.docx", mime="application/x")]})

    files, truncated = await _client(handler).list_folder("root")
    assert not truncated
    assert [(f.name, f.mime_type == FOLDER_MIME) for f in files] == [
        ("Reports", True), ("a.docx", False), ("b.txt", False)]
    assert files[1].size == 10 and files[1].md5 == "h-f2" and files[1].modified_time.startswith("2026")
    assert len(seen) == 2 and "skiptoken" in seen[1]


async def test_a_subfolder_and_another_drive_are_addressed_by_id():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return httpx.Response(200, json={"value": [_item("c1", "x.md")]})

    c = _client(handler)
    files, _ = await c.list_folder("d1")
    assert paths[-1] == "/v1.0/me/drive/items/d1/children" and files[0].id == "c1"
    # In someone else's drive every id names that drive, so it can be found again.
    files, _ = await c.list_folder(join_id("other!drive", "d9"))
    assert paths[-1] == "/v1.0/drives/other!drive/items/d9/children"
    assert split_id(files[0].id) == ("other!drive", "c1")


async def test_shortcuts_carry_their_drive_and_notebooks_are_skipped():
    def handler(req):
        return httpx.Response(200, json={"value": [
            {"id": "s1", "name": "Team plans", "remoteItem": {
                "id": "R1", "folder": {"childCount": 3},
                "parentReference": {"driveId": "team-drive"}}},
            {"id": "n1", "name": "Notebook", "package": {"type": "oneNote"}},
        ]})

    files, _ = await _client(handler).list_folder("root")
    assert [(f.name, f.id, f.mime_type == FOLDER_MIME) for f in files] == [
        ("Team plans", join_id("team-drive", "R1"), True)]


async def test_max_files_truncates():
    def handler(req):
        return httpx.Response(200, json={"value": [_item(f"f{i}", f"{i}.txt") for i in range(5)]})

    files, truncated = await _client(handler).list_folder("root", max_files=3)
    assert len(files) == 3 and truncated


async def test_shared_with_me_lists_folders_only():
    def handler(req):
        assert req.url.path.endswith("/me/drive/sharedWithMe")
        return httpx.Response(200, json={"value": [
            {"id": "x", "name": "Board pack", "remoteItem": {
                "id": "B1", "folder": {}, "parentReference": {"driveId": "ana-drive"}}},
            {"id": "y", "name": "memo.docx", "remoteItem": {
                "id": "M1", "file": {"mimeType": "x"}, "parentReference": {"driveId": "ana-drive"}}},
        ]})

    shared = await _client(handler).list_shared_folders()
    assert [(f.name, f.id) for f in shared] == [("Board pack", join_id("ana-drive", "B1"))]


async def test_shared_with_me_unavailable_is_empty():
    shared = await _client(lambda req: httpx.Response(400, json={"error": {"message": "no"}})).list_shared_folders()
    assert shared == []


async def test_fetch_follows_the_download_redirect_and_keeps_the_extension():
    def handler(req):
        if req.url.host == "graph.microsoft.com":
            assert req.url.path == "/v1.0/drives/d!x/items/I1/content"
            return httpx.Response(302, headers={"Location": "https://cdn.example/dl?sig=1"})
        assert "Authorization" not in req.headers  # never sent to the download host
        return httpx.Response(200, content=b"PK\x03\x04docx bytes")

    data, ext = await _client(handler).fetch(DriveFile(id=join_id("d!x", "I1"), name="Plan.DOCX", mime_type=""))
    assert data.startswith(b"PK") and ext == ".docx"


async def test_text_of_an_older_office_file_comes_as_its_pdf():
    def handler(req):
        assert req.url.params.get("format") == "pdf"
        return httpx.Response(200, content=b"%PDF-1.7")

    c = _client(handler)
    data, ext = await c.fetch_for_text(DriveFile(id="I1", name="old.doc", mime_type=""))
    assert (data, ext) == (b"%PDF-1.7", ".pdf")


async def test_no_token_or_no_files_scope_is_not_connected():
    with pytest.raises(DriveNotConnected):
        await _client(lambda r: httpx.Response(200, json={}), token="").list_folder("root")
    with pytest.raises(DriveNotConnected, match="OneDrive access"):
        await _client(lambda r: httpx.Response(200, json={}), scope="openid User.Read Mail.Read").list_folder("root")
    files, _ = await _client(lambda r: httpx.Response(200, json={"value": []}),
                             scope="openid Files.ReadWrite.All").list_folder("root")
    assert files == []


async def test_errors_become_drive_errors_and_throttling_is_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"value": []})

    c = _client(handler)
    files, _ = await c.list_folder("root", sleep=_no_sleep)
    assert files == [] and len(calls) == 2

    with pytest.raises(DriveNotConnected):
        await _client(lambda r: httpx.Response(401)).list_folder("root")
    with pytest.raises(DriveError, match="Not found"):
        await _client(lambda r: httpx.Response(404)).list_folder("gone")
    with pytest.raises(DriveError, match="refused"):
        await _client(lambda r: httpx.Response(403, json={"error": {"message": "accessDenied"}})).list_folder("x")
