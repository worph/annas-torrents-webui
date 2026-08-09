"""FastAPI app: wiring, lifespan, static UI."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import runtime as rt
from .auth import ApiTokenMiddleware, SecurityHeadersMiddleware, auth_required
from .routes import controls, health, provision, settings_routes, space, status, storage, torrents
from .schemas import (  # noqa: F401 — re-export for tests
    ProvisionRequest,
    RateLimitRequest,
    SettingsRequest,
    SpaceFreeRequest,
    SpacePreviewRequest,
    TorrentRemoveRequest,
)

# Re-exports so `from app.main import X` / `from app import main as main_mod` keep working.
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")
DATA_DIR = rt.DATA_DIR
PUBLIC_URL = rt.PUBLIC_URL
_snapshot_cache = rt._snapshot_cache
provision_state = rt.provision_state
_allowed_destination = rt._allowed_destination
_unlink_unadded_torrents = rt._unlink_unadded_torrents
_build_snapshot = rt._build_snapshot
_available_free = provision._available_free
_provision_task_done = provision._provision_task_done


# session property-like access for tests that read/patch main.session —
# prefer patching app.runtime.session; main.session is a snapshot at import.


def __getattr__(name: str):
    """Lazy re-export of runtime attrs (session, TORRENT_BACKEND, …)."""
    if name in {"session", "TORRENT_BACKEND", "QBIT_URL", "QBIT_USER", "QBIT_PASS", "QBIT_CATEGORY"}:
        return getattr(rt, name)
    raise AttributeError(name)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with rt._session_lock:
        await asyncio.to_thread(rt._locked_call, rt.session.load_existing)
    rt._bg_tasks.clear()
    rt._bg_tasks.append(asyncio.create_task(rt.coverage_index.refresh()))
    rt._bg_tasks.append(asyncio.create_task(rt._background_loop()))
    rt._bg_tasks.append(asyncio.create_task(rt._snapshot_loop()))
    yield
    if rt._provision_task and not rt._provision_task.done():
        rt._provision_task.cancel()
        await asyncio.gather(rt._provision_task, return_exceptions=True)
    rt._provision_task = None
    for t in rt._bg_tasks:
        t.cancel()
    if rt._bg_tasks:
        await asyncio.gather(*rt._bg_tasks, return_exceptions=True)
    rt._bg_tasks.clear()
    async with rt._session_lock:
        try:
            await asyncio.to_thread(rt._locked_call, rt.session.save_resume)
        except Exception as e:  # noqa: BLE001
            rt.log.warning("save_resume on shutdown failed: %s", e)
        try:
            await asyncio.to_thread(rt._locked_call, rt.session.close)
        except Exception as e:  # noqa: BLE001
            rt.log.warning("session.close failed: %s", e)



app = FastAPI(
    title="annas-torrents-webui",
    lifespan=lifespan,
    # Hide OpenAPI when private APIs require a token — docs otherwise leak the contract.
    docs_url=None if auth_required() else "/docs",
    redoc_url=None if auth_required() else "/redoc",
    openapi_url=None if auth_required() else "/openapi.json",
)
app.add_middleware(ApiTokenMiddleware)
app.add_middleware(SecurityHeadersMiddleware)  # outermost: headers on 401s too

app.include_router(health.router)
app.include_router(status.router)
app.include_router(storage.router)
app.include_router(space.router)
app.include_router(provision.router)
app.include_router(controls.router)
app.include_router(settings_routes.router)
app.include_router(torrents.router)


def _bake_view_mode_html(html: str) -> str:
    """Ensure <body> carries view-mode so CSS hides private chrome without JS."""
    if 'class="view-mode"' in html:
        return html
    if "<body>" in html:
        return html.replace("<body>", '<body class="view-mode">', 1)
    if "<body " in html:
        return html.replace("<body ", '<body class="view-mode" ', 1)
    return html


# ---- static frontend (mounted last so /api/* wins) ----------------------

if os.path.isdir(FRONTEND_DIR):

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/view")
    async def view():
        # Read-only vantage page: bake view-mode into HTML so CSS hides private
        # chrome before/without JavaScript (API still serves redacted public data).
        path = os.path.join(FRONTEND_DIR, "index.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(_bake_view_mode_html(html))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
