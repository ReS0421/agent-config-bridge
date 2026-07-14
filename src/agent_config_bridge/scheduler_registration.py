"""Ownership-aware reconciliation around host scheduler backends."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from agent_config_bridge.catalog import CatalogInventory
from agent_config_bridge.models import BridgeConfig, Component, Platform, Product, TargetConfig
from agent_config_bridge.schedule_store import schedule_set_is_current
from agent_config_bridge.scheduler_backends import (
    HeartbeatSpec,
    LinuxCronBackend,
    ScheduleBackendKind,
    ScheduleDisposition,
    SchedulePlan,
    WindowsTaskSchedulerBackend,
)
from agent_config_bridge.schedules import discover_schedules
from agent_config_bridge.state import SchedulerState, read_scheduler_state, write_scheduler_state

__all__ = [
    "SchedulerRegistrationError",
    "SchedulerRegistrationPlan",
    "apply_scheduler_registration",
    "apply_scheduler_registrations",
    "build_scheduler_registration",
    "resolve_agentbridge_executable",
    "resolve_vendor_executable",
    "validate_vendor_executable",
]

SchedulerBackend = LinuxCronBackend | WindowsTaskSchedulerBackend


class SchedulerRegistrationError(RuntimeError):
    """Raised when a heartbeat cannot be reconciled within recorded ownership."""


@dataclass(frozen=True, slots=True)
class SchedulerRegistrationPlan:
    """One reviewed host heartbeat change with target ownership context."""

    target: TargetConfig
    spec: HeartbeatSpec
    backend: SchedulerBackend
    plan: SchedulePlan
    desired: bool
    previous_state: SchedulerState | None

    @property
    def has_conflict(self) -> bool:
        """Return whether this plan would adopt or remove unowned scheduler state."""

        return self.plan.disposition is ScheduleDisposition.CONFLICT

    @property
    def has_changes(self) -> bool:
        """Return whether register must mutate the scheduler or ownership state."""

        if self.plan.disposition in {
            ScheduleDisposition.CREATE,
            ScheduleDisposition.UPDATE,
            ScheduleDisposition.REMOVE,
        }:
            return True
        return not self.desired and self.previous_state is not None


def resolve_agentbridge_executable() -> Path:
    """Resolve the installed console entry point used by scheduler heartbeats."""

    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name.casefold() in {"agentbridge", "agentbridge.exe", "agentbridge.com"} and invoked.is_absolute():
        return _validate_agentbridge_executable(invoked)
    resolved = _find_executable_on_path("agentbridge", windows=os.name == "nt")
    if resolved is not None:
        return _validate_agentbridge_executable(resolved)
    raise SchedulerRegistrationError(
        "could not resolve an installed agentbridge executable; install the package console entry point first"
    )


def resolve_vendor_executable(target: TargetConfig) -> Path:
    """Resolve the target product CLI to a host-native absolute executable."""

    command = "codex" if target.product is Product.CODEX else "claude"
    if target.executable is not None:
        return validate_vendor_executable(target, target.executable)
    resolved = _find_executable_on_path(command, windows=target.platform is Platform.WINDOWS)
    if resolved is None:
        raise SchedulerRegistrationError(
            f"could not resolve the {command} executable for target {target.name!r} on an absolute, non-CWD PATH entry"
        )
    return validate_vendor_executable(target, resolved)


def validate_vendor_executable(target: TargetConfig, executable: Path) -> Path:
    """Validate and physically resolve an explicit product CLI executable."""

    command = "codex" if target.product is Product.CODEX else "claude"
    if not executable.is_absolute():
        raise SchedulerRegistrationError(f"{command} executable must be absolute: {executable}")
    if any(character in os.fspath(executable) for character in ("\x00", "\r", "\n")):
        raise SchedulerRegistrationError(f"{command} executable contains a control character: {executable}")
    stable_path = Path(os.path.abspath(executable))
    try:
        physical_path = stable_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchedulerRegistrationError(
            f"could not resolve the {command} executable for target {target.name!r}: {executable}"
        ) from exc
    if not physical_path.is_file():
        raise SchedulerRegistrationError(f"resolved {command} command is not a file: {physical_path}")
    if target.platform is Platform.LINUX and not os.access(stable_path, os.X_OK):
        raise SchedulerRegistrationError(f"resolved {command} command is not executable: {stable_path}")
    if target.platform is Platform.WINDOWS and stable_path.suffix.casefold() not in {".exe", ".com"}:
        raise SchedulerRegistrationError(
            f"scheduled Windows targets require a native .exe or .com {command} launcher, found: {stable_path}"
        )
    return stable_path


def _validate_agentbridge_executable(executable: Path) -> Path:
    """Validate the exact host launcher used for recurring heartbeat commands."""

    if not executable.is_absolute() or any(character in os.fspath(executable) for character in ("\x00", "\r", "\n")):
        raise SchedulerRegistrationError(f"agentbridge executable must be an absolute safe path: {executable}")
    stable_path = Path(os.path.abspath(executable))
    try:
        physical_path = stable_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchedulerRegistrationError(f"could not resolve the agentbridge executable: {executable}") from exc
    if not physical_path.is_file():
        raise SchedulerRegistrationError(f"resolved agentbridge command is not a file: {physical_path}")
    if os.name == "nt":
        if stable_path.suffix.casefold() not in {".exe", ".com"}:
            raise SchedulerRegistrationError(f"scheduled Windows heartbeat requires a native launcher: {stable_path}")
    elif not os.access(stable_path, os.X_OK):
        raise SchedulerRegistrationError(f"resolved agentbridge command is not executable: {stable_path}")
    return stable_path


def _find_executable_on_path(command: str, *, windows: bool) -> Path | None:
    """Search only explicit absolute PATH entries, never the current directory."""

    if not command or Path(command).name != command or any(character in command for character in ("\x00", "\r", "\n")):
        raise SchedulerRegistrationError(f"invalid executable command name: {command!r}")
    path_value = os.environ.get("PATH", os.defpath)
    try:
        current_directory = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchedulerRegistrationError("could not establish the current directory for safe PATH lookup") from exc
    names = (command,) if Path(command).suffix else ((f"{command}.COM", f"{command}.EXE") if windows else (command,))
    for raw_directory in path_value.split(os.pathsep):
        directory_text = raw_directory
        if windows and len(directory_text) >= 2 and directory_text[0] == directory_text[-1] == '"':
            directory_text = directory_text[1:-1]
        if not directory_text or any(character in directory_text for character in ("\x00", "\r", "\n")):
            continue
        directory = Path(directory_text)
        if not directory.is_absolute():
            continue
        stable_directory = Path(os.path.abspath(directory))
        try:
            physical_directory = stable_directory.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not physical_directory.is_dir() or _path_key(physical_directory, windows) == _path_key(
            current_directory, windows
        ):
            continue
        for name in names:
            candidate = Path(os.path.abspath(stable_directory / name))
            try:
                is_file = candidate.resolve(strict=True).is_file()
            except (OSError, RuntimeError):
                continue
            if not is_file:
                continue
            if windows:
                if candidate.suffix.casefold() not in {".exe", ".com"}:
                    continue
            elif not os.access(candidate, os.X_OK):
                continue
            return candidate
    return None


def _path_key(path: Path, windows: bool) -> str:
    value = os.path.normpath(os.fspath(path))
    return value.casefold() if windows else value


def build_scheduler_registration(
    config: BridgeConfig,
    inventory: CatalogInventory,
    target: TargetConfig,
    *,
    executable: Path | None = None,
    vendor_executable: Path | None = None,
    backend: SchedulerBackend | None = None,
) -> SchedulerRegistrationPlan:
    """Inspect one target's desired heartbeat and enforce state-backed ownership."""

    if config.config_path is None or not config.config_path.is_absolute():
        raise SchedulerRegistrationError("host scheduler registration requires an absolute loaded config path")
    if target.platform not in {Platform.LINUX, Platform.WINDOWS}:
        raise SchedulerRegistrationError(f"unsupported scheduler platform: {target.platform.value}")
    selected = Component.SCHEDULES in target.components and bool(inventory.schedules)
    executable_path = executable or (resolve_agentbridge_executable() if selected else Path(sys.executable).resolve())
    vendor_executable_path = vendor_executable or (
        resolve_vendor_executable(target) if selected else Path(sys.executable).resolve()
    )
    spec = HeartbeatSpec(
        agentbridge_executable=executable_path,
        config_path=config.config_path,
        target=target.name,
        vendor_executable=vendor_executable_path,
    )
    if selected and not schedule_set_is_current(config, discover_schedules(config), target):
        raise SchedulerRegistrationError(
            f"target {target.name!r} schedule snapshot is missing or stale; run agentbridge apply first"
        )
    selected_backend = backend or _backend_for(target.platform)
    raw_plan = selected_backend.plan(spec) if selected else selected_backend.plan_remove(spec)
    state = read_scheduler_state(config, target)
    guarded = _guard_ownership(raw_plan, state, target, selected)
    return SchedulerRegistrationPlan(
        target=target,
        spec=spec,
        backend=selected_backend,
        plan=guarded,
        desired=selected,
        previous_state=state,
    )


