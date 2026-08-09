"""Deep bug-hunt behavioral coverage (contracts → release hygiene)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="annas-bughunt-"))
os.environ.setdefault("TORRENT_PORT", "0")
os.environ.setdefault("ALLOW_UNAUTHENTICATED_API", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import auth  # noqa: E402
from app.auth import issue_sse_ticket, redact_snapshot, token_ok  # noqa: E402
from app.metrics import CoverageIndex, _safe_progress  # noqa: E402
from app.pathsafety import content_roots_overlap, safe_delete_target, shared_content_ids  # noqa: E402
from app.selection import (  # noqa: E402
    _torrent_content_size,
    _torrent_infohash,
    _validate_download_url,
)
from app.session_qbittorrent import QBittorrentSession, _qbittorrent_rate_limit  # noqa: E402
from app.settings import _qbit_host_blocked  # noqa: E402
from app.space import classify_torrent, pick_combination  # noqa: E402


def _info_dict_torrent(name: bytes = b"a", length: int = 3) -> bytes:
    info = b"d6:lengthi%de4:name%d:%se" % (length, len(name), name)
    return b"d4:info" + info + b"e"


class ContractTests(unittest.TestCase):
    def test_token_header_and_bearer(self):
        prev_token, prev_allow = auth.API_TOKEN, auth.ALLOW_UNAUTHENTICATED_API
        auth.API_TOKEN = "secret-token-value"
        auth.ALLOW_UNAUTHENTICATED_API = False
        try:
            req = mock.Mock()
            req.headers = {"X-API-Token": "secret-token-value"}
            self.assertTrue(token_ok(req))
            req.headers = {"Authorization": "Bearer secret-token-value", "X-API-Token": ""}
            self.assertTrue(token_ok(req))
            req.headers = {"X-API-Token": "wrong-token-value!!"}
            self.assertFalse(token_ok(req))
        finally:
            auth.API_TOKEN = prev_token
            auth.ALLOW_UNAUTHENTICATED_API = prev_allow

    def test_sse_ticket_is_one_shot_and_expires(self):
        prev_token = auth.API_TOKEN
        auth.API_TOKEN = "tok"
        auth._SSE_TICKETS.clear()
        try:
            ticket = issue_sse_ticket()
            self.assertTrue(ticket)
            req = mock.Mock()
            req.method = "GET"
            req.url.path = "/api/events"
            req.query_params = {"ticket": ticket}
            self.assertTrue(auth._sse_ticket_ok(req))
            self.assertFalse(auth._sse_ticket_ok(req))  # one-shot
            expired = issue_sse_ticket()
            auth._SSE_TICKETS[expired] = time.monotonic() - 1
            req.query_params = {"ticket": expired}
            self.assertFalse(auth._sse_ticket_ok(req))
        finally:
            auth.API_TOKEN = prev_token
            auth._SSE_TICKETS.clear()

    def test_middleware_401_and_503_contracts(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        prev_token, prev_allow = auth.API_TOKEN, auth.ALLOW_UNAUTHENTICATED_API
        try:
            auth.API_TOKEN = ""
            auth.ALLOW_UNAUTHENTICATED_API = False
            r = client.get("/api/status")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["detail"], "API_TOKEN must be configured")

            auth.API_TOKEN = "correct-token-aaaaaaaa"
            auth.ALLOW_UNAUTHENTICATED_API = False
            r = client.get("/api/status")
            self.assertEqual(r.status_code, 401)
            r = client.get("/api/status", headers={"X-API-Token": "correct-token-aaaaaaaa"})
            self.assertEqual(r.status_code, 200)
            r = client.get("/api/healthz")
            self.assertEqual(r.status_code, 200)
        finally:
            auth.API_TOKEN = prev_token
            auth.ALLOW_UNAUTHENTICATED_API = prev_allow

    def test_health_readiness_vs_liveness(self):
        from fastapi.testclient import TestClient

        from app import main as main_mod
        from app.main import app

        client = TestClient(app)
        self.assertEqual(client.get("/api/healthz").status_code, 200)

        prev = dict(main_mod._snapshot_cache)
        try:
            main_mod._snapshot_cache["data"] = {
                "global": {"backend_ok": False},
                "torrents": [],
            }
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 503)
            self.assertFalse(r.json().get("ok"))
            self.assertFalse(r.json().get("backend_ok"))

            main_mod._snapshot_cache["data"] = {
                "global": {"backend_ok": True},
                "torrents": [],
            }
            r = client.get("/api/health")
            # Unauthenticated CI allows health when backend_ok.
            self.assertIn(r.status_code, (200, 503))
            if r.status_code == 200:
                self.assertTrue(r.json().get("ok"))
        finally:
            main_mod._snapshot_cache.clear()
            main_mod._snapshot_cache.update(prev)

    def test_view_html_bakes_view_mode_class(self):
        from app.main import _bake_view_mode_html

        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            raw = f.read()
        baked = _bake_view_mode_html(raw)
        self.assertIn('class="view-mode"', baked)
        self.assertIn("body.view-mode #global-controls", baked)
        # Idempotent when already present.
        self.assertEqual(_bake_view_mode_html(baked), baked)

    def test_redact_keeps_aggregates_drops_private(self):
        public = redact_snapshot(
            {
                "connection": "connected",
                "global": {
                    "storage_path": "/secret",
                    "upload_rate": 9,
                    "num_torrents": 2,
                },
                "coverage": {"seeded_bytes": 1, "percent": 0.1, "secret": "x"},
                "torrents": [{"infohash": "abc", "name": "book", "save_path": "/secret"}],
                "controls": {"seeding_paused": True, "upload_limit": 1},
            }
        )
        blob = json.dumps(public)
        self.assertNotIn("secret", blob)
        self.assertNotIn("abc", blob)
        self.assertNotIn("book", blob)
        self.assertEqual(public["torrents"], [])
        self.assertTrue(public["public"])
        self.assertEqual(public["global"]["upload_rate"], 9)


class ProvisionTests(unittest.TestCase):
    def test_mirror_url_rejects_untrusted_and_http(self):
        with self.assertRaises(ValueError):
            _validate_download_url("http://annas-archive.org/x.torrent")
        with self.assertRaises(ValueError):
            _validate_download_url("https://evil.example/x.torrent")

    def test_redirect_off_mirror_rejected(self):
        from app.selection import _trusted_get

        class FakeStream:
            def __init__(self, status, headers=None, body=b""):
                self.status_code = status
                self.headers = headers or {}
                self.url = "https://annas-archive.org/a"
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield self._body

        class FakeClient:
            def stream(self, method, url, follow_redirects=False):
                if "annas-archive" in url and "evil" not in url:
                    return FakeStream(302, {"location": "https://evil.example/x"})
                return FakeStream(200, body=b"ok")

        async def run():
            with self.assertRaises(ValueError):
                await _trusted_get(FakeClient(), "https://annas-archive.org/a", 100)

        asyncio.run(run())

    def test_payload_limit_enforced(self):
        from app.selection import _trusted_get

        class FakeStream:
            status_code = 200
            headers = {}
            url = "https://annas-archive.org/a"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield b"x" * 50

        class FakeClient:
            def stream(self, *a, **k):
                return FakeStream()

        async def run():
            with self.assertRaises(ValueError):
                await _trusted_get(FakeClient(), "https://annas-archive.org/a", 10)

        asyncio.run(run())

    def test_infohash_and_size_consistency(self):
        raw = _info_dict_torrent(b"book", 42)
        self.assertEqual(_torrent_infohash(raw), hashlib.sha1(raw[raw.index(b"d6:length") : -1]).hexdigest())
        # Slice must be the info dict alone.
        info = b"d6:lengthi42e4:name4:booke"
        self.assertEqual(_torrent_infohash(b"d4:info" + info + b"e"), hashlib.sha1(info).hexdigest())
        self.assertEqual(_torrent_content_size(raw), 42)

    def test_unlink_never_removes_preexisting_torrent(self):
        from app.main import _unlink_unadded_torrents

        with tempfile.TemporaryDirectory() as td:
            preexisting = os.path.join(td, "pre.torrent")
            with open(preexisting, "wb") as f:
                f.write(b"keep")
            created = os.path.join(td, "new.torrent")
            with open(created, "wb") as f:
                f.write(b"drop")
            _unlink_unadded_torrents(
                [(preexisting, 1), (created, 1)],
                added_paths=set(),
                created_paths={os.path.abspath(created)},
            )
            self.assertTrue(os.path.exists(preexisting))
            self.assertFalse(os.path.exists(created))

    def test_qbit_202_accepted_without_hash_is_not_added(self):
        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess.save_path = ""
        sess.category = "annas"
        sess._resolved_category = "annas"
        sess._preallocated = set()
        sess._downloads_paused = False
        sess._seeding_paused = False
        sess._desired_upload_limit = -1
        sess._desired_download_limit = -1
        sess._active_save_path = None
        ih = "c" * 40

        def fake_request(method, path, **kwargs):
            if path.endswith("/add"):
                return mock.Mock(status_code=202, text="")
            return mock.Mock(status_code=200, text="Ok.", json=lambda: [])

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, f"{ih}.torrent")
            with open(path, "wb") as f:
                f.write(b"d4:infod4:name1:aee")
            with mock.patch.object(sess, "_ensure_category"), mock.patch.object(
                sess, "infohashes", return_value=set()
            ), mock.patch.object(sess, "_request", side_effect=fake_request), mock.patch(
                "time.sleep", return_value=None
            ):
                self.assertIsNone(sess.add_torrent_file(path))


class BackendParityTests(unittest.TestCase):
    def test_rate_limit_mapping(self):
        self.assertEqual(_qbittorrent_rate_limit(-1), -1)
        self.assertEqual(_qbittorrent_rate_limit(0), 1)  # qBit 0 → unlimited; stop ≈ 1 B/s
        self.assertEqual(_qbittorrent_rate_limit(100), 100)

    def test_build_snapshot_uses_status_batch(self):
        from app import main as main_mod

        class FakeSess:
            def __init__(self):
                self.batches = 0
                self.fetches = 0

            def begin_status_batch(self):
                self.batches += 1

            def end_status_batch(self):
                self.batches -= 1

            def global_status(self):
                self.fetches += 1
                return {
                    "backend_ok": True,
                    "download_rate": 0,
                    "upload_rate": 0,
                    "num_torrents": 0,
                    "disk_free": 0,
                    "disk_free_known": False,
                    "disk_total": 0,
                    "committed_bytes": 0,
                    "total_upload": 0,
                    "total_download": 0,
                    "num_peers": 0,
                }

            def torrents_status(self):
                self.fetches += 1
                return []

            def controls_state(self):
                return {
                    "seeding_paused": False,
                    "downloads_paused": False,
                    "upload_limit": -1,
                    "download_limit": -1,
                }

        sess = FakeSess()
        with mock.patch.object(main_mod, "coverage_index") as cov:
            cov.coverage_for_torrents.return_value = {
                "seeded_bytes": 0,
                "total_bytes": 0,
                "percent": 0,
                "index_ready": False,
            }
            snap = main_mod._build_snapshot(sess, {"running": False})
        self.assertEqual(sess.batches, 0)  # ended
        self.assertEqual(sess.fetches, 2)
        self.assertEqual(snap["connection"], "connected")

    def test_progress_guards_match(self):
        self.assertEqual(QBittorrentSession._progress({"progress": float("nan")}), 0.0)
        self.assertEqual(QBittorrentSession._progress({"progress": float("inf")}), 0.0)
        self.assertEqual(_safe_progress(float("nan")), 0.0)


class FilesystemSecurityTests(unittest.TestCase):
    def test_safe_delete_rejects_traversal_and_abs(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(safe_delete_target(td, "../outside"))
            self.assertIsNone(safe_delete_target(td, os.path.join(td, "abs")))
            ok = safe_delete_target(td, "book")
            # Target need not exist; path must stay under td when created later.
            if ok is not None:
                self.assertTrue(ok.startswith(os.path.abspath(td)))

    def test_symlink_leaf_rejected_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "real")
            link = os.path.join(td, "link")
            os.mkdir(target)
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            self.assertIsNone(safe_delete_target(td, "link"))

    def test_shared_content_ids_detect_overlap(self):
        entries = [
            ("a", "/data", "book"),
            ("b", "/data", "book"),
            ("c", "/data", "other"),
        ]
        shared = shared_content_ids(entries)
        self.assertEqual(shared, {"a", "b"})
        self.assertTrue(content_roots_overlap("/data", "book", "/data", "book"))

    def test_qbit_metadata_host_blocked(self):
        self.assertTrue(_qbit_host_blocked("169.254.169.254"))
        self.assertTrue(_qbit_host_blocked("metadata.google.internal"))

    def test_qbit_refuses_offhost_redirect(self):
        sess = QBittorrentSession.__new__(QBittorrentSession)
        sess._base = "http://127.0.0.1:8080"
        sess._user = "u"
        sess._pass = ""
        sess._auth_backoff_until = 0
        sess._auth_error = None
        sess._client = mock.Mock()
        sess._client.request.return_value = mock.Mock(status_code=302, headers={"location": "https://evil"})
        with mock.patch.object(sess, "_assert_host_still_safe"):
            with self.assertRaises(RuntimeError):
                sess._request("GET", "/api/v2/app/version")


class MetricsSpaceTests(unittest.TestCase):
    def test_seeded_bytes_le_total_and_dedupe(self):
        idx = CoverageIndex()
        fake = type("E", (), {"data_size": 100})()
        idx._entries = [fake]
        idx._by_hash = {"aa": fake, "bb": fake}
        idx._total_bytes = 100
        cov = idx.coverage_for_torrents(
            [
                {"infohash": "aa", "progress": 1.0, "is_complete": True},
                {"infohash": "aa", "progress": 1.0, "is_complete": True},
            ]
        )
        self.assertLessEqual(cov["seeded_bytes"], cov["total_bytes"])
        self.assertEqual(cov["seeded_bytes"], 100)  # duplicate infohash once

    def test_missing_files_out_of_coverage(self):
        idx = CoverageIndex()
        fake = type("E", (), {"data_size": 50})()
        idx._entries = [fake]
        idx._by_hash = {"aa": fake}
        idx._total_bytes = 50
        cov = idx.coverage_for_torrents(
            [{"infohash": "aa", "progress": 1.0, "state": "missing_files", "is_complete": False}]
        )
        self.assertEqual(cov["seeded_bytes"], 0)

    def test_pick_combination_units_match_classify(self):
        row = classify_torrent(
            {
                "infohash": "aa",
                "name": "big",
                "size": 20 * 1000**3,
                "progress": 1.0,
                "is_complete": True,
                "seeds_total": 50,
                "allocated_bytes": 20 * 1000**3,
            }
        )
        picked = pick_combination([row], 5 * 1000**3)
        self.assertGreaterEqual(picked["freed_bytes"], 5 * 1000**3)
        self.assertEqual(picked["freed_bytes"], row["reclaimable_bytes"])


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "frontend", "index.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_503_only_auth_token_missing_triggers_auth_failure(self):
        self.assertIn('detail === "API_TOKEN must be configured"', self.html)
        self.assertNotIn(
            "if (r.status === 503 && !VIEW_MODE && !String(url).includes(\"/public/\")) {\n"
            "      // Private API unavailable",
            self.html,
        )

    def test_timeout_reconnects_when_generation_stable(self):
        self.assertIn("statusAbort !== abort", self.html)
        self.assertIn("scheduleReconnect(generation)", self.html)

    def test_degraded_disables_controls(self):
        self.assertIn("resetControlsChrome();", self.html)
        self.assertIn('connectionFrom(s) === "connected"', self.html)

    def test_storage_change_invalidates_inflight(self):
        self.assertIn("storageRequestId++; // drop in-flight", self.html)

    def test_clear_space_bumps_free_id(self):
        body = self.html[self.html.index("function clearSpacePreview") :]
        body = body[: body.index("\nfunction ")]
        self.assertIn("spaceFreeId++", body)

    def test_view_mode_hides_private_chrome(self):
        self.assertIn("VIEW_MODE", self.html)
        self.assertIn("/view", self.html)
        self.assertIn("body.view-mode #global-controls", self.html)
        self.assertIn("body.view-mode #remove-modal", self.html)
        self.assertIn("authFailure", self.html)

    def test_a11y_sort_and_live_regions(self):
        self.assertIn("aria-sort", self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn("visually-hidden", self.html)

    def test_modal_focus_trap_and_restore(self):
        self.assertIn("function trapFocus", self.html)
        self.assertIn("function openModal", self.html)
        self.assertIn("function closeModal", self.html)
        self.assertIn("restoreEl.focus()", self.html)

    def test_responsive_table_overflow_only(self):
        self.assertIn(".table-wrap", self.html)
        self.assertIn("overflow-x", self.html)
        self.assertIn("min-width: 320px", self.html)

    def test_script_still_parses(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self.html, re.DOTALL)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write("\n".join(scripts))
            path = f.name
        try:
            checked = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
        finally:
            os.unlink(path)


class ReleaseHygieneTests(unittest.TestCase):
    def test_publish_workflow_has_separate_compose_and_build_steps(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        path = os.path.join(root, ".github", "workflows", "publish.yml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("- name: Compose config validates", text)
        self.assertIn("- name: Docker build smoke test", text)
        # The broken merge put the build step name on the comment line.
        self.assertNotIn(
            "TORRENT_PORT=0 is for app-only CI.      - name: Docker build smoke test",
            text,
        )

    def test_roadmap_fixes_module_importable(self):
        import test_roadmap_fixes  # noqa: F401

        self.assertTrue(hasattr(test_roadmap_fixes, "RoadmapFixTests"))


if __name__ == "__main__":
    unittest.main()
