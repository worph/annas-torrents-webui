"""Download destination helpers: defaults, browse API, native pick, path checks.

Default content lives under DATA_DIR/content. Extra presets come from
STORAGE_PATHS / other drive letters.

Browse uses a separate native Windows process (pythonw + tk dialog); browsers
cannot return absolute paths themselves.

The API route that invokes the picker is protected by the app auth middleware.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import shutil
import string
import subprocess
import sys
import tempfile

# Created at the root of other drives (D:\, E:\, …) when used as a destination.
ANNA_FOLDER = "Anna's Archive Torrents"


def parse_storage_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        path = part.strip()
        if not path:
            continue
        key = normalize_path(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def normalize_path(path: str) -> str:
    module = _path_module(path)
    return module.normcase(module.normpath(str(path).strip()))


def _path_module(path: str):
    """Pick path semantics: Windows drive paths vs remote POSIX (case-sensitive)."""
    raw = str(path).strip()
    if ntpath.splitdrive(raw)[0] or "\\" in raw:
        return ntpath
    # Forward-slash absolute/remote paths stay case-sensitive even on Windows hosts
    # (qBittorrent on Linux must not conflate /data/Content with /data/content).
    if raw.startswith("/") or "/" in raw:
        return posixpath
    return os.path


def path_key(path: str) -> str:
    """Absolute/normalized path for comparisons."""
    module = _path_module(path)
    if module is ntpath:
        # Resolve local reparse points (junctions/symlinks) when the path is on this host.
        try:
            if os.name == "nt" and (os.path.exists(path) or os.path.lexists(path)):
                return normalize_path(os.path.realpath(path))
        except OSError:
            pass
        return normalize_path(path)
    if module is posixpath:
        # Remote qBit paths — lexical only; never abspath against the Windows host.
        return normalize_path(path)
    return normalize_path(os.path.realpath(os.path.abspath(path)))


def resolved_path_key(path: str) -> str:
    """Like path_key, but resolve through existing ancestors (junction escape guard).

    For ``C:\\allowed\\link\\new`` where ``link`` is a junction to ``C:\\outside``,
    walks up until a real path exists and resolves that ancestor.
    """
    raw = str(path).strip()
    if not raw:
        return path_key(raw)
    module = _path_module(raw)
    if module is posixpath:
        return path_key(raw)
    cur = module.normpath(raw)
    trail: list[str] = []
    while True:
        try:
            if os.path.exists(cur) or os.path.lexists(cur):
                resolved = os.path.realpath(cur)
                for part in reversed(trail):
                    resolved = module.join(resolved, part)
                return normalize_path(resolved)
        except OSError:
            pass
        parent = module.dirname(cur)
        if parent == cur:
            return path_key(raw)
        trail.append(module.basename(cur))
        cur = parent


def path_is_within(path: str | None, parent: str | None) -> bool:
    if not path or not parent:
        return False
    # Resolve junctions/symlinks through existing ancestors so a missing leaf
    # under a reparse point cannot lexical-bypass the allowlist.
    child = resolved_path_key(path)
    base = resolved_path_key(parent)
    module = _path_module(parent)
    sep = "\\" if module is ntpath else "/"
    return child == base or child.startswith(base.rstrip("\\/") + sep)


def anna_destination(path: str) -> str:
    return _path_module(path).join(path, ANNA_FOLDER)


def matches_destination(torrent_save_path: str | None, destination: str | None) -> bool:
    """Torrent belongs to a download destination.

    Matches when the torrent save path equals or is inside the destination.
    Selecting a child folder must not match torrents stored in the parent.
    """
    if not torrent_save_path or not destination:
        return False
    sp = str(torrent_save_path).strip()
    dest = str(destination).strip()
    if not sp or not dest:
        return False
    return path_is_within(sp, dest)


def disk_usage(path: str) -> tuple[int, int, int] | None:
    """Free/total for the containing drive/mount when the path (or its parent) exists."""
    got = _disk_usage_resolved(path)
    return got[0] if got else None


def _disk_usage_resolved(path: str) -> tuple[tuple[int, int, int], str] | None:
    """Like disk_usage, plus the path shutil actually queried (for st_dev dedupe)."""
    if not path or not os.path.isabs(path):
        return None
    candidate = os.path.normpath(path)
    if not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        # Need an existing parent directory. Refuse missing paths whose only
        # ancestor is Unix `/` — unmounted `/extra` must not report root free space.
        # Windows drive roots (D:\\) are fine for a missing Anna folder.
        if not os.path.isdir(parent):
            return None
        if parent in ("/", os.sep) and os.name != "nt":
            return None
        candidate = parent
    try:
        u = shutil.disk_usage(candidate)
        return (u.total, u.used, u.free), candidate
    except OSError:
        return None


def sum_unique_disk_usage(paths) -> tuple[int, int, bool]:
    """Sum free and total across distinct volumes. Third value is False when any path is unknown."""
    path_list = [p for p in paths if p]
    if not path_list:
        return 0, 0, False
    seen: set = set()
    free = total = 0
    for path in path_list:
        got = _disk_usage_resolved(path)
        if not got:
            return 0, 0, False
        usage, resolved = got
        try:
            key: object = os.stat(resolved).st_dev
        except OSError:
            key = usage
        if key in seen:
            continue
        seen.add(key)
        total += usage[0]
        free += usage[2]
    return free, total, True


def option(path: str, *, label: str | None = None, default: bool = False) -> dict:
    usage = disk_usage(path)
    return {
        "path": path,
        "label": label or path,
        "default": default,
        "disk_free": usage[2] if usage else None,
        "disk_total": usage[0] if usage else None,
    }


def remote_option(path: str, *, label: str | None = None, default: bool = False) -> dict:
    return {
        "path": path,
        "label": label or path,
        "default": default,
        "disk_free": None,  # unknown — UI must not render as "0 B free"
        "disk_total": None,
    }


def list_roots() -> list[dict]:
    """Top-level browse entries: Windows drive letters, or / on Unix."""
    if os.name == "nt":
        roots = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    else:
        roots = ["/"]
    out = []
    for root in roots:
        usage = disk_usage(root)
        out.append(
            {
                "name": root.rstrip("\\/") or root,
                "path": root,
                "disk_free": usage[2] if usage else None,
            }
        )
    return out


def pick_folder() -> str | None:
    """Native folder dialog via a short-lived GUI process (not inside uvicorn).

    Spawning a separate pythonw process avoids the hang we saw when showing a
    WinForms dialog from the server's worker thread / captured PowerShell.
    """
    if os.name != "nt":
        return None
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".path.txt")
    out_path = out.name
    out.close()
    try:
        os.unlink(out_path)
    except OSError:
        pass

    # json.dumps → valid Python string literal (raw r'...' + doubled \\ was wrong).
    out_lit = json.dumps(out_path)
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk()\n"
        "r.withdraw()\n"
        "try:\n"
        "    r.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "p = filedialog.askdirectory(parent=r, mustexist=True) or ''\n"
        "r.destroy()\n"
        f"open({out_lit}, 'w', encoding='utf-8').write(p)\n"
    )
    exe = sys.executable
    # Prefer pythonw on Windows so no console flash steals focus.
    if os.name == "nt":
        candidate = exe.replace("python.exe", "pythonw.exe").replace("PYTHON.EXE", "pythonw.exe")
        if os.path.isfile(candidate):
            exe = candidate

    try:
        # No CREATE_NO_WINDOW — that can prevent the GUI dialog from appearing.
        subprocess.run([exe, "-c", code], timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return None

    if not os.path.isfile(out_path):
        return None
    try:
        with open(out_path, encoding="utf-8") as f:
            path = f.read().strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return path or None


def is_drive_root(path: str) -> bool:
    module = _path_module(path)
    cur = module.normpath(path)
    return module.dirname(cur) == cur


def drive_root_of(path: str) -> str:
    module = _path_module(path)
    cur = module.normpath(path)
    drive, _ = module.splitdrive(cur)
    if drive:
        return drive + ("\\" if module is ntpath else os.sep)
    return "\\" if module is ntpath else os.sep


def other_drive_destinations(default_path: str) -> list[str]:
    """``{D,E,…}:\\Anna's Archive Torrents`` for every drive except the default's."""
    skip = normalize_path(drive_root_of(default_path))
    out: list[str] = []
    for root in list_roots():
        if normalize_path(root["path"]) == skip:
            continue
        out.append(os.path.join(root["path"], ANNA_FOLDER))
    return out


def ensure_save_dir(path: str) -> str:
    """Drive root → append Anna folder; create directory if missing."""
    cur = os.path.abspath(path.strip())
    if is_drive_root(cur):
        cur = os.path.join(cur, ANNA_FOLDER)
    os.makedirs(cur, exist_ok=True)
    return cur


def preset_options(default: str, extra: list[str] | None = None) -> list[dict]:
    """Default path + other-drive Anna folders + STORAGE_PATHS extras.

    Drive options store ``X:\\Anna's Archive Torrents`` but the label is just ``X:``.
    """
    opts = [option(default, label=f"(Default) · {default}", default=True)]
    seen = {normalize_path(default)}
    for path in other_drive_destinations(default):
        key = normalize_path(path)
        if key in seen:
            continue
        seen.add(key)
        letter = os.path.splitdrive(path)[0] or path  # "D:"
        opts.append(option(path, label=letter))
    for path in extra or []:
        key = normalize_path(path)
        if key in seen:
            continue
        seen.add(key)
        opts.append(option(path))
    return opts


if __name__ == "__main__":
    assert parse_storage_paths("D:/a, E:/b;D:/a") == ["D:/a", "E:/b"]
    assert path_is_within("D:/a", "D:\\a")
    assert not path_is_within("C:/x", "D:/a")
    if os.name == "nt":
        assert is_drive_root("D:\\")
        assert not is_drive_root("D:\\foo")
        assert matches_destination(r"E:\Anna's Archive Torrents\book", r"E:\Anna's Archive Torrents")
        assert not matches_destination(r"E:\Anna's Archive Torrents", r"E:\Anna's Archive Torrents\book")
        assert not matches_destination(r"E:\Anna's Archive", r"E:\Anna's Archive Torrents")
        assert not matches_destination(r"E:\Anna's Archive", r"C:\data\content")
        # Remote POSIX paths stay case-sensitive on a Windows host.
        assert not path_is_within("/data/Content/file", "/data/content")
        assert path_is_within("/data/content/file", "/data/content")
    else:
        assert is_drive_root("/")
        assert matches_destination("/data/content/a", "/data/content")
        assert not matches_destination("/data/content", "/data/content/a")
        assert not matches_destination("/other", "/data/content")
    roots = list_roots()
    assert roots
    # Same mount counted once even when two absolute paths share a volume.
    here = os.path.abspath(".")
    child = os.path.join(here, "storage_sum_child")
    one = sum_unique_disk_usage([here])
    two = sum_unique_disk_usage([here, child])
    assert one == two, (one, two)
    assert one[2] is True
    # Missing Unix path under `/` must not invent root free space.
    if os.name != "nt":
        assert disk_usage("/annas_webui_missing_mount_xyz") is None
        assert sum_unique_disk_usage(["/annas_webui_missing_mount_xyz"])[2] is False
    print(f"ok: storage helpers ({len(roots)} roots)")
