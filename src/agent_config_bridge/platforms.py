"""Operating-system and product path helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path

from agent_config_bridge.models import Platform, Product, TargetConfig
from agent_config_bridge.path_safety import path_comparison_key

__all__ = [
    "UnsupportedPlatformError",
    "current_platform",
    "default_config_home",
    "product_home_environment",
    "product_home_environment_unsets",
    "resolve_platform",
    "scope_product_home_environment",
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


def product_home_environment(target: TargetConfig) -> tuple[tuple[str, str], ...]:
    """Return environment overrides needed to select a target product home.

    Claude Code's default profile metadata lives beside ``~/.claude`` rather
    than beneath it. Setting ``CLAUDE_CONFIG_DIR`` to that default directory
    would therefore select a different nested profile. Codex does not have
    that split layout, so its target home remains explicit for every command.
    """

    if target.product is Product.CODEX:
        return (("CODEX_HOME", str(target.config_home)),)

    windows = target.platform is Platform.WINDOWS
    default_home = default_config_home(target.product, target.user_home)
    if path_comparison_key(target.config_home, windows=windows) == path_comparison_key(
        default_home,
        windows=windows,
    ):
        return ()
    return (("CLAUDE_CONFIG_DIR", str(target.config_home)),)


def product_home_environment_unsets(target: TargetConfig) -> tuple[str, ...]:
    """Return inherited variables that must be absent for a target profile.

    Claude Code's default profile is selected by leaving ``CLAUDE_CONFIG_DIR``
    unset. Registration plans carry that removal explicitly so their JSON and
    copyable command previews cannot accidentally retain a caller override.
    """

    if target.product is not Product.CLAUDE_CODE:
        return ()
    windows = target.platform is Platform.WINDOWS
    default_home = default_config_home(target.product, target.user_home)
    if path_comparison_key(target.config_home, windows=windows) == path_comparison_key(
        default_home,
        windows=windows,
    ):
        return ("CLAUDE_CONFIG_DIR",)
    return ()


def scope_product_home_environment(environment: MutableMapping[str, str], target: TargetConfig) -> None:
    """Mutate a subprocess environment to select exactly one product profile.

    Removing an inherited Claude override is required for the default profile;
    merely omitting a new assignment would keep the caller's custom profile.
    """

    if target.product is Product.CLAUDE_CODE:
        environment.pop("CLAUDE_CONFIG_DIR", None)
    environment.update(product_home_environment(target))
