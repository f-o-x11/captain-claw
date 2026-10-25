"""A structured, paginated, retrying OneDrive client (Microsoft Graph) for VFS
mounts — the OneDrive twin of :mod:`captain_claw.drive_client`.

It speaks the small interface the mount engine (:mod:`captain_claw.vfs_drive`)
uses — ``list_folder``, ``fetch``, ``close`` — and returns the same
:class:`DriveFile` records (a folder carries ``FOLDER_MIME``), so a OneDrive
folder mounts, refreshes, hydrates and clones exactly like a Google Drive one.
Failures are :class:`DriveError` / :class:`DriveNotConnected` for the same
reason: the engine already turns those into readable notes.

Items outside the user's own drive (a folder someone shared, a shortcut added
to My files) live in another drive, and Graph addresses them by drive *and*
item. Their ids are made self-describing — ``<drive id>|<item id>`` — so the
manifest, which records one id per file, can always find them again.

Auth-agnostic like the Drive client: Flight Deck routes pass a per-user token
provider (the calling user's own Microsoft connection); agents use
:func:`global_token_provider` (the agent owner's, through Flight Deck).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from captain_claw.drive_client import (
    FOLDER_MIME,
    DriveError,
    DriveFile,
    DriveNotConnected,
    TokenProvider,
)
from captain_claw.logging import get_logger
from captain_claw.microsoft_oauth import GRAPH_API, granted_scopes

log = get_logger(__name__)

# Any of these reads files (as microsoft_graph.FILES_READ — not imported from
# there: that module pulls in the tool registry, and agents import this one
# from the read path).
FILES_READ = frozenset({"files.read", "files.readwrite", "files.read.all", "files.readwrite.all"})

_SELECT = "id,name,size,lastModifiedDateTime,createdDateTime,folder,file,package,remoteItem,parentReference,webUrl"

# Office formats Graph converts to PDF (``?format=pdf``), so their text can be
# read the way a PDF's is. The raw bytes stay what a download serves.
_PDF_CONVERTIBLE = frozenset({
    ".doc", ".dot", ".dotx", ".docm", ".rtf", ".odt", ".ppt", ".pps", ".ppsx", ".pptm",
    ".odp", ".xls", ".xlsm", ".xlsb", ".ods",
})

RECONNECT = "Flight Deck → Connections → Microsoft"
ID_SEP = "|"


def _seg(value: str) -> str:
    return quote(str(value).strip(), safe="!-_.~")


def split_id(item_id: str, drive_id: str = "") -> tuple[str, str]:
    """``(drive id, item id)`` of a possibly drive-qualified id."""
    if ID_SEP in item_id:
        drive, _, item = item_id.partition(ID_SEP)
        return drive, item
    return drive_id, item_id


def join_id(drive_id: str, item_id: str) -> str:
    return f"{drive_id}{ID_SEP}{item_id}" if drive_id else item_id


class OneDriveClient:
    """Async Microsoft Graph files client returning structured data."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        timeout: float = 60.0,
        max_retries: int = 4,
        page_size: int = 200,
    ) -> None:
        self._token_provider = token_provider
        self._timeout = timeout
        self._max_retries = max_retries
        self._page_size = page_size
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # /content answers with a redirect to a pre-authenticated download
            # URL on another host; httpx drops the bearer token on the way.
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "Captain Claw (OneDrive VFS)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _headers(self) -> dict[str, str]:
        token, scope = await self._token_provider()
        if not token:
            raise DriveNotConnected(f"Microsoft account is not connected. Connect it in {RECONNECT}.")
        # No scope string: the provider couldn't tell — let Graph judge (403).
        granted = granted_scopes(scope) if scope else frozenset()
        if granted and not granted & FILES_READ:
            raise DriveNotConnected(
                "The Microsoft connection has no OneDrive access (Files.Read). An admin "
                f"enables it in {RECONNECT}, then the account is connected again."
            )
        return {"Authorization": f"Bearer {token}"}

    # ── requests ──

    async def _request(
        self, method: str, url: str, *, sleep=asyncio.sleep, **kwargs: Any
    ) -> httpx.Response:
        """One Graph call, retrying throttling and transient 5xx (honouring
        ``Retry-After``). Anything else raises at once."""
        headers = await self._headers()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http().request(method, url, headers=headers, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise DriveError(f"OneDrive request failed: {exc}") from exc
                await sleep(min(2**attempt, 8))
                continue
            if resp.status_code < 400:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                await sleep(self._retry_after(resp) or min(2**attempt, 8))
                continue
            self._raise_for_status(resp)
        raise DriveError(f"OneDrive request failed after retries: {last_exc}")

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return float(resp.headers.get("Retry-After", ""))
        except ValueError:
            return None

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        status = resp.status_code
        message = ""
        try:
            err = resp.json().get("error", {})
            message = str(err.get("message") or err.get("code") or "")
        except Exception:
            message = resp.text[:200]
        if status == 401:
            raise DriveNotConnected(f"Microsoft sign-in expired. Connect the account again in {RECONNECT}.")
        if status == 403:
            raise DriveError(f"Microsoft refused access: {message or 'permission denied'}.")
        if status == 404:
            raise DriveError("Not found in OneDrive (it may have been moved, deleted or unshared).")
        raise DriveError(f"OneDrive error ({status}): {message}")

    @staticmethod
    def _base(drive_id: str) -> str:
        return f"{GRAPH_API}/drives/{_seg(drive_id)}" if drive_id else f"{GRAPH_API}/me/drive"

    # ── listing ──

    @staticmethod
    def to_file(item: dict[str, Any], drive_id: str = "") -> DriveFile | None:
        """A Graph driveItem as a :class:`DriveFile`; None for one a mount
        can't hold (a OneNote notebook). A shortcut or a shared item carries
        its own drive in its id; so does anything listed in a drive that
        isn't the user's own."""
        remote = item.get("remoteItem") or {}
        src = remote if remote.get("id") else item
        # A facet's presence is what counts (Graph may send it empty: {}).
        if "package" in src or "package" in item:
            return None
        own_drive = str((src.get("parentReference") or {}).get("driveId") or "")
        if remote.get("id"):
            fid = join_id(own_drive, str(remote["id"]))
        else:
            fid = join_id(drive_id, str(item.get("id", "")))
        is_folder = "folder" in src or "folder" in item
        file_meta = src.get("file") or item.get("file") or {}
        hashes = file_meta.get("hashes") or {}
        raw_size = src.get("size", item.get("size"))
        return DriveFile(
            id=fid,
            name=str(item.get("name") or src.get("name") or ""),
            mime_type=FOLDER_MIME if is_folder else str(file_meta.get("mimeType") or ""),
            size=None if is_folder or raw_size in (None, "") else int(raw_size),
            modified_time=str(src.get("lastModifiedDateTime") or item.get("lastModifiedDateTime") or ""),
            created_time=str(src.get("createdDateTime") or item.get("createdDateTime") or ""),
            md5=str(hashes.get("quickXorHash") or hashes.get("sha1Hash") or ""),
            web_view_link=str(item.get("webUrl") or ""),
        )

    async def list_folder(
        self,
        folder_id: str = "root",
        *,
        drive_id: str = "",
        max_files: int | None = None,
        sleep=asyncio.sleep,
        **_ignored: Any,
    ) -> tuple[list[DriveFile], bool]:
        """A folder's direct children, following ``@odata.nextLink``.

        Returns ``(files, truncated)`` like :meth:`DriveClient.list_folder`.
        *folder_id* ``root`` (or empty) is the drive's root; *drive_id* names a
        drive other than the user's own, unless the id carries one itself.
        """
        drive, item = split_id(folder_id or "root", drive_id)
        base = self._base(drive)
        url: str | None = (f"{base}/root/children" if item in ("", "root")
                           else f"{base}/items/{_seg(item)}/children")
        params: dict[str, Any] | None = {"$select": _SELECT, "$top": self._page_size}
        files: list[DriveFile] = []
        truncated = False
        while url:
            resp = await self._request("GET", url, params=params, sleep=sleep)
            params = None  # the next link carries them
            data = resp.json()
            for raw in data.get("value", []) or []:
                f = self.to_file(raw, drive)
                if f is None:
                    continue
                if max_files is not None and len(files) >= max_files:
                    truncated = True
                    break
                files.append(f)
            if truncated:
                break
            url = data.get("@odata.nextLink")
        files.sort(key=lambda f: (f.mime_type != FOLDER_MIME, f.name.lower()))
        return files, truncated

    async def list_shared_folders(self, *, sleep=asyncio.sleep) -> list[DriveFile]:
        """Folders other people shared with the user (Graph ``sharedWithMe``),
        their ids drive-qualified. Empty when the account can't list them
        (some account types and tenants don't offer it)."""
        try:
            resp = await self._request("GET", f"{GRAPH_API}/me/drive/sharedWithMe", sleep=sleep)
        except DriveNotConnected:
            raise
        except DriveError as exc:
            log.debug("OneDrive sharedWithMe unavailable", error=str(exc))
            return []
        out: list[DriveFile] = []
        for raw in resp.json().get("value", []) or []:
            f = self.to_file(raw)
            if f is not None and f.mime_type == FOLDER_MIME and ID_SEP in f.id:
                out.append(f)
        out.sort(key=lambda f: f.name.lower())
        return out

    # ── content ──

    async def _content(self, f: DriveFile, *, as_pdf: bool, sleep) -> bytes:
        drive, item = split_id(f.id)
        params = {"format": "pdf"} if as_pdf else None
        resp = await self._request(
            "GET", f"{self._base(drive)}/items/{_seg(item)}/content", params=params, sleep=sleep,
        )
        return resp.content

    async def fetch(self, f: DriveFile, *, sleep=asyncio.sleep) -> tuple[bytes, str]:
        """The file's own bytes and extension (a download, a binary reader)."""
        return await self._content(f, as_pdf=False, sleep=sleep), Path(f.name).suffix.lower()

    async def fetch_for_text(self, f: DriveFile, *, sleep=asyncio.sleep) -> tuple[bytes, str]:
        """Bytes to read as text: an older Office format comes as Graph's PDF
        of it, which the extractors read; anything else as itself."""
        ext = Path(f.name).suffix.lower()
        if ext in _PDF_CONVERTIBLE:
            return await self._content(f, as_pdf=True, sleep=sleep), ".pdf"
        return await self.fetch(f, sleep=sleep)


# ── token providers ──


async def global_token_provider() -> tuple[str, str]:
    """This agent's owner's Microsoft token, through Flight Deck (agents have
    no Microsoft connection of their own). ``("", "")`` when not connected; a
    refusal (a member's turn, Flight Deck said no) keeps its reason."""
    from captain_claw.microsoft_oauth_manager import MicrosoftOAuthManager, MicrosoftRefused

    try:
        tokens = await MicrosoftOAuthManager().get_tokens()
    except MicrosoftRefused as exc:
        raise DriveNotConnected(str(exc)) from exc
    if not tokens or not tokens.access_token:
        return "", ""
    return tokens.access_token, (tokens.scope or "")


def make_client(token_provider: TokenProvider | None = None) -> OneDriveClient:
    """A OneDriveClient over the given provider, or this agent's owner's
    connection (see :func:`global_token_provider`)."""
    return OneDriveClient(token_provider or global_token_provider)
