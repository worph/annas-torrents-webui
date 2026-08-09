"""Shared payload helpers for the dashboard controls."""

from __future__ import annotations

def controls_payload(
    *,
    seeding_paused: bool,
    downloads_paused: bool,
    upload_limit: int,
    download_limit: int,
) -> dict:
    """Canonical controls block for /api/status and SSE snapshots."""
    return {
        "seeding_paused": bool(seeding_paused),
        "downloads_paused": bool(downloads_paused),
        "upload_limit": int(upload_limit),
        "download_limit": int(download_limit),
    }
