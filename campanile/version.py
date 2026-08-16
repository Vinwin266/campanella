"""Single source of truth for the package version.

The version is duplicated in ``pyproject.toml`` because build backends read it
statically; ``tests/test_packaging.py`` keeps the two in sync.
"""

from __future__ import annotations

__all__ = ["VERSION", "VERSION_INFO", "version_string"]

VERSION_INFO = (0, 4, 0)

VERSION = ".".join(str(part) for part in VERSION_INFO)


def version_string(prefix: str = "campanile") -> str:
    """Return a display version such as ``campanile 0.4.0``."""

    return f"{prefix} {VERSION}"
