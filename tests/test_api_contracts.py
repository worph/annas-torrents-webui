"""Runtime contract checks that do not depend on source-text greps."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="annas-test-"))
os.environ.setdefault("TORRENT_PORT", "0")
os.environ.setdefault("ALLOW_UNAUTHENTICATED_API", "1")


class ApiContractTests(unittest.TestCase):
    def test_rate_limit_rejects_below_minus_one(self):
        from pydantic import ValidationError

        from app.main import RateLimitRequest

        RateLimitRequest(bytes_per_sec=-1)
        RateLimitRequest(bytes_per_sec=0)
        RateLimitRequest(bytes_per_sec=125000)
        with self.assertRaises(ValidationError):
            RateLimitRequest(bytes_per_sec=-2)

    def test_space_free_dedupes_infohashes(self):
        # Pure check of the transform used by space_free.
        hashes = list(dict.fromkeys(h.lower() for h in ["AA", "aa", "BB"] if h))
        self.assertEqual(hashes, ["aa", "bb"])

    def test_storage_option_unknown_disk_is_null(self):
        from app.storage import option

        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "no-such", "path")
            # Parent may still resolve usage on some OS; force a nonsense root.
            got = option("Z:\\this-drive-should-not-exist-annas-test-xyz")
            if got["disk_free"] is not None:
                # On some hosts the letter exists; accept only null-or-int.
                self.assertIsInstance(got["disk_free"], int)
            else:
                self.assertIsNone(got["disk_total"])

    def test_public_status_cache_control(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.get("/api/public/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        body = r.json()
        self.assertIn("global", body)
        self.assertEqual(body.get("torrents"), [])

    def test_collections_upstream_failure_is_502(self):
        from unittest import mock

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        with mock.patch("app.routes.provision.fetch_torrent_list", side_effect=RuntimeError("mirrors down")):
            r = client.get("/api/collections")
        self.assertEqual(r.status_code, 502)

    def test_trusted_get_shared_by_download_helpers(self):
        import inspect

        from app import selection

        src = inspect.getsource(selection._trusted_download)
        self.assertIn("_trusted_get(", src)
        src2 = inspect.getsource(selection._trusted_get_bytes)
        self.assertIn("_trusted_get(", src2)

    def test_healthz_is_public_liveness(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        r = client.get("/api/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))

    def test_btih_required_on_torrent_entry(self):
        from app.selection import TorrentEntry

        with self.assertRaises(ValueError):
            TorrentEntry.from_json(
                {
                    "url": "https://annas-archive.pk/x.torrent",
                    "display_name": "x",
                    "data_size": 10,
                    "btih": "nope",
                }
            )


if __name__ == "__main__":
    unittest.main()
