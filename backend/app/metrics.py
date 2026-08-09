"""Coverage computation: how much of Anna's Archive are we seeding?

Both the numerator (what we seed) and denominator (the whole archive) come from
the same generate_torrents endpoint, keyed by ``data_size``. The full index is
cached in memory and refreshed lazily.
"""

from __future__ import annotations

import logging
import math
import time

from .selection import FULL_INDEX_TB, TorrentEntry, fetch_torrent_list

log = logging.getLogger("metrics")

_CACHE_TTL = 24 * 3600  # refresh the full index at most once a day


def _safe_progress(value: object) -> float:
    """Clamp torrent progress to [0, 1]; NaN/Inf/invalid → 0."""
    try:
        progress = float(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(progress):
        return 0.0
    return min(1.0, max(0.0, progress))


class CoverageIndex:
    def __init__(self) -> None:
        self._entries: list[TorrentEntry] = []
        self._by_hash: dict[str, TorrentEntry] = {}
        self._fetched_at: float = 0.0
        self._total_bytes: int = 0

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
        self._by_hash = {e.btih.lower(): e for e in entries if e.btih and len(e.btih) == 40}
        self._total_bytes = sum(e.data_size for e in self._by_hash.values())
        self._fetched_at = now
        log.info("coverage index: %d entries, %d bytes total", len(self._by_hash), self._total_bytes)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def ready(self) -> bool:
        return bool(self._entries)

    def coverage(self, seeded_infohashes: set[str]) -> dict:
        """Full-size coverage for complete infohashes (legacy / tests)."""
        return self.coverage_for_torrents(
            [{"infohash": h, "progress": 1.0} for h in seeded_infohashes]
        )

    def coverage_for_torrents(self, torrents: list[dict]) -> dict:
        """Coverage weighted by download progress (grows as torrents finish).

        Only counts infohashes present in the Anna's Archive index — unknown
        local torrents must not inflate the numerator.
        """
        seeded_bytes = 0
        seen: set[str] = set()
        for t in torrents:
            ih = (t.get("infohash") or "").lower()
            if not ih or ih not in self._by_hash or ih in seen:
                continue
            state = str(t.get("state") or "").lower()
            # Missing/errored content must not count as archive coverage.
            if "missing" in state or state == "error":
                continue
            progress = _safe_progress(t.get("progress"))
            seen.add(ih)
            seeded_bytes += int(self._by_hash[ih].data_size * progress)
        total = self.total_bytes
        if seeded_bytes > total:
            seeded_bytes = total
        pct = (seeded_bytes / total * 100.0) if total else 0.0
        return {
            "seeded_bytes": seeded_bytes,
            "total_bytes": total,
            "percent": pct,
            "index_ready": self.ready,
        }


if __name__ == "__main__":
    idx = CoverageIndex()
    fake = type("E", (), {"data_size": 100})()
    idx._entries = [fake]
    idx._by_hash = {"aabb": fake}
    idx._total_bytes = 100
    assert idx.coverage({"AABB"})["seeded_bytes"] == 100
    assert idx.coverage({"aabb"})["seeded_bytes"] == 100
    assert idx.coverage({"ffff"})["seeded_bytes"] == 0
    half = idx.coverage_for_torrents([{"infohash": "aabb", "progress": 0.5}])
    assert half["seeded_bytes"] == 50
    nan = idx.coverage_for_torrents([{"infohash": "aabb", "progress": float("nan")}])
    assert nan["seeded_bytes"] == 0
    unk = idx.coverage_for_torrents(
        [{"infohash": "ffff", "progress": 1.0, "downloaded": 99999, "size": 99999}]
    )
    assert unk["seeded_bytes"] == 0
    missing = idx.coverage_for_torrents(
        [{"infohash": "aabb", "progress": 1.0, "state": "missing_files", "is_complete": True}]
    )
    assert missing["seeded_bytes"] == 0
    dup = idx.coverage_for_torrents(
        [
            {"infohash": "aabb", "progress": 1.0},
            {"infohash": "aabb", "progress": 1.0},
        ]
    )
    assert dup["seeded_bytes"] == 100
    print("ok: coverage case-insensitive + progress-weighted + unknown ignored")
