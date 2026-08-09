"""API token auth and public (/view) snapshot redaction."""

from __future__ import annotations

import os
import secrets
import time
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

API_TOKEN = (os.environ.get("API_TOKEN") or "").strip()
ALLOW_UNAUTHENTICATED_API = (os.environ.get("ALLOW_UNAUTHENTICATED_API") or "").strip().lower() in {
    "1",
    "true",
    "yes",
}

# Paths that never require a token.
_PUBLIC_PREFIXES = ("/api/public/",)
_PUBLIC_EXACT = {"/", "/view", "/favicon.ico"}
_SSE_TICKET_TTL = 60.0
_SSE_TICKET_MAX = 64
_SSE_TICKETS: dict[str, float] = {}


def auth_required() -> bool:
    return bool(API_TOKEN) or not ALLOW_UNAUTHENTICATED_API


def auth_configured() -> bool:
    return bool(API_TOKEN)


def token_ok(request: Request) -> bool:
    if not API_TOKEN:
        return ALLOW_UNAUTHENTICATED_API
    got = (request.headers.get("X-API-Token") or "").strip()
    if not got:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    if not got or len(got) != len(API_TOKEN):
        return False
    return secrets.compare_digest(got, API_TOKEN)


def issue_sse_ticket() -> str | None:
    """Create a short-lived, one-use ticket for browsers that cannot set SSE headers."""
    if not API_TOKEN:
        return None
    now = time.monotonic()
    for ticket, expires in list(_SSE_TICKETS.items()):
        if expires <= now:
            _SSE_TICKETS.pop(ticket, None)
    # Cap outstanding tickets so a stolen token cannot fill memory unboundedly.
    while len(_SSE_TICKETS) >= _SSE_TICKET_MAX:
        oldest = min(_SSE_TICKETS, key=_SSE_TICKETS.get)
        _SSE_TICKETS.pop(oldest, None)
    ticket = secrets.token_urlsafe(24)
    _SSE_TICKETS[ticket] = now + _SSE_TICKET_TTL
    return ticket


def _sse_ticket_ok(request: Request) -> bool:
    if request.method.upper() != "GET" or request.url.path != "/api/events":
        return False
    ticket = (request.query_params.get("ticket") or "").strip()
    if not ticket:
        return False
    expires = _SSE_TICKETS.pop(ticket, None)
    return bool(expires and expires > time.monotonic())


def _is_public_path(path: str) -> bool:
    import posixpath

    # Normalize so /api/public/../config cannot prefix-match as public.
    path = posixpath.normpath(path or "/")
    if not path.startswith("/"):
        path = "/" + path
    if path in _PUBLIC_EXACT:
        return True
    if path.startswith("/assets/") or path.startswith("/static/"):
        return True
    if path == "/api/healthz":
        return True
    return any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


class ApiTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if _is_public_path(path) or not path.startswith("/api/"):
            return await call_next(request)
        if not API_TOKEN and not ALLOW_UNAUTHENTICATED_API:
            return JSONResponse(
                {"detail": "API_TOKEN must be configured"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        if not token_ok(request) and not _sse_ticket_ok(request):
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


def redact_snapshot(snap: dict) -> dict:
    """Strip hashes, paths, and torrent names from a live snapshot.

    Pause/limit aggregates remain so /view can show whether the seedbox is
    actively transferring without exposing private controls UI.
    """
    g = snap.get("global") or {}
    raw_coverage = snap.get("coverage") or {}
    coverage = {
        key: raw_coverage[key]
        for key in ("seeded_bytes", "total_bytes", "percent", "index_ready")
        if key in raw_coverage
    }
    return {
        "connection": snap.get("connection"),
        "coverage": coverage,
        "global": {
            "download_rate": g.get("download_rate", 0),
            "upload_rate": g.get("upload_rate", 0),
            "total_upload": g.get("total_upload", 0),
            "total_download": g.get("total_download", 0),
            "committed_bytes": g.get("committed_bytes", 0),
            "disk_free": g.get("disk_free", 0),
            "disk_free_known": g.get("disk_free_known", False),
            "disk_total": g.get("disk_total", 0),
            "num_torrents": g.get("num_torrents", 0),
            "num_peers": g.get("num_peers", 0),
            "backend_ok": g.get("backend_ok", True),
        },
        # Aggregates only — no per-torrent names or swarm detail on /view.
        "torrents": [],
        "provision": {"running": False, "phase": "idle", "message": "idle"},
        "controls": {
            "seeding_paused": False,
            "downloads_paused": False,
            "upload_limit": -1,
            "download_limit": -1,
        },
        "public": True,
    }
