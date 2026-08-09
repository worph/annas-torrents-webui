"""Shared frontend source loader for string-contract tests."""

from __future__ import annotations

import os


def frontend_bundle() -> str:
    root = os.path.join(os.path.dirname(__file__), "..", "frontend")
    parts: list[str] = []
    for name in ("index.html", "app.js", "app.css"):
        path = os.path.join(root, name)
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n".join(parts)


def frontend_js() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "frontend", "app.js")
    with open(path, encoding="utf-8") as f:
        return f.read()
