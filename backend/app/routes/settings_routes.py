"""Config GET and settings PUT."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .. import runtime as rt
from .. import settings, storage
from ..auth import auth_required
from ..schemas import SettingsRequest

router = APIRouter()


@router.get("/api/config")
async def config():
    """Client config: share URL, backend, qBit connection fields."""
    async with rt._session_lock:
        cat = getattr(rt.session, "category", None) or settings.resolve_qbit_category(rt.DATA_DIR, rt.QBIT_CATEGORY_ENV)
    try:
        default_url = settings._clean_qbit_url(rt.QBIT_URL_ENV) if rt.QBIT_URL_ENV else settings.DEFAULT_QBIT_URL
    except ValueError:
        default_url = settings.DEFAULT_QBIT_URL
    return {
        "public_url": rt.PUBLIC_URL,
        "auth_required": auth_required(),
        "backend": rt.TORRENT_BACKEND,
        "backends": list(settings.BACKENDS),
        "qbit_category": cat,
        "qbit_url": rt.QBIT_URL,
        "qbit_user": rt.QBIT_USER,
        "qbit_pass_set": bool(rt.QBIT_PASS),
        "defaults": {
            "qbit_category": storage.ANNA_FOLDER,
            "qbit_url": default_url,
            "qbit_user": rt.QBIT_USER_ENV or "admin",
        },
    }


@router.put("/api/settings")
async def put_settings(req: SettingsRequest):
    """Validate + try new session before persisting settings.json.

    The full replace (validate → persist → session swap) holds ``_session_lock``
    so concurrent settings updates cannot race the live session and disk file.
    """

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

    async with rt._session_lock:
        if rt.provision_state.get("running"):
            raise HTTPException(status_code=409, detail="cannot change settings while provisioning")
        current_session = rt.session
        current_backend = rt.TORRENT_BACKEND
        current_url = rt.QBIT_URL
        current_user = rt.QBIT_USER
        current_pass = rt.QBIT_PASS
        current_category = rt.QBIT_CATEGORY

        try:
            merged = await asyncio.to_thread(
                settings.apply_patch, settings.load_settings(rt.DATA_DIR), patch
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Never revive a stale settings.json backend when the user did not ask to switch.
        if "torrent_backend" in patch:
            new_backend = settings.resolve_from(merged, "torrent_backend", rt.TORRENT_BACKEND_ENV)
            if (
                patch.get("torrent_backend")
                and settings.normalize_backend(patch["torrent_backend"]) == "libtorrent"
                and not settings._libtorrent_available()
            ):
                raise HTTPException(
                    status_code=400,
                    detail="libtorrent is not installed in this image — rebuild with rt.TORRENT_BACKEND=libtorrent",
                )
        else:
            new_backend = current_backend
            # Scrub unavailable persisted libtorrent so later loads match runtime.
            if settings.normalize_backend(merged.get("torrent_backend")) == "libtorrent":
                if not settings._libtorrent_available():
                    patch = {**patch, "torrent_backend": "qbittorrent"}
                    merged["torrent_backend"] = "qbittorrent"
        new_url = settings.resolve_from(merged, "qbit_url", rt.QBIT_URL_ENV)
        new_user = settings.resolve_from(merged, "qbit_user", rt.QBIT_USER_ENV)
        # Omit password field → keep runtime password; "" clears without falling back to env.
        new_pass = current_pass if req.qbit_pass is None else req.qbit_pass
        new_cat = settings.resolve_from(merged, "qbit_category", rt.QBIT_CATEGORY_ENV)

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
                    await asyncio.to_thread(rt._locked_call, current_session.set_category, new_cat)
                    await asyncio.to_thread(rt._locked_call, current_session.verify)
                else:
                    await asyncio.to_thread(rt._locked_call, current_session.set_category, new_cat)
                settings.save_settings(rt.DATA_DIR, patch)
            except (ValueError, OSError, RuntimeError) as e:
                try:
                    await asyncio.to_thread(rt._locked_call, current_session.set_category, current_category)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001
                try:
                    await asyncio.to_thread(rt._locked_call, current_session.set_category, current_category)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=f"could not reach qBittorrent: {e}") from e
            rt.QBIT_CATEGORY = new_cat
            rt._session_generation += 1
            rt._clear_snapshot_cache()
            return {"ok": True, "backend": rt.TORRENT_BACKEND, "qbit_category": new_cat, "rebuilt": False}

        need_rebuild = conn_changed or (
            new_backend == "qbittorrent" and "qbit_category" in patch and new_cat != current_category
        )
        if need_rebuild:
            new_sess = None
            try:
                new_sess = rt._new_session(
                    new_backend,
                    qbit_url=new_url,
                    qbit_user=new_user,
                    qbit_pass=new_pass,
                    qbit_category=new_cat,
                )
                if hasattr(new_sess, "verify"):
                    await asyncio.to_thread(rt._locked_call, new_sess.verify)
                # qBit→libtorrent: do not auto-add every cached .torrent (would re-download
                # while qBit may still be seeding the same content). Startup load_existing
                # still resumes libtorrent's own torrents after a normal restart.
                if not (current_backend == "qbittorrent" and new_backend == "libtorrent"):
                    await asyncio.to_thread(rt._locked_call, new_sess.load_existing)
                else:
                    rt.log.warning(
                        "switched qbittorrent→libtorrent without importing torrents_dir; "
                        "stop qBit or re-provision if you want libtorrent to seed them"
                    )
            except Exception as e:  # noqa: BLE001
                rt.log.exception("failed to switch backend")
                if new_sess is not None:
                    try:
                        await asyncio.to_thread(rt._locked_call, new_sess.close)
                    except Exception:  # noqa: BLE001
                        pass
                raise HTTPException(status_code=400, detail=f"could not switch backend: {e}") from e
            try:
                settings.save_settings(rt.DATA_DIR, patch)
            except (ValueError, OSError) as e:
                try:
                    await asyncio.to_thread(rt._locked_call, new_sess.close)
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=400, detail=str(e)) from e
            old = rt.session
            rt.session = new_sess
            rt.TORRENT_BACKEND = new_backend
            rt.QBIT_URL = new_url
            rt.QBIT_USER = new_user
            rt.QBIT_PASS = new_pass
            rt.QBIT_CATEGORY = new_cat
            rt._session_generation += 1
            rt._clear_snapshot_cache()
            try:
                if hasattr(old, "save_resume"):
                    await asyncio.to_thread(rt._locked_call, old.save_resume)
            except Exception as e:  # noqa: BLE001
                rt.log.warning("old session.save_resume failed: %s", e)
            try:
                await asyncio.to_thread(rt._locked_call, old.close)
            except Exception as e:  # noqa: BLE001
                rt.log.warning("old session.close failed: %s", e)
            return {
                "ok": True,
                "backend": rt.TORRENT_BACKEND,
                "qbit_category": rt.QBIT_CATEGORY,
                "qbit_url": rt.QBIT_URL,
                "qbit_user": rt.QBIT_USER,
                "qbit_pass_set": bool(rt.QBIT_PASS),
                "rebuilt": True,
            }

        try:
            settings.save_settings(rt.DATA_DIR, patch)
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        rt.TORRENT_BACKEND = new_backend
        rt.QBIT_CATEGORY = new_cat
        return {
            "ok": True,
            "backend": rt.TORRENT_BACKEND,
            "qbit_category": rt.QBIT_CATEGORY,
            "qbit_url": rt.QBIT_URL,
            "qbit_user": rt.QBIT_USER,
            "qbit_pass_set": bool(rt.QBIT_PASS),
            "rebuilt": False,
        }

