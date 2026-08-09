"""Storage destination listing and folder picker."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .. import runtime as rt
from .. import storage

router = APIRouter()


@router.get("/api/storage")
async def storage_api():
    """Preset download destinations (default = repo rt.DATA_DIR/content)."""
    async with rt._session_lock:
        sess = rt.session
        gen = rt._session_generation
        backend = rt.TORRENT_BACKEND
    options = await asyncio.to_thread(rt._locked_call, sess.storage_options, rt.STORAGE_PATHS)
    seen = {storage.normalize_path(o["path"]) for o in options}
    for t in await asyncio.to_thread(rt._locked_call, sess.torrents_status):
        sp = (t.get("save_path") or "").strip()
        if not sp:
            continue
        key = storage.normalize_path(sp)
        if key in seen:
            continue
        seen.add(key)
        maker = storage.remote_option if backend == "qbittorrent" else storage.option
        options.append(maker(sp, label=f"In use · {sp}"))
    with rt._picked_paths_lock:
        picked_snapshot = sorted(rt._picked_paths)
    for picked in picked_snapshot:
        key = storage.normalize_path(picked)
        if key in seen:
            continue
        seen.add(key)
        maker = storage.remote_option if backend == "qbittorrent" else storage.option
        options.append(maker(picked, label=f"Browse · {picked}"))
    default = next((o["path"] for o in options if o.get("default")), options[0]["path"] if options else "")
    g = await asyncio.to_thread(rt._locked_call, sess.global_status)
    async with rt._session_lock:
        if rt.session is not sess or rt._session_generation != gen:
            raise HTTPException(status_code=503, detail="torrent backend changed")
    return {
        "options": options,
        "default": default,
        "active": g.get("storage_path") or default,
        "backend": backend,
    }


@router.post("/api/storage/pick")
async def storage_pick():
    """Native OS folder dialog (separate GUI process on the host)."""
    if rt.TORRENT_BACKEND == "qbittorrent":
        raise HTTPException(
            status_code=400,
            detail="Browse is only available with the embedded libtorrent backend",
        )
    path = await asyncio.to_thread(storage.pick_folder)
    if not path:
        return {"ok": False, "cancelled": True, "path": None}
    try:
        path = storage.ensure_save_dir(path)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"could not use folder: {e}") from e
    with rt._picked_paths_lock:
        rt._picked_paths.add(path)
        while len(rt._picked_paths) > rt._PICKED_PATHS_MAX:
            rt._picked_paths.pop()
    return {"ok": True, "cancelled": False, "path": path}
