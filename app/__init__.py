"""
Taser-Bot runtime package initializer.

This module is intentionally side‑effect free so that `import app` is fast and
works in CI. Avoid importing heavy submodules here; use local imports in call
sites instead. Convenience types are only imported when type checking.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

# Public version string (best effort).
try:  # pragma: no cover - environment dependent in CI
    __version__ = version("taser-bot-smart-runtime-v2")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

# Type-only imports to help editors/mypy without incurring runtime cost.
if TYPE_CHECKING:  # pragma: no cover
    from . import config as _config  # noqa: F401
    from . import db as _db  # noqa: F401
    from . import messaging as _messaging  # noqa: F401
    from . import money as _money  # noqa: F401

__all__: list[str] = ["__version__"]
