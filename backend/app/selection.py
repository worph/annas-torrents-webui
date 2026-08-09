"""Fetch and download the Anna's Archive torrent list.

Anna's Archive does the selection server-side via the ``generate_torrents``
endpoint: given a ``max_tb`` target it returns a JSON list, already prioritized
by ``total_sort_score``, of the torrents most in need of seeding.

We reuse only the *contract* of the original ``annas-torrents`` CLI (the URL,
the ``max_tb`` param, and the mirror fallback) — not its blocking code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("selection")

# Mirror domains, tried in order until one responds.
MIRRORS = ["annas-archive.gl", "annas-archive.pk", "annas-archive.gd"]

# A very large target used to pull the *whole* index for coverage denominators.
FULL_INDEX_TB = 100_000

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


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
        if not isinstance(d, dict) or not isinstance(d.get("url"), str) or not d["url"].strip():
            raise ValueError("torrent entry has no URL")
        if not isinstance(d.get("display_name"), str):
            raise ValueError("torrent entry has no display name")
        data_size = int(d.get("data_size", 0) or 0)
        try:
            torrent_size = int(d.get("torrent_size", 0) or 0)
        except (TypeError, ValueError) as e:
            raise ValueError("torrent entry has invalid torrent_size") from e
        if torrent_size < 0:
            raise ValueError("torrent entry has negative torrent_size")
        if data_size <= 0:
            raise ValueError("torrent entry has non-positive data_size")
        sort_score = float(d.get("total_sort_score", 0.0))
        if not math.isfinite(sort_score):
            sort_score = 0.0
        btih = d.get("btih", "")
        if not isinstance(btih, str):
            btih = ""
        btih = btih.strip().lower()
        if not _HEX40.match(btih):
            raise ValueError("torrent entry has invalid btih")
        return cls(
            url=d["url"].strip(),
            display_name=d["display_name"],
            btih=btih,
            data_size=data_size,
            torrent_size=torrent_size,
            top_level_group=d.get("top_level_group_name", ""),
            group=d.get("group_name", ""),
            seeders=int(d.get("seeders", 0)),
            leechers=int(d.get("leechers", 0)),
            sort_score=sort_score,
        )

    @property
    def collection(self) -> str:
        """A stable collection key used for filtering in the UI."""
        return f"{self.top_level_group}/{self.group}" if self.group else self.top_level_group


def _parse_bencode(data: bytes, pos: int = 0, depth: int = 0) -> tuple[object, int]:
    """Parse enough bencode to validate metadata without trusting its contents."""
    if depth > 100 or pos >= len(data):
        raise ValueError("invalid bencode")
    token = data[pos : pos + 1]
    if token == b"i":
        end = data.find(b"e", pos + 1)
        if end < 0:
            raise ValueError("unterminated integer")
        raw = data[pos + 1 : end]
        if not raw or (raw.startswith(b"0") and raw != b"0") or raw.startswith(b"-0"):
            raise ValueError("invalid integer")
        int(raw)
        return int(raw), end + 1
    if token == b"l":
        items = []
        pos += 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            item, pos = _parse_bencode(data, pos, depth + 1)
            items.append(item)
        if pos >= len(data):
            raise ValueError("unterminated list")
        return items, pos + 1
    if token == b"d":
        items: dict[bytes, object] = {}
        pos += 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            key, pos = _parse_bencode(data, pos, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("dictionary key is not bytes")
            if key in items:
                raise ValueError("duplicate dictionary key")
            value, pos = _parse_bencode(data, pos, depth + 1)
            items[key] = value
        if pos >= len(data):
            raise ValueError("unterminated dictionary")
        return items, pos + 1
    if token.isdigit():
        colon = data.find(b":", pos)
        if colon < 0:
            raise ValueError("invalid byte string")
        size_raw = data[pos:colon]
        if not size_raw or (len(size_raw) > 1 and size_raw.startswith(b"0")):
            raise ValueError("invalid byte string length")
        size = int(size_raw)
        start = colon + 1
        end = start + size
        if size < 0 or end > len(data):
            raise ValueError("byte string exceeds payload")
        return data[start:end], end
    raise ValueError("unknown bencode token")


def _torrent_info_slice(data: bytes) -> tuple[int, int]:
    """Return (start, end) of the raw info dictionary in a torrent payload."""
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("torrent metadata is too large")
    if data[:1] != b"d":
        raise ValueError("torrent is not a bencoded dictionary")
    pos = 1
    info_start = None
    info_end = None
    while pos < len(data) and data[pos : pos + 1] != b"e":
        key, pos = _parse_bencode(data, pos)
        if not isinstance(key, bytes):
            raise ValueError("dictionary key is not bytes")
        value_start = pos
        _, pos = _parse_bencode(data, pos)
        if key == b"info":
            if info_start is not None:
                raise ValueError("torrent has duplicate info dictionary")
            info_start, info_end = value_start, pos
    if pos >= len(data) or data[pos : pos + 1] != b"e" or info_start is None or info_end is None:
        raise ValueError("torrent has no info dictionary")
    if pos + 1 != len(data):
        raise ValueError("torrent has trailing data after dictionary")
    return info_start, info_end


def _torrent_infohash(data: bytes) -> str:
    """Return the v1 infohash from a valid top-level torrent dictionary."""
    info_start, info_end = _torrent_info_slice(data)
    return hashlib.sha1(data[info_start:info_end]).hexdigest()


def _torrent_content_size(data: bytes) -> int:
    """Total content bytes from the info dict (single- or multi-file)."""
    info_start, info_end = _torrent_info_slice(data)
    info, _ = _parse_bencode(data, info_start)
    if not isinstance(info, dict):
        raise ValueError("torrent info is not a dictionary")
    if b"length" in info:
        length = int(info[b"length"])
        if length < 0:
            raise ValueError("torrent length is negative")
        return length
    files = info.get(b"files")
    if not isinstance(files, list):
        raise ValueError("torrent has no length or files")
    total = 0
    for item in files:
        if not isinstance(item, dict) or b"length" not in item:
            raise ValueError("torrent file entry has no length")
        length = int(item[b"length"])
        if length < 0:
            raise ValueError("torrent file length is negative")
        total += length
    return total


def _validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {domain.lower() for domain in MIRRORS}
    allowed.update(f"www.{domain}".lower() for domain in MIRRORS)
    if parsed.scheme != "https" or host not in allowed:
        raise ValueError("torrent URL is outside the trusted HTTPS mirrors")


async def _trusted_get(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
) -> tuple[str, bytes]:
    """Stream a trusted GET; abort once the body exceeds ``max_bytes``.

    Returns ``(final_url, body)``. Redirect targets must stay on trusted mirrors.
    """
    for _ in range(4):
        _validate_download_url(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("trusted mirror returned an empty redirect")
                url = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            cl = response.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise ValueError(f"response exceeds the {max_bytes} byte limit")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"response exceeds the {max_bytes} byte limit")
                chunks.append(chunk)
            return str(response.url), b"".join(chunks)
    raise ValueError("too many redirects from trusted mirror")


async def _trusted_get_bytes(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
) -> bytes:
    _url, body = await _trusted_get(client, url, max_bytes)
    return body


def _validate_entry_sizes(entry: TorrentEntry, body: bytes, content_size: int) -> None:
    """Apply index size checks to freshly downloaded or reused metadata."""
    # Metadata must be a plausible .torrent even when the index omits torrent_size.
    if len(body) < 16:
        raise ValueError(f"torrent metadata too small: {len(body)} bytes")
    if entry.torrent_size > 0:
        if len(body) > max(entry.torrent_size * 4, 64_000):
            raise ValueError(f"size mismatch: got {len(body)}, expected ~{entry.torrent_size}")
    if entry.data_size <= 0:
        raise ValueError("index entry missing positive data_size")
    if content_size <= 0:
        raise ValueError(f"content size mismatch: got {content_size}, expected ~{entry.data_size}")
    lo = max(1, entry.data_size // 4)
    hi = max(entry.data_size * 4, 64_000)
    if content_size < lo or content_size > hi:
        raise ValueError(f"content size mismatch: got {content_size}, expected ~{entry.data_size}")


async def _trusted_download(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int = 8 * 1024 * 1024,
) -> tuple[str, bytes]:
    return await _trusted_get(client, url, max_bytes)


async def fetch_torrent_list(max_tb: float, timeout: float = 60.0) -> list[TorrentEntry]:
    """Fetch the prioritized torrent list for a given TB target, with mirror fallback.

    User-facing targets are capped at 30 TB. Coverage refresh may request
    FULL_INDEX_TB so the whole archive index can be cached as the denominator.
    """
    if not math.isfinite(max_tb) or not 0 < max_tb <= FULL_INDEX_TB:
        raise ValueError(f"max_tb must be finite and between 0 and {FULL_INDEX_TB} TB")
    last_err: Exception | None = None
    list_limit = 32 * 1024 * 1024
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for domain in MIRRORS:
            url = f"https://{domain}/dyn/generate_torrents?max_tb={max_tb}&format=json"
            try:
                raw = await _trusted_get_bytes(client, url, list_limit)
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise ValueError("mirror returned a non-list torrent response")
                if len(data) > 200_000:
                    raise ValueError("mirror returned too many torrent entries")
                entries: list[TorrentEntry] = []
                skipped = 0
                for d in data:
                    try:
                        entries.append(TorrentEntry.from_json(d))
                    except (ValueError, TypeError, OverflowError):
                        skipped += 1
                if not entries:
                    raise ValueError("mirror returned no usable torrent entries")
                log.info(
                    "fetched %d entries from %s (max_tb=%s, skipped=%d)",
                    len(entries),
                    domain,
                    max_tb,
                    skipped,
                )
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
) -> tuple[list[tuple[str, int]], int, set[str]]:
    """Download .torrent metadata files into dest_dir.

    Returns ``((path, expected_content_bytes), failed_count, created_abs_paths)``.
    ``created_abs_paths`` are files written this call (not reused existing metadata).
    Uses unique temporary files and verifies the metadata's actual infohash.
    """
    os.makedirs(dest_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    paths: list[tuple[str, int]] = []
    created: set[str] = set()
    failed = 0
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:

        async def one(entry: TorrentEntry) -> None:
            nonlocal failed
            async with sem:
                tmp = os.path.join(dest_dir, f".{uuid.uuid4().hex}.part")
                try:
                    _validate_download_url(entry.url)
                    expected = (entry.btih or "").lower().strip()
                    expected_path = os.path.join(dest_dir, f"{expected}.torrent") if _HEX40.match(expected) else None
                    if expected_path and os.path.isfile(expected_path):
                        try:
                            with open(expected_path, "rb") as existing:
                                body = existing.read()
                            existing_hash = _torrent_infohash(body)
                            if existing_hash == expected:
                                content_size = _torrent_content_size(body)
                                _validate_entry_sizes(entry, body, content_size)
                                async with lock:
                                    paths.append((expected_path, content_size))
                                return
                        except (OSError, ValueError):
                            pass
                    response_url, body = await _trusted_download(client, entry.url)
                    _validate_download_url(response_url)
                    actual = _torrent_infohash(body)
                    if not expected or not _HEX40.match(expected):
                        raise ValueError("torrent entry missing valid btih")
                    if actual != expected:
                        raise ValueError(f"infohash mismatch: got {actual}, expected {expected}")
                    content_size = _torrent_content_size(body)
                    _validate_entry_sizes(entry, body, content_size)
                    path = os.path.join(dest_dir, f"{actual}.torrent")
                    # Never overwrite pre-existing metadata — reuse if valid, else skip.
                    if os.path.isfile(path):
                        try:
                            with open(path, "rb") as existing:
                                existing_body = existing.read()
                            if _torrent_infohash(existing_body) == actual:
                                content_size = _torrent_content_size(existing_body)
                                _validate_entry_sizes(entry, existing_body, content_size)
                                async with lock:
                                    paths.append((path, content_size))
                                return
                        except (OSError, ValueError):
                            pass
                        raise ValueError(f"refusing to overwrite existing torrent metadata: {path}")
                    with open(tmp, "wb") as f:
                        f.write(body)
                    # replace + register under one lock, with no await between —
                    # cancel cannot leave an orphan .torrent outside `created`.
                    async with lock:
                        if os.path.isfile(path):
                            try:
                                with open(path, "rb") as existing:
                                    existing_body = existing.read()
                                if _torrent_infohash(existing_body) == actual:
                                    content_size = _torrent_content_size(existing_body)
                                    _validate_entry_sizes(entry, existing_body, content_size)
                                    paths.append((path, content_size))
                                    try:
                                        os.unlink(tmp)
                                    except OSError:
                                        pass
                                    return
                            except (OSError, ValueError):
                                pass
                            raise ValueError(f"refusing to overwrite existing torrent metadata: {path}")
                        os.replace(tmp, path)
                        paths.append((path, content_size))
                        created.add(os.path.abspath(path))
                    log.info("downloaded %s (%d bytes)", os.path.basename(path), len(body))
                except asyncio.CancelledError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                except Exception as e:  # noqa: BLE001
                    async with lock:
                        failed += 1
                    log.warning("failed to download %s: %s", entry.url, e)
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        unique: list[TorrentEntry] = []
        seen: set[str] = set()
        for entry in entries:
            key = (entry.btih or "").lower().strip()
            key = key if _HEX40.match(key) else f"url:{entry.url}"
            if key not in seen:
                seen.add(key)
                unique.append(entry)
        try:
            await asyncio.gather(*(one(e) for e in unique))
        except asyncio.CancelledError:
            # Drop .part temps and any metadata this call created but not yet
            # returned to the provisioner (cancel can race gather completion).
            async with lock:
                orphaned = list(created)
                created.clear()
                paths.clear()
            for abs_path in orphaned:
                try:
                    os.unlink(abs_path)
                except OSError:
                    pass
            for name in os.listdir(dest_dir) if os.path.isdir(dest_dir) else []:
                if name.startswith(".") and name.endswith(".part"):
                    try:
                        os.unlink(os.path.join(dest_dir, name))
                    except OSError:
                        pass
            raise

    return paths, failed, created
