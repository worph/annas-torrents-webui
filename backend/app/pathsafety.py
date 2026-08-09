"""Safe path helpers for torrent file deletion."""

from __future__ import annotations

import os
import shutil
import stat


def content_roots_overlap(save_a: str, name_a: str, save_b: str, name_b: str) -> bool:
    """True when two torrent content roots are the same or nested.

    Uses realpath when local paths exist so junction aliases compare equal;
    remote POSIX/Windows paths stay lexical (never host abspath on foreign paths).
    """
    if not save_a or not name_a or not save_b or not name_b:
        return False

    import ntpath
    import posixpath

    from .storage import _path_module, path_key

    def _root(save: str, name: str) -> tuple[object, str]:
        module = _path_module(save)
        joined = module.join(save, name)
        if module is posixpath:
            return module, path_key(joined)
        if module is ntpath:
            # Local Windows host: resolve reparse points when the path exists here.
            if os.name == "nt":
                try:
                    if os.path.exists(joined) or os.path.lexists(joined):
                        return module, os.path.normcase(os.path.realpath(joined))
                except OSError:
                    pass
            # Remote Windows path (e.g. Linux Docker → qBit on Windows): lexical only.
            return module, module.normcase(module.normpath(joined))
        try:
            if os.path.exists(joined) or os.path.lexists(joined):
                return module, os.path.normcase(os.path.realpath(joined))
        except OSError:
            pass
        return module, os.path.normcase(os.path.abspath(joined))

    mod_a, a = _root(save_a, name_a)
    _mod_b, b = _root(save_b, name_b)
    if a == b:
        return True
    sep = "\\" if mod_a is ntpath else ("/" if mod_a is posixpath else os.sep)
    return a.startswith(b.rstrip("\\/") + sep) or b.startswith(a.rstrip("\\/") + sep)


def shared_content_ids(entries: list[tuple[str, str, str]]) -> set[str]:
    """Return ids whose (save_path, name) overlaps another entry.

    ``entries`` is ``(id, save_path, name)``. Used as a preflight before any
    remove/delete so batch victims cannot clear shared content after peers leave.
    """
    shared: set[str] = set()
    for i, (id_a, save_a, name_a) in enumerate(entries):
        if not id_a or not save_a or not name_a:
            continue
        for j, (id_b, save_b, name_b) in enumerate(entries):
            if i == j or id_a == id_b:
                continue
            if content_roots_overlap(save_a, name_a, save_b, name_b):
                shared.add(id_a)
                break
    return shared


def _is_reparse(path: str) -> bool:
    """True for symlinks and Windows junctions/mount points."""
    try:
        if os.path.islink(path):
            return True
        isj = getattr(os.path, "isjunction", None)
        if callable(isj) and isj(path):
            return True
    except OSError:
        return True
    return False


def _reparse_on_walk(base: str, parts: list[str]) -> bool:
    """True if any component from base through parts is a reparse point."""
    cur = base
    for part in parts:
        if not part or part == ".":
            continue
        cur = os.path.join(cur, part)
        if _is_reparse(cur):
            return True
    return False


def _reparse_on_ancestors(path: str) -> bool:
    """True if path or any ancestor is a reparse point (before realpath)."""
    cur = os.path.abspath(path)
    while True:
        if _is_reparse(cur):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def safe_delete_target(save_path: str, name: str) -> str | None:
    """Return absolute path to delete if it stays under save_path; else None.

    Rejects absolute names, empty names, reparse points (junctions/symlinks) on
    the walk or ancestors, and any path that escapes the save dir after realpath.
    """
    if not save_path or not name or not str(name).strip():
        return None
    name = str(name).strip()
    if os.path.isabs(name):
        return None
    parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        return None
    # Refuse before realpath — an ancestor junction would otherwise relocate base.
    if _reparse_on_ancestors(save_path):
        return None
    try:
        base = os.path.realpath(save_path)
    except OSError:
        return None
    if not os.path.isdir(base):
        return None
    # Refuse before resolving — realpath would hide an in-tree junction alias.
    if _reparse_on_walk(base, parts):
        return None
    lexical = os.path.normpath(os.path.join(base, *parts)) if parts else base
    try:
        target = os.path.realpath(lexical)
    except OSError:
        return None
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    if target == base:
        return None
    return target


