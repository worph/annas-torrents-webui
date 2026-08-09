"""Fortress hardening regressions (R0–R1)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import warnings
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="annas-fortress-"))
os.environ.setdefault("TORRENT_PORT", "0")
os.environ.setdefault("ALLOW_UNAUTHENTICATED_API", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


class SnapshotInvalidationTests(unittest.TestCase):
    def test_remove_clears_snapshot_cache(self):
        from fastapi.testclient import TestClient

        from app import main as main_mod
        from app import runtime as runtime_mod
        from app.main import app

        client = TestClient(app)
        main_mod._snapshot_cache["data"] = {
            "global": {"backend_ok": True},
            "torrents": [{"infohash": "a" * 40}],
            "controls": {},
            "provision": {},
            "coverage": {},
            "connection": "connected",
        }
        main_mod._snapshot_cache["json"] = "{}"

        class FakeSess:
            def infohashes(self):
                return {"a" * 40}

            def remove_torrents(self, hashes, delete_files=False):
                return {"removed": 1, "files_deleted": False}

        with mock.patch.object(runtime_mod, "session", FakeSess()):
            r = client.post(
                "/api/torrents/remove",
                json={"infohash": "a" * 40, "confirm": True, "delete_files": False},
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(main_mod._snapshot_cache["data"])


class LibtorrentStatsTests(unittest.TestCase):
    def test_global_status_avoids_deprecated_session_status(self):
        from app.session_libtorrent import LibtorrentSession

        with tempfile.TemporaryDirectory() as td:
            content = os.path.join(td, "c")
            torrents = os.path.join(td, "t")
            resume = os.path.join(td, "r")
            sess = LibtorrentSession(content, torrents, resume, listen_port=0)
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", DeprecationWarning)
                    g = sess.global_status()
                deprecated = [
                    w
                    for w in caught
                    if issubclass(w.category, DeprecationWarning)
                    and "status()" in str(w.message)
                ]
                self.assertEqual(deprecated, [], [str(w.message) for w in deprecated])
                for key in (
                    "download_rate",
                    "upload_rate",
                    "total_download",
                    "total_upload",
                    "dht_nodes",
                    "backend_ok",
                ):
                    self.assertIn(key, g)
                self.assertTrue(g["backend_ok"])
            finally:
                sess.close()


class ParityTests(unittest.TestCase):
    def test_parity_qbit_custom_dest_free_space_unknown(self):
        from app.session_qbittorrent import QBittorrentSession

        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess.save_path = "/downloads"
        sess._active_save_path = "/other"
        self.assertIsNone(
            sess._disk_free_for_status("/other", {"free_space_on_disk": 999})
        )
        self.assertEqual(
            sess._disk_free_for_status("/downloads", {"free_space_on_disk": 999}),
            999,
        )

    def test_parity_qbit_add_applies_limits_when_category_was_empty(self):
        from app.session_qbittorrent import QBittorrentSession

        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess.save_path = ""
        sess.category = "annas"
        sess._resolved_category = "annas"
        sess._preallocated = set()
        sess._downloads_paused = False
        sess._seeding_paused = False
        sess._desired_upload_limit = 125_000
        sess._desired_download_limit = -1
        sess._active_save_path = None
        ih = "d" * 40
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(path)
            return mock.Mock(status_code=200, text="Ok.", json=lambda: [])

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, f"{ih}.torrent")
            with open(path, "wb") as f:
                f.write(b"d4:infod4:name1:aee")
            with mock.patch.object(sess, "_ensure_category"), mock.patch.object(
                sess, "infohashes", side_effect=[set(), {ih}]
            ), mock.patch.object(sess, "_request", side_effect=fake_request):
                self.assertEqual(sess.add_torrent_file(path), ih)
        self.assertIn("/api/v2/torrents/setUploadLimit", calls)

    def test_parity_qbit_preallocate_flag_restored(self):
        from app.session_qbittorrent import QBittorrentSession

        with tempfile.TemporaryDirectory() as td:
            sess = QBittorrentSession.__new__(QBittorrentSession)
            sess.torrents_dir = os.path.join(td, "torrents")
            os.makedirs(sess.torrents_dir)
            flag = sess._prealloc_flag_path()
            with open(flag, "w", encoding="utf-8") as f:
                f.write("0\n")
            calls = []

            def fake_request(method, path, **kwargs):
                calls.append(path)
                return mock.Mock(status_code=200, text="Ok.", json=lambda: {})

            with mock.patch.object(sess, "_request", side_effect=fake_request):
                sess._restore_preallocate_if_flagged()
            self.assertFalse(os.path.exists(flag))
            self.assertIn("/api/v2/app/setPreferences", calls)


if __name__ == "__main__":
    unittest.main()
