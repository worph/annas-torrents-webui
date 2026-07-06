"""Fetch and download the Anna's Archive torrent list.

Anna's Archive does the selection server-side via the ``generate_torrents``
endpoint: given a ``max_tb`` target it returns a JSON list, already prioritized
by ``total_sort_score``, of the torrents most in need of seeding.

We reuse only the *contract* of the original ``annas-torrents`` CLI (the URL,
the ``max_tb`` param, and the mirror fallback) — not its blocking code.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger("selection")

# Mirror domains, tried in order until one responds.
MIRRORS = ["annas-archive.gl", "annas-archive.pk", "annas-archive.gd"]

# A very large target used to pull the *whole* index for coverage denominators.
FULL_INDEX_TB = 100_000


@dataclass
class TorrentEntry:
    """One entry from the generate_torrents JSON response."""

    url: str
    display_name: str
    btih: str  # bittorrent info hash — stable join/dedup key
    data_size: int  # actual content size in bytes (what we download & seed)
    torrent_size: int  # size of the .torrent metadata file itself
    top_level_group: str  # e.g. "external"
    group: str  # e.g. "libgen_li_magazines"
    seeders: int
    leechers: int
    sort_score: float

    @classmethod
    def from_json(cls, d: dict) -> "TorrentEntry":
        return cls(
            url=d["url"],
            display_name=d["display_name"],
            btih=d.get("btih", ""),
            data_size=int(d.get("data_size", 0)),
            torrent_size=int(d.get("torrent_size", 0)),
            top_level_group=d.get("top_level_group_name", ""),
            group=d.get("group_name", ""),
            seeders=int(d.get("seeders", 0)),
            leechers=int(d.get("leechers", 0)),
            sort_score=float(d.get("total_sort_score", 0.0)),
        )

    @property
    def collection(self) -> str:
        """A stable collection key used for filtering in the UI."""
        return f"{self.top_level_group}/{self.group}" if self.group else self.top_level_group


async def fetch_torrent_list(max_tb: float, timeout: float = 60.0) -> list[TorrentEntry]:
    """Fetch the prioritized torrent list for a given TB target, with mirror fallback."""
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for domain in MIRRORS:
            url = f"https://{domain}/dyn/generate_torrents?max_tb={max_tb}&format=json"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                entries = [TorrentEntry.from_json(d) for d in data]
                log.info("fetched %d entries from %s (max_tb=%s)", len(entries), domain, max_tb)
                return entries
            except Exception as e:  # noqa: BLE001 — any failure falls through to next mirror
                log.warning("mirror %s failed: %s", domain, e)
                last_err = e
    raise RuntimeError(f"all mirrors failed; last error: {last_err}")


async def download_torrent_files(
    entries: list[TorrentEntry],
    dest_dir: str,
    concurrency: int = 8,
    timeout: float = 60.0,
) -> list[str]:
    """Download the .torrent metadata files for the given entries into dest_dir.

    Returns the list of local file paths successfully written. Skips files that
    already exist so re-provisioning is idempotent.
    """
    os.makedirs(dest_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    paths: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:

        async def one(entry: TorrentEntry) -> None:
            # display_name is a bare filename; guard against path traversal.
            safe = os.path.basename(entry.display_name)
            path = os.path.join(dest_dir, safe)
            if os.path.exists(path):
                paths.append(path)
                return
            async with sem:
                try:
                    resp = await client.get(entry.url)
                    resp.raise_for_status()
                    tmp = path + ".part"
                    with open(tmp, "wb") as f:
                        f.write(resp.content)
                    os.replace(tmp, path)
                    paths.append(path)
                    log.info("downloaded %s (%d bytes)", safe, len(resp.content))
                except Exception as e:  # noqa: BLE001
                    log.warning("failed to download %s: %s", entry.url, e)

        await asyncio.gather(*(one(e) for e in entries))

    return paths
