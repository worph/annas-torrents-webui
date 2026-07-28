"""Torrent backend factory: libtorrent (default) or qbittorrent.

Set TORRENT_BACKEND=libtorrent|qbittorrent. Same public methods either way:
load_existing, add_torrent_file, save_resume, global_status, torrents_status, infohashes.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("session")


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
    qbit_category: str = "Anna's Archive",
    qbit_save_path: str | None = None,
) -> Any:
    backend = (backend or os.environ.get("TORRENT_BACKEND", "libtorrent")).strip().lower()
    if backend in ("qbittorrent", "qbit", "qb"):
        from .session_qbittorrent import QBittorrentSession

        log.info("torrent backend: qbittorrent (%s)", qbit_url)
        return QBittorrentSession(
            content_dir=content_dir,
            torrents_dir=torrents_dir,
            qbit_url=qbit_url,
            qbit_user=qbit_user,
            qbit_pass=qbit_pass,
            category=qbit_category,
            save_path=qbit_save_path,
        )
    if backend not in ("libtorrent", "lt", "embedded"):
        log.warning("unknown TORRENT_BACKEND=%r, falling back to libtorrent", backend)
    from .session_libtorrent import LibtorrentSession

    log.info("torrent backend: libtorrent (port %s)", listen_port)
    return LibtorrentSession(
        content_dir=content_dir,
        torrents_dir=torrents_dir,
        resume_dir=resume_dir,
        listen_port=listen_port,
    )
