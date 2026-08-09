"""FastAPI app: provisioning, live metrics (SSE), coverage, static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import settings, space, storage
from .auth import (
    ALLOW_UNAUTHENTICATED_API,
    ApiTokenMiddleware,
    auth_configured,
    auth_required,
    issue_sse_ticket,
    redact_snapshot,
)
from .metrics import CoverageIndex
from .selection import download_torrent_files, fetch_torrent_list
from .session import create_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", "/data"))
LISTEN_PORT = int(os.environ.get("TORRENT_PORT", "6881"))
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")
# Public base URL used for share links (e.g. https://seed.example.com).
# When empty, share posts omit the /view link.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# Reuse auth flags (do not re-parse env here — `not x in set` is easy to misread).
if not auth_configured() and not ALLOW_UNAUTHENTICATED_API:
    log.error("API_TOKEN is empty; private APIs are disabled (set API_TOKEN or explicitly opt into local unauthenticated mode)")

# TORRENT_BACKEND=libtorrent (default) | qbittorrent — overridable in Settings UI.
TORRENT_BACKEND_ENV = os.environ.get("TORRENT_BACKEND", "libtorrent")
TORRENT_BACKEND = settings.resolve_backend(DATA_DIR, TORRENT_BACKEND_ENV)
# qBittorrent — used when backend is qbittorrent (settings.json > env)
QBIT_URL_ENV = os.environ.get("QBIT_URL", "http://127.0.0.1:8080")
QBIT_USER_ENV = os.environ.get("QBIT_USER", "admin")
QBIT_PASS_ENV = os.environ.get("QBIT_PASS", "")
QBIT_URL = settings.resolve_qbit_url(DATA_DIR, QBIT_URL_ENV)
QBIT_USER = settings.resolve_qbit_user(DATA_DIR, QBIT_USER_ENV)
QBIT_PASS = QBIT_PASS_ENV if isinstance(QBIT_PASS_ENV, str) else ""
# Env if set; else settings.json / storage.ANNA_FOLDER — see settings.resolve_qbit_category.
QBIT_CATEGORY_ENV = os.environ.get("QBIT_CATEGORY")
QBIT_CATEGORY = settings.resolve_qbit_category(DATA_DIR, QBIT_CATEGORY_ENV)
QBIT_SAVE_PATH = os.environ.get("QBIT_SAVE_PATH", "")
# Extra allowlisted destinations for new torrents (comma/semicolon-separated).
STORAGE_PATHS = storage.parse_storage_paths(os.environ.get("STORAGE_PATHS"))

# Preview tokens for space recovery (confirm must match preview).
_space_tokens: dict[str, dict] = {}
_SPACE_TOKEN_TTL = 600.0
_SPACE_TOKEN_MAX = 64
_space_lock = asyncio.Lock()
_session_lock = asyncio.Lock()
_session_thread_lock = threading.RLock()
_provision_lock = asyncio.Lock()
_provision_task: asyncio.Task | None = None
_picked_paths: set[str] = set()
_PICKED_PATHS_MAX = 64
_picked_paths_lock = threading.Lock()
# Bumped on every live session rebuild so space tokens cannot outlive a switch.
_session_generation = 0
# Cap anonymous /view SSE so a public page cannot open unlimited streams.
_public_sse_count = 0
_PUBLIC_SSE_MAX = 32
_public_sse_per_ip: dict[str, int] = {}
_PUBLIC_SSE_PER_IP_MAX = 4
_sse_lock = asyncio.Lock()
_private_sse_count = 0
_PRIVATE_SSE_MAX = 64


def _new_session(
    backend: str,
    *,
    qbit_url: str,
    qbit_user: str,
    qbit_pass: str,
    qbit_category: str,
):
    return create_session(
        content_dir=os.path.join(DATA_DIR, "content"),
        torrents_dir=os.path.join(DATA_DIR, "torrents"),
        resume_dir=os.path.join(DATA_DIR, "resume"),
        listen_port=LISTEN_PORT,
        backend=backend,
        qbit_url=qbit_url,
        qbit_user=qbit_user,
        qbit_pass=qbit_pass,
        qbit_category=qbit_category,
        qbit_save_path=QBIT_SAVE_PATH or None,
    )


def _session_fingerprint() -> dict:
    """Identity of the live torrent session for space-token binding."""
    return {
        "generation": _session_generation,
        "backend": TORRENT_BACKEND,
        "qbit_url": QBIT_URL if TORRENT_BACKEND == "qbittorrent" else "",
        "qbit_category": QBIT_CATEGORY if TORRENT_BACKEND == "qbittorrent" else "",
    }


session = _new_session(
    TORRENT_BACKEND,
    qbit_url=QBIT_URL,
    qbit_user=QBIT_USER,
    qbit_pass=QBIT_PASS,
    qbit_category=QBIT_CATEGORY,
)
coverage_index = CoverageIndex()

# Typed live-only provisioning state (no DB).
provision_state: dict = {
    "running": False,
    "phase": "idle",  # idle|selecting|downloading|adding|done|error
    "message": "idle",
    "added": 0,
    "failed": 0,
    "requested_tb": None,
    "selected_bytes": 0,
    "started_at": None,
    "finished_at": None,
}

# ponytail: 1s module cache; writes happen under _session_lock in _snapshot_loop.
_snapshot_cache: dict = {"data": None, "json": "{}"}
_bg_tasks: list[asyncio.Task] = []


def _clear_snapshot_cache() -> None:
    """Drop cached status so a backend switch cannot mix old torrents with new controls."""
    _snapshot_cache["data"] = None
    _snapshot_cache["json"] = "{}"


def _locked_call(fn, *args, **kwargs):
    with _session_thread_lock:
        return fn(*args, **kwargs)


async def _call_session(method: str, *args, **kwargs):
    # Capture session under the asyncio lock, then release it for I/O so a slow
    # qBit HTTP call cannot stall unrelated awaiters. Thread lock still serializes
    # the session object itself.
    async with _session_lock:
        sess = session
        gen = _session_generation
    result = await asyncio.to_thread(_locked_call, getattr(sess, method), *args, **kwargs)
    async with _session_lock:
        if session is not sess or _session_generation != gen:
            raise RuntimeError("torrent backend changed")
    return result


async def _call_session_object(sess, method: str, *args, **kwargs):
    async with _session_lock:
        if session is not sess:
            raise RuntimeError("torrent backend changed")
        gen = _session_generation
    result = await asyncio.to_thread(_locked_call, getattr(sess, method), *args, **kwargs)
    async with _session_lock:
        if session is not sess or _session_generation != gen:
            raise RuntimeError("torrent backend changed")
    return result


class ProvisionRequest(BaseModel):
    max_tb: float = Field(gt=0, le=30)
    collections: list[str] | None = None  # filter by "top_level/group" keys; None = all
    save_path: str | None = None  # configured destination or server-picked path
    preallocate: bool = False  # reserve full file size on disk when adding

    @field_validator("max_tb")
    @classmethod
    def _finite_max_tb(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_tb must be a finite number")
        return v


class SettingsRequest(BaseModel):
    torrent_backend: str | None = None
    qbit_category: str | None = None
    qbit_url: str | None = None
    qbit_user: str | None = None
    qbit_pass: str | None = None


class SpacePreviewRequest(BaseModel):
    bytes: int | None = None
    gb: float | None = None
    save_path: str | None = None

    @field_validator("bytes")
    @classmethod
    def _valid_bytes(cls, v: int | None) -> int | None:
        if v is not None and not 0 < v <= 1_000_000 * 1000**3:
            raise ValueError("bytes must be between 1 and 1 PB")
        return v

    @field_validator("gb")
    @classmethod
    def _valid_gb(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or not 0 < v <= 1_000_000):
            raise ValueError("gb must be finite and between 0 and 1,000,000")
        return v


class SpaceFreeRequest(BaseModel):
    infohashes: list[str]
    confirm: bool = False
    save_path: str | None = None
    token: str | None = None
    request_bytes: int | None = None


class TorrentRemoveRequest(BaseModel):
    infohash: str
    confirm: bool = False
    delete_files: bool = True


class RateLimitRequest(BaseModel):
    bytes_per_sec: int = Field(ge=-1)


def _allowed_paths(sess) -> list[str]:
    paths = [o["path"] for o in sess.storage_options(STORAGE_PATHS)]
    try:
        paths.extend(
            (t.get("save_path") or "").strip()
            for t in sess.torrents_status()
            if (t.get("save_path") or "").strip()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not enumerate torrent save paths: %s", e)
    with _picked_paths_lock:
        paths.extend(_picked_paths)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        # Never allowlist bare FS/drive roots — they authorize the whole volume.
        if storage.is_drive_root(path):
            path = storage.anna_destination(path)
        key = storage.path_key(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _allowed_destination(requested: str | None, allowed: list[str]) -> str | None:
    """Return an allowlisted destination, or None when unset. Raises 400 if not allowed.

    An allowlisted drive root becomes ``X:\\Anna's Archive Torrents``. Having only a
    child path allowlisted does not authorize the parent drive/root.
    """
    if not requested or not requested.strip():
        return None
    raw = requested.strip()
    if storage.is_drive_root(raw):
        raw = storage.anna_destination(raw)
    # Always resolve junctions/symlinks (exact lexical match alone can escape).
    if any(storage.path_is_within(raw, p) for p in allowed if p):
        if storage.is_drive_root(raw):
            return storage.anna_destination(raw)
        # Prefer the allowlisted spelling when it matches after normalization.
        exact = next(
            (p for p in allowed if storage.normalize_path(p) == storage.normalize_path(raw)),
            None,
        )
        return exact or raw
    raise HTTPException(status_code=400, detail="save_path is not an allowed destination")


def _resolve_save_path(
    requested: str | None,
    allowed: list[str],
    backend: str,
) -> str | None:
    """Presets / Browse. Same allowlist as space preview, then ensure the directory.

    For qBittorrent the path is sent to qBit as-is (its filesystem) — we do not
    mkdir on this host unless the path is actually local.
    """
    path = _allowed_destination(requested, allowed)
    if path is None:
        return None
    if backend == "qbittorrent":
        # Path is for qBit's filesystem — never mkdir here (dirname('/downloads')
        # is '/' and would create host-local dirs / poison local disk accounting).
        return path
    try:
        return storage.ensure_save_dir(path)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"could not create save_path: {e}") from e


def _json_safe(obj):
    """Make libtorrent floats JSON-safe (NaN/Inf break EventSource JSON.parse)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _dump_snapshot(snap: dict) -> str:
    return json.dumps(_json_safe(snap), allow_nan=False)


