"""Embedded libtorrent session — the app *is* the BitTorrent client.

We run one libtorrent session inside the process. Torrents are added from the
.torrent files on the data volume; their content is downloaded and seeded to the
data volume. There is no metrics database (live-only), but the torrent set and
libtorrent resume data are persisted to disk so restarts don't re-check from
scratch.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil

import libtorrent as lt

from .disks import list_disks

log = logging.getLogger("session.libtorrent")


class LibtorrentSession:
    def __init__(self, content_dir: str, torrents_dir: str, resume_dir: str, listen_port: int = 6881):
        self.content_dir = content_dir
        self.torrents_dir = torrents_dir
        self.resume_dir = resume_dir
        for d in (content_dir, torrents_dir, resume_dir):
            os.makedirs(d, exist_ok=True)

        self._ses = lt.session(
            {
                "listen_interfaces": f"0.0.0.0:{listen_port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
                "alert_mask": lt.alert.category_t.status_notification,
            }
        )
        self._handles: dict[str, "lt.torrent_handle"] = {}

    def load_existing(self) -> int:
        """Re-add every .torrent file already on the data volume (restart resume)."""
        count = 0
        for path in glob.glob(os.path.join(self.torrents_dir, "*.torrent")):
            try:
                self.add_torrent_file(path)
                count += 1
            except Exception as e:  # noqa: BLE001
                log.warning("could not re-add %s: %s", path, e)
        log.info("reloaded %d existing torrents", count)
        return count

    def add_torrent_file(self, path: str) -> str | None:
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
        if os.path.exists(resume_path):
            with open(resume_path, "rb") as f:
                atp = lt.read_resume_data(f.read())
        else:
            atp = lt.add_torrent_params()
        atp.ti = info
        atp.save_path = self.content_dir

        handle = self._ses.add_torrent(atp)
        self._handles[ih] = handle
        log.info("added torrent %s (%s)", info.name(), ih)
        return ih

    def save_resume(self) -> None:
        """Persist fastresume data for all torrents so a restart resumes cleanly."""
        for ih, h in self._handles.items():
            try:
                if not (h.is_valid() and h.status().has_metadata):
                    continue
                h.save_resume_data()
            except Exception:  # noqa: BLE001
                continue
        for a in self._ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                ih = str(a.handle.info_hash()).lower()
                data = lt.write_resume_data_buf(a.params)
                with open(os.path.join(self.resume_dir, ih + ".fastresume"), "wb") as f:
                    f.write(data)

    def global_status(self) -> dict:
        s = self._ses.status()
        disks = list_disks()
        try:
            usage = shutil.disk_usage(self.content_dir)
            disk_free = usage.free
            disk_total = usage.total
        except OSError:
            disk_free = disk_total = 0

        content_bytes = 0
        for h in self._handles.values():
            try:
                content_bytes += h.status().total_wanted
            except Exception:  # noqa: BLE001
                continue

        return {
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "total_download": s.total_download,
            "total_upload": s.total_upload,
            "num_torrents": len(self._handles),
            "num_peers": s.num_peers,
            "dht_nodes": s.dht_nodes,
            "committed_bytes": content_bytes,
            "disk_free": disk_free,
            "disk_total": disk_total,
            "disks": disks,
        }

    def torrents_status(self) -> list[dict]:
        out = []
        for ih, h in self._handles.items():
            try:
                st = h.status()
            except Exception:  # noqa: BLE001
                continue
            out.append(
                {
                    "infohash": ih.lower(),
                    "name": st.name,
                    "state": str(st.state),
                    "progress": st.progress,
                    "size": st.total_wanted,
                    "download_rate": st.download_rate,
                    "upload_rate": st.upload_rate,
                    "num_seeds": st.num_seeds,
                    "num_peers": st.num_peers,
                    "is_seeding": st.is_seeding,
                }
            )
        return out

    def infohashes(self) -> set[str]:
        return {h.lower() for h in self._handles}
