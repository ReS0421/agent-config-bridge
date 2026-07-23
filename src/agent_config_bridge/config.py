"""Strict TOML configuration loading for the agent configuration bridge."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import TypeVar, cast

from agent_config_bridge.models import (
    BridgeConfig,
    Component,
    LinkMode,
    Platform,
    Product,
    RetentionConfig,
    Surface,
    TargetConfig,
)
from agent_config_bridge.path_safety import paths_overlap
from agent_config_bridge.platforms import current_platform, default_config_home, resolve_platform

__all__ = ["ConfigError", "load_config"]

_SCHEMA_VERSION = 1
_TOP_LEVEL_KEYS = frozenset({"schema_version", "bridge", "targets"})
_BRIDGE_KEYS = frozenset({"catalog", "state_dir", "link_mode", "components", "retention"})
_RETENTION_KEYS = frozenset({"marketplace_builds", "skill_backups"})
_TARGET_KEYS = frozenset(
    {
        "name",
        "product",
        "platform",
        "user_home",
        "config_home",
        "executable",
        "components",
        "surfaces",
        "enabled",
    }
)
_WINDOWS_ENV_REFERENCE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_TARGET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

_EnumT = TypeVar("_EnumT", bound=StrEnum)


class ConfigError(ValueError):
    """Raised when a bridge configuration cannot be loaded or validated."""


def load_config(path: str | os.PathLike[str]) -> BridgeConfig:
    """Load and validate a bridge TOML configuration.

    Relative catalog and state paths are resolved against the configuration
    file. Target homes are expanded and made absolute; a relative explicit
    ``config_home`` is interpreted beneath its target's ``user_home``.

    Args:
        path: Path to a TOML configuration file.

    Returns:
        A fully typed, immutable bridge configuration.

    Raises:
        ConfigError: If the file cannot be read or violates the schema.
    """
    try:
        config_path = _absolute_path(Path(_expand_environment(os.fspath(path))).expanduser())
    except (KeyError, RuntimeError) as error:
        raise ConfigError(f"could not expand configuration path: {error}") from error
    document = _read_toml(config_path)
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, "configuration")

    schema_version = _parse_schema_version(_required(document, "schema_version", "configuration"))
    bridge = _as_table(_required(document, "bridge", "configuration"), "bridge")
    _reject_unknown_keys(bridge, _BRIDGE_KEYS, "bridge")

    config_directory = config_path.parent
    catalog = _parse_path(
        _required(bridge, "catalog", "bridge"),
        "bridge.catalog",
        base=config_directory,
    )
    state_dir = _parse_path(
        _required(bridge, "state_dir", "bridge"),
        "bridge.state_dir",
        base=config_directory,
    )
    link_mode = _parse_enum(
        _required(bridge, "link_mode", "bridge"),
        LinkMode,
        "bridge.link_mode",
    )
    components = _parse_enum_set(
        _required(bridge, "components", "bridge"),
        Component,
        "bridge.components",
    )
    retention = _parse_retention(bridge["retention"]) if "retention" in bridge else RetentionConfig()

    _require_directory(catalog, "bridge.catalog")
    _reject_existing_non_directory(state_dir, "bridge.state_dir")

    targets_value = _required(document, "targets", "configuration")
    targets = _parse_targets(
        targets_value,
        config_directory=config_directory,
        inherited_components=components,
    )
    _validate_schedule_surfaces(targets)
    _validate_target_destinations(targets)
    _validate_bridge_path_isolation(catalog, state_dir, targets)

    return BridgeConfig(
        schema_version=schema_version,
        catalog=catalog,
        state_dir=state_dir,
        link_mode=link_mode,
        components=components,
        targets=targets,
        config_path=config_path,
        retention=retention,
    )


def _read_toml(path: Path) -> Mapping[str, object]:
    if not path.exists():
        raise ConfigError(f"configuration file does not exist: {path}")
    if not path.is_file():
        raise ConfigError(f"configuration path is not a file: {path}")

    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"could not read configuration {path}: {error}") from error
    return cast(dict[str, object], document)


def _parse_schema_version(value: object) -> int:
    if type(value) is not int:
        raise ConfigError("configuration.schema_version must be integer 1")
    if value != _SCHEMA_VERSION:
        raise ConfigError(f"unsupported configuration.schema_version {value}; expected {_SCHEMA_VERSION}")
    return value


def _parse_targets(
    value: object,
    *,
    config_directory: Path,
    inherited_components: frozenset[Component],
) -> tuple[TargetConfig, ...]:
    if not isinstance(value, list):
        raise ConfigError("configuration.targets must be an array of tables")
    if not value:
        raise ConfigError("configuration.targets must contain at least one target")

    targets: list[TargetConfig] = []
    names: set[str] = set()
    for index, target_value in enumerate(cast(list[object], value)):
        context = f"targets[{index}]"
        target = _parse_target(
            target_value,
            context=context,
            config_directory=config_directory,
            inherited_components=inherited_components,
        )
        if target.name in names:
            raise ConfigError(f"duplicate target name: {target.name!r}")
        names.add(target.name)
        targets.append(target)
    return tuple(targets)


def _parse_target(
    value: object,
    *,
    context: str,
    config_directory: Path,
    inherited_components: frozenset[Component],
) -> TargetConfig:
    target = _as_table(value, context)
    _reject_unknown_keys(target, _TARGET_KEYS, context)

    name = _parse_nonempty_string(_required(target, "name", context), f"{context}.name")
    if _TARGET_NAME.fullmatch(name) is None:
        raise ConfigError(f"{context}.name must be a lowercase kebab-case identifier")
    if name in _WINDOWS_DEVICE_NAMES:
        raise ConfigError(f"{context}.name is reserved on Windows")
    product = _parse_enum(
        _required(target, "product", context),
        Product,
        f"{context}.product",
    )
    platform = resolve_platform(
        _parse_enum(
            _required(target, "platform", context),
            Platform,
            f"{context}.platform",
        )
    )
    user_home = _parse_path(
        _required(target, "user_home", context),
        f"{context}.user_home",
        base=config_directory,
    )

    if "config_home" in target:
        config_home = _parse_path(
            target["config_home"],
            f"{context}.config_home",
            base=user_home,
        )
    else:
        config_home = default_config_home(product, user_home)

    executable = (
        _parse_path(target["executable"], f"{context}.executable", base=user_home) if "executable" in target else None
    )

    if "components" in target:
        components = _parse_enum_set(
            target["components"],
            Component,
            f"{context}.components",
        )
    else:
        components = inherited_components

    surfaces = _parse_enum_set(
        _required(target, "surfaces", context),
        Surface,
        f"{context}.surfaces",
    )
    if not surfaces:
        raise ConfigError(f"{context}.surfaces must contain at least one surface")
    enabled = _parse_bool(_required(target, "enabled", context), f"{context}.enabled")

    if enabled:
        _require_directory(user_home, f"{context}.user_home")
        _reject_existing_non_directory(config_home, f"{context}.config_home")

    return TargetConfig(
        name=name,
        product=product,
        platform=platform,
        user_home=user_home,
        config_home=config_home,
        components=components,
        surfaces=surfaces,
        enabled=enabled,
        executable=executable,
    )


def _validate_target_destinations(targets: tuple[TargetConfig, ...]) -> None:
    config_homes: list[tuple[TargetConfig, Path]] = []
    skill_destinations: list[tuple[TargetConfig, Path]] = []
    for target in targets:
        if not target.enabled:
            continue

        for previous_target, previous_home in config_homes:
            if _paths_overlap(
                target.config_home,
                target.platform,
                previous_home,
                previous_target.platform,
                left_context=f"target {target.name!r} config_home",
                right_context=f"target {previous_target.name!r} config_home",
            ):
                product = f"{target.product.value} " if target.product is previous_target.product else ""
                raise ConfigError(
                    f"targets {previous_target.name!r} and {target.name!r} manage the same {product}"
                    f"config_home or overlapping config_home paths: {previous_home} <-> {target.config_home}"
                )
        config_homes.append((target, target.config_home))

        skill_root = _skill_root(target)
        for previous_target, previous_root in skill_destinations:
            if _paths_overlap(
                skill_root,
                target.platform,
                previous_root,
                previous_target.platform,
                left_context=f"target {target.name!r} Skill root",
                right_context=f"target {previous_target.name!r} Skill root",
            ) and (Component.SKILLS in target.components and Component.SKILLS in previous_target.components):
                raise ConfigError(
                    f"targets {previous_target.name!r} and {target.name!r} "
                    f"both select skills for the same skill destination or overlapping Skill roots: "
                    f"{previous_root} <-> {skill_root}"
                )
        skill_destinations.append((target, skill_root))

    for home_target, config_home in config_homes:
        for skill_target, skill_root in skill_destinations:
            if home_target.name == skill_target.name and home_target.product is Product.CLAUDE_CODE:
                continue
            if _paths_overlap(
                config_home,
                home_target.platform,
                skill_root,
                skill_target.platform,
                left_context=f"target {home_target.name!r} config_home",
                right_context=f"target {skill_target.name!r} Skill root",
            ):
                raise ConfigError(
                    f"target {home_target.name!r} config_home overlaps target {skill_target.name!r} "
                    f"Skill root: {config_home} <-> {skill_root}"
                )


def _validate_schedule_surfaces(targets: tuple[TargetConfig, ...]) -> None:
    """Require the CLI surface for host-managed recurring executions."""

    for target in targets:
        if target.enabled and Component.SCHEDULES in target.components and Surface.CLI not in target.surfaces:
            raise ConfigError(
                f"target {target.name!r} selects schedules but has no cli surface; "
                "host-managed schedules invoke the product CLI"
            )


def _validate_bridge_path_isolation(
    catalog: Path,
    state_dir: Path,
    targets: tuple[TargetConfig, ...],
) -> None:
    host_platform = current_platform()
    _reject_path_overlap(
        state_dir,
        host_platform,
        "bridge.state_dir",
        catalog,
        host_platform,
        "bridge.catalog",
    )

    for target in targets:
        if not target.enabled:
            continue
        skill_root = _skill_root(target)
        target_context = f"target {target.name!r}"
        for bridge_path, bridge_platform, bridge_context, target_path, target_path_context in (
            (
                state_dir,
                host_platform,
                "bridge.state_dir",
                target.config_home,
                f"{target_context} config_home",
            ),
            (
                state_dir,
                host_platform,
                "bridge.state_dir",
                skill_root,
                f"{target_context} Skill root",
            ),
            (
                catalog,
                host_platform,
                "bridge.catalog",
                target.config_home,
                f"{target_context} config_home",
            ),
            (
                catalog,
                host_platform,
                "bridge.catalog",
                skill_root,
                f"{target_context} Skill root",
            ),
        ):
            _reject_path_overlap(
                bridge_path,
                bridge_platform,
                bridge_context,
                target_path,
                target.platform,
                target_path_context,
            )


def _skill_root(target: TargetConfig) -> Path:
    if target.product is Product.CODEX:
        return target.user_home / ".agents" / "skills"
    return target.config_home / "skills"


def _reject_path_overlap(
    left: Path,
    left_platform: Platform,
    left_context: str,
    right: Path,
    right_platform: Platform,
    right_context: str,
) -> None:
    if _paths_overlap(
        left,
        left_platform,
        right,
        right_platform,
        left_context=left_context,
        right_context=right_context,
    ):
        raise ConfigError(f"{left_context} must not overlap {right_context}: {left} <-> {right}")


def _paths_overlap(
    left: Path,
    left_platform: Platform,
    right: Path,
    right_platform: Platform,
    *,
    left_context: str,
    right_context: str,
) -> bool:
    windows = left_platform is Platform.WINDOWS or right_platform is Platform.WINDOWS
    try:
        return paths_overlap(left, right, windows=windows)
    except (OSError, RuntimeError) as error:
        raise ConfigError(
            f"could not physically resolve {left_context} and {right_context} for overlap validation"
        ) from error


def _parse_enum(value: object, enum_type: type[_EnumT], context: str) -> _EnumT:
    raw_value = _parse_nonempty_string(value, context)
    try:
        return enum_type(raw_value)
    except ValueError as error:
        allowed = ", ".join(member.value for member in enum_type)
        raise ConfigError(f"{context} has unknown value {raw_value!r}; expected one of: {allowed}") from error


def _parse_enum_set(
    value: object,
    enum_type: type[_EnumT],
    context: str,
) -> frozenset[_EnumT]:
    if not isinstance(value, list):
        raise ConfigError(f"{context} must be an array")

    members: list[_EnumT] = []
    raw_values: set[str] = set()
    for index, item in enumerate(cast(list[object], value)):
        item_context = f"{context}[{index}]"
        raw_value = _parse_nonempty_string(item, item_context)
        if raw_value in raw_values:
            raise ConfigError(f"{context} contains duplicate value {raw_value!r}")
        raw_values.add(raw_value)
        members.append(_parse_enum(raw_value, enum_type, item_context))
    return frozenset(members)


def _parse_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{context} must be a boolean")
    return value


def _parse_retention(value: object) -> RetentionConfig:
    table = _as_table(value, "bridge.retention")
    _reject_unknown_keys(table, _RETENTION_KEYS, "bridge.retention")
    defaults = RetentionConfig()
    return RetentionConfig(
        marketplace_builds=_parse_positive_integer(
            table.get("marketplace_builds", defaults.marketplace_builds),
            "bridge.retention.marketplace_builds",
        ),
        skill_backups=_parse_positive_integer(
            table.get("skill_backups", defaults.skill_backups),
            "bridge.retention.skill_backups",
        ),
    )


def _parse_positive_integer(value: object, context: str) -> int:
    if type(value) is not int or value < 1:
        raise ConfigError(f"{context} must be an integer greater than or equal to 1")
    return value


def _parse_path(value: object, context: str, *, base: Path) -> Path:
    raw_path = _parse_nonempty_string(value, context)
    if "\x00" in raw_path:
        raise ConfigError(f"{context} contains a null byte")

    expanded = _expand_environment(raw_path)
    try:
        parsed_path = Path(expanded).expanduser()
    except (KeyError, RuntimeError) as error:
        raise ConfigError(f"could not expand {context}: {error}") from error

    if not parsed_path.is_absolute():
        parsed_path = base / parsed_path
    return _absolute_path(parsed_path)


def _expand_windows_env_reference(match: re.Match[str]) -> str:
    """Expand a percent-style environment reference on every host OS."""
    name = match.group(1)
    return os.environ.get(name, match.group(0))


def _expand_environment(value: str) -> str:
    """Expand POSIX and percent-style environment variables on every host."""
    expanded = os.path.expandvars(value)
    return _WINDOWS_ENV_REFERENCE.sub(_expand_windows_env_reference, expanded)


def _absolute_path(path: Path) -> Path:
    """Normalize a path and make it absolute without dereferencing symlinks."""
    return Path(os.path.abspath(path))


def _parse_nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value


def _required(table: Mapping[str, object], key: str, context: str) -> object:
    try:
        return table[key]
    except KeyError as error:
        raise ConfigError(f"{context}.{key} is required") from error


def _as_table(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a table")
    return cast(dict[str, object], value)


def _reject_unknown_keys(
    table: Mapping[str, object],
    allowed: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        formatted = ", ".join(repr(key) for key in unknown)
        raise ConfigError(f"{context} contains unknown keys: {formatted}")


def _require_directory(path: Path, context: str) -> None:
    if not path.exists():
        raise ConfigError(f"{context} directory does not exist: {path}")
    if not path.is_dir():
        raise ConfigError(f"{context} must be a directory: {path}")


def _reject_existing_non_directory(path: Path, context: str) -> None:
    if (path.exists() or path.is_symlink()) and not path.is_dir():
        raise ConfigError(f"{context} must be a directory: {path}")
