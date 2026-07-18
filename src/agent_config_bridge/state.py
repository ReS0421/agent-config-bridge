"""Small, non-secret ownership records for bridge-managed product state."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_config_bridge.catalog import Artifact, CatalogInventory
from agent_config_bridge.models import BridgeConfig, Component, LinkMode, Platform, Product, TargetConfig
from agent_config_bridge.path_safety import path_comparison_key

__all__ = [
    "BridgeStateError",
    "RegistrationState",
    "SchedulerState",
    "SettingStateEntry",
    "SettingsState",
    "SkillStateEntry",
    "desired_plugin_names",
    "effective_link_mode",
    "find_orphaned_target_states",
    "read_registration_state",
    "read_scheduler_state",
    "read_settings_state",
    "read_skill_state",
    "read_registered_plugins",
    "registration_state_path",
    "registration_marketplace_source",
    "scheduler_state_path",
    "settings_state_path",
    "skill_root_for_target",
    "skill_state_path",
    "write_registered_plugins",
    "write_scheduler_state",
    "write_settings_state",
    "write_skill_state",
]

_ARTIFACT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BridgeStateError(RuntimeError):
    """Raised when bridge ownership state is malformed or cannot be updated."""


@dataclass(frozen=True, slots=True)
class SkillStateEntry:
    """One standalone Skill previously managed for a target."""

    name: str
    mode: LinkMode
    source_id: str
    link_target: str | None


@dataclass(frozen=True, slots=True)
class RegistrationState:
    """Plugins and marketplace source previously reconciled for a target."""

    plugins: tuple[str, ...]
    marketplace_source: str | None


@dataclass(frozen=True, slots=True)
class SettingStateEntry:
    """One product setting leaf previously managed for a target."""

    source_id: str
    path: tuple[str, ...]
    value_digest: str


@dataclass(frozen=True, slots=True)
class SettingsState:
    """Ownership needed to update or remove selected product-setting leaves."""

    file_created: bool
    created_containers: tuple[tuple[str, ...], ...]
    entries: tuple[SettingStateEntry, ...]


@dataclass(frozen=True, slots=True)
class SchedulerState:
    """One host heartbeat previously registered for a target."""

    backend: str
    heartbeat_digest: str
    config_path: str


def desired_plugin_names(
    target: TargetConfig,
    inventory: CatalogInventory,
    hooks_for_target: tuple[Artifact, ...],
) -> tuple[str, ...]:
    """Return the ordered plugin IDs selected for one target.

    ``hooks_for_target`` must be the governance-gated hook set for this
    target; the synthetic hook plugin is desired only when that set is
    non-empty, so a fully gated-out target deregisters it on reconcile.
    """

    names: list[str] = []
    if Component.PLUGINS in target.components:
        names.extend(artifact.name for artifact in inventory.plugins)
    if Component.HOOKS in target.components and hooks_for_target:
        names.append("agent-config-bridge-hooks")
    return tuple(names)


def registration_state_path(config: BridgeConfig, target: TargetConfig) -> Path:
    """Return the validated per-target plugin ownership record path."""

    return _state_path(config, target, "plugins.json")


def registration_marketplace_source(config: BridgeConfig, target: TargetConfig) -> str:
    """Return the normalized stable marketplace source recorded for a target."""

    return _path_identity(config.state_dir / "marketplace", target.platform)


def skill_state_path(config: BridgeConfig, target: TargetConfig) -> Path:
    """Return the validated per-target standalone Skill ownership record path."""

    return _state_path(config, target, "skills.json")


def settings_state_path(config: BridgeConfig, target: TargetConfig) -> Path:
    """Return the validated per-target setting ownership record path."""

    return _state_path(config, target, "settings.json")


def scheduler_state_path(config: BridgeConfig, target: TargetConfig) -> Path:
    """Return the validated per-target host-scheduler ownership record path."""

    return _state_path(config, target, "scheduler.json")


def read_skill_state(config: BridgeConfig, target: TargetConfig) -> tuple[SkillStateEntry, ...]:
    """Read standalone Skills previously reconciled through ``agentbridge apply``."""

    path = skill_state_path(config, target)
    if not path.exists():
        if path.is_symlink():
            raise BridgeStateError(f"skill ownership state is a broken symlink: {path}")
        return ()
    if not path.is_file() or path.is_symlink():
        raise BridgeStateError(f"skill ownership state is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["schema_version"] != 2 or payload["target"] != target.name:
            raise ValueError("schema version or target does not match")
        _require_identity(payload, _skill_identity(target), path)
        raw_skills = payload["skills"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise BridgeStateError(f"invalid skill ownership state: {path}") from exc
    if not isinstance(raw_skills, list):
        raise BridgeStateError(f"invalid skill list in ownership state: {path}")

    entries: list[SkillStateEntry] = []
    names: set[str] = set()
    for raw_entry in raw_skills:
        if not isinstance(raw_entry, dict):
            raise BridgeStateError(f"invalid skill entry in ownership state: {path}")
        try:
            name = raw_entry["name"]
            mode = LinkMode(raw_entry["mode"])
            source_id = raw_entry["source_id"]
            link_target = raw_entry.get("link_target")
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeStateError(f"invalid skill entry in ownership state: {path}") from exc
        if (
            not isinstance(name, str)
            or _ARTIFACT_NAME.fullmatch(name) is None
            or name in names
            or mode is LinkMode.AUTO
            or source_id != f"skills/{name}"
            or (link_target is not None and not isinstance(link_target, str))
            or (mode is LinkMode.SYMLINK and not link_target)
            or (mode is LinkMode.COPY and link_target is not None)
        ):
            raise BridgeStateError(f"invalid skill entry in ownership state: {path}")
        names.add(name)
        entries.append(
            SkillStateEntry(
                name=name,
                mode=mode,
                source_id=source_id,
                link_target=link_target,
            )
        )
    return tuple(entries)


def read_registered_plugins(config: BridgeConfig, target: TargetConfig) -> tuple[str, ...]:
    """Read plugin IDs previously registered through ``agentbridge register``."""

    return read_registration_state(config, target).plugins


def read_settings_state(config: BridgeConfig, target: TargetConfig) -> SettingsState:
    """Read product-setting leaves previously reconciled through apply."""

    path = settings_state_path(config, target)
    if not path.exists():
        if path.is_symlink():
            raise BridgeStateError(f"settings ownership state is a broken symlink: {path}")
        return SettingsState(file_created=False, created_containers=(), entries=())
    payload = _read_state_payload(path, "settings ownership state")
    try:
        if payload["schema_version"] != 1 or payload["target"] != target.name:
            raise ValueError("schema version or target does not match")
        _require_identity(payload, _settings_identity(target), path)
        file_created = payload["file_created"]
        raw_containers = payload["created_containers"]
        raw_entries = payload["settings"]
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeStateError(f"invalid settings ownership state: {path}") from exc
    if type(file_created) is not bool or not isinstance(raw_containers, list) or not isinstance(raw_entries, list):
        raise BridgeStateError(f"invalid settings ownership state: {path}")

    containers = tuple(_parse_setting_path(item, path) for item in raw_containers)
    if len(containers) != len(set(containers)):
        raise BridgeStateError(f"duplicate created containers in settings ownership state: {path}")

    entries: list[SettingStateEntry] = []
    seen_paths: set[tuple[str, ...]] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise BridgeStateError(f"invalid setting entry in ownership state: {path}")
        source_id = raw_entry.get("source_id")
        value_digest = raw_entry.get("value_digest")
        setting_path = _parse_setting_path(raw_entry.get("path"), path)
        if (
            not isinstance(source_id, str)
            or not source_id.startswith("settings/")
            or "\x00" in source_id
            or not isinstance(value_digest, str)
            or _SHA256.fullmatch(value_digest) is None
            or setting_path in seen_paths
        ):
            raise BridgeStateError(f"invalid setting entry in ownership state: {path}")
        seen_paths.add(setting_path)
        entries.append(SettingStateEntry(source_id=source_id, path=setting_path, value_digest=value_digest))
    return SettingsState(file_created=file_created, created_containers=containers, entries=tuple(entries))


def read_scheduler_state(config: BridgeConfig, target: TargetConfig) -> SchedulerState | None:
    """Read a host heartbeat registration previously reconciled through register."""

    path = scheduler_state_path(config, target)
    if not path.exists():
        if path.is_symlink():
            raise BridgeStateError(f"scheduler ownership state is a broken symlink: {path}")
        return None
    payload = _read_state_payload(path, "scheduler ownership state")
    try:
        if payload["schema_version"] != 1 or payload["target"] != target.name:
            raise ValueError("schema version or target does not match")
        _require_identity(payload, _scheduler_identity(target), path)
        backend = payload["backend"]
        heartbeat_digest = payload["heartbeat_digest"]
        config_path = payload["config_path"]
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeStateError(f"invalid scheduler ownership state: {path}") from exc
    if (
        backend not in {"cron", "task-scheduler"}
        or not isinstance(heartbeat_digest, str)
        or _SHA256.fullmatch(heartbeat_digest) is None
        or not isinstance(config_path, str)
        or not config_path
    ):
        raise BridgeStateError(f"invalid scheduler ownership state: {path}")
    return SchedulerState(backend=backend, heartbeat_digest=heartbeat_digest, config_path=config_path)


def read_registration_state(config: BridgeConfig, target: TargetConfig) -> RegistrationState:
    """Read registered Plugin IDs and their recorded marketplace source."""

    path = registration_state_path(config, target)
    if not path.exists():
        if path.is_symlink():
            raise BridgeStateError(f"plugin ownership state is a broken symlink: {path}")
        return RegistrationState(plugins=(), marketplace_source=None)
    if not path.is_file() or path.is_symlink():
        raise BridgeStateError(f"plugin ownership state is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["schema_version"] != 2 or payload["target"] != target.name:
            raise ValueError("schema version or target does not match")
        _require_identity(payload, _registration_identity(target), path)
        raw_plugins = payload["plugins"]
        marketplace_source = payload["marketplace_source"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise BridgeStateError(f"invalid plugin ownership state: {path}") from exc
    if not isinstance(raw_plugins, list) or not all(
        isinstance(name, str) and (_ARTIFACT_NAME.fullmatch(name) is not None or name == "agent-config-bridge-hooks")
        for name in raw_plugins
    ):
        raise BridgeStateError(f"invalid plugin list in ownership state: {path}")
    plugins = tuple(raw_plugins)
    if len(plugins) != len(set(plugins)):
        raise BridgeStateError(f"duplicate plugin IDs in ownership state: {path}")
    if not isinstance(marketplace_source, str) or not marketplace_source:
        raise BridgeStateError(f"invalid marketplace source in ownership state: {path}")
    return RegistrationState(plugins=plugins, marketplace_source=marketplace_source)


def write_registered_plugins(
    config: BridgeConfig,
    target: TargetConfig,
    plugins: tuple[str, ...],
) -> None:
    """Atomically record the plugin IDs reconciled for one target."""

    path = registration_state_path(config, target)
    if not plugins:
        _remove_state_document(path, "plugin ownership state")
        return
    payload: dict[str, Any] = {
        "schema_version": 2,
        "target": target.name,
        "identity": _registration_identity(target),
        "marketplace_source": registration_marketplace_source(config, target),
        "plugins": list(plugins),
    }
    _write_state_document(path, payload, "plugin ownership state")


def write_settings_state(
    config: BridgeConfig,
    target: TargetConfig,
    state: SettingsState,
) -> None:
    """Atomically record owned product-setting leaves for one target."""

    path = settings_state_path(config, target)
    if not state.entries:
        _remove_state_document(path, "settings ownership state")
        return
    payload: dict[str, Any] = {
        "schema_version": 1,
        "target": target.name,
        "identity": _settings_identity(target),
        "file_created": state.file_created,
        "created_containers": [list(item) for item in state.created_containers],
        "settings": [
            {
                "source_id": entry.source_id,
                "path": list(entry.path),
                "value_digest": entry.value_digest,
            }
            for entry in state.entries
        ],
    }
    _write_state_document(path, payload, "settings ownership state")


def write_scheduler_state(
    config: BridgeConfig,
    target: TargetConfig,
    state: SchedulerState | None,
) -> None:
    """Atomically record or clear one host scheduler registration."""

    path = scheduler_state_path(config, target)
    if state is None:
        _remove_state_document(path, "scheduler ownership state")
        return
    _write_state_document(
        path,
        {
            "schema_version": 1,
            "target": target.name,
            "identity": _scheduler_identity(target),
            "backend": state.backend,
            "heartbeat_digest": state.heartbeat_digest,
            "config_path": state.config_path,
        },
        "scheduler ownership state",
    )


def write_skill_state(
    config: BridgeConfig,
    target: TargetConfig,
    skills: tuple[Artifact, ...],
) -> None:
    """Atomically record the desired standalone Skills for one target.

    ``skills`` must be the governance-gated desired set for this target, not
    the raw catalog inventory — recording gated-out skills would make the next
    plan retract them again forever.
    """

    entries: list[dict[str, Any]] = []
    if Component.SKILLS in target.components:
        mode = effective_link_mode(config.link_mode, target.platform)
        for skill in skills:
            entries.append(
                {
                    "name": skill.name,
                    "mode": mode.value,
                    "source_id": f"skills/{skill.name}",
                    "link_target": str(skill.path.resolve()) if mode is LinkMode.SYMLINK else None,
                }
            )
    path = skill_state_path(config, target)
    if not entries:
        _remove_state_document(path, "skill ownership state")
        return
    _write_state_document(
        path,
        {
            "schema_version": 2,
            "target": target.name,
            "identity": _skill_identity(target),
            "skills": entries,
        },
        "skill ownership state",
    )


def effective_link_mode(mode: LinkMode, platform: Platform) -> LinkMode:
    """Resolve AUTO into the concrete installation mode used on one platform."""

    if mode is not LinkMode.AUTO:
        return mode
    return LinkMode.COPY if platform is Platform.WINDOWS else LinkMode.SYMLINK


def skill_root_for_target(target: TargetConfig) -> Path:
    """Return the standalone Skill discovery root one target deploys into."""

    if target.product is Product.CODEX:
        return target.user_home / ".agents" / "skills"
    return target.config_home / "skills"


def find_orphaned_target_states(config: BridgeConfig) -> tuple[str, ...]:
    """Return ownership-state target IDs that have no enabled matching target."""

    targets_root = config.state_dir / "targets"
    if not targets_root.exists():
        if targets_root.is_symlink():
            raise BridgeStateError(f"target ownership state root is a broken symlink: {targets_root}")
        return ()
    state_root = config.state_dir.resolve()
    try:
        resolved_targets_root = targets_root.resolve(strict=True)
        resolved_targets_root.relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BridgeStateError(f"target ownership state escapes configured state_dir: {targets_root}") from exc
    if targets_root.is_symlink() or not resolved_targets_root.is_dir():
        raise BridgeStateError(f"target ownership state root is not a regular directory: {targets_root}")

    enabled_names = {target.name for target in config.targets if target.enabled}
    orphaned: list[str] = []
    for candidate in sorted(resolved_targets_root.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink() or not candidate.is_dir() or _ARTIFACT_NAME.fullmatch(candidate.name) is None:
            raise BridgeStateError(f"invalid target ownership state directory: {candidate}")
        has_state = any(
            (candidate / filename).exists()
            for filename in ("skills.json", "plugins.json", "settings.json", "scheduler.json")
        )
        if has_state and candidate.name not in enabled_names:
            orphaned.append(candidate.name)
    return tuple(orphaned)


def _skill_identity(target: TargetConfig) -> dict[str, str]:
    skill_root = skill_root_for_target(target)
    return {
        "product": target.product.value,
        "platform": target.platform.value,
        "skill_root": _path_identity(skill_root, target.platform),
    }


def _registration_identity(target: TargetConfig) -> dict[str, str]:
    return {
        "product": target.product.value,
        "platform": target.platform.value,
        "config_home": _path_identity(target.config_home, target.platform),
    }


def _settings_identity(target: TargetConfig) -> dict[str, str]:
    filename = "config.toml" if target.product is Product.CODEX else "settings.json"
    return {
        "product": target.product.value,
        "platform": target.platform.value,
        "settings_file": _path_identity(target.config_home / filename, target.platform),
    }


def _scheduler_identity(target: TargetConfig) -> dict[str, str]:
    return {
        "product": target.product.value,
        "platform": target.platform.value,
        "user_home": _path_identity(target.user_home, target.platform),
        "config_home": _path_identity(target.config_home, target.platform),
    }


def _require_identity(payload: dict[str, Any], expected: dict[str, str], path: Path) -> None:
    if payload.get("identity") != expected:
        raise BridgeStateError(
            f"ownership state identity does not match the configured target: {path}; "
            "restore the previous target identity and reconcile with components=[] before changing it"
        )


def _path_identity(path: Path, platform: Platform) -> str:
    return path_comparison_key(path, windows=platform is Platform.WINDOWS)


def _parse_setting_path(value: object, state_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise BridgeStateError(f"invalid setting path in ownership state: {state_path}")
    return tuple(value)


def _read_state_payload(path: Path, description: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise BridgeStateError(f"{description} is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeStateError(f"invalid {description}: {path}") from exc
    if not isinstance(payload, dict):
        raise BridgeStateError(f"invalid {description}: {path}")
    return payload


def _write_state_document(path: Path, payload: dict[str, Any], description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_if_supported(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _chmod_if_supported(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BridgeStateError(f"could not update {description}: {path}: {exc}") from exc


def _remove_state_document(path: Path, description: str) -> None:
    if not path.exists():
        if path.is_symlink():
            raise BridgeStateError(f"cannot remove broken-symlink {description}: {path}")
        return
    if path.is_symlink() or not path.is_file():
        raise BridgeStateError(f"cannot remove non-regular {description}: {path}")
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError as exc:
        if path.exists():
            raise BridgeStateError(f"could not remove {description}: {path}: {exc}") from exc


def _state_path(config: BridgeConfig, target: TargetConfig, filename: str) -> Path:
    state_root = config.state_dir.resolve()
    candidate = config.state_dir / "targets" / target.name / filename
    try:
        candidate.parent.resolve().relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BridgeStateError(f"target state path escapes configured state_dir: {candidate}") from exc
    return candidate


def _chmod_if_supported(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Windows ACLs do not map cleanly to POSIX mode bits. The parent user's
        # ACL remains authoritative there.
        return
