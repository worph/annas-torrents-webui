"""qBittorrent Web API session — read/seed via your qBittorrent.

Lists torrents in a category (default: Anna's Archive Torrents) for the dashboard.
New provisioned torrents are pushed into that same category.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import storage

log = logging.getLogger("session.qbittorrent")


def _qbittorrent_rate_limit(bytes_per_sec: int) -> int:
    """Map app rate (-1 unlimited, 0 blocked) to qBit (0 is rewritten to unlimited)."""
    if bytes_per_sec < 0:
        return -1
    if bytes_per_sec == 0:
        return 1  # nearest stop; qBit converts 0 → -1 (unlimited)
    return int(bytes_per_sec)


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

_SEEDING_STATES = frozenset({"uploading", "forcedUP"})
_LEGACY_CATEGORY = "Anna's Archive"
_DEFAULT_CATEGORY = "Anna's Archive Torrents"


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
        category: str = "Anna's Archive Torrents",
        save_path: str | None = None,
    ):
        self.content_dir = content_dir
        self.torrents_dir = torrents_dir
        self.category = category
        self.save_path = (save_path or "").strip() or None
        self._active_save_path: str | None = None
        self._preallocate_enabled = False
        self._preallocate_original: bool | None = None
        for d in (content_dir, torrents_dir):
            os.makedirs(d, exist_ok=True)

        self._base = qbit_url.rstrip("/")
        self._user = qbit_user
        self._pass = qbit_pass
        # Never follow cross-host redirects — credentials must stay on the configured host.
        self._client = httpx.Client(base_url=self._base, timeout=30.0, follow_redirects=False)
        self._auth_error: str | None = None
        self._auth_backoff_until = 0.0
        self._resolved_category: str | None = None
        self._desired_upload_limit = -1  # -1 = unlimited
        self._desired_download_limit = -1
        self._seeding_paused = False
        self._downloads_paused = False
        self._controls_synced = False
        self._preallocated: set[str] = set()
        self._status_torrents: list[dict] | None = None
        self._status_batch = False

    def _assert_host_still_safe(self) -> None:
        """Re-check host so DNS rebinding cannot reach metadata or public cleartext."""
        from .settings import _http_allowed_without_tls, _qbit_host_blocked

        parsed = urlparse(self._base)
        host = parsed.hostname or ""
        if _qbit_host_blocked(host):
            raise RuntimeError("qBittorrent host resolves to a blocked metadata address")
        if (parsed.scheme or "").lower() == "http" and not _http_allowed_without_tls(host):
            raise RuntimeError(
                "qBittorrent HTTP host no longer resolves to a private/loopback address"
            )

    def _login(self) -> None:
        self._assert_host_still_safe()
        r = self._client.post(
            "/api/v2/auth/login",
            data={"username": self._user, "password": self._pass},
        )
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {r.status_code} {r.text[:200]}")
        self._auth_error = None
        log.info("logged into qBittorrent at %s", urlparse(self._base).netloc or self._base)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        now = time.time()
        if self._auth_backoff_until > now:
            raise RuntimeError(self._auth_error or "qBittorrent auth backoff")
        self._assert_host_still_safe()

        # Try first without login (qBit "bypass auth for localhost").
        r = self._client.request(method, path, **kwargs)
        if 300 <= r.status_code < 400:
            raise RuntimeError(
                f"qBittorrent redirected ({r.status_code}); refusing to follow off-host redirects"
            )
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
        if 300 <= r.status_code < 400:
            raise RuntimeError(
                f"qBittorrent redirected ({r.status_code}); refusing to follow off-host redirects"
            )
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
                _LEGACY_CATEGORY
                if self.category == _DEFAULT_CATEGORY and _LEGACY_CATEGORY in cats
                else self.category,
            )
        if self._resolved_category != self.category:
            log.info("resolved category %r → %r", self.category, self._resolved_category)
        return self._resolved_category

    def _ensure_category(self) -> str:
        """Create the category in qBit if missing (with optional default save path)."""
        cat = self._category()
        cats = self._request("GET", "/api/v2/torrents/categories").json()
        if cat in cats:
            return cat
        data: dict[str, str] = {"category": cat}
        if self.save_path:
            data["savePath"] = self.save_path
        self._request("POST", "/api/v2/torrents/createCategory", data=data)
        self._resolved_category = cat
        log.info("created qBittorrent category %r", cat)
        return cat

    def set_category(self, name: str) -> None:
        """Change category used for list/add (clears name-resolution cache)."""
        self.category = name.strip()
        self._resolved_category = None
        self._active_save_path = None

    def default_save_path(self) -> str:
        """Empty means qBittorrent's own default destination."""
        return self.save_path or ""

    def storage_options(self, extra: list[str] | None = None) -> list[dict]:
        """Remote qBit paths are choices, not paths on this Python host."""
        default = self.default_save_path()
        options = [
            storage.remote_option(
                default,
                label="qBittorrent default" if not default else "Configured qBittorrent path",
                default=True,
            )
        ]
        for path in extra or []:
            if path and not any(storage.matches_destination(path, item["path"]) for item in options):
                options.append(storage.remote_option(path, label="In use"))
        return options

    def load_existing(self) -> int:
        """Import whatever is already in the qBittorrent category (soft on startup)."""
        try:
            self.verify()
            n = len(self.infohashes())
            log.info("imported %d torrents from qBittorrent category %r", n, self._category())
            return n
        except Exception as e:  # noqa: BLE001
            log.warning("qBittorrent unreachable on startup: %s", e)
            return 0

    def _sync_controls_from_qbit(self) -> None:
        """Rebuild local pause/limit mirrors from this category once per session.

        Reads per-torrent limits (the scope this app writes), not global server_state.
        """
        if self._controls_synced:
            return
        try:
            torrents = self._torrents()
            if torrents:
                h = self._hash_of(torrents[0])
                if h:
                    up = self._request(
                        "GET", "/api/v2/torrents/uploadLimit", params={"hashes": h}
                    ).json()
                    down = self._request(
                        "GET", "/api/v2/torrents/downloadLimit", params={"hashes": h}
                    ).json()
                    if isinstance(up, dict) and h in up:
                        up_i = int(up[h])
                        self._desired_upload_limit = -1 if up_i <= 0 else up_i
                    if isinstance(down, dict) and h in down:
                        down_i = int(down[h])
                        self._desired_download_limit = -1 if down_i <= 0 else down_i
            self._controls_synced = True
        except Exception as e:  # noqa: BLE001
            log.warning("could not sync qBittorrent controls: %s", e)

    def verify(self) -> None:
        """Raise if qBittorrent is unreachable or auth fails (used on settings switch)."""
        self._ensure_category()
        self.infohashes()
        self._restore_preallocate_if_flagged()
        self._sync_controls_from_qbit()

    def _prealloc_flag_path(self) -> str:
        return os.path.join(os.path.dirname(self.torrents_dir), ".qbit_prealloc_restore")

    def _enable_preallocate(self) -> None:
        """Temporarily enable qBittorrent's global preallocate_all; restore later."""
        if self._preallocate_enabled:
            return
        # Global preference — refuse when other categories still have torrents.
        try:
            all_torrents = self._request("GET", "/api/v2/torrents/info").json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not list qBittorrent torrents for preallocate: {e}") from e
        cat = self._category()
        if any((t.get("category") or "") != cat for t in all_torrents):
            raise RuntimeError(
                "disk preallocation refused — qBittorrent has torrents outside this category"
            )
        prefs = self._request("GET", "/api/v2/app/preferences").json()
        self._preallocate_original = bool(prefs.get("preallocate_all", False))
        if not self._preallocate_original:
            # Write the restore flag BEFORE mutating qBit so a crash mid-call
            # still leaves a durable undo hint.
            try:
                with open(self._prealloc_flag_path(), "w", encoding="utf-8") as f:
                    f.write("0\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                raise RuntimeError(f"could not write preallocate restore flag: {e}") from e
            self._request(
                "POST",
                "/api/v2/app/setPreferences",
                data={"json": json.dumps({"preallocate_all": True})},
            )
        self._preallocate_enabled = True
        log.info(
            "qBittorrent preallocate_all enabled for provision (was %s)",
            self._preallocate_original,
        )

    def _restore_preallocate_if_flagged(self) -> None:
        """Undo sticky preallocate_all left behind by a crash mid-provision."""
        flag = self._prealloc_flag_path()
        if not os.path.isfile(flag):
            return
        try:
            self._request(
                "POST",
                "/api/v2/app/setPreferences",
                data={"json": json.dumps({"preallocate_all": False})},
            )
            log.info("restored qBittorrent preallocate_all from crash-recovery flag")
        except Exception as e:  # noqa: BLE001
            log.warning("could not restore preallocate_all from flag: %s", e)
            return
        try:
            os.unlink(flag)
        except OSError:
            pass

    def _restore_preallocate(self) -> None:
        if not self._preallocate_enabled:
            return
        restored = True
        try:
            if self._preallocate_original is False:
                self._request(
                    "POST",
                    "/api/v2/app/setPreferences",
                    data={"json": json.dumps({"preallocate_all": False})},
                )
                log.info("restored qBittorrent preallocate_all to False")
        except Exception as e:  # noqa: BLE001
            restored = False
            log.warning("could not restore qBittorrent preallocate_all: %s", e)
        if restored:
            self._preallocate_enabled = False
            self._preallocate_original = None
            try:
                os.unlink(self._prealloc_flag_path())
            except OSError:
                pass

    def add_torrent_file(
        self, path: str, save_path: str | None = None, *, preallocate: bool = False
    ) -> str | None:
        """Push a .torrent into qBittorrent. Returns infohash if known, else None.

        Omits savepath when neither an explicit save_path nor QBIT_SAVE_PATH is set,
        so qBittorrent uses its own default download folder.
        """
        self._ensure_category()
        if preallocate:
            try:
                self._enable_preallocate()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"could not enable qBittorrent disk preallocation: {e}") from e
        try:
            # Prefer the infohash from the .torrent filename (we name by v1 hash).
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            expected_ih = stem if len(stem) == 40 and all(c in "0123456789abcdef" for c in stem) else None
            before = self.infohashes()
            dest = (save_path or "").strip() or self.save_path
            with open(path, "rb") as f:
                files = {"torrents": (os.path.basename(path), f, "application/x-bittorrent")}
                data: dict[str, str] = {
                    "category": self._category(),
                    "tags": "annas-archive",
                }
                if dest:
                    data["savepath"] = dest
                r = self._request("POST", "/api/v2/torrents/add", files=files, data=data)
            body = (r.text or "").strip()
            # Modern qBit may return empty body, JSON, or 202 while the add is pending.
            code = r.status_code if isinstance(r.status_code, int) else 200
            if body == "Fails." or code not in (200, 202):
                log.warning("qBittorrent rejected add for %s (%s): %s", path, code, r.text[:200])
                return None
            if body and body not in ("Ok.",) and not body.startswith("{"):
                log.warning("unexpected add response for %s: %s", path, r.text[:200])
            accepted = body in ("Ok.", "") or body.startswith("{") or code == 202
            if not body and code in (200, 202):
                accepted = True
            if save_path:
                self._active_save_path = save_path
            after = before
            for _ in range(6):
                try:
                    after = self.infohashes()
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
                    continue
                if expected_ih and expected_ih in after:
                    break
                if after - before:
                    break
                time.sleep(0.5)
            new = after - before
            ih = None
            if expected_ih and expected_ih in after:
                ih = expected_ih
            elif len(new) == 1:
                ih = next(iter(new))
            elif expected_ih and accepted:
                # Accepted but not listed — do not count as added.
                log.info(
                    "qBittorrent accepted %s; hash %s not yet listed — not counting as added",
                    path,
                    expected_ih,
                )
            if ih:
                log.info("added torrent %s (%s) → %s", os.path.basename(path), ih, dest or "qBit default")
                if preallocate:
                    self._preallocated.add(ih)
                # Apply current category limits to the new hash (setters only touch existing hashes).
                try:
                    if not self._seeding_paused:
                        self._request(
                            "POST",
                            "/api/v2/torrents/setUploadLimit",
                            data={
                                "hashes": ih,
                                "limit": str(_qbittorrent_rate_limit(self._desired_upload_limit)),
                            },
                        )
                    self._request(
                        "POST",
                        "/api/v2/torrents/setDownloadLimit",
                        data={
                            "hashes": ih,
                            "limit": str(_qbittorrent_rate_limit(self._desired_download_limit)),
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("could not apply rate limits to new torrent %s: %s", ih, e)
                if self._downloads_paused:
                    try:
                        self._request("POST", "/api/v2/torrents/pause", data={"hashes": ih})
                    except Exception as e:  # noqa: BLE001
                        pass
                elif self._seeding_paused:
                    try:
                        info = self._request(
                            "GET", "/api/v2/torrents/info", params={"hashes": ih}
                        ).json()
                        if info and self._progress(info[0]) >= 1.0:
                            self._request("POST", "/api/v2/torrents/pause", data={"hashes": ih})
                    except Exception as e:  # noqa: BLE001
                        pass
            return ih
        finally:
            # Restore global preference after each add — never leave sticky True.
            if preallocate:
                self._restore_preallocate()

    def save_resume(self) -> None:
        """No-op — qBittorrent persists its own resume data."""

    def close(self) -> None:
        self._restore_preallocate()
        self._client.close()

    def _torrents(self) -> list[dict]:
        r = self._request("GET", "/api/v2/torrents/info", params={"category": self._category()})
        return r.json()

    def begin_status_batch(self) -> None:
        """Fetch torrents once for a paired global_status + torrents_status call."""
        self._status_torrents = self._torrents()
        self._status_batch = True

    def end_status_batch(self) -> None:
        self._status_torrents = None
        self._status_batch = False

    def _batch_or_fetch_torrents(self) -> list[dict]:
        if self._status_batch and self._status_torrents is not None:
            return self._status_torrents
        return self._torrents()

    @staticmethod
    def _hash_of(t: dict) -> str:
        # Prefer v1 — Anna's Archive index keys on classic infohash.
        return (t.get("infohash_v1") or t.get("hash") or "").lower()

    @staticmethod
    def _swarm_known(t: dict, swarm_key: str) -> bool:
        if swarm_key not in t or t[swarm_key] is None:
            return False
        return int(t[swarm_key]) >= 0

    @staticmethod
    def _progress(t: dict) -> float:
        """qBittorrent progress in [0, 1]; NaN/Inf → 0 (matches libtorrent guard)."""
        try:
            p = float(t.get("progress", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        if p != p or p in (float("inf"), float("-inf")):
            return 0.0
        return min(1.0, max(0.0, p))

    @staticmethod
    def _completed_bytes(t: dict, *, preallocate: bool = False) -> int:
        """Bytes on disk for this torrent (not lifetime downloaded, which includes waste)."""
        if "completed" in t and t["completed"] is not None:
            done = max(0, int(t["completed"]))
        else:
            size = int(t.get("total_size", 0) or 0)
            done = max(0, int(size * QBittorrentSession._progress(t)))
        size = int(t.get("total_size", 0) or 0)
        amount_left = t.get("amount_left")
        if amount_left is not None and size > 0:
            try:
                left = max(0, int(amount_left))
                done = max(done, size - left)
            except (TypeError, ValueError):
                pass
        # While this torrent was added with preallocate_all, qBit reserves full size.
        if preallocate and size > done and QBittorrentSession._progress(t) < 1.0:
            return size
        return done

    def _status_storage_path(self) -> str:
        return self._active_save_path or self.default_save_path()

    def _disk_free_for_status(self, storage_path: str, server_state: dict) -> int | None:
        """qBit free space: only trust free_space_on_disk for the default save path.

        Never query local volumes — a Compose /data mount can collide with a
        remote qBit path string and report the wrong disk.
        """
        if storage_path == "multiple destinations":
            return None
        if "free_space_on_disk" not in server_state or server_state.get("free_space_on_disk") is None:
            return None
        default = (self.default_save_path() or "").strip()
        path = (storage_path or "").strip()
        # Empty path / matching configured default → qBit's free_space_on_disk applies.
        if path and default and storage.path_key(path) != storage.path_key(default):
            return None
        if path and not default:
            # Explicit non-empty destination with no configured default — unknown.
            return None
        return max(0, int(server_state.get("free_space_on_disk") or 0))

    def global_status(self) -> dict:
        storage_path = self._status_storage_path()
        try:
            torrents = self._batch_or_fetch_torrents()
            save_paths = {
                str(t.get("save_path") or "").strip()
                for t in torrents
                if str(t.get("save_path") or "").strip()
            }
            if self._active_save_path:
                save_paths.add(self._active_save_path)
            if not torrents:
                self._active_save_path = None
            if len(save_paths) == 1:
                storage_path = next(iter(save_paths))
            elif save_paths:
                storage_path = "multiple destinations"
            dht_nodes = 0
            disk_free = None
            try:
                main = self._request("GET", "/api/v2/sync/maindata").json()
                server_state = main.get("server_state", {})
                dht_nodes = int(server_state.get("dht_nodes", 0))
                disk_free = self._disk_free_for_status(storage_path, server_state)
            except Exception:  # noqa: BLE001
                # Do not fall back to local disk_usage — wrong volume when qBit is remote.
                disk_free = None
            # Keep pause_all coverage for torrents that finish after seeding was paused.
            if self._seeding_paused:
                done = [
                    h
                    for t in torrents
                    if self._progress(t) >= 1.0
                    and t.get("state") not in ("pausedUP", "pausedDL", "stoppedUP", "stoppedDL")
                    and (h := self._hash_of(t))
                ]
                if done:
                    try:
                        self._request(
                            "POST", "/api/v2/torrents/pause", data={"hashes": "|".join(done)}
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return {
                "download_rate": sum(int(t.get("dlspeed", 0)) for t in torrents),
                "upload_rate": sum(int(t.get("upspeed", 0)) for t in torrents),
                "total_download": sum(int(t.get("downloaded", 0)) for t in torrents),
                "total_upload": sum(int(t.get("uploaded", 0)) for t in torrents),
                "num_torrents": len(torrents),
                # Same semantics as per-torrent / libtorrent: connected leeches only.
                "num_peers": sum(int(t.get("num_leechs", 0) or 0) for t in torrents),
                "dht_nodes": dht_nodes,
                "committed_bytes": sum(
                    self._completed_bytes(
                        t, preallocate=self._hash_of(t) in self._preallocated
                    )
                    for t in torrents
                ),
                "disk_free": 0 if disk_free is None else disk_free,
                "disk_free_known": disk_free is not None,
                "disk_total": 0,
                "storage_path": storage_path,
                "backend_ok": True,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("global_status failed: %s", e)
            if not self._status_batch:
                self._status_torrents = None
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
                "disk_free_known": False,
                "disk_total": 0,
                "storage_path": storage_path,
                "backend_ok": False,
                "error": str(e),
            }

    def torrents_status(self) -> list[dict]:
        try:
            torrents = self._batch_or_fetch_torrents()
        except Exception as e:  # noqa: BLE001
            log.warning("torrents_status failed: %s", e)
            return []
        out = []
        for t in torrents:
            state = t.get("state", "")
            progress = self._progress(t)
            size = int(t.get("total_size", 0))
            paused = state in ("pausedUP", "pausedDL", "stoppedUP", "stoppedDL")
            mapped = "paused" if paused else _STATE.get(state, state)
            ih = self._hash_of(t)
            done = self._completed_bytes(t, preallocate=ih in self._preallocated)
            missing = mapped == "missing_files"
            out.append(
                {
                    "infohash": ih,
                    "name": t.get("name", ""),
                    "state": mapped,
                    "progress": progress,
                    "size": size,
                    "downloaded": done,
                    "allocated_bytes": done,
                    "download_rate": int(t.get("dlspeed", 0)),
                    "upload_rate": int(t.get("upspeed", 0)),
                    # qBit UI style: connected (swarm). num_* = connected; *_total = tracker/swarm.
                    "num_seeds": int(t.get("num_seeds", 0) or 0),
                    "seeds_total": (
                        int(t["num_complete"])
                        if self._swarm_known(t, "num_complete")
                        else None
                    ),
                    "num_peers": int(t.get("num_leechs", 0) or 0),
                    "peers_total": (
                        int(t["num_incomplete"])
                        if self._swarm_known(t, "num_incomplete")
                        else None
                    ),
                    "seeds_known": self._swarm_known(t, "num_complete"),
                    "is_seeding": state in _SEEDING_STATES and not paused and not missing,
                    "is_complete": progress >= 1.0 and not missing,
                    "paused": paused,
                    "save_path": t.get("save_path") or "",
                }
            )
        return out

    def infohashes(self) -> set[str]:
        return {h for t in self._torrents() if (h := self._hash_of(t))}

    def pause_all(self) -> None:
        """Stop seeding everywhere; incomplete keep downloading (global upload=0)."""
        prev = self._seeding_paused
        self._seeding_paused = True
        limit_applied = False
        try:
            self._apply_upload_limit(0)
            limit_applied = True
            hashes = [
                h
                for t in self._torrents()
                if self._progress(t) >= 1.0 and (h := self._hash_of(t))
            ]
            if hashes:
                self._request("POST", "/api/v2/torrents/pause", data={"hashes": "|".join(hashes)})
        except Exception:
            self._seeding_paused = prev
            if limit_applied and not prev:
                try:
                    self._apply_upload_limit(self._desired_upload_limit)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def resume_all(self) -> None:
        prev = self._seeding_paused
        self._seeding_paused = False
        try:
            self._apply_upload_limit(self._desired_upload_limit)
            hashes = [
                h
                for t in self._torrents()
                if self._progress(t) >= 1.0 and (h := self._hash_of(t))
            ]
            if hashes:
                self._request("POST", "/api/v2/torrents/resume", data={"hashes": "|".join(hashes)})
        except Exception:
            self._seeding_paused = prev
            if prev:
                try:
                    self._apply_upload_limit(0)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def _category_hashes(self) -> list[str]:
        return [h for h in (self._hash_of(t) for t in self._torrents()) if h]

    def set_upload_limit(self, bytes_per_sec: int) -> None:
        """Per-torrent upload limit for this category only. Pass -1 for unlimited."""
        prev = self._desired_upload_limit
        self._desired_upload_limit = int(bytes_per_sec)
        try:
            if not self._seeding_paused:
                self._apply_upload_limit(self._desired_upload_limit)
        except Exception:
            self._desired_upload_limit = prev
            raise

    def _apply_upload_limit(self, bytes_per_sec: int) -> None:
        limit = _qbittorrent_rate_limit(bytes_per_sec)
        hashes = self._category_hashes()
        if not hashes:
            return
        self._request(
            "POST",
            "/api/v2/torrents/setUploadLimit",
            data={"hashes": "|".join(hashes), "limit": str(limit)},
        )

    def _incomplete_hashes(self) -> list[str]:
        out = []
        for t in self._torrents():
            if self._progress(t) < 1.0:
                h = self._hash_of(t)
                if h:
                    out.append(h)
        return out

    def pause_downloads(self) -> None:
        prev = self._downloads_paused
        self._downloads_paused = True
        try:
            hashes = self._incomplete_hashes()
            if hashes:
                self._request("POST", "/api/v2/torrents/pause", data={"hashes": "|".join(hashes)})
        except Exception:
            self._downloads_paused = prev
            raise

    def resume_downloads(self) -> None:
        prev = self._downloads_paused
        self._downloads_paused = False
        try:
            hashes = self._incomplete_hashes()
            if hashes:
                self._request("POST", "/api/v2/torrents/resume", data={"hashes": "|".join(hashes)})
        except Exception:
            self._downloads_paused = prev
            raise

    def set_download_limit(self, bytes_per_sec: int) -> None:
        """Per-torrent download limit for this category only. Pass -1 for unlimited."""
        prev = self._desired_download_limit
        self._desired_download_limit = int(bytes_per_sec)
        try:
            limit = _qbittorrent_rate_limit(bytes_per_sec)
            hashes = self._category_hashes()
            if hashes:
                self._request(
                    "POST",
                    "/api/v2/torrents/setDownloadLimit",
                    data={"hashes": "|".join(hashes), "limit": str(limit)},
                )
        except Exception:
            self._desired_download_limit = prev
            raise

    def controls_state(self) -> dict:
        from .ux import controls_payload

        self._sync_controls_from_qbit()
        return controls_payload(
            seeding_paused=self._seeding_paused,
            downloads_paused=self._downloads_paused,
            upload_limit=self._desired_upload_limit,
            download_limit=self._desired_download_limit,
        )

    def remove_torrents(self, infohashes: list[str], delete_files: bool = True) -> dict:
        """Remove torrents. Returns ``{removed, files_deleted}``.

        ``files_deleted`` is False when shared-content refuse applied, True when
        ``delete_files`` was false (N/A kept as None), and None when qBit was
        asked to delete files (disk outcome unverified).
        """
        hashes = [h.lower() for h in infohashes if h]
        if not hashes:
            return {"removed": 0, "files_deleted": None if not delete_files else True}
        want = set(hashes)
        files_ok = True
        effective_delete = delete_files
        if delete_files:
            from .pathsafety import shared_content_ids

            torrents = self._torrents()
            entries = [
                (self._hash_of(t), t.get("save_path") or "", t.get("name") or "")
                for t in torrents
            ]
            # Preflight against the full set (victims + others) before any delete.
            shared = shared_content_ids(entries)
            if any(h in shared for h in want):
                for h in want:
                    if h in shared:
                        name = next((n for i, _s, n in entries if i == h), h)
                        log.warning(
                            "refusing qBittorrent file delete for %s — shared content root",
                            name or h,
                        )
                files_ok = False
                effective_delete = False
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if effective_delete else "false",
            },
        )
        deadline = time.time() + 5.0
        remaining = set(hashes)
        while remaining and time.time() < deadline:
            try:
                known = self.infohashes()
            except Exception:  # noqa: BLE001
                break
            remaining = {h for h in remaining if h in known}
            if not remaining:
                break
            time.sleep(0.2)
        gone = [h for h in hashes if h not in remaining]
        for h in gone:
            self._preallocated.discard(h)
        if remaining:
            log.warning("qBittorrent delete incomplete; still present: %s", sorted(remaining)[:5])
        # Only purge local .torrent metadata when content delete was requested and
        # not refused — keep files for re-add when deleteFiles=false / shared.
        if effective_delete and gone:
            from .session import purge_torrent_files

            purge_torrent_files(self.torrents_dir, gone)
        if not gone:
            return {"removed": 0, "files_deleted": False if delete_files else None}
        if not delete_files:
            return {"removed": len(gone), "files_deleted": None}
        if not files_ok:
            return {"removed": len(gone), "files_deleted": False}
        if remaining:
            return {"removed": len(gone), "files_deleted": False}
        # qBit deleted entries; physical file removal is not confirmed here.
        return {"removed": len(gone), "files_deleted": None}
