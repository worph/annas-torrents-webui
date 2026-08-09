"""Pause / resume / rate-limit controls."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from .. import runtime as rt
from ..schemas import RateLimitRequest

router = APIRouter()


def _patch_controls_cache(ctrl: dict) -> dict:
    """Keep SSE/status controls in sync immediately after control POSTs."""
    data = rt._snapshot_cache.get("data")
    if isinstance(data, dict):
        data = {**data, "controls": ctrl}
        rt._snapshot_cache["data"] = data
        try:
            rt._snapshot_cache["json"] = rt._dump_snapshot(data)
        except Exception:  # noqa: BLE001
            pass
    return ctrl

async def _apply_control(method: str, *args):
    async with rt._session_lock:
        sess = rt.session
        await asyncio.to_thread(rt._locked_call, getattr(sess, method), *args)
        ctrl = await asyncio.to_thread(rt._locked_call, sess.controls_state)
    # Pause/resume change per-torrent rows; drop full cache. Rate limits only need controls patch.
    if method in {"pause_all", "resume_all", "pause_downloads", "resume_downloads"}:
        rt._clear_snapshot_cache()
        return {"ok": True, "controls": ctrl}
    return {"ok": True, "controls": _patch_controls_cache(ctrl)}


@router.post("/api/controls/pause")
async def controls_pause():
    return await _apply_control("pause_all")


@router.post("/api/controls/resume")
async def controls_resume():
    return await _apply_control("resume_all")


@router.post("/api/controls/upload-limit")
async def controls_upload_limit(req: RateLimitRequest):
    body = await _apply_control("set_upload_limit", req.bytes_per_sec)
    body["bytes_per_sec"] = req.bytes_per_sec
    return body


@router.post("/api/controls/pause-downloads")
async def controls_pause_downloads():
    return await _apply_control("pause_downloads")


@router.post("/api/controls/resume-downloads")
async def controls_resume_downloads():
    return await _apply_control("resume_downloads")


@router.post("/api/controls/download-limit")
async def controls_download_limit(req: RateLimitRequest):
    body = await _apply_control("set_download_limit", req.bytes_per_sec)
    body["bytes_per_sec"] = req.bytes_per_sec
    return body

