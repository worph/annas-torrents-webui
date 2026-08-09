"""Unit tests for settings persistence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.settings import load_settings, resolve_qbit_category, save_settings  # noqa: E402
from app.storage import ANNA_FOLDER  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_resolve_order(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(resolve_qbit_category(td, None), ANNA_FOLDER)
            self.assertEqual(resolve_qbit_category(td, "From Env"), "From Env")
            save_settings(td, {"qbit_category": "From File"})
            self.assertEqual(resolve_qbit_category(td, "From Env"), "From File")

    def test_backend_resolve(self):
        from app.settings import resolve_backend

        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(resolve_backend(td, None), "libtorrent")
            self.assertEqual(resolve_backend(td, "qbittorrent"), "qbittorrent")
            save_settings(td, {"torrent_backend": "qbittorrent"})
            self.assertEqual(resolve_backend(td, "libtorrent"), "qbittorrent")

    def test_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            save_settings(td, {"qbit_category": "Anna's Archive Torrents"})
            path = os.path.join(td, "settings.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["qbit_category"], "Anna's Archive Torrents")
            self.assertEqual(load_settings(td)["qbit_category"], "Anna's Archive Torrents")

    def test_empty_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                save_settings(td, {"qbit_category": "  "})

    def test_qbit_url_must_be_http(self):
        from app.settings import apply_patch

        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "ftp://host"})
        with self.assertRaises(ValueError):
            apply_patch({}, {"qbit_url": "not-a-url"})
        out = apply_patch({}, {"qbit_url": "http://127.0.0.1:8080/"})
        self.assertEqual(out["qbit_url"], "http://127.0.0.1:8080")

    def test_backend_falls_back_without_libtorrent(self):
        from app import settings as settings_mod

        with tempfile.TemporaryDirectory() as td:
            save_settings(td, {"torrent_backend": "libtorrent"})
            with mock.patch.object(settings_mod, "_libtorrent_available", return_value=False):
                self.assertEqual(settings_mod.resolve_backend(td, "qbittorrent"), "qbittorrent")
                self.assertEqual(settings_mod.resolve_backend(td, None), "qbittorrent")

    def test_corrupt_settings_quarantines(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not-json")
            self.assertEqual(load_settings(td), {})
            self.assertFalse(os.path.isfile(path))
            corrupt = [n for n in os.listdir(td) if n.startswith("settings.json.corrupt.")]
            self.assertEqual(len(corrupt), 1)
            # After quarantine, a fresh save is allowed.
            save_settings(td, {"qbit_category": "Recovered"})
            self.assertEqual(load_settings(td)["qbit_category"], "Recovered")

if __name__ == "__main__":
    unittest.main()
