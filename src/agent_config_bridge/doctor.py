"""Read-only environment diagnostics for configured bridge targets."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_config_bridge.catalog import CatalogInventory
from agent_config_bridge.models import BridgeConfig, Component, Platform, Product, Surface
from agent_config_bridge.path_safety import path_comparison_key
from agent_config_bridge.planner import Disposition, SyncPlan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.state import find_orphaned_target_states

__all__ = ["CheckLevel", "DoctorCheck", "run_doctor"]


class CheckLevel(StrEnum):
    """Diagnostic severity."""

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic result."""

    level: CheckLevel
    code: str
    message: str
    target: str | None = None


def run_doctor(config: BridgeConfig, inventory: CatalogInventory, plan: SyncPlan) -> tuple[DoctorCheck, ...]:
    """Inspect target health without changing files or product state."""

    checks: list[DoctorCheck] = [
        DoctorCheck(
            CheckLevel.OK,
            "catalog.valid",
            (
                f"catalog contains {len(inventory.skills)} skills, {len(inventory.plugins)} plugins, "
                f"and {len(inventory.hooks)} hook bundles"
            ),
        )
    ]

    conflict_count = sum(action.disposition is Disposition.CONFLICT for action in plan.actions)
    checks.append(
        DoctorCheck(
            CheckLevel.ERROR if conflict_count else CheckLevel.OK,
            "plan.conflicts",
            f"plan contains {conflict_count} conflicts",
        )
    )

    for target_name in find_orphaned_target_states(config):
        checks.append(
            DoctorCheck(
                CheckLevel.ERROR,
                "state.orphaned-target",
                (
                    "ownership state has no enabled matching target; apply/register are blocked until the identity "
                    "is restored and reconciled with components=[]"
                ),
                target_name,
            )
        )

    direct_skill_count = len(inventory.skills)
    if direct_skill_count > 90:
        checks.append(
            DoctorCheck(
                CheckLevel.WARNING,
                "skills.large-catalog",
                (
                    f"{direct_skill_count} direct skills may approach product discovery/context limits; "
                    "prefer repo-scoped skills or plugins for narrower activation"
                ),
            )
        )

    host_platform = current_platform()
    current_codex_home = os.environ.get("CODEX_HOME")
    for target in config.targets:
        if not target.enabled:
            checks.append(DoctorCheck(CheckLevel.INFO, "target.disabled", "target is disabled", target.name))
            continue

        checks.append(_home_check(target.name, target.user_home))
        checks.append(_config_home_check(target.name, target.config_home))

        same_platform = target.platform is host_platform
        if not same_platform:
            checks.append(
                DoctorCheck(
                    CheckLevel.INFO,
                    "target.platform-mismatch",
                    (
                        f"target platform is {target.platform.value}, current process is {host_platform.value}; "
                        "skipped executable and product-setting probes"
                    ),
                    target.name,
                )
            )
        else:
            executable = "codex" if target.product is Product.CODEX else "claude"
            resolved = shutil.which(executable)
            checks.append(
                DoctorCheck(
                    CheckLevel.OK if resolved else CheckLevel.WARNING,
                    "target.executable",
                    f"{executable} executable: {resolved or 'not found on this process PATH'}",
                    target.name,
                )
            )

        if same_platform and target.product is Product.CODEX and current_codex_home:
            expected_path = target.config_home.resolve()
            actual_path = Path(current_codex_home).expanduser().resolve()
            windows = target.platform is Platform.WINDOWS
            if path_comparison_key(actual_path, windows=windows) != path_comparison_key(
                expected_path,
                windows=windows,
            ):
                checks.append(
                    DoctorCheck(
                        CheckLevel.WARNING,
                        "codex.home-mismatch",
                        f"process CODEX_HOME is {actual_path}, target config_home is {expected_path}",
                        target.name,
                    )
                )

        if same_platform and Component.HOOKS in target.components:
            checks.extend(_hook_feature_checks(target.name, target.product, target.config_home))

        if (
            target.product is Product.CLAUDE_CODE
            and Surface.DESKTOP in target.surfaces
            and (Component.PLUGINS in target.components or Component.HOOKS in target.components)
        ):
            checks.append(
                DoctorCheck(
                    CheckLevel.INFO,
                    "claude.desktop-session-limit",
                    (
                        "Claude Code Desktop plugins require Local or SSH sessions; "
                        "Remote (cloud) and WSL sessions do not load them"
                    ),
                    target.name,
                )
            )

    return tuple(checks)


