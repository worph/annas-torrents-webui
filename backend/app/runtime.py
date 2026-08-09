"""Shared mutable app state and session helpers used by routes."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time

from fastapi import HTTPException

from . import settings, storage
from .auth import ALLOW_UNAUTHENTICATED_API, auth_configured
from .metrics import CoverageIndex
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

# Idle refresh interval; mutations clear the cache so UI does not wait a full tick.
_snapshot_cache: dict = {"data": None, "json": "{}"}
_bg_tasks: list[asyncio.Task] = []


def _clear_snapshot_cache() -> None:
    """Drop cached status so mutations and backend switches cannot serve stale rows."""
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