def apply_scheduler_registration(
    config: BridgeConfig,
    registration: SchedulerRegistrationPlan,
) -> bool:
    """Apply one reviewed heartbeat plan and update digest-only ownership state."""

    if registration.has_conflict:
        raise SchedulerRegistrationError(
            f"target {registration.target.name!r} scheduler conflict: {registration.plan.detail}"
        )
    changed: bool
    if registration.desired:
        changed = registration.backend.apply(registration.spec, registration.plan)
        converged = registration.backend.plan(registration.spec)
        if converged.disposition is not ScheduleDisposition.NOOP or converged.managed_digest is None:
            raise SchedulerRegistrationError("scheduler heartbeat did not converge after installation")
        write_scheduler_state(
            config,
            registration.target,
            SchedulerState(
                backend=_state_backend(converged.backend),
                heartbeat_digest=converged.managed_digest,
                config_path=str(registration.spec.config_path),
            ),
        )
    else:
        changed = registration.backend.remove(registration.spec, registration.plan)
        converged = registration.backend.plan_remove(registration.spec)
        if converged.disposition is not ScheduleDisposition.NOOP:
            raise SchedulerRegistrationError("scheduler heartbeat did not converge after removal")
        write_scheduler_state(config, registration.target, None)
    return changed


def apply_scheduler_registrations(
    config: BridgeConfig,
    inventory: CatalogInventory,
    registrations: tuple[SchedulerRegistrationPlan, ...],
) -> tuple[bool, ...]:
    """Apply a reviewed target batch without stale shared-crontab plans."""

    changed: list[bool] = []
    for reviewed in registrations:
        # Each earlier Linux target legitimately changes the same crontab
        # document. Replan immediately before this target's mutation while
        # preserving the exact reviewed target-scoped intent.
        current = build_scheduler_registration(
            config,
            inventory,
            reviewed.target,
            executable=reviewed.spec.agentbridge_executable,
            vendor_executable=reviewed.spec.vendor_executable,
            backend=reviewed.backend,
        )
        if _registration_intent_key(current) != _registration_intent_key(reviewed):
            raise SchedulerRegistrationError(
                f"target {current.target.name!r} host scheduler intent changed during registration"
            )
        changed.append(apply_scheduler_registration(config, current))
    return tuple(changed)


