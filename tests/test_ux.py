"""Checks for the shared controls payload."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.ux import controls_payload  # noqa: E402


class UxHelpersTests(unittest.TestCase):
    def test_controls_payload(self):
        p = controls_payload(
            seeding_paused=True,
            downloads_paused=False,
            upload_limit=-1,
            download_limit=625_000,
        )
        self.assertTrue(p["seeding_paused"])
        self.assertFalse(p["downloads_paused"])
        self.assertEqual(p["upload_limit"], -1)
        self.assertEqual(p["download_limit"], 625_000)


if __name__ == "__main__":
    unittest.main()
