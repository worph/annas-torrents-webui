"""Unit tests for space recovery ranking."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.space import PROTECT_BELOW_BYTES, classify_torrent, deletion_score, pick_combination  # noqa: E402

GB = 1000**3


class SpaceRankingTests(unittest.TestCase):
    def test_prefer_keep_below_10gb_soft(self):
        c = classify_torrent(
            {"infohash": "a", "name": "small", "size": 8 * GB, "num_seeds": 100, "progress": 1.0}
        )
        self.assertTrue(c["protected"])
        self.assertLess(c["size"], PROTECT_BELOW_BYTES)

    def test_small_used_only_when_needed(self):
        big = {"infohash": "b", "name": "big", "size": 50 * GB, "num_seeds": 50, "progress": 1.0}
        small = {"infohash": "s", "name": "small", "size": 5 * GB, "num_seeds": 50, "progress": 1.0}
        out = pick_combination([big, small], 40 * GB)
        self.assertEqual([t["infohash"] for t in out["selected"]], ["b"])
        only = pick_combination([small], 3 * GB)
        self.assertEqual([t["infohash"] for t in only["selected"]], ["s"])

    def test_among_small_largest_first(self):
        # Size descending under 10 GB — not seed score.
        out = pick_combination(
            [
                {"infohash": "s1", "name": "tiny", "size": 2 * GB, "num_seeds": 999, "progress": 1.0},
                {"infohash": "s2", "name": "mid", "size": 8 * GB, "num_seeds": 1, "progress": 1.0},
            ],
            5 * GB,
        )
        self.assertEqual(out["selected"][0]["infohash"], "s2")

    def test_small_after_large_exhausted(self):
        big = {"infohash": "b", "name": "big", "size": 12 * GB, "num_seeds": 10, "progress": 1.0}
        s8 = {"infohash": "s8", "name": "8", "size": 8 * GB, "num_seeds": 1, "progress": 1.0}
        s3 = {"infohash": "s3", "name": "3", "size": 3 * GB, "num_seeds": 99, "progress": 1.0}
        out = pick_combination([big, s8, s3], 20 * GB)
        self.assertEqual([t["infohash"] for t in out["selected"]], ["b", "s8"])

    def test_prefer_large_well_seeded(self):
        few = {"infohash": "f", "name": "few", "size": 50 * GB, "num_seeds": 1, "progress": 1.0}
        many = {"infohash": "m", "name": "many", "size": 40 * GB, "num_seeds": 500, "progress": 1.0}
        self.assertGreater(deletion_score(many["size"], many["num_seeds"]), deletion_score(few["size"], few["num_seeds"]))
        out = pick_combination([few, many], 30 * GB)
        self.assertEqual(out["selected"][0]["infohash"], "m")

    def test_unknown_seeds_unscored(self):
        out = pick_combination(
            [
                {
                    "infohash": "u",
                    "name": "unk",
                    "size": 20 * GB,
                    "num_seeds": None,
                    "seeds_known": False,
                    "progress": 1.0,
                }
            ],
            10 * GB,
        )
        self.assertEqual(out["selected"], [])
        self.assertEqual(len(out["unscored"]), 1)

    def test_overshoot_reported(self):
        out = pick_combination(
            [{"infohash": "o", "name": "over", "size": 25 * GB, "num_seeds": 50, "progress": 1.0}],
            10 * GB,
        )
        self.assertEqual(out["freed_bytes"], 25 * GB)
        self.assertEqual(out["overshoot_bytes"], 15 * GB)

    def test_classify_uses_seeds_total_without_num_seeds_key(self):
        c = classify_torrent(
            {
                "infohash": "x",
                "name": "t",
                "size": 20 * GB,
                "seeds_total": 7,
                "progress": 1.0,
            }
        )
        self.assertTrue(c["seeds_known"])
        self.assertEqual(c["num_seeds"], 7)

    def test_classify_missing_seed_fields_is_unknown(self):
        c = classify_torrent(
            {"infohash": "y", "name": "t", "size": 20 * GB, "progress": 1.0}
        )
        self.assertFalse(c["seeds_known"])
        self.assertIsNone(c["num_seeds"])

    def test_classify_missing_files_beats_progress(self):
        c = classify_torrent(
            {
                "infohash": "m",
                "name": "gone",
                "size": 20 * GB,
                "progress": 1.0,
                "is_complete": False,
                "state": "missing_files",
            }
        )
        self.assertTrue(c["incomplete"])
        self.assertEqual(c["reason"], "incomplete")


if __name__ == "__main__":
    unittest.main()
