"""Provisioning endpoints and worker."""

from __future__ import annotations

import asyncio
import math
import os
import time

from fastapi import APIRouter, HTTPException, Query

from .. import runtime as rt
from .. import storage
from ..schemas import ProvisionRequest
from ..selection import download_torrent_files, fetch_torrent_list

router = APIRouter()


async def _available_free(sess, backend: str, dest: str | None) -> int | None:
    """Free bytes for the provision destination, or None when unknown."""
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
        status = await rt._call_session_object(sess, "global_status")
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
    rt.provision_state.update(
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
        async with rt._session_lock:
            if rt.session is not sess:
                raise RuntimeError("torrent backend changed")
            torrents_dir = sess.torrents_dir
            default_path = sess.default_save_path()
            backend = rt.TORRENT_BACKEND
        rt.provision_state.update(
            phase="downloading",
            message=f"downloading {len(entries)} .torrent files",
            selected_bytes=sum(max(0, e.data_size) for e in entries),
        )
        paths, dl_failed, created_paths = await download_torrent_files(entries, torrents_dir)
        failed = dl_failed
        rt.provision_state.update(
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
            if rt.provision_state.get("running") is False:
                break
            # Stop once the locally-validated content size hits the request.
            if selected_actual >= target_bytes:
                rt._unlink_unadded_torrents(ordered, added_paths, created_paths)
                rt.provision_state.update(
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
                rt._unlink_unadded_torrents(ordered, added_paths, created_paths)
                rt.provision_state.update(
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
                rt._unlink_unadded_torrents(ordered, added_paths, created_paths)
                rt.provision_state.update(
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
                known = await rt._call_session_object(sess, "infohashes")
                if stem in known:
                    # Already active — keep metadata, do not count toward the new target.
                    added_paths.add(abs_p)
                    continue
                try:
                    ih = await rt._call_session_object(
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
                rt.log.warning("add failed for %s: %s", p, e)
        rt._unlink_unadded_torrents(ordered, added_paths, created_paths)
        if added == 0 and failed > 0:
            rt.provision_state.update(
                phase="error",
                message=f"error: nothing added ({failed} failed)",
                added=added,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
        elif added == 0:
            rt.provision_state.update(
                phase="done",
                message="nothing added — seeding continues",
                added=0,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
        else:
            rt.provision_state.update(
                phase="done",
                message=f"added {added} torrents" + (f" ({failed} failed)" if failed else ""),
                added=added,
                failed=failed,
                selected_bytes=selected_actual,
                finished_at=time.time(),
            )
            rt._clear_snapshot_cache()
    except asyncio.CancelledError:
        # Keep metadata only for torrents that actually made it into the rt.session
        # (in-flight add may finish in a worker thread after cancel).
        try:
            known = await asyncio.to_thread(rt._locked_call, sess.infohashes)
        except Exception:  # noqa: BLE001
            known = set()
        keep = set(added_paths)
        for path_i, _sz in (ordered if ordered else paths):
            key = os.path.splitext(os.path.basename(path_i))[0].lower()
            if key in known:
                keep.add(os.path.abspath(path_i))
        rt._unlink_unadded_torrents(ordered if ordered else paths, keep, created_paths)
        rt.provision_state.update(
            phase="error",
            message="error: provisioning cancelled",
            finished_at=time.time(),
        )
        raise
    except Exception as e:  # noqa: BLE001
        rt._unlink_unadded_torrents(paths, added_paths, created_paths)
        rt.provision_state.update(
            phase="error",
            message=f"error: {e}",
            finished_at=time.time(),
        )
        rt.log.exception("provisioning failed")
    finally:
        if hasattr(sess, "_restore_preallocate"):
            try:
                await rt._call_session_object(sess, "_restore_preallocate")
            except Exception as e:  # noqa: BLE001
                rt.log.warning("restore preallocate failed: %s", e)
        rt.provision_state["running"] = False


def _provision_task_done(task: asyncio.Task) -> None:
    """Safety net when cancel wins before ``_run_provision`` runs (no ``finally``)."""
    if not rt.provision_state.get("running"):
        return
    rt.provision_state["running"] = False
    if task.cancelled() and rt.provision_state.get("finished_at") is None:
        rt.provision_state.update(
            phase="error",
            message="error: provisioning cancelled",
            finished_at=time.time(),
        )


@router.post("/api/provision")
async def provision(req: ProvisionRequest):
    async with rt._provision_lock:
        if rt.provision_state["running"] or (rt._provision_task and not rt._provision_task.done()):
            return {"ok": False, "message": "provisioning already in progress"}
        async with rt._session_lock:
            sess = rt.session
            gen = rt._session_generation
        allowed = await asyncio.to_thread(rt._locked_call, rt._allowed_paths, sess)
        async with rt._session_lock:
            if rt.session is not sess or rt._session_generation != gen:
                return {"ok": False, "message": "torrent backend changed"}
            save_path = rt._resolve_save_path(req.save_path, allowed, rt.TORRENT_BACKEND)
        free = await _available_free(sess, rt.TORRENT_BACKEND, save_path)
        if free is None and not req.allow_unknown_disk:
            return {
                "ok": False,
                "code": "unknown_disk",
                "message": (
                    "Free space is unknown for this destination "
                    "(common when qBittorrent is remote or the path differs from "
                    "the client's default). Confirm to proceed without a disk check, "
                    "or pick a destination whose free space is reported."
                ),
            }
        async with rt._session_lock:
            if rt.session is not sess or rt._session_generation != gen:
                return {"ok": False, "message": "torrent backend changed"}
            if rt.provision_state["running"] or (rt._provision_task and not rt._provision_task.done()):
                return {"ok": False, "message": "provisioning already in progress"}
            rt.provision_state["running"] = True
            rt._provision_task = asyncio.create_task(_run_provision(req, save_path, sess))
            rt._provision_task.add_done_callback(_provision_task_done)
    return {"ok": True, "message": "provisioning started"}


@router.post("/api/provision/cancel")
async def provision_cancel():
    """Stop an in-flight provision; already-added torrents keep seeding."""
    async with rt._provision_lock:
        task = rt._provision_task
        if not task or task.done():
            return {"ok": False, "message": "no provisioning in progress"}
        task.cancel()
    return {"ok": True, "message": "provisioning cancel requested"}


@router.get("/api/provision")
async def provision_status():
    return rt.provision_state


@router.get("/api/collections")
async def collections(max_tb: float = Query(default=1.0, gt=0, le=30)):
    """Available collections for a target, so the UI can offer filters."""
    if not math.isfinite(max_tb):
        raise HTTPException(status_code=400, detail="max_tb must be finite")
    try:
        entries = await fetch_torrent_list(max_tb)
    except Exception as e:  # noqa: BLE001
        rt.log.warning("collections fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="could not fetch torrent collections") from e
    agg: dict[str, dict] = {}
    for e in entries:
        a = agg.setdefault(e.collection, {"collection": e.collection, "count": 0, "bytes": 0})
        a["count"] += 1
        a["bytes"] += e.data_size
    return sorted(agg.values(), key=lambda x: -x["bytes"])

