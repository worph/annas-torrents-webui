"""Embedded libtorrent session — the app *is* the BitTorrent client.

We run one libtorrent session inside the process. Torrents are added from the
.torrent files on the data volume; their content is downloaded and seeded to the
chosen save path. Resume data is persisted so restarts don't re-check from scratch.
"""

from __future__ import annotations

import glob
import logging
import os
import time

import libtorrent as lt

from . import storage
from .metrics import _safe_progress

log = logging.getLogger("session.libtorrent")


def _libtorrent_rate_limit(bytes_per_sec: int) -> int:
    """Map app rate (-1 unlimited, 0 blocked) to libtorrent (0 = unlimited)."""
    if bytes_per_sec < 0:
        return 0
    if bytes_per_sec == 0:
        return 1  # nearest stop; libtorrent treats 0 as unlimited
    return int(bytes_per_sec)


class LibtorrentSession:
    def __init__(self, content_dir: str, torrents_dir: str, resume_dir: str, listen_port: int = 6881):
        self.content_dir = content_dir
        self.torrents_dir = torrents_dir
        self.resume_dir = resume_dir
        self._active_save_path: str | None = None
        self._preallocated: set[str] = set()
        for d in (content_dir, torrents_dir, resume_dir):
            os.makedirs(d, exist_ok=True)

        mask = lt.alert.category_t.status_notification
        try:
            mask |= lt.alert.category_t.storage_notification
        except AttributeError:
            pass
        self._ses = lt.session(
            {
                "listen_interfaces": f"0.0.0.0:{listen_port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
                "alert_mask": mask,
            }
        )
        self._handles: dict[str, "lt.torrent_handle"] = {}
        self._desired_upload_limit = -1  # -1 = unlimited
        self._desired_download_limit = -1
        self._seeding_paused = False
        self._downloads_paused = False

    def default_save_path(self) -> str:
        return self.content_dir

    def storage_options(self, extra: list[str] | None = None) -> list[dict]:
        return storage.preset_options(self.default_save_path(), extra)

    def load_existing(self) -> int:
        """Re-add torrents this libtorrent session previously owned (resume or .owned)."""
        count = 0
        for path in glob.glob(os.path.join(self.torrents_dir, "*.torrent")):
            try:
                info = lt.torrent_info(path)
                ih = str(info.info_hash()).lower()
                resume = os.path.join(self.resume_dir, ih + ".fastresume")
                owned = os.path.join(self.resume_dir, ih + ".owned")
                legacy = os.path.join(self.resume_dir, str(info.info_hash()) + ".fastresume")
                prealloc_path = os.path.join(self.resume_dir, ih + ".prealloc")
                if not (os.path.exists(resume) or os.path.exists(owned) or os.path.exists(legacy)):
                    log.warning(
                        "skipping unmarked torrent metadata %s (no .owned/.fastresume; "
                        "re-add via provision or mark ownership after a backend switch)",
                        os.path.basename(path),
                    )
                    continue
                # Restore preallocate *before* add_torrent_file — _write_owned would
                # otherwise unlink .prealloc when called with preallocate=False.
                want_prealloc = os.path.exists(prealloc_path)
                if want_prealloc:
                    self._preallocated.add(ih)
                owned_sp = self._read_owned_save_path(owned)
                if os.path.exists(resume) or os.path.exists(legacy):
                    self.add_torrent_file(path, preallocate=want_prealloc)
                else:
                    self.add_torrent_file(path, owned_sp, preallocate=want_prealloc)
                count += 1
            except Exception as e:  # noqa: BLE001
                log.warning("could not re-add %s: %s", path, e)
        log.info("reloaded %d existing torrents", count)
        return count

    @staticmethod
    def _read_owned_save_path(owned_path: str) -> str | None:
        if not os.path.isfile(owned_path):
            return None
        try:
            with open(owned_path, encoding="utf-8") as f:
                line = f.readline().strip()
            return line or None
        except OSError:
            return None

    def _write_owned(self, ih: str, save_path: str, *, preallocate: bool) -> None:
        """Persist ownership markers so load_existing can resume after restart."""
        owned_path = os.path.join(self.resume_dir, ih + ".owned")
        try:
            with open(owned_path, "w", encoding="utf-8") as f:
                f.write((save_path or "") + "\n")
        except OSError as e:
            raise OSError(f"could not write ownership marker for {ih}: {e}") from e
        pre_path = os.path.join(self.resume_dir, ih + ".prealloc")
        try:
            if preallocate:
                open(pre_path, "a", encoding="utf-8").close()
            elif os.path.exists(pre_path):
                os.unlink(pre_path)
        except OSError as e:
            # Ownership already written; prealloc is advisory for metrics/resume.
            log.warning("could not update prealloc marker for %s: %s", ih, e)

    def add_torrent_file(
        self, path: str, save_path: str | None = None, *, preallocate: bool = False
    ) -> str | None:
        """Add a torrent from a .torrent file. Returns its infohash (hex), or None."""
        info = lt.torrent_info(path)
        ih = str(info.info_hash()).lower()
        if ih in self._handles:
            return ih

        resume_path = os.path.join(self.resume_dir, ih + ".fastresume")
        if not os.path.exists(resume_path):
            # legacy resume files written before hash lowercasing
            legacy = os.path.join(self.resume_dir, str(info.info_hash()) + ".fastresume")
            if os.path.exists(legacy) and legacy != resume_path:
                resume_path = legacy

        content_abs = os.path.abspath(self.content_dir)
        explicit = os.path.abspath(save_path) if (save_path or "").strip() else None
        need_recheck = False
        resume_sp = content_abs

        if os.path.exists(resume_path):
            with open(resume_path, "rb") as f:
                atp = lt.read_resume_data(f.read())
            resume_raw = getattr(atp, "save_path", "") or ""
            resume_sp = os.path.abspath(resume_raw or content_abs)
            if explicit:
                atp.save_path = explicit
                need_recheck = explicit != resume_sp
            elif resume_raw:
                # Keep resume path — do not rewrite onto content_dir.
                atp.save_path = resume_sp
            else:
                atp.save_path = content_abs
                need_recheck = True
        else:
            atp = lt.add_torrent_params()
            atp.save_path = explicit or content_abs

        if preallocate:
            atp.storage_mode = lt.storage_mode_t.storage_mode_allocate

        name = info.name()
        dest = os.path.abspath(getattr(atp, "save_path", None) or content_abs)
        atp.ti = info
        os.makedirs(dest, exist_ok=True)
        if explicit:
            self._active_save_path = explicit

        handle = self._ses.add_torrent(atp)
        self._handles[ih] = handle
        if preallocate:
            self._preallocated.add(ih)
        try:
            self._write_owned(ih, dest, preallocate=preallocate or ih in self._preallocated)
        except OSError as e:
            # Roll back the in-session add so restart cannot skip an "added" torrent.
            try:
                self._ses.remove_torrent(handle)
            except Exception:  # noqa: BLE001
                pass
            self._handles.pop(ih, None)
            self._preallocated.discard(ih)
            raise RuntimeError(str(e)) from e
        if self._downloads_paused:
            try:
                if handle.status().progress < 1.0:
                    handle.pause()
            except Exception as e:  # noqa: BLE001
                log.warning("pause newly added download failed for %s: %s", ih, e)
        if self._seeding_paused:
            try:
                if handle.status().progress >= 1.0:
                    handle.pause()
            except Exception as e:  # noqa: BLE001
                log.warning("pause newly added seeder failed for %s: %s", ih, e)
        if need_recheck:
            try:
                handle.force_recheck()
                log.info("recheck %s after save_path fix → %s", name, dest)
            except Exception as e:  # noqa: BLE001
                log.warning("force_recheck failed for %s: %s", name, e)
        log.info("added torrent %s (%s) → %s", name, ih, dest)
        return ih

    def save_resume(self) -> None:
        """Persist fastresume data for all torrents so a restart resumes cleanly."""
        pending: set[str] = set()
        for ih, h in list(self._handles.items()):
            try:
                if not (h.is_valid() and h.status().has_metadata):
                    continue
                h.save_resume_data()
                pending.add(ih)
            except Exception:  # noqa: BLE001
                continue
        deadline = time.time() + 3.0
        while pending and time.time() < deadline:
            alerts = self._ses.pop_alerts()
            if not alerts:
                time.sleep(0.05)
                continue
            for a in alerts:
                if isinstance(a, lt.save_resume_data_alert):
                    ih = str(a.handle.info_hash()).lower()
                    if ih not in pending:
                        continue
                    pending.discard(ih)
                    data = lt.write_resume_data_buf(a.params)
                    path = os.path.join(self.resume_dir, ih + ".fastresume")
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                elif type(a).__name__ == "save_resume_data_failed_alert":
                    try:
                        ih = str(a.handle.info_hash()).lower()
                    except Exception:  # noqa: BLE001
                        continue
                    pending.discard(ih)

    def close(self) -> None:
        """Best-effort pause; libtorrent tears down with the process."""
        try:
            self._ses.pause()
        except Exception:  # noqa: BLE001
            pass

    def global_status(self) -> dict:
        s = self._ses.status()
        save_paths: set[str] = set()
        for h in list(self._handles.values()):
            try:
                path = getattr(h.status(), "save_path", "") or ""
                if path:
                    save_paths.add(os.path.abspath(path))
            except Exception:  # noqa: BLE001
                continue
        if self._active_save_path:
            save_paths.add(os.path.abspath(self._active_save_path))
        if not save_paths:
            save_paths.add(os.path.abspath(self.content_dir))
        if len(save_paths) == 1:
            storage_path = next(iter(save_paths))
        elif save_paths:
            storage_path = "multiple destinations"
        else:
            storage_path = self.content_dir
        disk_free, disk_total, disk_known = storage.sum_unique_disk_usage(save_paths)

        content_bytes = 0
        num_peers = 0
        for ih, h in self._handles.items():
            try:
                st = h.status()
                done = int(st.total_done)
                # Only count full size when this add used allocate mode.
                wanted = int(getattr(st, "total_wanted", 0) or 0)
                if (
                    ih in self._preallocated
                    and wanted > done
                    and getattr(st, "progress", 1) < 1.0
                ):
                    content_bytes += wanted
                else:
                    content_bytes += done
                # Match qBit / per-torrent: connected leeches only.
                num_peers += max(0, int(st.num_peers) - int(st.num_seeds))
            except Exception:  # noqa: BLE001
                continue

        if not self._handles:
            self._active_save_path = None

        return {
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "total_download": s.total_download,
            "total_upload": s.total_upload,
            "num_torrents": len(self._handles),
            "num_peers": num_peers,
            "dht_nodes": s.dht_nodes,
            "committed_bytes": content_bytes,
            "disk_free": disk_free if disk_known else 0,
            "disk_free_known": disk_known,
            "disk_total": disk_total if disk_known else 0,
            "storage_path": storage_path,
            "backend_ok": True,
        }

    def torrents_status(self) -> list[dict]:
        out = []
        for ih, h in self._handles.items():
            try:
                st = h.status()
            except Exception:  # noqa: BLE001
                continue
            # Keep pause_all coverage for torrents that finish after seeding was paused.
            if self._seeding_paused and st.progress >= 1.0 and not st.paused:
                try:
                    h.pause()
                    st = h.status()
                except Exception:  # noqa: BLE001
                    pass
            save_path = getattr(st, "save_path", "") or ""
            # qBit-style: connected (swarm). Prefer tracker scrape for totals.
            connected_seeds = int(st.num_seeds)
            connected_peers = max(0, int(st.num_peers) - connected_seeds)
            complete = int(getattr(st, "num_complete", -1))
            incomplete = int(getattr(st, "num_incomplete", -1))
            list_seeds = int(getattr(st, "list_seeds", 0) or 0)
            list_peers = int(getattr(st, "list_peers", 0) or 0)
            seeds_total = complete if complete >= 0 else (list_seeds if list_seeds else None)
            if incomplete >= 0:
                peers_total = incomplete
            elif list_peers:
                peers_total = max(0, list_peers - list_seeds)
            else:
                peers_total = None
            wanted_done = getattr(st, "total_wanted_done", None)
            allocated = st.total_done if wanted_done is None else wanted_done
            progress = _safe_progress(st.progress)
            # Match global committed_bytes: preallocate reserves full wanted size.
            wanted = int(getattr(st, "total_wanted", 0) or 0)
            if (
                ih in self._preallocated
                and wanted > int(allocated)
                and progress < 1.0
            ):
                allocated = wanted
            paused = bool(getattr(st, "paused", False))
            state = str(st.state)
            if paused:
                state = "paused"
            out.append(
                {
                    "infohash": ih.lower(),
                    "name": st.name,
                    "state": state,
                    "progress": progress,
                    "size": st.total_wanted,
                    "downloaded": int(st.total_done),
                    "allocated_bytes": int(allocated),
                    "download_rate": st.download_rate,
                    "upload_rate": st.upload_rate,
                    "num_seeds": connected_seeds,
                    "seeds_total": seeds_total,
                    "num_peers": connected_peers,
                    "peers_total": peers_total,
                    "seeds_known": seeds_total is not None,
                    "is_seeding": st.is_seeding and not paused,
                    "is_complete": progress >= 1.0,
                    "paused": paused,
                    "save_path": save_path,
                }
            )
        return out

    def infohashes(self) -> set[str]:
        return {h.lower() for h in self._handles}

    def pause_all(self) -> None:
        """Stop seeding everywhere; incomplete torrents keep downloading (upload=0)."""
        prev = self._seeding_paused
        self._seeding_paused = True
        try:
            self._apply_upload_limit(0)
            for h in self._handles.values():
                try:
                    if h.status().progress >= 1.0:
                        h.pause()
                except Exception as e:  # noqa: BLE001
                    log.warning("pause seeder failed: %s", e)
        except Exception:
            self._seeding_paused = prev
            raise

    def resume_all(self) -> None:
        """Allow seeding again; restore upload limit and wake complete torrents."""
        prev = self._seeding_paused
        self._seeding_paused = False
        try:
            self._apply_upload_limit(self._desired_upload_limit)
            for h in self._handles.values():
                try:
                    if h.status().progress >= 1.0:
                        h.resume()
                except Exception as e:  # noqa: BLE001
                    log.warning("resume seeder failed: %s", e)
        except Exception:
            self._seeding_paused = prev
            raise

    def set_upload_limit(self, bytes_per_sec: int) -> None:
        """Session upload limit. Pass -1 for unlimited."""
        prev = self._desired_upload_limit
        self._desired_upload_limit = int(bytes_per_sec)
        try:
            if not self._seeding_paused:
                self._apply_upload_limit(self._desired_upload_limit)
        except Exception:
            self._desired_upload_limit = prev
            raise

    def _apply_upload_limit(self, bytes_per_sec: int) -> None:
        limit = _libtorrent_rate_limit(bytes_per_sec)
        try:
            self._ses.set_upload_rate_limit(limit)
        except AttributeError:
            settings = self._ses.get_settings()
            settings["upload_rate_limit"] = limit
            self._ses.apply_settings(settings)

    def pause_downloads(self) -> None:
        """Pause incomplete torrents only (seeders keep going)."""
        prev = self._downloads_paused
        self._downloads_paused = True
        try:
            for h in self._handles.values():
                try:
                    if h.status().progress < 1.0:
                        h.pause()
                except Exception as e:  # noqa: BLE001
                    log.warning("pause download failed: %s", e)
        except Exception:
            self._downloads_paused = prev
            raise

    def resume_downloads(self) -> None:
        prev = self._downloads_paused
        self._downloads_paused = False
        try:
            for h in self._handles.values():
                try:
                    if h.status().progress < 1.0:
                        h.resume()
                except Exception as e:  # noqa: BLE001
                    log.warning("resume download failed: %s", e)
        except Exception:
            self._downloads_paused = prev
            raise

    def set_download_limit(self, bytes_per_sec: int) -> None:
        """Session download limit. Pass -1 for unlimited."""
        prev = self._desired_download_limit
        self._desired_download_limit = int(bytes_per_sec)
        try:
            self._apply_download_limit(self._desired_download_limit)
        except Exception:
            self._desired_download_limit = prev
            raise

    def _apply_download_limit(self, bytes_per_sec: int) -> None:
        limit = _libtorrent_rate_limit(bytes_per_sec)
        try:
            self._ses.set_download_rate_limit(limit)
        except AttributeError:
            settings = self._ses.get_settings()
            settings["download_rate_limit"] = limit
            self._ses.apply_settings(settings)

    def controls_state(self) -> dict:
        from .ux import controls_payload

        return controls_payload(
            seeding_paused=self._seeding_paused,
            downloads_paused=self._downloads_paused,
            upload_limit=self._desired_upload_limit,
            download_limit=self._desired_download_limit,
        )

    def remove_torrents(self, infohashes: list[str], delete_files: bool = True) -> dict:
        """Remove torrents. Returns ``{removed, files_deleted}``.

        ``files_deleted``: True when local deletes succeeded; False when refused
        or failed; None when ``delete_files`` was false.
        """
        from .pathsafety import delete_under, safe_delete_target, shared_content_ids

        want = [(raw or "").lower() for raw in infohashes if raw]
        if not want:
            return {"removed": 0, "files_deleted": None if not delete_files else True}

        # Snapshot roots before any mutation — batch victims must see each other.
        snapshot: list[tuple[str, str, str]] = []
        for ih, h in list(self._handles.items()):
            save_path = ""
            name = ""
            try:
                st = h.status()
                name = st.name
                save_path = getattr(st, "save_path", "") or ""
            except Exception:  # noqa: BLE001
                pass
            snapshot.append((ih, save_path, name))
        shared_ids = shared_content_ids(snapshot) if delete_files else set()
        by_ih = {ih: (save, name) for ih, save, name in snapshot}

        session_ok = True
        files_ok = True
        removed: list[str] = []
        purged: list[str] = []
        for ih in want:
            h = self._handles.get(ih)
            if h is None:
                continue
            save_path, name = by_ih.get(ih, ("", ""))
            will_delete = bool(delete_files and save_path and name and ih not in shared_ids)
            if delete_files and ih in shared_ids:
                log.warning(
                    "refusing file delete for %s — shared content root with another torrent",
                    name or ih,
                )
                files_ok = False
                will_delete = False
            elif delete_files and not (save_path and name):
                files_ok = False
                will_delete = False
            # Preflight containment before leaving the session (keeps metadata on fail).
            if will_delete and safe_delete_target(save_path, name) is None:
                log.warning("refusing unsafe delete target for %s under %s", name, save_path)
                files_ok = False
                will_delete = False
            try:
                self._ses.remove_torrent(h)
            except Exception as e:  # noqa: BLE001
                log.warning("remove_torrent %s failed: %s", ih, e)
                session_ok = False
                continue
            self._handles.pop(ih, None)
            self._preallocated.discard(ih)
            removed.append(ih)
            for resume in (
                os.path.join(self.resume_dir, ih + ".fastresume"),
                os.path.join(self.resume_dir, ih + ".fastresume.tmp"),
                os.path.join(self.resume_dir, ih + ".owned"),
                os.path.join(self.resume_dir, ih + ".prealloc"),
            ):
                try:
                    os.unlink(resume)
                except OSError:
                    pass
            if will_delete:
                if delete_under(save_path, name):
                    purged.append(ih)
                else:
                    log.warning("refused or failed file delete for %s under %s", name, save_path)
                    files_ok = False
        from .session import purge_torrent_files

        # Keep .torrent metadata when content was kept so the torrent can be re-added.
        if purged:
            purge_torrent_files(self.torrents_dir, purged)
        if not delete_files and removed:
            # Intentional keep-files: leave .torrent for re-add.
            pass
        if not session_ok and not removed:
            return {"removed": 0, "files_deleted": False if delete_files else None}
        if not removed:
            return {"removed": 0, "files_deleted": False if delete_files else None}
        if not delete_files:
            return {"removed": len(removed), "files_deleted": None}
        return {"removed": len(removed), "files_deleted": files_ok}
