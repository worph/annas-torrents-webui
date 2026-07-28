"""Shared disk listing for the Disk committed selector."""

from __future__ import annotations

import os
import shutil
import string


def list_disks() -> list[dict]:
    roots: list[str] = []
    if os.name == "nt":
        roots = [f"{l}:\\" for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
    else:
        roots = ["/"]
        for p in ("/mnt", "/media", "/data"):
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    roots.append(os.path.join(p, name))
    out = []
    for root in roots:
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        out.append({"path": root, "free": u.free, "total": u.total, "used": u.used})
    return out


def disk_for_path(path: str | None, disks: list[dict]) -> dict | None:
    """Pick the disk/mount that contains ``path`` (longest match wins).

    Uses os.path so Unix save paths keep forward slashes — do not force ``\\``.
    """
    if not path or not disks:
        return None
    save = os.path.normcase(os.path.normpath(path))
    best: dict | None = None
    best_len = -1
    for d in disks:
        root = os.path.normcase(os.path.normpath(d["path"]))
        prefix = root if root.endswith(os.sep) else root + os.sep
        if save == root or save.startswith(prefix):
            if len(root) > best_len:
                best = d
                best_len = len(root)
    return best


if __name__ == "__main__":
    disks = list_disks()
    assert isinstance(disks, list)

    # Cross-platform match checks (no real mounts required).
    if os.sep == "\\":
        sample = [{"path": "D:\\", "free": 1, "total": 2, "used": 1}]
        assert disk_for_path("D:/Anna/Archive", sample)["path"] == "D:\\"
        assert disk_for_path(None, sample) is None
    else:
        sample = [
            {"path": "/", "free": 1, "total": 2, "used": 1},
            {"path": "/mnt/data", "free": 3, "total": 4, "used": 1},
        ]
        assert disk_for_path("/mnt/data/anna", sample)["path"] == "/mnt/data"
        assert disk_for_path("/home/x", sample)["path"] == "/"
        # Old bug: forcing backslashes would miss Unix mounts.
        broken = (("/mnt/data") or "").replace("/", "\\")
        assert not any(
            broken.upper().startswith(d["path"].rstrip("\\").upper()) for d in sample
        ), "sanity: slash-forced path must not match Unix disk paths"

    print(f"ok: {len(disks)} disks, disk_for_path checks passed")
