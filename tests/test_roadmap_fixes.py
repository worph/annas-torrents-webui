"""Behavioral checks for the post-audit roadmap fixes."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="annas-roadmap-"))
os.environ.setdefault("TORRENT_PORT", "0")
os.environ.setdefault("ALLOW_UNAUTHENTICATED_API", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.selection import _parse_bencode, _torrent_infohash  # noqa: E402
from app.session_qbittorrent import QBittorrentSession  # noqa: E402
from app.storage import path_key  # noqa: E402


class RoadmapFixTests(unittest.TestCase):
    def test_available_free_rejects_mismatched_qbit_dest(self):
        from app import main as main_mod

        class FakeSess:
            def global_status(self):
                return {
                    "backend_ok": True,
                    "disk_free_known": True,
                    "disk_free": 9_000_000_000,
                    "storage_path": "/downloads",
                }

        async def run():
            with mock.patch.object(main_mod, "_call_session_object", new=mock.AsyncMock()) as call:
                async def _fake(_sess, method, *args, **kwargs):
                    self.assertEqual(method, "global_status")
                    return FakeSess().global_status()

                call.side_effect = _fake
                got = await main_mod._available_free(FakeSess(), "qbittorrent", "/other-disk")
                self.assertIsNone(got)
                matched = await main_mod._available_free(FakeSess(), "qbittorrent", "/downloads")
                self.assertEqual(matched, 9_000_000_000)

        asyncio.run(run())
        self.assertEqual(path_key("/downloads"), path_key("/downloads"))

    def test_provision_task_done_clears_stuck_running(self):
        from app import main as main_mod

        main_mod.provision_state.update(
            running=True,
            phase="selecting",
            message="fetching",
            finished_at=None,
        )

        class FakeTask:
            def cancelled(self):
                return True

        main_mod._provision_task_done(FakeTask())
        self.assertFalse(main_mod.provision_state["running"])
        self.assertEqual(main_mod.provision_state["phase"], "error")
        self.assertIn("cancelled", main_mod.provision_state["message"])

    def test_bencode_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            _parse_bencode(b"d1:ai1e1:ai2ee")

    def test_torrent_rejects_duplicate_info(self):
        # Two info dictionaries — must not silently pick the last one.
        raw = b"d4:infod4:name1:ae4:infod4:name1:bee"
        with self.assertRaises(ValueError):
            _torrent_infohash(raw)

    def test_qbit_status_batch_reuses_list_only_inside_batch(self):
        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess._preallocated = set()
        sess._status_torrents = None
        sess._status_batch = False
        sample = [
            {
                "hash": "a" * 40,
                "name": "one",
                "state": "uploading",
                "progress": 1.0,
                "total_size": 10,
                "completed": 10,
                "dlspeed": 0,
                "upspeed": 0,
                "num_seeds": 1,
                "num_leechs": 0,
                "save_path": "/data",
            }
        ]
        sess._status_torrents = sample
        sess._status_batch = True
        with mock.patch.object(sess, "_torrents", side_effect=AssertionError("should reuse batch")):
            rows = sess.torrents_status()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["infohash"], "a" * 40)
        # Outside a batch, an independent call must fetch fresh (no stale reuse).
        sess.end_status_batch()
        with mock.patch.object(sess, "_torrents", return_value=[]) as fetch:
            self.assertEqual(sess.torrents_status(), [])
            fetch.assert_called_once()

    def test_qbit_add_applies_desired_rate_limits(self):
        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess.save_path = ""
        sess.category = "annas"
        sess._resolved_category = "annas"
        sess._preallocated = set()
        sess._downloads_paused = False
        sess._seeding_paused = False
        sess._desired_upload_limit = 125_000
        sess._desired_download_limit = 250_000
        sess._active_save_path = None
        ih = "b" * 40
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, dict(kwargs)))
            return mock.Mock(status_code=200, text="Ok.", json=lambda: [])

        with tempfile.TemporaryDirectory() as td:
            torrent_path = os.path.join(td, f"{ih}.torrent")
            with open(torrent_path, "wb") as f:
                f.write(b"d4:infod4:name1:aee")
            with mock.patch.object(sess, "_ensure_category"), mock.patch.object(
                sess, "infohashes", side_effect=[set(), {ih}]
            ), mock.patch.object(sess, "_request", side_effect=fake_request):
                got = sess.add_torrent_file(torrent_path)
        self.assertEqual(got, ih)
        limit_paths = [p for _m, p, _k in calls if "Limit" in p]
        self.assertIn("/api/v2/torrents/setUploadLimit", limit_paths)
        self.assertIn("/api/v2/torrents/setDownloadLimit", limit_paths)
        up = next(k for m, p, k in calls if p == "/api/v2/torrents/setUploadLimit")
        self.assertEqual(up["data"]["hashes"], ih)
        self.assertEqual(up["data"]["limit"], "125000")

    def test_save_resume_ignores_foreign_alerts(self):
        from app.session_libtorrent import LibtorrentSession

        class FakeLt:
            class save_resume_data_alert:
                pass

            @staticmethod
            def write_resume_data_buf(_params):
                return b"resume"

        with tempfile.TemporaryDirectory() as td:
            sess = LibtorrentSession.__new__(LibtorrentSession)
            sess.resume_dir = td
            sess._handles = {}
            sess._ses = mock.Mock()
            stale = FakeLt.save_resume_data_alert()
            stale.handle = mock.Mock()
            stale.handle.info_hash.return_value = "deadbeef" * 5
            stale.params = object()
            sess._ses.pop_alerts.side_effect = [[stale], []]

            handle = mock.Mock()
            handle.is_valid.return_value = True
            handle.status.return_value = mock.Mock(has_metadata=True)
            sess._handles["aabbccdd" * 5] = handle

            with mock.patch("app.session_libtorrent.lt", FakeLt), mock.patch(
                "app.session_libtorrent.time"
            ) as time_mod:
                time_mod.time.side_effect = [0.0, 0.0, 10.0]
                time_mod.sleep = mock.Mock()
                sess.save_resume()

            self.assertFalse(os.path.exists(os.path.join(td, ("deadbeef" * 5) + ".fastresume")))
            handle.save_resume_data.assert_called_once()


if __name__ == "__main__":
    unittest.main()