def _guard_ownership(
    plan: SchedulePlan,
    state: SchedulerState | None,
    target: TargetConfig,
    desired: bool,
) -> SchedulePlan:
    if plan.disposition is ScheduleDisposition.CONFLICT:
        return plan
    expected_backend = _state_backend(plan.backend)
    if state is not None and state.backend != expected_backend:
        return _conflict(plan, "scheduler ownership state records a different backend")

    if desired:
        if plan.disposition is ScheduleDisposition.CREATE:
            return plan
        if state is None:
            return _conflict(plan, "existing heartbeat has no matching target ownership state")
        if plan.managed_digest != state.heartbeat_digest:
            return _conflict(plan, "heartbeat marker digest does not match target ownership state")
        return plan

    if plan.disposition is ScheduleDisposition.NOOP:
        return plan
    if state is None:
        return _conflict(plan, "existing heartbeat is not recorded as bridge-owned for this target")
    if plan.managed_digest != state.heartbeat_digest:
        return _conflict(plan, "heartbeat marker digest does not match target ownership state")
    if target.name != plan.target:  # pragma: no cover - backend plan invariant
        return _conflict(plan, "scheduler plan target does not match configured target")
    return plan


def _conflict(plan: SchedulePlan, detail: str) -> SchedulePlan:
    return replace(plan, disposition=ScheduleDisposition.CONFLICT, detail=detail)


def _backend_for(platform: Platform) -> SchedulerBackend:
    if platform is Platform.LINUX:
        return LinuxCronBackend()
    if platform is Platform.WINDOWS:
        return WindowsTaskSchedulerBackend()
    raise SchedulerRegistrationError(f"unsupported scheduler platform: {platform.value}")


def _state_backend(backend: ScheduleBackendKind) -> str:
    if backend is ScheduleBackendKind.LINUX_CRONTAB:
        return "cron"
    return "task-scheduler"


def _registration_intent_key(registration: SchedulerRegistrationPlan) -> tuple[object, ...]:
    plan = registration.plan
    return (
        registration.target.name,
        registration.spec,
        plan.backend,
        plan.action,
        plan.disposition,
        plan.desired_digest,
        plan.managed_digest,
        plan.detail,
        registration.desired,
        registration.previous_state,
    )
