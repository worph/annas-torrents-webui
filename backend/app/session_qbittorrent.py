"""qBittorrent Web API session — read/seed via your qBittorrent.

Lists torrents in a category (default: Anna's Archive) for the dashboard.
New provisioned torrents are pushed into that same category.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .disks import disk_for_path, list_disks

log = logging.getLogger("session.qbittorrent")

_STATE = {
    "error": "error",
    "missingFiles": "missing_files",
    "uploading": "seeding",
    "pausedUP": "paused",
    "queuedUP": "queued",
    "stalledUP": "stalled",
    "checkingUP": "checking",
    "forcedUP": "seeding",
    "allocating": "allocating",
    "downloading": "downloading",
    "metaDL": "downloading",
    "pausedDL": "paused",
    "queuedDL": "queued",
    "stalledDL": "stalled",
    "checkingDL": "checking",
    "forcedDL": "downloading",
    "checkingResumeData": "checking",
    "moving": "moving",
}


def _norm_cat(s: str) -> str:
    return s.replace("\u2019", "'").replace("\u2018", "'").casefold()


class QBittorrentSession:
    def __init__(
        self,
        content_dir: str,
        torrents_dir: str,
        *,
        qbit_url: str,
        qbit_user: str = "admin",
        qbit_pass: str = "",
        category: str = "Anna's Archive",
        save_path: str | None = None,
    ):
        self.content_dir = content_dir
        self.torrents_dir = torrents_dir
        self.category = category
        self.save_path = save_path
        for d in (content_dir, torrents_dir):
            os.makedirs(d, exist_ok=True)

        self._base = qbit_url.rstrip("/")
        self._user = qbit_user
        self._pass = qbit_pass
        self._client = httpx.Client(base_url=self._base, timeout=30.0, follow_redirects=True)
        self._authed = False
        self._auth_error: str | None = None
        self._auth_backoff_until = 0.0
        self._resolved_category: str | None = None

    def _login(self) -> None:
        r = self._client.post(
            "/api/v2/auth/login",
            data={"username": self._user, "password": self._pass},
        )
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {r.status_code} {r.text[:200]}")
        self._authed = True
        self._auth_error = None
        log.info("logged into qBittorrent at %s", self._base)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        now = time.time()
        if self._auth_backoff_until > now:
            raise RuntimeError(self._auth_error or "qBittorrent auth backoff")

        # Try first without login (qBit "bypass auth for localhost").
        r = self._client.request(method, path, **kwargs)
        if r.status_code != 403:
            r.raise_for_status()
            return r

        # No password configured → don't spam login (gets the IP banned).
        if not self._pass:
            self._auth_error = (
                "qBittorrent auth required — set QBIT_PASS or enable "
                "'Bypass authentication for clients on localhost'"
            )
            self._auth_backoff_until = now + 60
            raise RuntimeError(self._auth_error)

        try:
            self._login()
        except Exception as e:  # noqa: BLE001
            self._auth_error = str(e)
            self._auth_backoff_until = now + 60
            raise
        r = self._client.request(method, path, **kwargs)
        r.raise_for_status()
        return r

    def _category(self) -> str:
        """Match configured name to qBit's real category (ASCII vs curly apostrophe)."""
        if self._resolved_category is not None:
            return self._resolved_category
        want = _norm_cat(self.category)
        try:
            cats = self._request("GET", "/api/v2/torrents/categories").json()
        except Exception:  # noqa: BLE001
            return self.category
        if self.category in cats:
            self._resolved_category = self.category
        else:
            self._resolved_category = next(
                (name for name in cats if _norm_cat(name) == want),
                self.category,
            )
        if self._resolved_category != self.category:
            log.info("resolved category %r → %r", self.category, self._resolved_category)
        return self._resolved_category

    def load_existing(self) -> int:
        """Import whatever is already in the qBittorrent category."""
        try:
            n = len(self.infohashes())
            log.info("imported %d torrents from qBittorrent category %r", n, self._category())
            return n
        except Exception as e:  # noqa: BLE001
            log.warning("qBittorrent unreachable on startup: %s", e)
            return 0

    def add_torrent_file(self, path: str) -> str | None:
        """Push a .torrent into qBittorrent. Returns infohash if known, else None."""
        before = self.infohashes()
        with open(path, "rb") as f:
            files = {"torrents": (os.path.basename(path), f, "application/x-bittorrent")}
            data: dict[str, str] = {
                "category": self._category(),
                "tags": "annas-archive",
            }
            if self.save_path:
                data["savepath"] = self.save_path
            r = self._request("POST", "/api/v2/torrents/add", files=files, data=data)
        if r.text.strip() not in ("Ok.", "Fails."):
            log.warning("unexpected add response for %s: %s", path, r.text[:200])
        after = self.infohashes()
        if after <= before:
            time.sleep(0.5)
            after = self.infohashes()
        ih = next(iter(after - before), None)
        if ih:
            log.info("added torrent %s (%s)", os.path.basename(path), ih)
        return ih

    def save_resume(self) -> None:
        """No-op — qBittorrent persists its own resume data."""

    def _torrents(self) -> list[dict]:
        r = self._request("GET", "/api/v2/torrents/info", params={"category": self._category()})
        return r.json()

    @staticmethod
    def _hash_of(t: dict) -> str:
        # Prefer v1 — Anna's Archive index keys on classic infohash.
        return (t.get("infohash_v1") or t.get("hash") or "").lower()

    @staticmethod
    def _swarm_count(t: dict, swarm_key: str, connected_key: str) -> int:
        """Use swarm total when qBit has it; -1/missing → connected count."""
        if swarm_key in t and t[swarm_key] is not None:
            n = int(t[swarm_key])
            if n >= 0:
                return n
        return int(t.get(connected_key, 0) or 0)

    def global_status(self) -> dict:
        disks = list_disks()
        try:
            torrents = self._torrents()
            dht_nodes = 0
            try:
                main = self._request("GET", "/api/v2/sync/maindata").json()
                dht_nodes = int(main.get("server_state", {}).get("dht_nodes", 0))
            except Exception:  # noqa: BLE001
                pass
            disk_free = disks[0]["free"] if disks else 0
            disk_total = disks[0]["total"] if disks else 0
            matched = disk_for_path(self.save_path, disks)
            if matched:
                disk_free, disk_total = matched["free"], matched["total"]
            return {
                "download_rate": sum(int(t.get("dlspeed", 0)) for t in torrents),
                "upload_rate": sum(int(t.get("upspeed", 0)) for t in torrents),
                "total_download": sum(int(t.get("downloaded", 0)) for t in torrents),
                "total_upload": sum(int(t.get("uploaded", 0)) for t in torrents),
                "num_torrents": len(torrents),
                "num_peers": sum(int(t.get("num_leechs", 0)) for t in torrents),
                "dht_nodes": dht_nodes,
                "committed_bytes": sum(int(t.get("total_size", 0)) for t in torrents),
                "disk_free": disk_free,
                "disk_total": disk_total,
                "disks": disks,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("global_status failed: %s", e)
            return {
                "download_rate": 0,
                "upload_rate": 0,
                "total_download": 0,
                "total_upload": 0,
                "num_torrents": 0,
                "num_peers": 0,
                "dht_nodes": 0,
                "committed_bytes": 0,
                "disk_free": 0,
                "disk_total": 0,
                "disks": disks,
            }

    def torrents_status(self) -> list[dict]:
        try:
            torrents = self._torrents()
        except Exception as e:  # noqa: BLE001
            log.warning("torrents_status failed: %s", e)
            return []
        out = []
        for t in torrents:
            state = t.get("state", "")
            out.append(
                {
                    "infohash": self._hash_of(t),
                    "name": t.get("name", ""),
                    "state": _STATE.get(state, state),
                    "progress": float(t.get("progress", 0)),
                    "size": int(t.get("total_size", 0)),
                    "download_rate": int(t.get("dlspeed", 0)),
                    "upload_rate": int(t.get("upspeed", 0)),
                    # Swarm totals when known (>= 0); -1 means tracker N/A → connected counts.
                    "num_seeds": self._swarm_count(t, "num_complete", "num_seeds"),
                    "num_peers": self._swarm_count(t, "num_incomplete", "num_leechs"),
                    "is_seeding": state in ("uploading", "forcedUP", "stalledUP", "queuedUP", "pausedUP"),
                }
            )
        return out

    def infohashes(self) -> set[str]:
        return {h for t in self._torrents() if (h := self._hash_of(t))}
