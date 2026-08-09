"""Status and SSE event streams."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import runtime as rt
from ..auth import redact_snapshot

router = APIRouter()

_TRUST_PROXY = (os.environ.get("TRUST_PROXY_HEADERS") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _client_ip(request: Request) -> str:
    """Peer IP for public SSE caps. X-Forwarded-For only when TRUST_PROXY_HEADERS is set."""
    if _TRUST_PROXY:
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
    return (request.client.host if request.client else "") or "unknown"


@router.get("/api/status")
async def status():
    # Snapshot + controls from the same session generation; release the asyncio
    # lock during I/O so a slow backend cannot stall unrelated routes.
    async with rt._session_lock:
        sess = rt.session
        gen = rt._session_generation
        cached = rt._snapshot_cache["data"]
        snap = dict(cached) if cached is not None else None
    if snap is None:
        snap = await asyncio.to_thread(
            rt._locked_call, rt._build_snapshot, sess, dict(rt.provision_state)
        )
    controls = await asyncio.to_thread(rt._locked_call, sess.controls_state)
    async with rt._session_lock:
        if rt.session is not sess or rt._session_generation != gen:
            raise HTTPException(status_code=503, detail="torrent backend changed")
    snap["controls"] = controls
    return snap


@router.get("/api/public/status")
async def public_status():
    if rt._snapshot_cache["data"] is not None:
        snap = dict(rt._snapshot_cache["data"])
    else:
        async with rt._session_lock:
            snap = await asyncio.to_thread(
                rt._locked_call, rt._build_snapshot, rt.session, dict(rt.provision_state)
            )
    return JSONResponse(
        redact_snapshot(snap),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/events")
async def events():
    async with rt._sse_lock:
        if rt._private_sse_count >= rt._PRIVATE_SSE_MAX:
            raise HTTPException(status_code=503, detail="too many event connections")
        rt._private_sse_count += 1

    async def gen():
        try:
            while True:
                if rt._snapshot_cache["data"] is None:
                    await asyncio.sleep(0.2)
                    continue
                payload = rt._snapshot_cache["json"]
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1)
        finally:
            async with rt._sse_lock:
                rt._private_sse_count = max(0, rt._private_sse_count - 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/public/events")
async def public_events(request: Request):
    client_ip = _client_ip(request)
    async with rt._sse_lock:
        if rt._public_sse_count >= rt._PUBLIC_SSE_MAX:
            raise HTTPException(status_code=503, detail="too many public event connections")
        if rt._public_sse_per_ip.get(client_ip, 0) >= rt._PUBLIC_SSE_PER_IP_MAX:
            raise HTTPException(status_code=503, detail="too many public event connections from this client")
        rt._public_sse_count += 1
        rt._public_sse_per_ip[client_ip] = rt._public_sse_per_ip.get(client_ip, 0) + 1

    async def gen():
        try:
            while True:
                if rt._snapshot_cache["data"] is None:
                    await asyncio.sleep(0.2)
                    continue
                payload = rt._dump_snapshot(redact_snapshot(rt._snapshot_cache["data"]))
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1)
        finally:
            async with rt._sse_lock:
                rt._public_sse_count = max(0, rt._public_sse_count - 1)
                left = rt._public_sse_per_ip.get(client_ip, 1) - 1
                if left <= 0:
                    rt._public_sse_per_ip.pop(client_ip, None)
                else:
                    rt._public_sse_per_ip[client_ip] = left

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
