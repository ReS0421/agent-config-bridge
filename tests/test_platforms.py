"""Tests for platform and product path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_config_bridge.models import Platform, Product
from agent_config_bridge.platforms import (
    UnsupportedPlatformError,
    current_platform,
    default_config_home,
    resolve_platform,
)


@pytest.mark.parametrize(
    ("sys_platform", "expected"),
    [
        ("linux", Platform.LINUX),
        ("linux2", Platform.LINUX),
        ("win32", Platform.WINDOWS),
        ("cygwin", Platform.WINDOWS),
        ("msys", Platform.WINDOWS),
    ],
)
def test_current_platform_recognizes_supported_systems(
    monkeypatch: pytest.MonkeyPatch,
    sys_platform: str,
    expected: Platform,
) -> None:
    """Runtime platform names map onto the supported domain enum."""
    monkeypatch.setattr("agent_config_bridge.platforms.sys.platform", sys_platform)

    assert current_platform() is expected


def test_current_platform_rejects_unsupported_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsupported operating systems fail instead of masquerading as Linux."""
    monkeypatch.setattr("agent_config_bridge.platforms.sys.platform", "darwin")

    with pytest.raises(UnsupportedPlatformError, match="darwin"):
        current_platform()


def test_resolve_platform_only_replaces_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit target platforms remain stable across host systems."""
    monkeypatch.setattr("agent_config_bridge.platforms.sys.platform", "linux")

    assert resolve_platform(Platform.AUTO) is Platform.LINUX
    assert resolve_platform(Platform.LINUX) is Platform.LINUX
    assert resolve_platform(Platform.WINDOWS) is Platform.WINDOWS


@pytest.mark.parametrize(
    ("product", "directory"),
    [
        (Product.CODEX, ".codex"),
        (Product.CLAUDE_CODE, ".claude"),
    ],
)
def test_default_config_home_is_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    product: Product,
    directory: str,
) -> None:
    """Product defaults are returned as absolute paths."""
    monkeypatch.chdir(tmp_path)

    assert default_config_home(product, Path("profile")) == (tmp_path / "profile" / directory).absolute()
