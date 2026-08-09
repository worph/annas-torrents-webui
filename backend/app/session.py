"""Torrent backend factory: libtorrent (default) or qbittorrent.

Set TORRENT_BACKEND=libtorrent|qbittorrent. Same public methods either way:
load_existing, add_torrent_file(path, save_path=None), save_resume,
global_status, torrents_status, infohashes, storage_options, default_save_path,
pause_all, resume_all, pause_downloads, resume_downloads,
set_upload_limit, set_download_limit, controls_state, remove_torrents, close.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Protocol

log = logging.getLogger("session")


def purge_torrent_files(torrents_dir: str, infohashes: list[str]) -> int:
    """Delete ``.torrent`` metadata files under torrents_dir matching infohashes.

    Returns how many files were removed.
    """
    want = {h.lower() for h in infohashes if h}
    if not want or not torrents_dir or not os.path.isdir(torrents_dir):
        return 0
    removed = 0
    try:
        import libtorrent as lt
    except ImportError:
        # Downloaded metadata is named by v1 infohash, so qBit-only images can
        # still remove it without installing the embedded client.
        for path in glob.glob(os.path.join(torrents_dir, "*.torrent")):
            if os.path.splitext(os.path.basename(path))[0].lower() not in want:
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError as e:
                log.warning("could not delete %s: %s", path, e)
        return removed
    for path in glob.glob(os.path.join(torrents_dir, "*.torrent")):
        try:
            ih = str(lt.torrent_info(path).info_hash()).lower()
        except Exception:  # noqa: BLE001
            continue
        if ih not in want:
            continue
        try:
            os.unlink(path)
            removed += 1
            log.info("deleted torrent file %s", os.path.basename(path))
        except OSError as e:
            log.warning("could not delete %s: %s", path, e)
    return removed


class TorrentSession(Protocol):
    """Shared contract for libtorrent and qBittorrent backends."""

    content_dir: str
    torrents_dir: str

    def default_save_path(self) -> str: ...
    def storage_options(self, extra: list[str] | None = None) -> list[dict]: ...
    def load_existing(self) -> int: ...
    def add_torrent_file(
        self, path: str, save_path: str | None = None, *, preallocate: bool = False
    ) -> str | None: ...
    def save_resume(self) -> None: ...
    def global_status(self) -> dict: ...
    def torrents_status(self) -> list[dict]: ...
    def infohashes(self) -> set[str]: ...
    def pause_all(self) -> None: ...
    def resume_all(self) -> None: ...
    def pause_downloads(self) -> None: ...
    def resume_downloads(self) -> None: ...
    def set_upload_limit(self, bytes_per_sec: int) -> None: ...
    def set_download_limit(self, bytes_per_sec: int) -> None: ...
    def controls_state(self) -> dict: ...
    def remove_torrents(self, infohashes: list[str], delete_files: bool = True) -> dict: ...
    def close(self) -> None: ...


def create_session(
    *,
    content_dir: str,
    torrents_dir: str,
    resume_dir: str,
    listen_port: int = 6881,
    backend: str | None = None,
    qbit_url: str = "http://127.0.0.1:8080",
    qbit_user: str = "admin",
    qbit_pass: str = "",
    qbit_category: str = "Anna's Archive Torrents",
    qbit_save_path: str | None = None,
) -> TorrentSession:
    backend = (backend or os.environ.get("TORRENT_BACKEND", "libtorrent")).strip().lower()
    if backend in ("qbittorrent", "qbit", "qb"):
        from .session_qbittorrent import QBittorrentSession

        # Never log credentials that may appear in a legacy QBIT_URL.
        safe = qbit_url.split("@")[-1] if "@" in qbit_url else qbit_url
        log.info("torrent backend: qbittorrent (%s)", safe)
        return QBittorrentSession(
            content_dir=content_dir,
            torrents_dir=torrents_dir,
            qbit_url=qbit_url,
            qbit_user=qbit_user,
            qbit_pass=qbit_pass,
            category=qbit_category,
            save_path=qbit_save_path,
        )
    if backend in ("libtorrent", "lt", "embedded"):
        try:
            from .session_libtorrent import LibtorrentSession
        except ImportError as e:
            raise ValueError(
                "libtorrent backend is unavailable in this image; rebuild with "
                "TORRENT_BACKEND=libtorrent"
            ) from e

        log.info("torrent backend: libtorrent (port %s)", listen_port)
        return LibtorrentSession(
            content_dir=content_dir,
            torrents_dir=torrents_dir,
            resume_dir=resume_dir,
            listen_port=listen_port,
        )
    raise ValueError(f"unknown TORRENT_BACKEND={backend!r} (expected libtorrent|qbittorrent)")
