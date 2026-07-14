"""Operating-system and product path helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent_config_bridge.models import Platform, Product

__all__ = [
    "UnsupportedPlatformError",
    "current_platform",
    "default_config_home",
    "resolve_platform",
]


class UnsupportedPlatformError(RuntimeError):
    """Raised when bridge execution is attempted on an unsupported host."""


def current_platform() -> Platform:
    """Return the supported platform for the current Python runtime.

    Raises:
        UnsupportedPlatformError: If the runtime is neither Linux nor Windows.
    """
    platform_name = sys.platform.casefold()
    if platform_name.startswith("linux"):
        return Platform.LINUX
    if platform_name.startswith(("win", "cygwin", "msys")):
        return Platform.WINDOWS
    raise UnsupportedPlatformError(f"unsupported host platform: {sys.platform}")


def resolve_platform(platform: Platform) -> Platform:
    """Resolve ``auto`` to the host platform and preserve explicit values."""
    if platform is Platform.AUTO:
        return current_platform()
    return platform


def default_config_home(product: Product, user_home: Path) -> Path:
    """Return the absolute default configuration home for a product."""
    directory = ".codex" if product is Product.CODEX else ".claude"
    return Path(os.path.abspath(user_home / directory))
