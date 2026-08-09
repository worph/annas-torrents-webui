"""Torrent remove."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .. import runtime as rt
from ..schemas import TorrentRemoveRequest

router = APIRouter()


@router.post("/api/torrents/remove")
async def torrents_remove(req: TorrentRemoveRequest):
    """Remove one torrent (and optionally its files) after explicit confirm."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    ih = (req.infohash or "").strip().lower()
    if not ih or len(ih) < 32:
        raise HTTPException(status_code=400, detail="infohash required")
    async with rt._session_lock:
        sess = rt.session
        known = {h.lower() for h in await asyncio.to_thread(rt._locked_call, sess.infohashes)}
        if ih not in known:
            raise HTTPException(status_code=404, detail="torrent not found")
        result = await asyncio.to_thread(
            rt._locked_call, sess.remove_torrents, [ih], req.delete_files
        )
    removed = int((result or {}).get("removed") or 0)
    files_deleted = (result or {}).get("files_deleted")
    if removed == 0:
        raise HTTPException(
            status_code=409,
            detail="removal incomplete — torrent could not be removed",
        )
    rt._clear_snapshot_cache()
    return {
        "ok": True,
        "removed": removed,
        "infohash": ih,
        "delete_files": req.delete_files,
        "files_deleted": files_deleted if req.delete_files else None,
    }

