"""Verify disk preallocation is applied by the torrent backends."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


class PreallocateTests(unittest.TestCase):
    def test_libtorrent_sets_allocate_storage_mode(self):
        try:
            import libtorrent as lt
        except ImportError:
            self.skipTest("libtorrent not installed")
        from app.session_libtorrent import LibtorrentSession

        size = 256 * 1024
        with tempfile.TemporaryDirectory(prefix="prealloc_") as td:
            src_dir = os.path.join(td, "src")
            torrents = os.path.join(td, "torrents")
            os.makedirs(src_dir)
            os.makedirs(torrents)
            src = os.path.join(src_dir, "blob.bin")
            with open(src, "wb") as f:
                f.write(b"\0" * size)
            fs = lt.file_storage()
            lt.add_files(fs, src)
            ct = lt.create_torrent(fs, piece_size=16 * 1024)
            lt.set_piece_hashes(ct, src_dir)
            tpath = os.path.join(torrents, "blob.torrent")
            with open(tpath, "wb") as f:
                f.write(lt.bencode(ct.generate()))
            os.unlink(src)

            for flag, expect in ((False, "storage_mode_sparse"), (True, "storage_mode_allocate")):
                content = os.path.join(td, f"c_{int(flag)}")
                resume = os.path.join(td, f"r_{int(flag)}")
                os.makedirs(content)
                os.makedirs(resume)
                sess = LibtorrentSession(content, torrents, resume, listen_port=0)
                try:
                    ih = sess.add_torrent_file(tpath, content, preallocate=flag)
                    mode = str(sess._handles[ih].status().storage_mode)
                    self.assertEqual(mode, expect)
                    self.assertIsNotNone(ih)
                finally:
                    sess.close()

    def test_libtorrent_preallocate_applies_with_resume(self):
        try:
            import libtorrent as lt
        except ImportError:
            self.skipTest("libtorrent not installed")
        from app.session_libtorrent import LibtorrentSession

        size = 256 * 1024
        with tempfile.TemporaryDirectory(prefix="prealloc_resume_") as td:
            src_dir = os.path.join(td, "src")
            torrents = os.path.join(td, "torrents")
            content = os.path.join(td, "content")
            resume = os.path.join(td, "resume")
            os.makedirs(src_dir)
            os.makedirs(torrents)
            os.makedirs(content)
            os.makedirs(resume)
            src = os.path.join(src_dir, "blob.bin")
            with open(src, "wb") as f:
                f.write(b"\0" * size)
            fs = lt.file_storage()
            lt.add_files(fs, src)
            ct = lt.create_torrent(fs, piece_size=16 * 1024)
            lt.set_piece_hashes(ct, src_dir)
            tpath = os.path.join(torrents, "blob.torrent")
            with open(tpath, "wb") as f:
                f.write(lt.bencode(ct.generate()))
            os.unlink(src)

            sess = LibtorrentSession(content, torrents, resume, listen_port=0)
            try:
                ih = sess.add_torrent_file(tpath, content, preallocate=False)
                sess.save_resume()
                h = sess._handles.pop(ih)
                sess._ses.remove_torrent(h)
                ih2 = sess.add_torrent_file(tpath, content, preallocate=True)
                self.assertEqual(ih, ih2)
                mode = str(sess._handles[ih2].status().storage_mode)
                self.assertEqual(mode, "storage_mode_allocate")
            finally:
                sess.close()

    def test_qbittorrent_enables_preallocate_all_once(self):
        from app.session_qbittorrent import QBittorrentSession

        with tempfile.TemporaryDirectory(prefix="qbit_pre_") as td:
            content = os.path.join(td, "content")
            torrents = os.path.join(td, "torrents")
            os.makedirs(content)
            os.makedirs(torrents)
            tpath = os.path.join(torrents, "x.torrent")
            with open(tpath, "wb") as f:
                f.write(b"dummy")

            sess = QBittorrentSession(
                content,
                torrents,
                qbit_url="http://127.0.0.1:9",
                qbit_user="admin",
                qbit_pass="x",
                category="Anna's Archive Torrents",
            )
            calls: list[tuple] = []

            def fake_request(method, path, **kwargs):
                calls.append((method, path, kwargs))
                resp = mock.Mock()
                resp.text = "Ok."
                if path == "/api/v2/torrents/categories":
                    resp.json.return_value = {"Anna's Archive Torrents": {}}
                elif path == "/api/v2/torrents/info":
                    resp.json.return_value = []
                elif path == "/api/v2/app/preferences":
                    resp.json.return_value = {"preallocate_all": False}
                else:
                    resp.json.return_value = {}
                return resp

            sess._request = fake_request  # type: ignore[method-assign]
            sess.infohashes = lambda: set()  # type: ignore[method-assign]

            sess.add_torrent_file(tpath, content, preallocate=True)
            sess.add_torrent_file(tpath, content, preallocate=True)
            enable = [
                c
                for c in calls
                if c[1] == "/api/v2/app/setPreferences"
                and json.loads(c[2]["data"]["json"]).get("preallocate_all") is True
            ]
            # Restore after each add so the global pref is never left sticky.
            self.assertEqual(len(enable), 2)
            restore = [
                c
                for c in calls
                if c[1] == "/api/v2/app/setPreferences"
                and json.loads(c[2]["data"]["json"]).get("preallocate_all") is False
            ]
            self.assertGreaterEqual(len(restore), 2)
            sess.close()

    def test_qbittorrent_global_num_peers_counts_leeches_only(self):
        """Match per-torrent / libtorrent: num_peers = connected leeches, not seeds+leeches."""
        from app.session_qbittorrent import QBittorrentSession

        with tempfile.TemporaryDirectory(prefix="qbit_peers_") as td:
            content = os.path.join(td, "content")
            torrents = os.path.join(td, "torrents")
            os.makedirs(content)
            os.makedirs(torrents)
            sess = QBittorrentSession(
                content,
                torrents,
                qbit_url="http://127.0.0.1:9",
                qbit_user="admin",
                qbit_pass="x",
                category="Anna's Archive Torrents",
            )
            sess._torrents = lambda: [  # type: ignore[method-assign]
                {
                    "save_path": content,
                    "dlspeed": 0,
                    "upspeed": 0,
                    "downloaded": 0,
                    "uploaded": 0,
                    "num_leechs": 3,
                    "num_seeds": 7,
                    "progress": 1.0,
                    "total_size": 0,
                },
                {
                    "save_path": content,
                    "dlspeed": 0,
                    "upspeed": 0,
                    "downloaded": 0,
                    "uploaded": 0,
                    "num_leechs": 2,
                    "num_seeds": 10,
                    "progress": 1.0,
                    "total_size": 0,
                },
            ]
            sess._request = mock.Mock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]
            g = sess.global_status()
            self.assertEqual(g["num_peers"], 5)  # 3+2 leeches; not 3+7+2+10
            sess.close()


if __name__ == "__main__":
    unittest.main()
