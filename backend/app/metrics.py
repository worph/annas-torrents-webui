"""Coverage computation: how much of Anna's Archive are we seeding?

Both the numerator (what we seed) and denominator (the whole archive) come from
the same generate_torrents endpoint, keyed by ``data_size``. The full index is
cached in memory and refreshed lazily.
"""

from __future__ import annotations

import logging
import time

from .selection import FULL_INDEX_TB, TorrentEntry, fetch_torrent_list

log = logging.getLogger("metrics")

_CACHE_TTL = 24 * 3600  # refresh the full index at most once a day


class CoverageIndex:
    def __init__(self) -> None:
        self._entries: list[TorrentEntry] = []
        self._by_hash: dict[str, TorrentEntry] = {}
        self._fetched_at: float = 0.0

    async def refresh(self, force: bool = False, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if not force and self._entries and (now - self._fetched_at) < _CACHE_TTL:
            return
        try:
            entries = await fetch_torrent_list(FULL_INDEX_TB)
        except Exception as e:  # noqa: BLE001
            log.warning("coverage index refresh failed: %s", e)
            return
        self._entries = entries
        self._by_hash = {e.btih.lower(): e for e in entries if e.btih}
        self._fetched_at = now
        log.info("coverage index: %d entries, %d bytes total", len(entries), self.total_bytes)

    @property
    def total_bytes(self) -> int:
        return sum(e.data_size for e in self._entries)

    @property
    def ready(self) -> bool:
        return bool(self._entries)

    def coverage(self, seeded_infohashes: set[str]) -> dict:
        """Coverage of the archive by the given set of seeded infohashes (hex)."""
        # Normalize case — libtorrent may return mixed-case; index keys are lower.
        seeded_bytes = sum(
            self._by_hash[h].data_size
            for raw in seeded_infohashes
            if (h := (raw or "").lower()) in self._by_hash
        )
        total = self.total_bytes
        pct = (seeded_bytes / total * 100.0) if total else 0.0
        return {
            "seeded_bytes": seeded_bytes,
            "total_bytes": total,
            "percent": pct,
            "index_ready": self.ready,
        }


if __name__ == "__main__":
    idx = CoverageIndex()
    fake = type("E", (), {"data_size": 42})()
    idx._entries = [fake]
    idx._by_hash = {"aabb": fake}
    assert idx.coverage({"AABB"})["seeded_bytes"] == 42
    assert idx.coverage({"aabb"})["seeded_bytes"] == 42
    assert idx.coverage({"ffff"})["seeded_bytes"] == 0
    print("ok: coverage case-insensitive lookup")
