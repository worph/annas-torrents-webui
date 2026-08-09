"""App settings persisted under DATA_DIR/settings.json.

Resolution order for most keys: settings.json > env > built-in default.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import tempfile
import threading
from urllib.parse import urlparse

from .storage import ANNA_FOLDER

SETTINGS_NAME = "settings.json"
BACKENDS = ("libtorrent", "qbittorrent")
_SETTINGS_LOCK = threading.RLock()
DEFAULT_QBIT_URL = "http://127.0.0.1:8080"
_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def _ip_is_cloud_metadata(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) before checks.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Link-local includes AWS/GCP 169.254.169.254 and IPv6 fe80::/10.
    if ip.is_link_local:
        return True
    # Oracle / Alibaba metadata (not link-local).
    if ip.version == 4 and str(ip) in ("100.100.100.200", "100.96.0.200"):
        return True
    # AWS IMDS IPv6 unique-local endpoint.
    if ip.version == 6 and ip == ipaddress.IPv6Address("fd00:ec2::254"):
        return True
    return False


def _http_allowed_without_tls(host: str) -> bool:
    """Plain HTTP is only fine for loopback / private / local Docker hostnames."""
    h = (host or "").lower().rstrip(".")
    if h in {"localhost", "host.docker.internal"} or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:
            infos = socket.getaddrinfo(h, None)
        except OSError:
            return False
        if not infos:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not (ip.is_loopback or ip.is_private):
                return False
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_private)


def _qbit_host_blocked(host: str) -> bool:
    """Block cloud metadata targets; keep RFC1918/localhost allowed for LAN qBit."""
    h = (host or "").lower().rstrip(".")
    if not h:
        return True
    if h in _METADATA_HOSTS or h.endswith(".metadata.google.internal"):
        return True
    # Reject decimal/hex IPv4 obfuscation (e.g. 2852039166, 0xa9fea9fe).
    if h.isdigit() or (h.startswith("0x") and len(h) > 2 and all(c in "0123456789abcdef" for c in h[2:])):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return _ip_is_cloud_metadata(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            if _ip_is_cloud_metadata(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            continue
    return False


def _clean_qbit_url(url: str) -> str:
    """Validate and normalize a qBittorrent URL (no credentials / metadata SSRF)."""
    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("qbit_url must be an http(s) URL with a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("qbit_url must not include credentials, query, or fragment")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("qbit_url must be an http(s) URL with a host")
    if _qbit_host_blocked(host):
        raise ValueError("qbit_url must not target cloud metadata endpoints")
    if parsed.scheme == "http" and not _http_allowed_without_tls(host):
        raise ValueError("qbit_url must use https unless the host is local/private")
    if ":" in host and not host.startswith("["):
        host_part = f"[{host}]"
    else:
        host_part = host
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or ""
    return f"{parsed.scheme}://{host_part}{port}{path}"


def settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, SETTINGS_NAME)


def load_settings(data_dir: str) -> dict:
    with _SETTINGS_LOCK:
        path = settings_path(data_dir)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except OSError as e:
            raise OSError(f"could not read settings: {e}") from e
        except json.JSONDecodeError as e:
            quarantine_settings(data_dir, f"corrupt JSON: {e}")
            return {}
        if not isinstance(data, dict):
            quarantine_settings(data_dir, "settings.json must contain a JSON object")
            return {}
        return data


def quarantine_settings(data_dir: str, reason: str) -> str | None:
    """Rename a bad settings.json aside so resolve_* can fall back to env."""
    import logging
    import time

    log = logging.getLogger("settings")
    path = settings_path(data_dir)
    if not os.path.isfile(path):
        return None
    dest = f"{path}.corrupt.{int(time.time())}.{os.getpid()}"
    try:
        os.replace(path, dest)
    except OSError as e:
        # Collision on same-second quarantine — try once more with a unique suffix.
        dest = f"{path}.corrupt.{int(time.time())}.{os.getpid()}.{id(reason) & 0xFFFF:x}"
        try:
            os.replace(path, dest)
        except OSError as e2:
            log.error("could not quarantine corrupt settings (%s): %s", reason, e2)
            raise ValueError(f"settings.json is corrupt: {reason}") from e2
    log.error("quarantined corrupt settings.json → %s (%s)", dest, reason)
    return dest


def _atomic_write(data_dir: str, cur: dict) -> None:
    with _SETTINGS_LOCK:
        os.makedirs(data_dir, exist_ok=True)
        path = settings_path(data_dir)
        fd, tmp = tempfile.mkstemp(prefix=f".{SETTINGS_NAME}.", suffix=".tmp", dir=data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def normalize_backend(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    v = str(value).strip().lower()
    if v in ("qbittorrent", "qbit", "qb"):
        return "qbittorrent"
    if v in ("libtorrent", "lt", "embedded"):
        return "libtorrent"
    return None


def apply_patch(cur: dict, patch: dict) -> dict:
    """Validate ``patch`` and return a new settings dict. Does not write disk."""
    out = dict(cur)

    if "torrent_backend" in patch:
        be = normalize_backend(patch.get("torrent_backend"))
        if not be:
            raise ValueError("torrent_backend must be libtorrent or qbittorrent")
        out["torrent_backend"] = be

    if "qbit_category" in patch:
        cat = patch.get("qbit_category")
        if not isinstance(cat, str) or not cat.strip():
            raise ValueError("qbit_category must be a non-empty string")
        out["qbit_category"] = cat.strip()

    if "qbit_url" in patch:
        url = patch.get("qbit_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("qbit_url must be a non-empty string")
        out["qbit_url"] = _clean_qbit_url(url)
    elif isinstance(out.get("qbit_url"), str) and out["qbit_url"].strip():
        # Scrub legacy credential-bearing URLs when any other setting is saved.
        try:
            out["qbit_url"] = _clean_qbit_url(out["qbit_url"])
        except ValueError:
            out.pop("qbit_url", None)

    if "qbit_user" in patch:
        user = patch.get("qbit_user")
        if not isinstance(user, str) or not user.strip():
            raise ValueError("qbit_user must be a non-empty string")
        out["qbit_user"] = user.strip()

    # Validate a transient password if provided, but never persist credentials.
    if "qbit_pass" in patch:
        pw = patch.get("qbit_pass")
        if pw is not None and not isinstance(pw, str):
            raise ValueError("qbit_pass must be a string")
    # Strip any legacy qbit_pass even when another setting is being saved.
    out.pop("qbit_pass", None)

    return out


def save_settings(data_dir: str, patch: dict) -> dict:
    with _SETTINGS_LOCK:
        # load_settings raises on corrupt JSON — refuse to overwrite a bad file.
        cur = apply_patch(load_settings(data_dir), patch)
        _atomic_write(data_dir, cur)
        return cur


def resolve_from(cur: dict, key: str, env_value: str | None, default: str = "") -> str:
    """Resolve one string setting from an in-memory settings dict."""
    def text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    env_text = text(env_value)
    if key == "torrent_backend":
        chosen = normalize_backend(cur.get("torrent_backend")) or normalize_backend(env_value) or "libtorrent"
        # Match resolve_backend: qBit-only images must not revive a persisted libtorrent.
        if chosen == "libtorrent" and not _libtorrent_available():
            return "qbittorrent"
        return chosen
    if key == "qbit_category":
        saved = text(cur.get("qbit_category"))
        if saved:
            return saved
        if isinstance(env_value, str) and env_value.strip():
            return env_value.strip()
        return ANNA_FOLDER
    if key == "qbit_url":
        raw = text(cur.get("qbit_url")) or env_text or DEFAULT_QBIT_URL
        try:
            return apply_patch({}, {"qbit_url": raw})["qbit_url"]
        except ValueError:
            return DEFAULT_QBIT_URL
    if key == "qbit_user":
        return text(cur.get("qbit_user")) or env_text or "admin"
    if key == "qbit_pass":
        return env_text
    return default


def _libtorrent_available() -> bool:
    try:
        import libtorrent  # noqa: F401

        return True
    except ImportError:
        return False


def resolve_backend(data_dir: str, env_value: str | None) -> str:
    return resolve_from(load_settings(data_dir), "torrent_backend", env_value)


def resolve_qbit_category(data_dir: str, env_value: str | None) -> str:
    """settings.json > env (if set/non-empty) > ANNA_FOLDER."""
    return resolve_from(load_settings(data_dir), "qbit_category", env_value)


def resolve_qbit_url(data_dir: str, env_value: str) -> str:
    return resolve_from(load_settings(data_dir), "qbit_url", env_value)


def resolve_qbit_user(data_dir: str, env_value: str) -> str:
    return resolve_from(load_settings(data_dir), "qbit_user", env_value)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        assert resolve_qbit_category(td, None) == ANNA_FOLDER
        assert resolve_backend(td, None) == "libtorrent"
        assert resolve_backend(td, "qbittorrent") == "qbittorrent"
        save_settings(td, {"torrent_backend": "qbittorrent", "qbit_category": "From File"})
        assert resolve_backend(td, "libtorrent") == "qbittorrent"
        assert resolve_qbit_category(td, "From Env") == "From File"
        try:
            save_settings(td, {"torrent_backend": "nope"})
            raise AssertionError("bad backend should fail")
        except ValueError:
            pass
        try:
            apply_patch({}, {"qbit_url": "ftp://x"})
            raise AssertionError("bad url should fail")
        except ValueError:
            pass
    print("ok: settings resolve/save checks passed")
