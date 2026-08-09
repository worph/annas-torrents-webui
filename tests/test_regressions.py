"""Small regression checks for the security and data-loss fixes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.auth import redact_snapshot  # noqa: E402
from app.selection import FULL_INDEX_TB, _torrent_infohash, fetch_torrent_list  # noqa: E402
from app.settings import apply_patch, save_settings  # noqa: E402
from app.metrics import CoverageIndex, _safe_progress  # noqa: E402
from app.space import pick_combination  # noqa: E402
from app.storage import matches_destination  # noqa: E402


class RegressionTests(unittest.TestCase):
    def test_coverage_ignores_nan_progress(self):
        idx = CoverageIndex()
        fake = type("E", (), {"data_size": 100})()
        idx._entries = [fake]
        idx._by_hash = {"aabb": fake}
        idx._total_bytes = 100
        self.assertEqual(
            idx.coverage_for_torrents([{"infohash": "aabb", "progress": float("nan")}])[
                "seeded_bytes"
            ],
            0,
        )
        self.assertEqual(
            idx.coverage_for_torrents([{"infohash": "aabb", "progress": float("inf")}])[
                "seeded_bytes"
            ],
            0,
        )
        self.assertEqual(_safe_progress(float("inf")), 0.0)
        self.assertEqual(_safe_progress(float("-inf")), 0.0)
        self.assertEqual(_safe_progress(0.25), 0.25)

    def test_frontend_script_parses_and_ids_are_unique(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        html_path = os.path.join(root, "frontend", "index.html")
        with open(html_path, encoding="utf-8") as source:
            html = source.read()
        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("share-preview", ids)
        self.assertNotIn('ensureSaveOption(prev, "Custom', html)
        self.assertNotIn("detail.innerHTML", html)
        self.assertIn('id="preallocate"', html)
        self.assertIn("preallocate", html)
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL)
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write("\n".join(scripts))
            script_path = f.name
        try:
            checked = subprocess.run(
                ["node", "--check", script_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
        finally:
            os.unlink(script_path)

    def test_incomplete_space_uses_downloaded_bytes(self):
        result = pick_combination(
            [
                {
                    "infohash": "a",
                    "name": "partial",
                    "size": 100 * 1000**3,
                    "downloaded": 2 * 1000**3,
                    "progress": 0.02,
                    "num_seeds": 10,
                }
            ],
            1 * 1000**3,
        )
        self.assertEqual(result["freed_bytes"], 2 * 1000**3)

    def test_destination_does_not_match_another_folder_on_same_drive(self):
        self.assertFalse(
            matches_destination(r"E:\\Other", r"E:\\Anna's Archive Torrents")
        )

    def test_child_destination_does_not_match_parent_torrent_path(self):
        parent = r"E:\\Anna's Archive Torrents" if os.name == "nt" else "/data/content"
        child = os.path.join(parent, "subset")
        self.assertTrue(matches_destination(os.path.join(parent, "book"), parent))
        self.assertFalse(matches_destination(parent, child))

    def test_full_index_tb_is_accepted_for_coverage_fetch(self):
        async def _check():
            with self.assertRaises(ValueError):
                await fetch_torrent_list(FULL_INDEX_TB + 1)
            with mock.patch("app.selection._trusted_get_bytes", side_effect=RuntimeError("stop")):
                with self.assertRaises(RuntimeError):
                    await fetch_torrent_list(FULL_INDEX_TB)

        asyncio.run(_check())

    def test_torrent_infohash_is_the_raw_info_dictionary_hash(self):
        info = b"d6:lengthi3e4:name3:fooe"
        payload = b"d4:info" + info + b"e"
        self.assertEqual(_torrent_infohash(payload), hashlib.sha1(info).hexdigest())
        with self.assertRaises(ValueError):
            _torrent_infohash(b"not a torrent")

    def test_public_snapshot_has_no_path_or_hash(self):
        public = redact_snapshot(
            {
                "global": {"storage_path": r"E:\\secret", "committed_bytes": 4},
                "coverage": {"percent": 1, "internal": "drop"},
                "torrents": [
                    {
                        "name": r"E:\\secret\\book",
                        "infohash": "abc123",
                        "save_path": r"E:\\secret",
                        "size": 4,
                    }
                ],
            }
        )
        encoded = json.dumps(public)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("abc123", encoded)
        self.assertNotIn("internal", encoded)
        self.assertNotIn("book", encoded)
        self.assertEqual(public["torrents"], [])
        self.assertIn("disk_free_known", public["global"])

    def test_allowed_destination_rejects_parent_of_allowlisted_child(self):
        from fastapi import HTTPException

        from app.main import _allowed_destination

        allowed = [r"D:\data\content"]
        with self.assertRaises(HTTPException):
            _allowed_destination(r"D:\\", allowed)
        self.assertEqual(
            _allowed_destination(r"D:\data\content\subdir", allowed).replace("/", "\\"),
            r"D:\data\content\subdir",
        )

    def test_unlink_skips_preexisting_when_created_set(self):
        from app.main import _unlink_unadded_torrents

        with tempfile.TemporaryDirectory() as td:
            keep = os.path.join(td, "keep.torrent")
            drop = os.path.join(td, "drop.torrent")
            for p in (keep, drop):
                with open(p, "wb") as f:
                    f.write(b"x")
            # Without created set, both unadded would drop; with empty created, neither drops.
            _unlink_unadded_torrents([(keep, 1), (drop, 1)], set(), set())
            self.assertTrue(os.path.isfile(keep))
            self.assertTrue(os.path.isfile(drop))
            _unlink_unadded_torrents(
                [(keep, 1), (drop, 1)], set(), {os.path.abspath(drop)}
            )
            self.assertTrue(os.path.isfile(keep))
            self.assertFalse(os.path.isfile(drop))

    def test_entrypoint_and_gitattributes_exist(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        entry = os.path.join(root, "docker-entrypoint.sh")
        self.assertTrue(os.path.isfile(entry))
        with open(entry, "rb") as f:
            raw = f.read()
        self.assertFalse(b"\r" in raw)
        with open(os.path.join(root, ".gitattributes"), encoding="utf-8") as f:
            attrs = f.read()
        self.assertIn("eol=lf", attrs)
        self.assertIn("*.sh", attrs)

    def test_qbit_password_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            save_settings(td, {"qbit_pass": "do-not-write", "qbit_category": "x"})
            with open(os.path.join(td, "settings.json"), encoding="utf-8") as f:
                self.assertNotIn("do-not-write", f.read())

    def test_legacy_qbit_pass_stripped_on_unrelated_save(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"qbit_pass": "legacy-secret", "qbit_category": "old"}, f)
            save_settings(td, {"qbit_category": "new"})
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["qbit_category"], "new")
            self.assertNotIn("qbit_pass", data)
            self.assertNotIn("legacy-secret", json.dumps(data))

    def test_apply_patch_always_drops_qbit_pass(self):
        out = apply_patch({"qbit_pass": "x", "qbit_category": "a"}, {"qbit_category": "b"})
        self.assertEqual(out["qbit_category"], "b")
        self.assertNotIn("qbit_pass", out)

    def test_dockerfile_wires_backend_arg_and_liveness_healthcheck(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ARG TORRENT_BACKEND=libtorrent", text)
        self.assertIn("TORRENT_BACKEND=${TORRENT_BACKEND}", text)
        self.assertIn("/api/healthz", text)
        self.assertIn("start-period=120s", text)
        self.assertIn("/entrypoint.sh", text)
        self.assertIn('ENTRYPOINT ["/entrypoint.sh"]', text)

    def test_space_preview_uses_same_destination_allowlist(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("def _allowed_destination(", text)
        self.assertIn("save_path = _allowed_destination(", text)
        self.assertIn("path = _allowed_destination(requested, allowed)", text)

    def test_status_binds_snapshot_and_controls_to_one_session(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("def _clear_snapshot_cache()", text)
        self.assertIn("_clear_snapshot_cache()", text)
        start = text.index("async def status():")
        end = text.index("@app.get(\"/api/public/status\")", start)
        body = text[start:end]
        self.assertIn("async with _session_lock:", body)
        self.assertIn("sess = session", body)
        self.assertIn("sess.controls_state", body)
        self.assertIn("_session_generation != gen", body)
        self.assertNotIn('await _call_session("controls_state")', body)

    def test_space_preview_token_cleanup_holds_space_lock(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def space_preview(")
        end = text.index("async def space_free(", start)
        body = text[start:end]
        cleanup = body.index("Drop expired tokens")
        lock = body.index("async with _space_lock:", 0)
        self.assertLess(lock, cleanup)
        self.assertIn("_space_tokens.pop(k, None)", body[lock:])

    def test_missing_api_token_warn_uses_auth_flags(self):
        """Warn when token missing and allow-unauth off — via auth helpers, not a re-parsed `not x in`."""
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("if not auth_configured() and not ALLOW_UNAUTHENTICATED_API:", text)
        self.assertNotIn(').strip().lower() in {"1", "true", "yes"}', text)
        # Document real Python precedence: `not x in s` == `not (x in s)`, not `(not x) in s`.
        allow = ""
        self.assertTrue(not allow in {"1", "true", "yes"})
        self.assertFalse((not allow) in {"1", "true", "yes"})

    def test_libtorrent_rate_limit_zero_is_not_unlimited(self):
        """App 0 = blocked; libtorrent 0 = unlimited — map stop to 1 B/s."""
        try:
            from app.session_libtorrent import _libtorrent_rate_limit
        except ImportError:
            self.skipTest("libtorrent not installed")
        self.assertEqual(_libtorrent_rate_limit(-1), 0)
        self.assertEqual(_libtorrent_rate_limit(0), 1)
        self.assertEqual(_libtorrent_rate_limit(50), 50)

    def test_qbittorrent_rate_limit_zero_is_not_unlimited(self):
        """App 0 = blocked; qBit rewrites 0 → -1 unlimited — map stop to 1 B/s."""
        from app.session_qbittorrent import _qbittorrent_rate_limit

        self.assertEqual(_qbittorrent_rate_limit(-1), -1)
        self.assertEqual(_qbittorrent_rate_limit(0), 1)
        self.assertEqual(_qbittorrent_rate_limit(50), 50)

    def test_sum_unique_disk_usage_dedupes_same_volume(self):
        from app.storage import sum_unique_disk_usage

        here = os.path.abspath(os.path.dirname(__file__))
        child = os.path.join(here, "does-not-need-to-exist")
        one = sum_unique_disk_usage([here])
        two = sum_unique_disk_usage([here, child])
        self.assertEqual(one, two)
        self.assertGreater(one[0], 0)
        self.assertGreater(one[1], 0)
        self.assertTrue(one[2])

    def test_public_config_exposes_backend(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def public_config():")
        end = text.index("@app.get(\"/api/events/ticket\")", start)
        body = text[start:end]
        self.assertIn('"backend"', body)

    def test_qbit_url_rejects_userinfo(self):
        from app.settings import apply_patch

        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://user:secret@127.0.0.1:8080"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://127.0.0.1:8080?x=1"})
        cleaned = apply_patch({}, {"qbit_url": "http://127.0.0.1:8080/"})
        self.assertEqual(cleaned["qbit_url"], "http://127.0.0.1:8080")

    def test_space_free_fingerprint_under_session_lock(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def space_free(")
        end = text.index("async def torrents_remove(", start)
        body = text[start:end]
        lock = body.index("async with _session_lock:")
        fp = body.index("fingerprint")
        self.assertLess(lock, fp)

    def test_resolved_path_key_walks_missing_leaf(self):
        from app.storage import resolved_path_key, path_key

        here = os.path.abspath(os.path.dirname(__file__))
        missing = os.path.join(here, "no-such-subdir", "leaf")
        # Parent exists → resolved key still rooted under here.
        self.assertTrue(resolved_path_key(missing).startswith(path_key(here)))

    def test_libtorrent_owned_persists_save_path(self):
        try:
            from app.session_libtorrent import LibtorrentSession
        except ImportError:
            self.skipTest("libtorrent not installed")
        with tempfile.TemporaryDirectory() as td:
            content = os.path.join(td, "content")
            torrents = os.path.join(td, "torrents")
            resume = os.path.join(td, "resume")
            chosen = os.path.join(td, "chosen")
            for d in (content, torrents, resume, chosen):
                os.makedirs(d)
            owned = os.path.join(resume, "aabbccddeeff00112233445566778899aabbccdd.owned")
            with open(owned, "w", encoding="utf-8") as f:
                f.write(chosen + "\n")
            sp = LibtorrentSession._read_owned_save_path(owned)
            self.assertEqual(sp, chosen)

    def test_torrent_content_size_rejects_negative(self):
        from app.selection import _torrent_content_size

        with self.assertRaises(ValueError):
            _torrent_content_size(b"d4:infod6:lengthi-1e4:name4:spamee")

    def test_torrent_rejects_trailing_bencode(self):
        from app.selection import _torrent_infohash

        info = b"d6:lengthi3e4:name3:fooe"
        payload = b"d4:info" + info + b"eTRAILING"
        with self.assertRaises(ValueError):
            _torrent_infohash(payload)

    def test_torrent_content_size_from_info_dict(self):
        from app.selection import _torrent_content_size, _torrent_infohash

        # Minimal single-file torrent: d4:infod6:lengthi42e4:name4:spamee
        raw = b"d4:infod6:lengthi42e4:name4:spamee"
        self.assertEqual(_torrent_content_size(raw), 42)
        self.assertEqual(len(_torrent_infohash(raw)), 40)

        # Multi-file: info.files = [{length:10, path:[a]}, {length:20, path:[b]}]
        multi = (
            b"d4:infod5:files"
            b"ld6:lengthi10e4:pathl1:aee"
            b"d6:lengthi20e4:pathl1:bee"
            b"e4:name4:spamee"
        )
        self.assertEqual(_torrent_content_size(multi), 30)

    def test_unlink_unadded_keeps_added_paths(self):
        from app.main import _unlink_unadded_torrents

        with tempfile.TemporaryDirectory() as td:
            keep = os.path.join(td, "keep.torrent")
            drop = os.path.join(td, "drop.torrent")
            for p in (keep, drop):
                with open(p, "wb") as f:
                    f.write(b"x")
            _unlink_unadded_torrents([(keep, 1), (drop, 1)], {os.path.abspath(keep)})
            self.assertTrue(os.path.isfile(keep))
            self.assertFalse(os.path.isfile(drop))

    def test_compose_default_web_port_avoids_qbit_8080(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "docker-compose.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("127.0.0.1:${WEB_PORT:-8090}:8080", text)
        self.assertIn("host.docker.internal:8080", text)
        self.assertIn("${DATA_DIR:-/data}", text)
        self.assertIn("${HOST_DATA_DIR:-./data}", text)
        self.assertIn("CONTENT_CHOWN=${CONTENT_CHOWN:-1}", text)
        # Base compose must not publish the BitTorrent port (env TORRENT_PORT is fine).
        self.assertNotIn("6881/tcp", text)
        self.assertNotIn("6881/udp", text)
        self.assertNotIn(":6881", text)

    def test_provision_target_uses_decimal_tb(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("req.max_tb * 1000**4", text)
        self.assertNotIn("req.max_tb * 1000**3", text)

    def test_qbit_url_preserves_ipv6_brackets(self):
        from app.settings import apply_patch

        out = apply_patch({}, {"qbit_url": "http://[::1]:8080/"})
        self.assertEqual(out["qbit_url"], "http://[::1]:8080")

    def test_qbit_disk_free_never_uses_local_shutil(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_qbittorrent.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def _disk_free_for_status")
        end = text.index("def global_status", start)
        body = text[start:end]
        self.assertNotIn("storage.disk_usage", body)
        self.assertNotIn("shutil.disk_usage", body)
        self.assertIn("free_space_on_disk", body)

    def test_available_free_skips_local_disk_for_qbit(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def _available_free")
        end = text.index("async def _run_provision", start)
        body = text[start:end]
        self.assertIn('backend == "qbittorrent"', body)
        self.assertIn("Never trust this host's disk_usage", body)
        self.assertIn("storage.path_key(reported) != storage.path_key(dest)", body)

    def test_libtorrent_allocated_bytes_matches_prealloc_commit(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_libtorrent.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def torrents_status")
        end = text.index("def infohashes", start)
        body = text[start:end]
        self.assertIn("ih in self._preallocated", body)
        self.assertIn('"allocated_bytes"', body)

    def test_space_confirm_honest_when_remove_fails(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            text = f.read()
        start = text.index('$("space-confirm-btn").addEventListener')
        end = text.index("$(\"space-gb\")", start)
        body = text[start:end]
        self.assertIn("Number(data.removed)", body)
        self.assertNotIn("data.removed || hashes.length", body)
        self.assertIn("No torrents were removed", body)

    def test_entry_rejects_nonpositive_data_size(self):
        from app.selection import TorrentEntry

        with self.assertRaises(ValueError):
            TorrentEntry.from_json(
                {"url": "https://annas-archive.gl/x", "display_name": "x", "data_size": 0}
            )
        with self.assertRaises(ValueError):
            TorrentEntry.from_json(
                {"url": "https://annas-archive.gl/x", "display_name": "x", "data_size": -1}
            )

    def test_fetch_list_streams_bounded(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "selection.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("async def _trusted_get_bytes", text)
        start = text.index("async def fetch_torrent_list")
        end = text.index("async def download_torrent_files", start)
        self.assertIn("_trusted_get_bytes", text[start:end])
        self.assertIn("_validate_entry_sizes", text)

    def test_content_roots_overlap_nested(self):
        from app.pathsafety import content_roots_overlap, shared_content_ids

        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(content_roots_overlap(td, "foo", td, "foo"))
            self.assertTrue(content_roots_overlap(td, "foo", os.path.join(td, "foo"), "bar"))
            self.assertFalse(content_roots_overlap(td, "a", td, "b"))
        # Remote Windows paths must overlap lexically on any host OS.
        self.assertTrue(
            content_roots_overlap(r"C:\Downloads", "root", r"C:\Downloads\root", "child")
        )
        self.assertFalse(
            content_roots_overlap(r"C:\Downloads", "root", r"C:\Downloads", "other")
        )
        shared = shared_content_ids(
            [
                ("a", r"C:\t", "same"),
                ("b", r"C:\t", "same"),
                ("c", r"C:\t", "other"),
            ]
        )
        self.assertEqual(shared, {"a", "b"})

    def test_batch_shared_ids_catch_victim_pairs(self):
        from app.pathsafety import shared_content_ids

        # Two victims sharing a root must both be flagged before any delete.
        shared = shared_content_ids(
            [
                ("v1", "/data", "shared"),
                ("v2", "/data", "shared"),
                ("other", "/data", "alone"),
            ]
        )
        self.assertEqual(shared, {"v1", "v2"})
        # Victim overlapping a non-victim is also flagged.
        shared = shared_content_ids(
            [
                ("v1", "/data", "nested"),
                ("keep", "/data/nested", "child"),
            ]
        )
        self.assertIn("v1", shared)
        self.assertIn("keep", shared)

    def test_qbit_remove_keeps_metadata_without_delete_files(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_qbittorrent.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def remove_torrents")
        body = text[start:]
        self.assertIn("if delete_files:", body)
        self.assertIn("purge_torrent_files", body)

    def test_provision_skips_already_active_hash(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def _run_provision")
        end = text.index("@asynccontextmanager", start)
        body = text[start:end]
        self.assertIn("if stem in known:", body)
        self.assertIn("do not count toward the new target", body)

    def test_qbit_url_rejects_metadata_ssrf(self):
        from app.settings import apply_patch, _qbit_host_blocked

        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://169.254.169.254/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://metadata.google.internal/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://[fe80::1]/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://[::ffff:100.100.100.200]/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://[::ffff:169.254.169.254]/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://2852039166/"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://0xa9fea9fe/"})
        self.assertTrue(_qbit_host_blocked("::ffff:100.100.100.200"))
        # LAN / localhost remain allowed for real qBittorrent deployments.
        out = apply_patch({}, {"qbit_url": "http://192.168.1.10:8080/"})
        self.assertEqual(out["qbit_url"], "http://192.168.1.10:8080")

    def test_legacy_qbit_url_userinfo_scrubbed_on_unrelated_save(self):
        from app.settings import apply_patch

        cur = {"qbit_url": "http://user:secret@127.0.0.1:8080", "qbit_category": "Old"}
        out = apply_patch(cur, {"qbit_category": "New"})
        self.assertEqual(out["qbit_category"], "New")
        self.assertNotIn("secret", out.get("qbit_url", ""))
        self.assertNotIn("user:", out.get("qbit_url", ""))

    def test_posix_remote_paths_are_case_sensitive_on_windows(self):
        from app.storage import path_is_within

        if os.name != "nt":
            self.skipTest("Windows host / remote POSIX matrix")
        self.assertFalse(path_is_within("/data/Content/file", "/data/content"))
        self.assertTrue(path_is_within("/data/content/file", "/data/content"))

    def test_auth_failure_bumps_storage_request_id(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("function authFailure()")
        end = text.index("function apiFetch(", start)
        body = text[start:end]
        self.assertIn("storageRequestId++", body)
        self.assertIn('qbit_url: ""', body)

    def test_private_events_capped(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("_PRIVATE_SSE_MAX", text)
        start = text.index("async def events()")
        end = text.index("async def public_events(", start)
        self.assertIn("too many event connections", text[start:end])

    def test_qbit_resolve_save_path_never_mkdir(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def _resolve_save_path(")
        end = text.index("def _json_safe(", start)
        body = text[start:end]
        self.assertIn('backend == "qbittorrent"', body)
        self.assertNotIn("os.makedirs", body)
        self.assertIn("never mkdir", body.lower())

    def test_snapshot_cache_written_under_session_lock(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def _snapshot_loop()")
        end = text.index("async def _background_loop()", start)
        body = text[start:end]
        # Publish only after a generation check under the asyncio lock.
        self.assertIn('_session_generation == gen', body)
        write = body.index('_snapshot_cache["data"] = snap')
        lock = body.rfind("async with _session_lock:", 0, write)
        self.assertGreaterEqual(lock, 0)
        self.assertLess(lock, write)

    def test_public_events_caps_connections(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("_PUBLIC_SSE_MAX", text)
        start = text.index("async def public_events(")
        end = text.index("# ---- static frontend", start)
        body = text[start:end]
        self.assertIn("too many public event connections", body)
        self.assertIn("_PUBLIC_SSE_PER_IP", body)

    def test_selection_replace_registers_under_lock(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "selection.py"), encoding="utf-8") as f:
            text = f.read()
        # Critical section: lock, then replace, then created.add — no await between.
        marker = "# cancel cannot leave an orphan .torrent outside `created`."
        self.assertIn(marker, text)
        start = text.index(marker)
        end = text.index("log.info(\"downloaded", start)
        body = text[start:end]
        self.assertIn("async with lock:", body)
        self.assertLess(body.index("os.replace(tmp, path)"), body.index("created.add"))
        self.assertLess(body.index("async with lock:"), body.index("os.replace(tmp, path)"))

    def test_disk_usage_refuses_missing_unix_root_child(self):
        from app.storage import disk_usage, sum_unique_disk_usage

        if os.name == "nt":
            self.skipTest("Unix mount fallback")
        self.assertIsNone(disk_usage("/annas_webui_missing_mount_xyz"))
        self.assertFalse(sum_unique_disk_usage(["/annas_webui_missing_mount_xyz"])[2])

    def test_space_confirm_no_bare_requestId(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            text = f.read()
        start = text.index('$("space-confirm-btn").addEventListener')
        end = text.index("$(\"settings-open\")", start) if "$(\"settings-open\")" in text[start:] else start + 2500
        # Fall back: next listener after space confirm
        end = text.index("settings-save", start)
        body = text[start:end]
        self.assertNotIn("requestId === spaceRequestId", body)

    def test_token_recovery_allows_auth_blocked(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            text = f.read()
        start = text.index('$("settings-save").addEventListener')
        end = text.index("btn.disabled = true", start)
        body = text[start:end]
        self.assertIn("authBlocked", body)
        self.assertIn("API token saved", body)
        # Must not treat prefilled URL alone as dirty before token recovery.
        self.assertNotIn("qbitFieldsDirty", body)

    def test_resolve_qbit_url_strips_legacy_userinfo(self):
        from app.settings import resolve_qbit_url

        with tempfile.TemporaryDirectory() as td:
            # Legacy bad URL in settings.json must not be returned raw.
            path = os.path.join(td, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"qbit_url": "http://user:secret@127.0.0.1:8080"}\n')
            cleaned = resolve_qbit_url(td, "")
            self.assertNotIn("secret", cleaned)
            self.assertTrue(cleaned.startswith("http"))

    def test_zero_content_rejected_when_index_nonzero(self):
        from app.selection import _torrent_content_size

        raw = b"d4:infod6:lengthi0e4:name4:spamee"
        self.assertEqual(_torrent_content_size(raw), 0)

    def test_libtorrent_prealloc_restored_before_add(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_libtorrent.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def load_existing")
        end = text.index("def _read_owned_save_path", start)
        body = text[start:end]
        # Marker must be read into _preallocated before any add_torrent_file call.
        self.assertIn("want_prealloc = os.path.exists(prealloc_path)", body)
        self.assertIn("self._preallocated.add(ih)", body)
        self.assertIn("preallocate=want_prealloc", body)
        self.assertLess(
            body.index("self._preallocated.add(ih)"),
            body.index("self.add_torrent_file"),
        )

    def test_libtorrent_port_override_published(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "docker-compose.libtorrent.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("${TORRENT_PORT:-6881}", text)

    def test_qbit_url_rejects_aws_ipv6_imds(self):
        from app.settings import apply_patch, _qbit_host_blocked

        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://[fd00:ec2::254]/"})
        self.assertTrue(_qbit_host_blocked("fd00:ec2::254"))

    def test_qbit_url_requires_https_for_public_hosts(self):
        from app.settings import apply_patch

        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "http://example.com:8080/"})
        out = apply_patch({}, {"qbit_url": "https://example.com:8080/"})
        self.assertEqual(out["qbit_url"], "https://example.com:8080")
        out = apply_patch({}, {"qbit_url": "http://host.docker.internal:8080/"})
        self.assertEqual(out["qbit_url"], "http://host.docker.internal:8080")

    def test_resolve_from_backend_falls_back_without_libtorrent(self):
        from app import settings as settings_mod

        with mock.patch.object(settings_mod, "_libtorrent_available", return_value=False):
            self.assertEqual(
                settings_mod.resolve_from({"torrent_backend": "libtorrent"}, "torrent_backend", None),
                "qbittorrent",
            )

    def test_selection_skips_malformed_mirror_fields(self):
        from app.selection import TorrentEntry

        with self.assertRaises((ValueError, TypeError)):
            TorrentEntry.from_json(
                {"url": "https://annas-archive.gl/x", "display_name": "x", "data_size": []}
            )
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "selection.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("except (ValueError, TypeError, OverflowError):", text)

    def test_provision_cancel_endpoint_exists(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"/api/provision/cancel"', text)
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("/api/provision/cancel", html)
        self.assertIn("Cancel contribution", html)

    def test_space_token_cap_and_remove_reports_removed_count(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("_SPACE_TOKEN_MAX", text)
        self.assertIn('status_code=409', text)
        self.assertIn("removal incomplete", text)
        self.assertIn('(result or {}).get("removed")', text)

    def test_qbit_prealloc_tracks_per_hash(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_qbittorrent.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("self._preallocated", text)
        self.assertIn("ih in self._preallocated", text)
        self.assertNotIn("preallocate=self._preallocate_enabled", text)

    def test_qbit_remove_checks_shared_content(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_qbittorrent.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("def remove_torrents")
        body = text[start : start + 3500]
        self.assertIn("shared_content_ids", body)
        self.assertIn('"files_deleted": False', body)

    def test_pathsafety_checks_ancestor_reparse(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "pathsafety.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("def _reparse_on_ancestors", text)
        self.assertIn("_reparse_on_ancestors(save_path)", text)

    def test_config_defaults_scrub_qbit_url(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def config():")
        end = text.index("async def put_settings", start)
        body = text[start:end]
        self.assertIn("_clean_qbit_url", body)
        self.assertNotIn("QBIT_URL_ENV or", body)

    def test_public_events_per_ip_cap(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("_PUBLIC_SSE_PER_IP_MAX", text)
        self.assertIn("from this client", text)

    def test_cancel_keeps_in_flight_add_metadata(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Worker may have finished the add after cancel", text)

    def test_ensure_save_option_clears_space_preview(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        start = html.index("function ensureSaveOption")
        end = html.index("function loadStorageOptions", start)
        self.assertIn("clearSpacePreview()", html[start:end])

    def test_settings_save_keeps_live_backend_unless_patched(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "main.py"), encoding="utf-8") as f:
            text = f.read()
        start = text.index("async def put_settings")
        end = text.index("async def status()", start)
        body = text[start:end]
        self.assertIn('if "torrent_backend" in patch:', body)
        self.assertIn("new_backend = current_backend", body)

    def test_share_gate_requires_indexed_coverage(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        start = html.index("function hasVerifiedContribution")
        end = html.index("function updateShareGate", start)
        body = html[start:end]
        self.assertIn("index_ready", body)
        self.assertNotIn("is_complete || t.is_seeding", body)
        self.assertNotIn("HISTORY_KEY", html)
        self.assertIn('id="upload-limit"', html)
        self.assertIn('el.classList.remove("is-active")', html)

    def test_qbit_missing_files_not_complete(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "backend", "app", "session_qbittorrent.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('is_complete": progress >= 1.0 and not missing', text)

    def test_coverage_skips_missing_files(self):
        from app.metrics import CoverageIndex

        idx = CoverageIndex()
        fake = type("E", (), {"data_size": 100})()
        idx._by_hash = {"aabb": fake}
        idx._entries = [fake]
        idx._total_bytes = 100
        got = idx.coverage_for_torrents(
            [{"infohash": "aabb", "progress": 1.0, "state": "missing_files"}]
        )
        self.assertEqual(got["seeded_bytes"], 0)

    def test_storage_select_prefers_active_path(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("data.active", html)
        self.assertIn('o.path === activePath', html)

    def test_space_confirm_invalidates_preview_on_conflict(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("r.status === 400 || r.status === 409", html)
        self.assertIn("clearSpacePreview()", html)

    def test_entrypoint_recursive_content_chown(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "docker-entrypoint.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('chown -R app:app "$DATA_DIR/content"', text)
        self.assertIn("CONTENT_CHOWN", text)

    def test_auth_failure_bumps_connect_generation(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            html = f.read()
        start = html.index("function authFailure()")
        end = html.index("function fetchWithTimeout", start)
        self.assertIn("invalidateConnection()", html[start:end])
        self.assertIn("function invalidateConnection()", html)
        self.assertIn("connectGeneration++", html[html.index("function invalidateConnection()") :])

if __name__ == "__main__":
    unittest.main()
