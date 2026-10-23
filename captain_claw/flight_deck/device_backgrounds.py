"""A picture behind every screen of a round device (the StopWatch).

The owner sends a picture to their agent ("make this my watch background");
the agent's ``watch_background`` tool hands it to Flight Deck, which crops it
to fill the circle, darkens it so the screens' text stays readable and keeps it
per user and device kind. The device bridge sends it to the watch — now if it's
connected, else when it next connects: the watch names the background it has in
its "prefs", and only firmware that does gets one.

    POST /fd/device/agent/background   agent: {"image_b64", "filename"} or {"clear": true}

Stored as ``<fd_home>/device_backgrounds/<user>/<hw>.jpg``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck.fd_home import fd_home

router = APIRouter()

HW = "stopwatch"                 # the one device kind with a round screen
SIZE = 466                       # its screen, square
DIM = 0.42                       # brightness kept: white text stays readable on it
MAX_BYTES = 120_000              # one chunked picture (device_bridge.MAX_CHUNKED_JPEG_BYTES)
MAX_UPLOAD = 20 * 1024 * 1024    # what the agent may send (a phone photo is a few MB)
_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def root() -> Path:
    return fd_home(Path(os.path.expanduser("~/.captain-claw-fd"))) / "device_backgrounds"


def _path(user_id: str, hw: str) -> Path:
    user = _ID_RE.sub("", str(user_id or ""))[:64]
    kind = _ID_RE.sub("", str(hw or ""))[:24]
    if not user or not kind:
        raise ValueError("no owner or device kind")
    return root() / user / f"{kind}.jpg"


def prepare(blob: bytes, size: int = SIZE, dim: float = DIM, max_bytes: int = MAX_BYTES) -> bytes:
    """Any picture → a size×size baseline JPEG that fills the round screen
    (centre crop, turned the way the camera held it), darkened by *dim*."""
    from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(blob))
        im = ImageOps.exif_transpose(im)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not a picture Flight Deck can read") from exc
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGBA", im.size, (0, 0, 0, 255))
        flat.alpha_composite(im)
        im = flat
    im = ImageOps.fit(im.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
    im = ImageEnhance.Brightness(im).enhance(dim)
    for quality in (88, 80, 72, 64, 56, 48, 40):
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
        if out.tell() <= max_bytes:
            return out.getvalue()
    raise ValueError("the picture doesn't fit the device even at low quality")


def picture_id(jpeg: bytes) -> str:
    return hashlib.sha256(jpeg).hexdigest()[:16]


def save(user_id: str, hw: str, jpeg: bytes) -> str:
    path = _path(user_id, hw)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(jpeg)
    os.replace(tmp, path)
    return picture_id(jpeg)


def current(user_id: str, hw: str) -> tuple[str, bytes] | None:
    """(id, JPEG) of the owner's background for this kind, or None."""
    try:
        jpeg = _path(user_id, hw).read_bytes()
    except (OSError, ValueError):
        return None
    return (picture_id(jpeg), jpeg) if jpeg else None


def clear(user_id: str, hw: str) -> bool:
    try:
        _path(user_id, hw).unlink()
    except (OSError, ValueError):
        return False
    return True


# ── the agent's route ────────────────────────────────────────────────


class BackgroundRequest(BaseModel):
    image_b64: str = ""
    filename: str = ""
    clear: bool = False


def _owner(request: Request) -> str:
    from captain_claw.flight_deck.agent_identity import require_agent_caller
    from captain_claw.flight_deck.auth import _fd_auth_enabled

    caller = require_agent_caller(request, what="device backgrounds")
    owner = caller.owner or ("" if _fd_auth_enabled() else "local")
    if not owner:
        raise HTTPException(403, "This agent has no owner on record, so it has no watch to set")
    return owner


@router.post("/fd/device/agent/background")
async def agent_background(req: BackgroundRequest, request: Request) -> dict:
    from captain_claw.flight_deck.device_bridge import push_background

    owner = _owner(request)
    if req.clear:
        cleared = clear(owner, HW)
        sent = await push_background(owner, HW)
        return {"ok": True, "cleared": cleared, "sent_to": sent}
    if len(req.image_b64) > MAX_UPLOAD * 4 // 3 + 4:
        raise HTTPException(413, f"The picture is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    try:
        blob = base64.b64decode(req.image_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "image_b64 isn't base64") from exc
    try:
        jpeg = prepare(blob)
    except ValueError as exc:
        raise HTTPException(400, f"{req.filename or 'That file'}: {exc}") from exc
    pid = save(owner, HW, jpeg)
    sent = await push_background(owner, HW)
    return {"ok": True, "id": pid, "bytes": len(jpeg), "sent_to": sent}