def _build_snapshot(sess=None, provision=None) -> dict:
    sess = sess or session
    provision = provision if provision is not None else dict(provision_state)
    batch = hasattr(sess, "begin_status_batch") and hasattr(sess, "end_status_batch")
    try:
        if batch:
            sess.begin_status_batch()
        try:
            g = sess.global_status()
            torrents = sess.torrents_status()
        finally:
            if batch:
                sess.end_status_batch()
        backend_ok = bool(g.get("backend_ok", True))
        connection = "connected" if backend_ok else "degraded"
        return {
            "global": g,
            "torrents": torrents,
            "coverage": coverage_index.coverage_for_torrents(torrents),
            "provision": provision,
            "controls": sess.controls_state(),
            "connection": connection,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("snapshot failed: %s", e)
        return {
            "global": {
                "backend_ok": False,
                "error": str(e),
                "download_rate": 0,
                "upload_rate": 0,
                "num_torrents": 0,
                "disk_free": 0,
                "disk_free_known": False,
                "disk_total": 0,
            },
            "torrents": [],
            "coverage": coverage_index.coverage(set()),
            "provision": provision,
            "controls": {
                "seeding_paused": False,
                "downloads_paused": False,
                "upload_limit": -1,
                "download_limit": -1,
            },
            "connection": "offline",
        }


async def _snapshot_loop() -> None:
    while True:
        try:
            # Build off the asyncio lock so remote I/O cannot stall other routes;
            # only publish when the session generation still matches.
            async with _session_lock:
                sess = session
                gen = _session_generation
                provision = dict(provision_state)
            snap = await asyncio.to_thread(_locked_call, _build_snapshot, sess, provision)
            async with _session_lock:
                if session is sess and _session_generation == gen:
                    _snapshot_cache["data"] = snap
                    _snapshot_cache["json"] = _dump_snapshot(snap)
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot refresh failed: %s", e)
        await asyncio.sleep(1)


async def _background_loop() -> None:
    """Periodically persist resume data and refresh the coverage index."""
    tick = 0
    while True:
        await asyncio.sleep(30)
        try:
            await _call_session("save_resume")
        except Exception as e:  # noqa: BLE001
            log.warning("save_resume failed: %s", e)
        if tick % 120 == 0:  # ~hourly
            await coverage_index.refresh()
        tick += 1


def _unlink_unadded_torrents(
    paths: list[tuple[str, int]],
    added_paths: set[str],
    created_paths: set[str] | None = None,
) -> None:
    """Drop metadata written this run but never added (never delete pre-existing files)."""
    for path, _ in paths:
        abs_path = os.path.abspath(path)
        if abs_path in added_paths:
            continue
        if created_paths is not None and abs_path not in created_paths:
            continue
        try:
            os.unlink(abs_path)
        except OSError:
            pass


async def _available_free(sess, backend: str, dest: str | None) -> int | None:
    """Free bytes for the provision destination, or None when unknown (do not block)."""
    if dest and dest != "multiple destinations":
        if backend == "qbittorrent":
            # Never trust this host's disk_usage for qBit paths — Compose often has
            # a local /data that collides with a remote qBit save path string.
            pass
        elif os.path.isdir(dest) or (os.path.isabs(dest) and os.path.exists(dest)):
            usage = await asyncio.to_thread(storage.disk_usage, dest)
            if usage:
                return usage[2]
        else:
            usage = await asyncio.to_thread(storage.disk_usage, dest)
            if usage:
                return usage[2]
    try:
        status = await _call_session_object(sess, "global_status")
        if not status.get("backend_ok", True):
            return 0
        if status.get("disk_free_known") is False:
            return None
        if status.get("disk_free") is None:
            return None
        # qBit free_space_on_disk is only meaningful for the reported storage path.
        if backend == "qbittorrent" and dest and dest != "multiple destinations":
            reported = (status.get("storage_path") or "").strip()
            if not reported or reported == "multiple destinations":
                return None
            if storage.path_key(reported) != storage.path_key(dest):
                return None
        return max(0, int(status["disk_free"]))
    except Exception:  # noqa: BLE001
        return None


async def _run_provision(req: ProvisionRequest, save_path: str | None, sess) -> None:
    now = time.time()
    provision_state.update(
        running=True,
        phase="selecting",
        message="fetching torrent list",
        added=0,
        failed=0,
        requested_tb=req.max_tb,
        selected_bytes=0,
        started_at=now,
        finished_at=None,
    )
    paths: list[tuple[str, int]] = []
    ordered: list[tuple[str, int]] = []
    created_paths: set[str] = set()
    added_paths: set[str] = set()
    try:
        entries = await fetch_torrent_list(req.max_tb)
        if req.collections:
            wanted = set(req.collections)
            entries = [e for e in entries if e.collection in wanted or e.top_level_group in wanted]
        async with _session_lock:
            if session is not sess:
                raise RuntimeError("torrent backend changed")
            torrents_dir = sess.torrents_dir
            default_path = sess.default_save_path()
            backend = TORRENT_BACKEND
        provision_state.update(
            phase="downloading",
            message=f"downloading {len(entries)} .torrent files",
            selected_bytes=sum(max(0, e.data_size) for e in entries),
        )
        paths, dl_failed, created_paths = await download_torrent_files(entries, torrents_dir)
        failed = dl_failed
        provision_state.update(
            phase="adding",
            message=f"adding {len(paths)} torrents",
            failed=failed,
        )
        added = 0
        dest = save_path or default_path
        margin = 2 * 1000**3
        target_bytes = int(req.max_tb * 1000**4)  # max_tb is decimal TB
        selected_actual = 0
        # Snapshot free space once so rapid adds cannot all pass the same headroom.
        baseline_free = await _available_free(sess, backend, dest)
        # Keep mirror priority: download_torrent_files returns completion order.
        by_path = {os.path.abspath(p): (p, sz) for p, sz in paths}
        ordered: list[tuple[str, int]] = []
        seen_abs: set[str] = set()
        for entry in entries:
            key = (entry.btih or "").lower().strip()
            if len(key) == 40 and all(c in "0123456789abcdef" for c in key):
                cand = os.path.abspath(os.path.join(torrents_dir, f"{key}.torrent"))
                if cand in by_path and cand not in seen_abs:
                    ordered.append(by_path[cand])
                    seen_abs.add(cand)
        for p, sz in paths:
            abs_p = os.path.abspath(p)
            if abs_p not in seen_abs:
                ordered.append((p, sz))
                seen_abs.add(abs_p)
        for p, expected_bytes in ordered:
            if provision_state.get("running") is False:
                break
            # Stop once the locally-validated content size hits the request.
            if selected_actual >= target_bytes:
                _unlink_unadded_torrents(ordered, added_paths, created_paths)
                provision_state.update(
                    phase="done",
                    message=f"added {added} torrents (reached {req.max_tb} TB target)",
                    added=added,
                    failed=failed,
                    selected_bytes=selected_actual,
                    finished_at=time.time(),
                )
                return
            # Prefer live free when known; else fall back to the start-of-add snapshot.
            free = await _available_free(sess, backend, dest)
            # Cumulative reserve against the start-of-run snapshot (rapid adds).
            if baseline_free is not None and selected_actual + expected_bytes + margin > baseline_free:
                _unlink_unadded_torrents(ordered, added_paths, created_paths)
                provision_state.update(
                    phase="done",
                    message=f"stopped: insufficient free space after adding {added} torrents",
                    added=added,
                    failed=failed,
                    selected_bytes=selected_actual,
                    finished_at=time.time(),
                )
                return
            # Also stop if live free can no longer fit the next torrent alone.
            if free is not None and free < expected_bytes + margin:
                _unlink_unadded_torrents(ordered, added_paths, created_paths)
                provision_state.update(
                    phase="done",
                    message=f"stopped: insufficient free space after adding {added} torrents",
                    added=added,
                    failed=failed,
                    selected_bytes=selected_actual,
                    finished_at=time.time(),
                )
                return
            try:
                abs_p = os.path.abspath(p)
                stem = os.path.splitext(os.path.basename(p))[0].lower()
                known = await _call_session_object(sess, "infohashes")
                if stem in known:
                    # Already active — keep metadata, do not count toward the new target.
                    added_paths.add(abs_p)
                    continue
                try:
                    ih = await _call_session_object(
                        sess, "add_torrent_file", p, save_path, preallocate=bool(req.preallocate)
                    )
                except asyncio.CancelledError:
                    # Worker may have finished the add after cancel — keep metadata.
                    added_paths.add(abs_p)
                    raise
                if ih:
                    added_paths.add(abs_p)
                    added += 1
                    selected_actual += max(0, int(expected_bytes))
                else:
                    failed += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.warning("add failed for %s: %s", p, e)
        _unlink_unadded_torrents(ordered, added_paths, created_paths)
        if added == 0 and failed > 0:
            provision_state.update(
                phase="error",
                message=f"error: nothing added ({failed} failed)",
                added=added,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
        elif added == 0:
            provision_state.update(
                phase="done",
                message="nothing added — seeding continues",
                added=0,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
        else:
            provision_state.update(
                phase="done",
                message=f"added {added} torrents" + (f" ({failed} failed)" if failed else ""),
                added=added,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
    except asyncio.CancelledError:
        # Keep metadata only for torrents that actually made it into the session
        # (in-flight add may finish in a worker thread after cancel).
        try:
            known = await asyncio.to_thread(_locked_call, sess.infohashes)
        except Exception:  # noqa: BLE001
            known = set()
        keep = set(added_paths)
        for path_i, _sz in (ordered if ordered else paths):
            key = os.path.splitext(os.path.basename(path_i))[0].lower()
            if key in known:
                keep.add(os.path.abspath(path_i))
        _unlink_unadded_torrents(ordered if ordered else paths, keep, created_paths)
        provision_state.update(
            phase="error",
            message="error: provisioning cancelled",
            finished_at=time.time(),
        )
        raise
    except Exception as e:  # noqa: BLE001
        _unlink_unadded_torrents(paths, added_paths, created_paths)
        provision_state.update(
            phase="error",
            message=f"error: {e}",
            finished_at=time.time(),
        )
        log.exception("provisioning failed")
    finally:
        if hasattr(sess, "_restore_preallocate"):
            try:
                await _call_session_object(sess, "_restore_preallocate")
            except Exception as e:  # noqa: BLE001
                log.warning("restore preallocate failed: %s", e)
        provision_state["running"] = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with _session_lock:
        await asyncio.to_thread(_locked_call, session.load_existing)
    _bg_tasks.clear()
    _bg_tasks.append(asyncio.create_task(coverage_index.refresh()))
    _bg_tasks.append(asyncio.create_task(_background_loop()))
    _bg_tasks.append(asyncio.create_task(_snapshot_loop()))
    yield
    global _provision_task
    if _provision_task and not _provision_task.done():
        _provision_task.cancel()
        await asyncio.gather(_provision_task, return_exceptions=True)
    _provision_task = None
    for t in _bg_tasks:
        t.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _bg_tasks.clear()
    async with _session_lock:
        try:
            await asyncio.to_thread(_locked_call, session.save_resume)
        except Exception as e:  # noqa: BLE001
            log.warning("save_resume on shutdown failed: %s", e)
        try:
            await asyncio.to_thread(_locked_call, session.close)
        except Exception as e:  # noqa: BLE001
            log.warning("session.close failed: %s", e)


app = FastAPI(
    title="annas-torrents-webui",
    lifespan=lifespan,
    # Hide OpenAPI when private APIs require a token — docs otherwise leak the contract.
    docs_url=None if auth_required() else "/docs",
    redoc_url=None if auth_required() else "/redoc",
    openapi_url=None if auth_required() else "/openapi.json",
)
app.add_middleware(ApiTokenMiddleware)


@app.get("/api/healthz")
async def healthz():
    """Public liveness — process is up. Use for Docker HEALTHCHECK."""
    return {"ok": True}


@app.get("/api/health")
async def health():
    g = (_snapshot_cache.get("data") or {}).get("global") or {}
    if not g:
        try:
            g = await _call_session("global_status")
        except Exception:  # noqa: BLE001
            g = {"backend_ok": False}
    backend_ok = bool(g.get("backend_ok", True))
    security_ok = not auth_required() or auth_configured()
    body = {
        "ok": backend_ok and security_ok,
        "backend": TORRENT_BACKEND.strip().lower(),
        "backend_ok": backend_ok,
        "auth_configured": auth_configured(),
    }
    if not backend_ok or not security_ok:
        return JSONResponse(body, status_code=503)
    return body


@app.get("/api/public/config")
async def public_config():
    return {
        "public_url": PUBLIC_URL,
        "auth_required": auth_required(),
        "auth_configured": auth_configured(),
        # Needed so a token-only Settings save cannot default-switch the backend.
        "backend": TORRENT_BACKEND.strip().lower(),
    }


@app.get("/api/events/ticket")
async def events_ticket():
    return {"ticket": issue_sse_ticket()}


def _provision_task_done(task: asyncio.Task) -> None:
    """Safety net when cancel wins before ``_run_provision`` runs (no ``finally``)."""
    if not provision_state.get("running"):
        return
    provision_state["running"] = False
    if task.cancelled() and provision_state.get("finished_at") is None:
        provision_state.update(
            phase="error",
            message="error: provisioning cancelled",
            finished_at=time.time(),
        )


@app.post("/api/provision")
async def provision(req: ProvisionRequest):
    global _provision_task
    async with _provision_lock:
        if provision_state["running"] or (_provision_task and not _provision_task.done()):
            return {"ok": False, "message": "provisioning already in progress"}
        async with _session_lock:
            sess = session
            gen = _session_generation
        allowed = await asyncio.to_thread(_locked_call, _allowed_paths, sess)
        async with _session_lock:
            if session is not sess or _session_generation != gen:
                return {"ok": False, "message": "torrent backend changed"}
            save_path = _resolve_save_path(req.save_path, allowed, TORRENT_BACKEND)
            provision_state["running"] = True
            _provision_task = asyncio.create_task(_run_provision(req, save_path, sess))
            _provision_task.add_done_callback(_provision_task_done)
    return {"ok": True, "message": "provisioning started"}


@app.post("/api/provision/cancel")
async def provision_cancel():
    """Stop an in-flight provision; already-added torrents keep seeding."""
    global _provision_task
    async with _provision_lock:
        task = _provision_task
        if not task or task.done():
            return {"ok": False, "message": "no provisioning in progress"}
        task.cancel()
    return {"ok": True, "message": "provisioning cancel requested"}


@app.get("/api/provision")
async def provision_status():
    return provision_state


@app.get("/api/storage")
async def storage_api():
    """Preset download destinations (default = repo DATA_DIR/content)."""
    async with _session_lock:
        sess = session
        gen = _session_generation
        backend = TORRENT_BACKEND
    options = await asyncio.to_thread(_locked_call, sess.storage_options, STORAGE_PATHS)
    seen = {storage.normalize_path(o["path"]) for o in options}
    for t in await asyncio.to_thread(_locked_call, sess.torrents_status):
        sp = (t.get("save_path") or "").strip()
        if not sp:
            continue
        key = storage.normalize_path(sp)
        if key in seen:
            continue
        seen.add(key)
        maker = storage.remote_option if backend == "qbittorrent" else storage.option
        options.append(maker(sp, label=f"In use · {sp}"))
    with _picked_paths_lock:
        picked_snapshot = sorted(_picked_paths)
    for picked in picked_snapshot:
        key = storage.normalize_path(picked)
        if key in seen:
            continue
        seen.add(key)
        maker = storage.remote_option if backend == "qbittorrent" else storage.option
        options.append(maker(picked, label=f"Browse · {picked}"))
    default = next((o["path"] for o in options if o.get("default")), options[0]["path"] if options else "")
    g = await asyncio.to_thread(_locked_call, sess.global_status)
    async with _session_lock:
        if session is not sess or _session_generation != gen:
            raise HTTPException(status_code=503, detail="torrent backend changed")
    return {
        "options": options,
        "default": default,
        "active": g.get("storage_path") or default,
        "backend": backend,
    }


@app.post("/api/storage/pick")
async def storage_pick():
    """Native OS folder dialog (separate GUI process on the host)."""
    if TORRENT_BACKEND == "qbittorrent":
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
    with _picked_paths_lock:
        _picked_paths.add(path)
        while len(_picked_paths) > _PICKED_PATHS_MAX:
            _picked_paths.pop()
    return {"ok": True, "cancelled": False, "path": path}


@app.get("/api/collections")
async def collections(max_tb: float = Query(default=1.0, gt=0, le=30)):
    """Available collections for a target, so the UI can offer filters."""
    if not math.isfinite(max_tb):
        raise HTTPException(status_code=400, detail="max_tb must be finite")
    try:
        entries = await fetch_torrent_list(max_tb)
    except Exception as e:  # noqa: BLE001
        log.warning("collections fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="could not fetch torrent collections") from e
    agg: dict[str, dict] = {}
    for e in entries:
        a = agg.setdefault(e.collection, {"collection": e.collection, "count": 0, "bytes": 0})
        a["count"] += 1
        a["bytes"] += e.data_size
    return sorted(agg.values(), key=lambda x: -x["bytes"])


def _filter_torrents_by_save_path(torrents: list[dict], save_path: str | None) -> list[dict]:
    if not save_path or not save_path.strip():
        return torrents
    return [t for t in torrents if storage.matches_destination(t.get("save_path"), save_path)]


@app.post("/api/space/preview")
async def space_preview(req: SpacePreviewRequest):
    if req.bytes is None and req.gb is None:
        raise HTTPException(status_code=400, detail="provide bytes or gb")
    request_bytes = int(req.bytes) if req.bytes is not None else int(float(req.gb) * 1000**3)
    if request_bytes <= 0 or request_bytes > 1_000_000 * 1000**3:
        raise HTTPException(status_code=400, detail="request must be positive")
    async with _session_lock:
        sess = session
        backend = TORRENT_BACKEND
        fingerprint = _session_fingerprint()
        allowed = await asyncio.to_thread(_locked_call, _allowed_paths, sess)
        torrents = await asyncio.to_thread(_locked_call, sess.torrents_status)
        default_path = (await asyncio.to_thread(_locked_call, sess.default_save_path) or "").strip()
    requested = (req.save_path or "").strip() or None
    # Empty qBit default would otherwise free across every category torrent.
    if not requested and not default_path:
        raise HTTPException(
            status_code=400,
            detail="choose a download destination before freeing space",
        )
    save_path = _allowed_destination(requested or default_path or None, allowed)
    if not save_path:
        raise HTTPException(
            status_code=400,
            detail="choose a download destination before freeing space",
        )
    torrents = _filter_torrents_by_save_path(torrents, save_path)
    result = space.pick_combination(torrents, request_bytes)
    token = secrets.token_urlsafe(16)
    now = time.time()
    async with _space_lock:
        # Drop expired tokens (cheap; few previews) under the same lock as free/consume.
        expired = [k for k, v in _space_tokens.items() if v.get("expires", 0) < now]
        for k in expired:
            _space_tokens.pop(k, None)
        while len(_space_tokens) >= _SPACE_TOKEN_MAX:
            oldest = min(_space_tokens, key=lambda k: _space_tokens[k].get("expires", 0))
            _space_tokens.pop(oldest, None)
        _space_tokens[token] = {
            "hashes": {s["infohash"].lower() for s in result["selected"] if s.get("infohash")},
            "save_path": save_path,
            "request_bytes": request_bytes,
            "backend": backend,
            "fingerprint": fingerprint,
            "expires": now + _SPACE_TOKEN_TTL,
            "consuming": False,
        }
    result["token"] = token
    result["request_bytes"] = request_bytes
    result["save_path"] = save_path
    return result


@app.post("/api/space/free")
async def space_free(req: SpaceFreeRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    if not req.token:
        raise HTTPException(status_code=400, detail="preview token required")
    async with _space_lock:
        entry = _space_tokens.get(req.token)
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
    async with _space_lock:
        current = _space_tokens.get(req.token)
        if current is not entry or entry.get("consuming"):
            raise HTTPException(status_code=400, detail="preview is already being used")
        entry["consuming"] = True
    try:
        # Fingerprint + delete under the same session lock so a concurrent
        # backend switch cannot sneak between the check and remove_torrents.
        async with _session_lock:
            if entry.get("fingerprint") != _session_fingerprint():
                raise HTTPException(
                    status_code=400,
                    detail="preview expired — backend or session changed, run Preview again",
                )
            sess = session
            known = {h.lower() for h in await asyncio.to_thread(_locked_call, sess.infohashes)}
            bad = [h for h in hashes if h not in known]
            if bad:
                raise HTTPException(status_code=400, detail=f"unknown infohashes: {bad[:5]}")
            # Abort before any mutation when selected torrents share content with
            # each other or with remaining torrents — reclaim assumes file delete.
            torrents = await asyncio.to_thread(_locked_call, sess.torrents_status)
            by_ih = {t["infohash"].lower(): t for t in torrents if t.get("infohash")}
            if req_sp:
                for h in hashes:
                    t = by_ih.get(h)
                    if not t or not storage.matches_destination(t.get("save_path"), req_sp):
                        raise HTTPException(status_code=400, detail=f"torrent not on destination: {h[:12]}")
            from .pathsafety import shared_content_ids

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
            result = await asyncio.to_thread(_locked_call, sess.remove_torrents, hashes, True)
    except Exception:
        async with _space_lock:
            if req.token in _space_tokens:
                _space_tokens[req.token]["consuming"] = False
        raise
    async with _space_lock:
        _space_tokens.pop(req.token, None)
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
    return {
        "ok": True,
        "removed": removed,
        "files_deleted": files_deleted,
    }


@app.post("/api/torrents/remove")
async def torrents_remove(req: TorrentRemoveRequest):
    """Remove one torrent (and optionally its files) after explicit confirm."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    ih = (req.infohash or "").strip().lower()
    if not ih or len(ih) < 32:
        raise HTTPException(status_code=400, detail="infohash required")
    async with _session_lock:
        sess = session
        known = {h.lower() for h in await asyncio.to_thread(_locked_call, sess.infohashes)}
        if ih not in known:
            raise HTTPException(status_code=404, detail="torrent not found")
        result = await asyncio.to_thread(
            _locked_call, sess.remove_torrents, [ih], req.delete_files
        )
    removed = int((result or {}).get("removed") or 0)
    files_deleted = (result or {}).get("files_deleted")
    if removed == 0:
        raise HTTPException(
            status_code=409,
            detail="removal incomplete — torrent could not be removed",
        )
    return {
        "ok": True,
        "removed": removed,
        "infohash": ih,
        "delete_files": req.delete_files,
        "files_deleted": files_deleted if req.delete_files else None,
    }


def _patch_controls_cache(ctrl: dict) -> dict:
    """Keep SSE/status controls in sync immediately after control POSTs."""
    data = _snapshot_cache.get("data")
    if isinstance(data, dict):
        data = {**data, "controls": ctrl}
        _snapshot_cache["data"] = data
        try:
            _snapshot_cache["json"] = _dump_snapshot(data)
        except Exception:  # noqa: BLE001
            pass
    return ctrl


async def _apply_control(method: str, *args):
    async with _session_lock:
        sess = session
        await asyncio.to_thread(_locked_call, getattr(sess, method), *args)
        ctrl = await asyncio.to_thread(_locked_call, sess.controls_state)
    return {"ok": True, "controls": _patch_controls_cache(ctrl)}


@app.post("/api/controls/pause")
async def controls_pause():
    return await _apply_control("pause_all")


@app.post("/api/controls/resume")
async def controls_resume():
    return await _apply_control("resume_all")


@app.post("/api/controls/upload-limit")
async def controls_upload_limit(req: RateLimitRequest):
    body = await _apply_control("set_upload_limit", req.bytes_per_sec)
    body["bytes_per_sec"] = req.bytes_per_sec
    return body


@app.post("/api/controls/pause-downloads")
async def controls_pause_downloads():
    return await _apply_control("pause_downloads")


@app.post("/api/controls/resume-downloads")
async def controls_resume_downloads():
    return await _apply_control("resume_downloads")


@app.post("/api/controls/download-limit")
async def controls_download_limit(req: RateLimitRequest):
    body = await _apply_control("set_download_limit", req.bytes_per_sec)
    body["bytes_per_sec"] = req.bytes_per_sec
    return body


@app.get("/api/config")
async def config():
    """Client config: share URL, backend, qBit connection fields."""
    async with _session_lock:
        cat = getattr(session, "category", None) or settings.resolve_qbit_category(DATA_DIR, QBIT_CATEGORY_ENV)
    try:
        default_url = settings._clean_qbit_url(QBIT_URL_ENV) if QBIT_URL_ENV else settings.DEFAULT_QBIT_URL
    except ValueError:
        default_url = settings.DEFAULT_QBIT_URL
    return {
        "public_url": PUBLIC_URL,
        "auth_required": auth_required(),
        "backend": TORRENT_BACKEND,
        "backends": list(settings.BACKENDS),
        "qbit_category": cat,
        "qbit_url": QBIT_URL,
        "qbit_user": QBIT_USER,
        "qbit_pass_set": bool(QBIT_PASS),
        "defaults": {
            "qbit_category": storage.ANNA_FOLDER,
            "qbit_url": default_url,
            "qbit_user": QBIT_USER_ENV or "admin",
        },
    }


@app.put("/api/settings")
async def put_settings(req: SettingsRequest):
    """Validate + try new session before persisting settings.json.

    The full replace (validate → persist → session swap) holds ``_session_lock``
    so concurrent settings updates cannot race the live session and disk file.
    """
    global session, TORRENT_BACKEND, QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_CATEGORY, _session_generation

    patch: dict = {}
    if req.torrent_backend is not None:
        patch["torrent_backend"] = req.torrent_backend
    if req.qbit_category is not None:
        patch["qbit_category"] = req.qbit_category
    if req.qbit_url is not None:
        patch["qbit_url"] = req.qbit_url
    if req.qbit_user is not None:
        patch["qbit_user"] = req.qbit_user
    if req.qbit_pass is not None:
        patch["qbit_pass"] = req.qbit_pass
    if not patch:
        raise HTTPException(status_code=400, detail="no settings provided")

    async with _session_lock:
        if provision_state.get("running"):
            raise HTTPException(status_code=409, detail="cannot change settings while provisioning")
        current_session = session
        current_backend = TORRENT_BACKEND
        current_url = QBIT_URL
        current_user = QBIT_USER
        current_pass = QBIT_PASS
        current_category = QBIT_CATEGORY

        try:
            merged = await asyncio.to_thread(
                settings.apply_patch, settings.load_settings(DATA_DIR), patch
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Never revive a stale settings.json backend when the user did not ask to switch.
        if "torrent_backend" in patch:
            new_backend = settings.resolve_from(merged, "torrent_backend", TORRENT_BACKEND_ENV)
            if (
                patch.get("torrent_backend")
                and settings.normalize_backend(patch["torrent_backend"]) == "libtorrent"
                and not settings._libtorrent_available()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="libtorrent is not installed in this image — rebuild with TORRENT_BACKEND=libtorrent",
                )
        else:
            new_backend = current_backend
            # Scrub unavailable persisted libtorrent so later loads match runtime.
            if settings.normalize_backend(merged.get("torrent_backend")) == "libtorrent":
                if not settings._libtorrent_available():
                    patch = {**patch, "torrent_backend": "qbittorrent"}
                    merged["torrent_backend"] = "qbittorrent"
        new_url = settings.resolve_from(merged, "qbit_url", QBIT_URL_ENV)
        new_user = settings.resolve_from(merged, "qbit_user", QBIT_USER_ENV)
        # Omit password field → keep runtime password; "" clears without falling back to env.
        new_pass = current_pass if req.qbit_pass is None else req.qbit_pass
        new_cat = settings.resolve_from(merged, "qbit_category", QBIT_CATEGORY_ENV)

        conn_changed = (
            new_backend != current_backend
            or new_url != current_url
            or new_user != current_user
            or new_pass != current_pass
        )
        cat_only = (
            not conn_changed
            and new_backend == "qbittorrent"
            and new_cat != current_category
            and "qbit_category" in patch
        )

        if cat_only and hasattr(current_session, "set_category"):
            try:
                if hasattr(current_session, "verify"):
                    # Point at the new category before verify so create/check matches the save.
                    await asyncio.to_thread(_locked_call, current_session.set_category, new_cat)
                    await asyncio.to_thread(_locked_call, current_session.verify)
                else:
                    await asyncio.to_thread(_locked_call, current_session.set_category, new_cat)
                settings.save_settings(DATA_DIR, patch)
            except (ValueError, OSError, RuntimeError) as e:
                try:
                    await asyncio.to_thread(_locked_call, current_session.set_category, current_category)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                try:
                    await asyncio.to_thread(_locked_call, current_session.set_category, current_category)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=f"could not reach qBittorrent: {e}") from e
            QBIT_CATEGORY = new_cat
            _session_generation += 1
            _clear_snapshot_cache()
            return {"ok": True, "backend": TORRENT_BACKEND, "qbit_category": new_cat, "rebuilt": False}

        need_rebuild = conn_changed or (
            new_backend == "qbittorrent" and "qbit_category" in patch and new_cat != current_category
        )
        if need_rebuild:
            new_sess = None
            try:
                new_sess = _new_session(
                    new_backend,
                    qbit_url=new_url,
                    qbit_user=new_user,
                    qbit_pass=new_pass,
                    qbit_category=new_cat,
                )
                if hasattr(new_sess, "verify"):
                    await asyncio.to_thread(_locked_call, new_sess.verify)
                # qBit→libtorrent: do not auto-add every cached .torrent (would re-download
                # while qBit may still be seeding the same content). Startup load_existing
                # still resumes libtorrent's own torrents after a normal restart.
                if not (current_backend == "qbittorrent" and new_backend == "libtorrent"):
                    await asyncio.to_thread(_locked_call, new_sess.load_existing)
                else:
                    log.warning(
                        "switched qbittorrent→libtorrent without importing torrents_dir; "
                        "stop qBit or re-provision if you want libtorrent to seed them"
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("failed to switch backend")
                if new_sess is not None:
                    try:
                        await asyncio.to_thread(_locked_call, new_sess.close)
                    except Exception:  # noqa: BLE001
                        pass
                raise HTTPException(status_code=400, detail=f"could not switch backend: {e}") from e
            try:
                settings.save_settings(DATA_DIR, patch)
            except (ValueError, OSError) as e:
                try:
                    await asyncio.to_thread(_locked_call, new_sess.close)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=str(e)) from e
            old = session
            session = new_sess
            TORRENT_BACKEND = new_backend
            QBIT_URL = new_url
            QBIT_USER = new_user
            QBIT_PASS = new_pass
            QBIT_CATEGORY = new_cat
            _session_generation += 1
            _clear_snapshot_cache()
            try:
                if hasattr(old, "save_resume"):
                    await asyncio.to_thread(_locked_call, old.save_resume)
            except Exception as e:  # noqa: BLE001
                log.warning("old session.save_resume failed: %s", e)
            try:
                await asyncio.to_thread(_locked_call, old.close)
            except Exception as e:  # noqa: BLE001
                log.warning("old session.close failed: %s", e)
            return {
                "ok": True,
                "backend": TORRENT_BACKEND,
                "qbit_category": QBIT_CATEGORY,
                "qbit_url": QBIT_URL,
                "qbit_user": QBIT_USER,
                "qbit_pass_set": bool(QBIT_PASS),
                "rebuilt": True,
            }

        try:
            settings.save_settings(DATA_DIR, patch)
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        TORRENT_BACKEND = new_backend
        QBIT_CATEGORY = new_cat
        return {
            "ok": True,
            "backend": TORRENT_BACKEND,
            "qbit_category": QBIT_CATEGORY,
            "qbit_url": QBIT_URL,
            "qbit_user": QBIT_USER,
            "qbit_pass_set": bool(QBIT_PASS),
            "rebuilt": False,
        }


@app.get("/api/status")
async def status():
    # Snapshot + controls from the same session generation; release the asyncio
    # lock during I/O so a slow backend cannot stall unrelated routes.
    async with _session_lock:
        sess = session
        gen = _session_generation
        cached = _snapshot_cache["data"]
        snap = dict(cached) if cached is not None else None
    if snap is None:
        snap = await asyncio.to_thread(
            _locked_call, _build_snapshot, sess, dict(provision_state)
        )
    controls = await asyncio.to_thread(_locked_call, sess.controls_state)
    async with _session_lock:
        if session is not sess or _session_generation != gen:
            raise HTTPException(status_code=503, detail="torrent backend changed")
    snap["controls"] = controls
    return snap


@app.get("/api/public/status")
async def public_status():
    if _snapshot_cache["data"] is not None:
        snap = dict(_snapshot_cache["data"])
    else:
        async with _session_lock:
            snap = await asyncio.to_thread(
                _locked_call, _build_snapshot, session, dict(provision_state)
            )
    return JSONResponse(
        redact_snapshot(snap),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/events")
async def events():
    global _private_sse_count
    async with _sse_lock:
        if _private_sse_count >= _PRIVATE_SSE_MAX:
            raise HTTPException(status_code=503, detail="too many event connections")
        _private_sse_count += 1

    async def gen():
        global _private_sse_count
        try:
            while True:
                if _snapshot_cache["data"] is None:
                    await asyncio.sleep(0.2)
                    continue
                payload = _snapshot_cache["json"]
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1)
        finally:
            async with _sse_lock:
                _private_sse_count = max(0, _private_sse_count - 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/public/events")
async def public_events(request: Request):
    global _public_sse_count
    client_ip = (request.client.host if request.client else "") or "unknown"
    async with _sse_lock:
        if _public_sse_count >= _PUBLIC_SSE_MAX:
            raise HTTPException(status_code=503, detail="too many public event connections")
        if _public_sse_per_ip.get(client_ip, 0) >= _PUBLIC_SSE_PER_IP_MAX:
            raise HTTPException(status_code=503, detail="too many public event connections from this client")
        _public_sse_count += 1
        _public_sse_per_ip[client_ip] = _public_sse_per_ip.get(client_ip, 0) + 1

    async def gen():
        global _public_sse_count
        try:
            while True:
                if _snapshot_cache["data"] is None:
                    await asyncio.sleep(0.2)
                    continue
                payload = _dump_snapshot(redact_snapshot(_snapshot_cache["data"]))
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1)
        finally:
            async with _sse_lock:
                _public_sse_count = max(0, _public_sse_count - 1)
                left = _public_sse_per_ip.get(client_ip, 1) - 1
                if left <= 0:
                    _public_sse_per_ip.pop(client_ip, None)
                else:
                    _public_sse_per_ip[client_ip] = left

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- static frontend (mounted last so /api/* wins) ----------------------

def _bake_view_mode_html(html: str) -> str:
    """Ensure <body> carries view-mode so CSS hides private chrome without JS."""
    if 'class="view-mode"' in html:
        return html
    if "<body>" in html:
        return html.replace("<body>", '<body class="view-mode">', 1)
    if "<body " in html:
        return html.replace("<body ", '<body class="view-mode" ', 1)
    return html


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