def _dir_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _rmtree_at(parent_fd: int, name: str) -> None:
    """Delete directory ``name`` under ``parent_fd`` without following leaf symlinks."""
    flags = _dir_open_flags()
    fd = os.openat(parent_fd, name, flags)
    try:
        with os.scandir(fd) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False), e.is_symlink()) for e in it]
        for child, is_dir, is_link in entries:
            if is_link or not is_dir:
                os.unlink(child, dir_fd=fd)
            else:
                _rmtree_at(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _delete_posix_at(base: str, parts: list[str]) -> bool:
    """Delete via directory fds + unlinkat/rmdir so a swapped symlink is not followed."""
    if not parts or not hasattr(os, "openat"):
        return False
    flags = _dir_open_flags()
    owned: list[int] = []
    try:
        parent_fd = os.open(base, flags)
        owned.append(parent_fd)
        for part in parts[:-1]:
            st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return False
            next_fd = os.openat(parent_fd, part, flags)
            owned.append(next_fd)
            parent_fd = next_fd
        leaf = parts[-1]
        st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            # Removing the link itself is fine and does not touch the link target.
            os.unlink(leaf, dir_fd=parent_fd)
            return True
        if stat.S_ISDIR(st.st_mode):
            _rmtree_at(parent_fd, leaf)
            return True
        if stat.S_ISREG(st.st_mode):
            os.unlink(leaf, dir_fd=parent_fd)
            return True
        return False
    except OSError:
        return False
    finally:
        for fd in reversed(owned):
            try:
                os.close(fd)
            except OSError:
                pass


def delete_under(save_path: str, name: str) -> bool:
    """Delete file/dir named ``name`` under ``save_path`` if containment holds."""
    target = safe_delete_target(save_path, name)
    if not target:
        return False
    if _reparse_on_ancestors(save_path):
        return False
    try:
        base = os.path.realpath(save_path)
    except OSError:
        return False
    # Re-check immediately before mutating — shrinks ancestor-junction TOCTOU.
    if _reparse_on_ancestors(save_path) or os.path.realpath(save_path) != base:
        return False
    if safe_delete_target(save_path, name) != target:
        return False
    parts = [p for p in str(name).strip().replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    lexical = os.path.normpath(os.path.join(base, *parts))
    if _reparse_on_walk(base, parts) or _is_reparse(lexical):
        return False

    # POSIX: delete relative to directory fds (symlink swap cannot redirect unlink).
    if os.name != "nt" and hasattr(os, "openat"):
        return _delete_posix_at(base, parts)

    # Windows: no portable openat — final reparse revalidation, then lexical delete.
    # ponytail: Windows residual TOCTOU between this check and rmtree; escalate with
    # FILE_FLAG_OPEN_REPARSE_POINT + file-id compare if a real race is observed.
    if _reparse_on_walk(base, parts) or _is_reparse(lexical):
        return False
    if safe_delete_target(save_path, name) != target:
        return False
    try:
        if os.path.isdir(lexical) and not _is_reparse(lexical):
            if os.path.realpath(lexical) != target:
                return False
            if _is_reparse(lexical):
                return False
            shutil.rmtree(lexical)
        elif os.path.isfile(lexical) or os.path.islink(lexical) or _is_reparse(lexical):
            os.unlink(lexical)
        else:
            return False
    except OSError:
        return False
    return True


if __name__ == "__main__":
    import tempfile
    import threading
    import time

    with tempfile.TemporaryDirectory() as td:
        nested = os.path.join(td, "ok")
        os.makedirs(nested)
        open(os.path.join(nested, "f"), "w").close()
        assert safe_delete_target(td, "ok")
        assert safe_delete_target(td, "../x") is None
        assert safe_delete_target(td, "/etc/passwd") is None
        assert delete_under(td, "ok")
        assert not os.path.exists(nested)
        assert content_roots_overlap(td, "foo", td, "foo")
        assert content_roots_overlap(td, "foo", os.path.join(td, "foo"), "bar")
        assert not content_roots_overlap(td, "a", td, "b")
        assert content_roots_overlap("/data", "foo", "/data", "foo")
        assert not content_roots_overlap("/data", "a", "/data", "b")
        # Remote Windows paths must overlap lexically even on a Linux host.
        assert content_roots_overlap(r"C:\Downloads", "root", r"C:\Downloads\root", "child")
        assert not content_roots_overlap(r"C:\Downloads", "root", r"C:\Downloads", "other")
        assert "a" in shared_content_ids(
            [("a", r"C:\t", "same"), ("b", r"C:\t", "same"), ("c", r"C:\t", "other")]
        )
        # Symlink/junction under save_path must be refused (when supported).
        link = os.path.join(td, "alias")
        real = os.path.join(td, "real")
        os.makedirs(real)
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            assert safe_delete_target(td, "alias") is None
            assert not delete_under(td, "alias")
            assert os.path.isdir(real)
            # Ancestor reparse: save_path under a junction must refuse deletion.
            sub = os.path.join(link, "nested")
            os.makedirs(sub, exist_ok=True)
            victim = os.path.join(real, "nested", "x")
            open(victim, "w").close()
            assert safe_delete_target(sub, "x") is None
            assert not delete_under(sub, "x")
            assert os.path.isfile(victim)

        # Race: after validation, replace leaf with symlink to outside — outside survives.
        outside_root = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(outside_root, "treasure")
            open(outside_file, "w").write("keep")
            victim_dir = os.path.join(td, "race")
            os.makedirs(victim_dir)
            open(os.path.join(victim_dir, "f"), "w").close()
            assert safe_delete_target(td, "race")

            barrier = threading.Barrier(2)
            result = {"ok": None}

            def deleter():
                barrier.wait()
                # Small delay so swap can win the race window on slow paths.
                time.sleep(0.01)
                result["ok"] = delete_under(td, "race")

            def swapper():
                barrier.wait()
                try:
                    shutil.rmtree(victim_dir)
                    os.symlink(outside_root, victim_dir, target_is_directory=True)
                except (OSError, NotImplementedError):
                    result["ok"] = False

            t1 = threading.Thread(target=deleter)
            t2 = threading.Thread(target=swapper)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            assert os.path.isfile(outside_file), "outside target must survive"
            assert open(outside_file).read() == "keep"
        finally:
            shutil.rmtree(outside_root, ignore_errors=True)
    print("ok: pathsafety")
