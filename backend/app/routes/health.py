"""Health, public config, SSE ticket."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import runtime as rt
from ..auth import auth_configured, auth_required, issue_sse_ticket

router = APIRouter()


@router.get("/api/healthz")
async def healthz():
    """Public liveness — process is up. Use for Docker HEALTHCHECK."""
    return {"ok": True}


@router.get("/api/health")
async def health():
    g = (rt._snapshot_cache.get("data") or {}).get("global") or {}
    if not g:
        try:
            g = await rt._call_session("global_status")
        except Exception:  # noqa: BLE001
            g = {"backend_ok": False}
    backend_ok = bool(g.get("backend_ok", True))
    security_ok = not auth_required() or auth_configured()
    body = {
        "ok": backend_ok and security_ok,
        "backend": rt.TORRENT_BACKEND.strip().lower(),
        "backend_ok": backend_ok,
        "auth_configured": auth_configured(),
    }
    if not backend_ok or not security_ok:
        return JSONResponse(body, status_code=503)
    return body


@router.get("/api/public/config")
async def public_config():
    return {
        "public_url": rt.PUBLIC_URL,
        "auth_required": auth_required(),
        "auth_configured": auth_configured(),
        # Needed so a token-only Settings save cannot default-switch the backend.
        "backend": rt.TORRENT_BACKEND.strip().lower(),
    }


@router.get("/api/events/ticket")
async def events_ticket():
    return {"ticket": issue_sse_ticket()}
