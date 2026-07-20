"""Read-only environment diagnostics for configured bridge targets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent_config_bridge.catalog import CatalogInventory
from agent_config_bridge.governance import (
    GovernanceError,
    GovernanceMode,
    GovernanceSeverity,
    run_governance,
)
from agent_config_bridge.models import BridgeConfig, Component, Platform, Product, Surface, TargetConfig
from agent_config_bridge.path_safety import is_directory_reparse_point, path_comparison_key
from agent_config_bridge.planner import Disposition, SyncPlan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.provenance import ROOT_MARKER_FILENAME, read_skill_root_marker
from agent_config_bridge.scheduler_registration import SchedulerRegistrationError, validate_vendor_executable
from agent_config_bridge.state import BridgeStateError, find_orphaned_target_states, skill_root_for_target

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
                f"{len(inventory.hooks)} hook bundles, {len(inventory.settings)} settings bundles, "
                f"{len(inventory.schedules)} schedules, and {len(inventory.instructions)} instruction bundles"
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

    checks.append(_governance_check(inventory))

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
        skill_root_redirect = _skill_discovery_root_redirect_check(target)
        if skill_root_redirect is not None:
            checks.append(skill_root_redirect)
        if Component.SKILLS in target.components:
            provenance = _skill_provenance_check(target, inventory)
            if provenance is not None:
                checks.append(provenance)

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
            checks.append(_executable_check(target))

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

        if Component.SETTINGS in target.components:
            filename = "config.toml" if target.product is Product.CODEX else "settings.json"
            checks.append(
                DoctorCheck(
                    CheckLevel.INFO,
                    "settings.owned-leaves",
                    f"settings are merged as owned leaves into {target.config_home / filename}",
                    target.name,
                )
            )

        if Component.SCHEDULES in target.components:
            checks.append(
                DoctorCheck(
                    CheckLevel.INFO,
                    "schedules.host-managed",
                    (
                        "recurring runs use the host scheduler and product CLI; they do not appear in "
                        "the product-native Desktop scheduler"
                    ),
                    target.name,
                )
            )
            if same_platform:
                scheduler = "crontab" if target.platform is Platform.LINUX else "schtasks.exe"
                scheduler_path = shutil.which(scheduler)
                checks.append(
                    DoctorCheck(
                        CheckLevel.OK if scheduler_path else CheckLevel.WARNING,
                        "schedules.backend",
                        f"host scheduler executable: {scheduler_path or f'{scheduler} not found on PATH'}",
                        target.name,
                    )
                )

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


def _executable_check(target: TargetConfig) -> DoctorCheck:
    """Validate and version-probe the exact product launcher a target selects."""

    command = "codex" if target.product is Product.CODEX else "claude"
    explicit = target.executable is not None
    candidate = target.executable
    if candidate is None:
        discovered = shutil.which(command)
        if discovered is None:
            return DoctorCheck(
                CheckLevel.WARNING,
                "target.executable",
                f"{command} executable was not found on this process PATH; version probe skipped",
                target.name,
            )
        candidate = Path(discovered)

    try:
        selected = validate_vendor_executable(target, candidate)
    except SchedulerRegistrationError as exc:
        source = "configured executable" if explicit else "PATH-selected executable"
        return DoctorCheck(
            CheckLevel.ERROR if explicit else CheckLevel.WARNING,
            "target.executable",
            f"{source} is invalid: {exc}; version probe skipped",
            target.name,
        )

    source = "configured" if explicit else "PATH-selected"
    try:
        result = subprocess.run(
            (str(selected), "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return DoctorCheck(
            CheckLevel.ERROR if explicit else CheckLevel.WARNING,
            "target.executable",
            f"{command} executable ({source}): {selected}; version probe failed: {exc}",
            target.name,
        )

    raw_version = result.stdout.strip() or result.stderr.strip()
    version = "".join(character for character in " ".join(raw_version.split()) if character.isprintable())
    if len(version) > 512:
        version = f"{version[:511]}…"
    if result.returncode != 0 or not version:
        detail = f"exit {result.returncode}" if result.returncode else "no version output"
        return DoctorCheck(
            CheckLevel.ERROR if explicit else CheckLevel.WARNING,
            "target.executable",
            f"{command} executable ({source}): {selected}; version probe failed ({detail})",
            target.name,
        )
    return DoctorCheck(
        CheckLevel.OK,
        "target.executable",
        f"{command} executable ({source}): {selected}; version: {version}",
        target.name,
    )


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


def _governance_check(inventory: CatalogInventory) -> DoctorCheck:
    """Report the governance mode and whether the ledger currently gates runtime."""

    try:
        report = run_governance(inventory)
    except GovernanceError as exc:
        return DoctorCheck(CheckLevel.ERROR, "governance.mode", str(exc))
    errors = sum(finding.severity is GovernanceSeverity.ERROR for finding in report.findings)
    warnings = sum(finding.severity is GovernanceSeverity.WARNING for finding in report.findings)
    if report.mode is GovernanceMode.REQUIRED and errors:
        return DoctorCheck(
            CheckLevel.ERROR,
            "governance.mode",
            (
                f"governance mode is 'required' with {errors} errors; plan/apply are blocked until "
                "`agentbridge registry check` passes"
            ),
        )
    gating = "gates skill deployment" if report.mode is GovernanceMode.REQUIRED else "does not gate runtime selection"
    return DoctorCheck(
        CheckLevel.WARNING if errors else CheckLevel.OK,
        "governance.mode",
        f"governance mode {report.mode.value!r} ({gating}); {errors} errors, {warnings} warnings",
    )


def _skill_provenance_check(target: TargetConfig, inventory: CatalogInventory) -> DoctorCheck | None:
    """Report whether the deployed skill root advertises its bridge provenance.

    Mirrors ``write_skill_root_marker``'s own conditions exactly: with an empty
    skill catalog the marker is intentionally absent, so its absence must never
    warn (apply could not resolve such a warning).
    """

    root = skill_root_for_target(target)
    has_skills = bool(inventory.skills)
    if not root.is_dir():
        if not has_skills:
            return None
        return DoctorCheck(
            CheckLevel.INFO,
            "skills.provenance",
            f"skill root does not exist yet; apply writes the provenance marker: {root}",
            target.name,
        )
    try:
        payload = read_skill_root_marker(target)
    except BridgeStateError as exc:
        return DoctorCheck(
            CheckLevel.WARNING,
            "skills.provenance",
            f"provenance marker is invalid; rewrite it with apply: {exc}",
            target.name,
        )
    if payload is None:
        if not has_skills:
            return None
        return DoctorCheck(
            CheckLevel.WARNING,
            "skills.provenance",
            (
                f"skill root has no provenance marker, so observers cannot tell it is a managed "
                f"projection; run apply to write it: {root}"
            ),
            target.name,
        )
    if not has_skills:
        return DoctorCheck(
            CheckLevel.WARNING,
            "skills.provenance",
            f"provenance marker is stale (catalog has no skills); apply removes it: {root / ROOT_MARKER_FILENAME}",
            target.name,
        )
    if payload["target"] != target.name:
        return DoctorCheck(
            CheckLevel.WARNING,
            "skills.provenance",
            (
                f"provenance marker names target {payload['target']!r}, expected {target.name!r}; "
                "run apply to refresh it"
            ),
            target.name,
        )
    return DoctorCheck(
        CheckLevel.OK,
        "skills.provenance",
        f"skill root advertises bridge provenance: {root / ROOT_MARKER_FILENAME}",
        target.name,
    )


def _skill_discovery_root_redirect_check(target: TargetConfig) -> DoctorCheck | None:
    """Report a discovery root redirected by an existing symlink or junction.

    Only the declared root and its parent chain are inspected. Standalone Skill
    symlinks below the root are expected bridge/user content and must not trigger
    this diagnostic.
    """

    root = skill_root_for_target(target)
    absolute_root = Path(os.path.abspath(root))
    redirected_components: list[Path] = []
    for candidate in reversed((absolute_root, *absolute_root.parents)):
        try:
            is_symlink = candidate.is_symlink()
            is_reparse_point = is_directory_reparse_point(candidate)
        except OSError:
            continue
        if is_symlink or is_reparse_point:
            redirected_components.append(candidate)

    if not redirected_components:
        return None

    try:
        physical_root = absolute_root.resolve(strict=False)
        destination = f"effective physical location {physical_root}"
    except (OSError, RuntimeError) as error:
        destination = f"an effective location that could not be resolved ({error})"
    redirects = ", ".join(str(path) for path in redirected_components)
    return DoctorCheck(
        CheckLevel.WARNING,
        "skills.discovery-root-redirected",
        (
            f"Skill discovery root {absolute_root} is redirected through {redirects} to {destination}; "
            "configuration validation and planning use physical path identity, so confirm this redirection "
            "is intentional"
        ),
        target.name,
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