def _home_check(target: str, path: Path) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck(CheckLevel.ERROR, "target.home", f"user home is not a directory: {path}", target)
    if not os.access(path, os.R_OK):
        return DoctorCheck(CheckLevel.ERROR, "target.home", f"user home is not readable: {path}", target)
    if not os.access(path, os.W_OK):
        return DoctorCheck(CheckLevel.WARNING, "target.home", f"user home is not writable: {path}", target)
    return DoctorCheck(CheckLevel.OK, "target.home", f"user home is readable and writable: {path}", target)


def _config_home_check(target: str, path: Path) -> DoctorCheck:
    if path.exists() or path.is_symlink():
        if not path.is_dir():
            return DoctorCheck(
                CheckLevel.ERROR,
                "target.config-home",
                f"config home is not a directory: {path}",
                target,
            )
        if not os.access(path, os.R_OK):
            return DoctorCheck(
                CheckLevel.ERROR,
                "target.config-home",
                f"config home is not readable: {path}",
                target,
            )
        if not os.access(path, os.W_OK):
            return DoctorCheck(
                CheckLevel.WARNING,
                "target.config-home",
                f"config home is not writable: {path}",
                target,
            )
        return DoctorCheck(
            CheckLevel.OK,
            "target.config-home",
            f"config home is readable and writable: {path}",
            target,
        )

    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir():
        return DoctorCheck(
            CheckLevel.ERROR,
            "target.config-home",
            f"config home has no existing directory ancestor: {path}",
            target,
        )
    if not os.access(parent, os.W_OK | os.X_OK):
        return DoctorCheck(
            CheckLevel.WARNING,
            "target.config-home",
            f"config home does not exist and its nearest existing parent is not writable: {parent}",
            target,
        )
    return DoctorCheck(
        CheckLevel.OK,
        "target.config-home",
        f"config home does not exist but can be created below writable parent: {parent}",
        target,
    )


def _hook_feature_checks(target: str, product: Product, config_home: Path) -> tuple[DoctorCheck, ...]:
    if product is Product.CODEX:
        config_path = config_home / "config.toml"
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        except (OSError, tomllib.TOMLDecodeError):
            return (
                DoctorCheck(
                    CheckLevel.WARNING,
                    "hooks.config-unreadable",
                    f"could not parse Codex config: {config_path}",
                    target,
                ),
            )
        enabled = _nested(payload, "features", "hooks")
        if enabled is False:
            return (
                DoctorCheck(
                    CheckLevel.WARNING,
                    "hooks.disabled",
                    "Codex hooks feature is explicitly disabled",
                    target,
                ),
            )
        return (DoctorCheck(CheckLevel.OK, "hooks.enabled", "Codex hooks are not disabled", target),)

    settings_path = config_home / "settings.json"
    if not settings_path.is_file():
        return (DoctorCheck(CheckLevel.OK, "hooks.enabled", "Claude hooks are not disabled", target),)
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (
            DoctorCheck(
                CheckLevel.WARNING,
                "hooks.config-unreadable",
                f"could not parse Claude settings: {settings_path}",
                target,
            ),
        )
    if isinstance(payload, dict) and payload.get("disableAllHooks") is True:
        return (
            DoctorCheck(
                CheckLevel.WARNING,
                "hooks.disabled",
                "Claude Code disableAllHooks is true",
                target,
            ),
        )
    return (DoctorCheck(CheckLevel.OK, "hooks.enabled", "Claude hooks are not disabled", target),)


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
