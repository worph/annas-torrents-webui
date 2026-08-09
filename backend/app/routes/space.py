"""Space preview / free endpoints."""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import APIRouter, HTTPException

from .. import runtime as rt
from .. import space, storage
from ..schemas import SpaceFreeRequest, SpacePreviewRequest

router = APIRouter()


def _filter_torrents_by_save_path(torrents: list[dict], save_path: str | None) -> list[dict]:
    if not save_path or not save_path.strip():
        return torrents
    return [t for t in torrents if storage.matches_destination(t.get("save_path"), save_path)]


@router.post("/api/space/preview")
async def space_preview(req: SpacePreviewRequest):
    if req.bytes is None and req.gb is None:
        raise HTTPException(status_code=400, detail="provide bytes or gb")
    request_bytes = int(req.bytes) if req.bytes is not None else int(float(req.gb) * 1000**3)
    if request_bytes <= 0 or request_bytes > 1_000_000 * 1000**3:
        raise HTTPException(status_code=400, detail="request must be positive")
    async with rt._session_lock:
        sess = rt.session
        backend = rt.TORRENT_BACKEND
        fingerprint = rt._session_fingerprint()
        allowed = await asyncio.to_thread(rt._locked_call, rt._allowed_paths, sess)
        torrents = await asyncio.to_thread(rt._locked_call, sess.torrents_status)
        default_path = (await asyncio.to_thread(rt._locked_call, sess.default_save_path) or "").strip()
    requested = (req.save_path or "").strip() or None
    # Empty qBit default would otherwise free across every category torrent.
    if not requested and not default_path:
        raise HTTPException(
            status_code=400,
            detail="choose a download destination before freeing space",
        )
    save_path = rt._allowed_destination(requested or default_path or None, allowed)
    if not save_path:
        raise HTTPException(
            status_code=400,
            detail="choose a download destination before freeing space",
        )
    torrents = _filter_torrents_by_save_path(torrents, save_path)
    result = space.pick_combination(torrents, request_bytes)
    token = secrets.token_urlsafe(16)
    now = time.time()
    async with rt._space_lock:
        # Drop expired tokens (cheap; few previews) under the same lock as free/consume.
        expired = [k for k, v in rt._space_tokens.items() if v.get("expires", 0) < now]
        for k in expired:
            rt._space_tokens.pop(k, None)
        while len(rt._space_tokens) >= rt._SPACE_TOKEN_MAX:
            oldest = min(rt._space_tokens, key=lambda k: rt._space_tokens[k].get("expires", 0))
            rt._space_tokens.pop(oldest, None)
        rt._space_tokens[token] = {
            "hashes": {s["infohash"].lower() for s in result["selected"] if s.get("infohash")},
            "save_path": save_path,
            "request_bytes": request_bytes,
            "backend": backend,
            "fingerprint": fingerprint,
            "expires": now + rt._SPACE_TOKEN_TTL,
            "consuming": False,
        }
    result["token"] = token
    result["request_bytes"] = request_bytes
    result["save_path"] = save_path
    return result


@router.post("/api/space/free")
async def space_free(req: SpaceFreeRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    if not req.token:
        raise HTTPException(status_code=400, detail="preview token required")
    async with rt._space_lock:
        entry = rt._space_tokens.get(req.token)
    if not entry or entry.get("expires", 0) < time.time():
        raise HTTPException(status_code=400, detail="preview expired — run Preview again")
    hashes = list(dict.fromkeys(h.lower() for h in req.infohashes if h))
    if not hashes:
        raise HTTPException(status_code=400, detail="infohashes required")
    want = entry.get("hashes") or set()
    if set(hashes) != want:
        raise HTTPException(status_code=400, detail="infohashes do not match preview")
    if req.request_bytes != entry.get("request_bytes"):
        raise HTTPException(status_code=400, detail="request_bytes does not match preview")
    req_sp = (req.save_path or "").strip() or None
    if req_sp != entry.get("save_path"):
        raise HTTPException(status_code=400, detail="save_path does not match preview")
    async with rt._space_lock:
        current = rt._space_tokens.get(req.token)
        if current is not entry or entry.get("consuming"):
            raise HTTPException(status_code=400, detail="preview is already being used")
        entry["consuming"] = True
    try:
        # Fingerprint + delete under the same rt.session lock so a concurrent
        # backend switch cannot sneak between the check and remove_torrents.
        async with rt._session_lock:
            if entry.get("fingerprint") != rt._session_fingerprint():
                raise HTTPException(
                    status_code=400,
                    detail="preview expired — backend or rt.session changed, run Preview again",
                )
            sess = rt.session
            known = {h.lower() for h in await asyncio.to_thread(rt._locked_call, sess.infohashes)}
            bad = [h for h in hashes if h not in known]
            if bad:
                raise HTTPException(status_code=400, detail=f"unknown infohashes: {bad[:5]}")
            # Abort before any mutation when selected torrents share content with
            # each other or with remaining torrents — reclaim assumes file delete.
            torrents = await asyncio.to_thread(rt._locked_call, sess.torrents_status)
            by_ih = {t["infohash"].lower(): t for t in torrents if t.get("infohash")}
            if req_sp:
                for h in hashes:
                    t = by_ih.get(h)
                    if not t or not storage.matches_destination(t.get("save_path"), req_sp):
                        raise HTTPException(status_code=400, detail=f"torrent not on destination: {h[:12]}")
            from ..pathsafety import shared_content_ids

            entries = [
                (t["infohash"].lower(), t.get("save_path") or "", t.get("name") or "")
                for t in torrents
                if t.get("infohash")
            ]
            shared = shared_content_ids(entries)
            if any(h in shared for h in hashes):
                raise HTTPException(
                    status_code=409,
                    detail="cannot free space — selected torrents share content paths; remove individually without deleting files",
                )
            result = await asyncio.to_thread(rt._locked_call, sess.remove_torrents, hashes, True)
    except Exception:
        async with rt._space_lock:
            if req.token in rt._space_tokens:
                rt._space_tokens[req.token]["consuming"] = False
        raise
    async with rt._space_lock:
        rt._space_tokens.pop(req.token, None)
    removed = int((result or {}).get("removed") or 0)
    files_deleted = (result or {}).get("files_deleted")
    if removed == 0:
        raise HTTPException(
            status_code=409,
            detail="removal incomplete — no torrents were removed",
        )
    if removed < len(hashes):
        raise HTTPException(
            status_code=409,
            detail="removal incomplete — some torrents could not be removed",
        )
    rt._clear_snapshot_cache()
    return {
        "ok": True,
        "removed": removed,
        "files_deleted": files_deleted,
    }

