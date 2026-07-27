"""FastAPI app: provisioning, live metrics (SSE), coverage, static UI. No auth."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .metrics import CoverageIndex
from .selection import download_torrent_files, fetch_torrent_list
from .session import create_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
LISTEN_PORT = int(os.environ.get("TORRENT_PORT", "6881"))
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")
# Public base URL used for share links (e.g. https://seed.example.com). When
# empty, the frontend falls back to the browser's own origin.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# TORRENT_BACKEND=libtorrent (default) | qbittorrent
TORRENT_BACKEND = os.environ.get("TORRENT_BACKEND", "libtorrent")
# qBittorrent — only used when TORRENT_BACKEND=qbittorrent
QBIT_URL = os.environ.get("QBIT_URL", "http://host.docker.internal:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
QBIT_CATEGORY = os.environ.get("QBIT_CATEGORY", "Anna's Archive")
QBIT_SAVE_PATH = os.environ.get("QBIT_SAVE_PATH", "")

session = create_session(
    content_dir=os.path.join(DATA_DIR, "content"),
    torrents_dir=os.path.join(DATA_DIR, "torrents"),
    resume_dir=os.path.join(DATA_DIR, "resume"),
    listen_port=LISTEN_PORT,
    backend=TORRENT_BACKEND,
    qbit_url=QBIT_URL,
    qbit_user=QBIT_USER,
    qbit_pass=QBIT_PASS,
    qbit_category=QBIT_CATEGORY,
    qbit_save_path=QBIT_SAVE_PATH or None,
)
coverage_index = CoverageIndex()

# Simple in-memory provisioning state (live-only, no DB).
provision_state: dict = {"running": False, "message": "idle", "added": 0, "target_tb": None}


class ProvisionRequest(BaseModel):
    max_tb: float
    collections: list[str] | None = None  # filter by "top_level/group" keys; None = all


async def _background_loop() -> None:
    """Periodically persist resume data and refresh the coverage index."""
    tick = 0
    while True:
        await asyncio.sleep(30)
        try:
            session.save_resume()
        except Exception as e:  # noqa: BLE001
            log.warning("save_resume failed: %s", e)
        if tick % 120 == 0:  # ~hourly
            await coverage_index.refresh()
        tick += 1


async def _run_provision(req: ProvisionRequest) -> None:
    provision_state.update(running=True, message="fetching torrent list", added=0, target_tb=req.max_tb)
    try:
        entries = await fetch_torrent_list(req.max_tb)
        if req.collections:
            wanted = set(req.collections)
            entries = [e for e in entries if e.collection in wanted or e.top_level_group in wanted]
        provision_state["message"] = f"downloading {len(entries)} .torrent files"
        paths = await download_torrent_files(entries, session.torrents_dir)
        added = 0
        for p in paths:
            try:
                if session.add_torrent_file(p):
                    added += 1
            except Exception as e:  # noqa: BLE001
                log.warning("add failed for %s: %s", p, e)
        provision_state.update(message=f"added {added} torrents", added=added)
    except Exception as e:  # noqa: BLE001
        provision_state["message"] = f"error: {e}"
        log.exception("provisioning failed")
    finally:
        provision_state["running"] = False


app = FastAPI(title="annas-torrents-webui")


@app.on_event("startup")
async def _startup() -> None:
    session.load_existing()
    asyncio.create_task(coverage_index.refresh())
    asyncio.create_task(_background_loop())


@app.post("/api/provision")
async def provision(req: ProvisionRequest):
    if provision_state["running"]:
        return {"ok": False, "message": "provisioning already in progress"}
    asyncio.create_task(_run_provision(req))
    return {"ok": True, "message": "provisioning started"}


@app.get("/api/provision")
async def provision_status():
    return provision_state


@app.get("/api/collections")
async def collections(max_tb: float = 1.0):
    """Available collections for a target, so the UI can offer filters."""
    entries = await fetch_torrent_list(max_tb)
    agg: dict[str, dict] = {}
    for e in entries:
        a = agg.setdefault(e.collection, {"collection": e.collection, "count": 0, "bytes": 0})
        a["count"] += 1
        a["bytes"] += e.data_size
    return sorted(agg.values(), key=lambda x: -x["bytes"])


def _snapshot() -> dict:
    return {
        "global": session.global_status(),
        "torrents": session.torrents_status(),
        "coverage": coverage_index.coverage(session.infohashes()),
        "provision": provision_state,
    }


@app.get("/api/config")
async def config():
    """Client config: the public base URL for share links (may be empty)."""
    return {"public_url": PUBLIC_URL}


@app.get("/api/status")
async def status():
    return _snapshot()


@app.get("/api/events")
async def events():
    async def gen():
        while True:
            data = json.dumps(_snapshot())
            yield f"data: {data}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- static frontend (mounted last so /api/* wins) ----------------------

if os.path.isdir(FRONTEND_DIR):

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/view")
    async def view():
        # Read-only "vantage" page — same SPA, rendered in view mode client-side.
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
