"""Typed domain models for an agent configuration bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "BridgeConfig",
    "Component",
    "LinkMode",
    "Platform",
    "Product",
    "Surface",
    "TargetConfig",
]


class Component(StrEnum):
    """A category of agent configuration artifact."""

    SKILLS = "skills"
    PLUGINS = "plugins"
    HOOKS = "hooks"
    SETTINGS = "settings"
    SCHEDULES = "schedules"
    INSTRUCTIONS = "instructions"


class Product(StrEnum):
    """An agent product supported by the bridge."""

    CODEX = "codex"
    CLAUDE_CODE = "claude-code"


class Platform(StrEnum):
    """A target operating system, or automatic host detection."""

    AUTO = "auto"
    LINUX = "linux"
    WINDOWS = "windows"


class LinkMode(StrEnum):
    """How rendered artifacts are installed into product homes."""

    AUTO = "auto"
    SYMLINK = "symlink"
    COPY = "copy"


class Surface(StrEnum):
    """A product surface that should consume shared artifacts."""

    CLI = "cli"
    DESKTOP = "desktop"


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Resolved configuration for one product installation target.

    ``platform`` is always an effective Linux or Windows value. The loader
    resolves a configured ``auto`` value before constructing this model.
    """

    name: str
    product: Product
    platform: Platform
    user_home: Path
    config_home: Path
    components: frozenset[Component]
    surfaces: frozenset[Surface]
    enabled: bool
    executable: Path | None = None


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Validated bridge configuration loaded from a TOML document."""

    schema_version: int
    catalog: Path
    state_dir: Path
    link_mode: LinkMode
    components: frozenset[Component]
    targets: tuple[TargetConfig, ...]
    config_path: Path | None = None
