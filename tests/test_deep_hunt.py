"""Behavioral checks from the deep bug-hunt (H0–H2). Prefer runtime over source greps."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="annas-deep-"))
os.environ.setdefault("TORRENT_PORT", "0")
os.environ.setdefault("ALLOW_UNAUTHENTICATED_API", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.auth import issue_sse_ticket, redact_snapshot  # noqa: E402
from app.pathsafety import shared_content_ids  # noqa: E402
from app import auth as auth_mod  # noqa: E402


class SharedContentTests(unittest.TestCase):
    def test_overlapping_roots_flag_both(self):
        shared = shared_content_ids(
            [
                ("a" * 40, "/data", "pack"),
                ("b" * 40, "/data/pack", "child"),
                ("c" * 40, "/data", "other"),
            ]
        )
        self.assertIn("a" * 40, shared)
        self.assertIn("b" * 40, shared)
        self.assertNotIn("c" * 40, shared)


class LibtorrentRemoveHonestyTests(unittest.TestCase):
    def test_empty_hashes_never_claim_files_deleted(self):
        from app.session_libtorrent import LibtorrentSession

        # Avoid constructing a real session — call unbound via a stub instance.
        stub = object.__new__(LibtorrentSession)
        got = LibtorrentSession.remove_torrents(stub, [], delete_files=True)
        self.assertEqual(got, {"removed": 0, "files_deleted": None})


class SpaceTokenTests(unittest.TestCase):
    def test_double_consume_rejected(self):
        from fastapi import HTTPException

        from app import runtime as rt
        from app.routes import space as space_mod
        from app.schemas import SpaceFreeRequest

        token = "deep-hunt-token"
        hashes = {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        entry = {
            "hashes": hashes,
            "save_path": "/data/content",
            "request_bytes": 1000,
            "backend": "libtorrent",
            "fingerprint": {"generation": 0, "backend": "libtorrent", "qbit_url": "", "qbit_category": ""},
            "expires": 1e18,
            "consuming": False,
        }

        class FakeSess:
            def infohashes(self):
                return list(hashes)

            def torrents_status(self):
                return [
                    {
                        "infohash": next(iter(hashes)),
                        "save_path": "/data/content",
                        "name": "t",
                    }
                ]

            def remove_torrents(self, ihs, delete_files=True):
                return {"removed": len(ihs), "files_deleted": True}

        async def run():
            rt._space_tokens.clear()
            rt._space_tokens[token] = entry
            prev_sess, prev_fp = rt.session, rt._session_fingerprint
            rt.session = FakeSess()
            rt._session_fingerprint = lambda: entry["fingerprint"]
            req = SpaceFreeRequest(
                infohashes=list(hashes),
                confirm=True,
                token=token,
                request_bytes=1000,
                save_path="/data/content",
            )
            try:
                # Mark consuming as if first request already started.
                entry["consuming"] = True
                with self.assertRaises(HTTPException) as ctx:
                    await space_mod.space_free(req)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("already being used", str(ctx.exception.detail))
            finally:
                rt.session = prev_sess
                rt._session_fingerprint = prev_fp
                rt._space_tokens.clear()

        asyncio.run(run())

    def test_fingerprint_mismatch_rejects(self):
        from fastapi import HTTPException

        from app import runtime as rt
        from app.routes import space as space_mod
        from app.schemas import SpaceFreeRequest

        token = "fp-token"
        hashes = {"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
        entry = {
            "hashes": hashes,
            "save_path": "/data/content",
            "request_bytes": 50,
            "backend": "libtorrent",
            "fingerprint": {"generation": 1, "backend": "libtorrent", "qbit_url": "", "qbit_category": ""},
            "expires": 1e18,
            "consuming": False,
        }

        class FakeSess:
            def infohashes(self):
                return list(hashes)

            def torrents_status(self):
                return []

            def remove_torrents(self, *a, **k):
                raise AssertionError("must not delete after fingerprint mismatch")

        async def run():
            rt._space_tokens.clear()
            rt._space_tokens[token] = entry
            prev_sess, prev_fp = rt.session, rt._session_fingerprint
            rt.session = FakeSess()
            rt._session_fingerprint = lambda: {
                "generation": 99,
                "backend": "libtorrent",
                "qbit_url": "",
                "qbit_category": "",
            }
            req = SpaceFreeRequest(
                infohashes=list(hashes),
                confirm=True,
                token=token,
                request_bytes=50,
                save_path="/data/content",
            )
            try:
                with self.assertRaises(HTTPException) as ctx:
                    await space_mod.space_free(req)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("expired", str(ctx.exception.detail).lower())
            finally:
                rt.session = prev_sess
                rt._session_fingerprint = prev_fp
                rt._space_tokens.clear()

        asyncio.run(run())


class AllowlistTests(unittest.TestCase):
    def test_drive_root_coerced_not_bare(self):
        from app import runtime as rt
        from app import storage

        if os.name != "nt":
            self.skipTest("drive-root coercion is a Windows path concern")

        class FakeSess:
            def storage_options(self, extra):
                return [{"path": "D:\\"}]

            def torrents_status(self):
                return []

        paths = rt._allowed_paths(FakeSess())
        self.assertTrue(paths)
        for p in paths:
            self.assertFalse(storage.is_drive_root(p), p)
            self.assertIn("Anna", p)


class AuthPublicSurfaceTests(unittest.TestCase):
    def test_public_path_matrix(self):
        from app.auth import _is_public_path

        self.assertTrue(_is_public_path("/api/public/status"))
        self.assertTrue(_is_public_path("/api/healthz"))
        self.assertTrue(_is_public_path("/view"))
        self.assertFalse(_is_public_path("/api/status"))
        self.assertFalse(_is_public_path("/api/events"))
        self.assertFalse(_is_public_path("/api/public/../status"))
        self.assertFalse(_is_public_path("/api/provision"))

    def test_sse_ticket_one_shot(self):
        prev = auth_mod.API_TOKEN
        auth_mod.API_TOKEN = "deep-hunt-token-value-xxxxxx"
        auth_mod._SSE_TICKETS.clear()
        try:
            ticket = issue_sse_ticket()
            self.assertTrue(ticket)
            from starlette.datastructures import QueryParams

            class Req:
                method = "GET"
                url = type("U", (), {"path": "/api/events"})()
                query_params = QueryParams(f"ticket={ticket}")

            self.assertTrue(auth_mod._sse_ticket_ok(Req()))
            self.assertFalse(auth_mod._sse_ticket_ok(Req()))
        finally:
            auth_mod.API_TOKEN = prev
            auth_mod._SSE_TICKETS.clear()

    def test_redact_drops_unknown_global_keys(self):
        public = redact_snapshot(
            {
                "global": {
                    "upload_rate": 1,
                    "secret_new_field": "leak",
                    "disk_free": 99,
                    "storage_path": "/secret",
                },
                "torrents": [{"infohash": "x", "name": "n"}],
                "controls": {"seeding_paused": True},
            }
        )
        self.assertNotIn("secret_new_field", public["global"])
        self.assertNotIn("disk_free", public["global"])
        self.assertNotIn("storage_path", public["global"])
        self.assertEqual(public["torrents"], [])


class ClientIpTests(unittest.TestCase):
    def test_xff_ignored_without_trust(self):
        from app.routes import status as status_mod

        prev = status_mod._TRUST_PROXY
        status_mod._TRUST_PROXY = False
        try:
            req = mock.Mock()
            req.headers = {"x-forwarded-for": "1.2.3.4"}
            req.client = mock.Mock(host="9.9.9.9")
            self.assertEqual(status_mod._client_ip(req), "9.9.9.9")
            status_mod._TRUST_PROXY = True
            self.assertEqual(status_mod._client_ip(req), "1.2.3.4")
        finally:
            status_mod._TRUST_PROXY = prev


class ProvisionUnknownDiskTests(unittest.TestCase):
    def test_provision_endpoint_unknown_disk(self):
        from app import runtime as rt
        from app.routes import provision as prov
        from app.schemas import ProvisionRequest

        async def run():
            async def none_free(sess, backend, dest):
                return None

            prev = prov._available_free
            prov._available_free = none_free
            rt.provision_state["running"] = False
            rt._provision_task = None

            class Sess:
                pass

            with mock.patch.object(rt, "session", Sess()):
                with mock.patch.object(rt, "_session_generation", 1):
                    with mock.patch.object(
                        rt,
                        "_locked_call",
                        side_effect=lambda fn, *a, **k: ["/data/content"]
                        if fn is rt._allowed_paths
                        else (fn(*a, **k) if callable(fn) else None),
                    ):
                        with mock.patch.object(rt, "_resolve_save_path", return_value="/data/content"):
                            with mock.patch.object(rt, "TORRENT_BACKEND", "qbittorrent"):
                                out = await prov.provision(
                                    ProvisionRequest(max_tb=0.01, allow_unknown_disk=False)
                                )
            prov._available_free = prev
            self.assertEqual(out.get("code"), "unknown_disk")
            self.assertFalse(out.get("ok"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
