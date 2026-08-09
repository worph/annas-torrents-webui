"""Pydantic request models for API routes."""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, field_validator


class ProvisionRequest(BaseModel):
    max_tb: float = Field(gt=0, le=30)
    collections: list[str] | None = None  # filter by "top_level/group" keys; None = all
    save_path: str | None = None  # configured destination or server-picked path
    preallocate: bool = False  # reserve full file size on disk when adding
    # When free space cannot be read (common for remote qBit), require explicit opt-in.
    allow_unknown_disk: bool = False

    @field_validator("max_tb")
    @classmethod
    def _finite_max_tb(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("max_tb must be a finite number")
        return v


class SettingsRequest(BaseModel):
    torrent_backend: str | None = None
    qbit_category: str | None = None
    qbit_url: str | None = None
    qbit_user: str | None = None
    qbit_pass: str | None = None


class SpacePreviewRequest(BaseModel):
    bytes: int | None = None
    gb: float | None = None
    save_path: str | None = None

    @field_validator("bytes")
    @classmethod
    def _valid_bytes(cls, v: int | None) -> int | None:
        if v is not None and not 0 < v <= 1_000_000 * 1000**3:
            raise ValueError("bytes must be between 1 and 1 PB")
        return v

    @field_validator("gb")
    @classmethod
    def _valid_gb(cls, v: float | None) -> float | None:
        if v is not None and (not math.isfinite(v) or not 0 < v <= 1_000_000):
            raise ValueError("gb must be finite and between 0 and 1,000,000")
        return v


class SpaceFreeRequest(BaseModel):
    infohashes: list[str]
    confirm: bool = False
    save_path: str | None = None
    token: str | None = None
    request_bytes: int | None = None


class TorrentRemoveRequest(BaseModel):
    infohash: str
    confirm: bool = False
    delete_files: bool = True


class RateLimitRequest(BaseModel):
    bytes_per_sec: int = Field(ge=-1)
