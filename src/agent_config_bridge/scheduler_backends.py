"""Host heartbeat schedulers with explicit ownership and drift detection.

The Linux backend edits only a marked block in the current user's crontab.
Cron necessarily passes its command field through ``/bin/sh``; every argument
is therefore POSIX-shell quoted and percent signs are escaped for cron's own
pre-shell parser.  The Windows backend keeps the executable and argument string
in separate Task Scheduler XML elements and invokes ``schtasks.exe`` with an
argument vector, never through a shell.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, cast

__all__ = [
    "HeartbeatSpec",
    "LinuxCronBackend",
    "ScheduleAction",
    "ScheduleBackendError",
    "ScheduleBackendKind",
    "ScheduleConflictError",
    "ScheduleDisposition",
    "SchedulePlan",
    "StaleSchedulePlanError",
    "WindowsTaskSchedulerBackend",
]

CommandRunner = Callable[[tuple[str, ...], str | None], subprocess.CompletedProcess[str]]

_TARGET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CRON_META = re.compile(
    r"^# agent-config-bridge-heartbeat schema=1 "
    r"target=(?P<target>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?) digest=(?P<digest>[0-9a-f]{64})$"
)
_WINDOWS_MARKER = re.compile(
    r"^agent-config-bridge-heartbeat schema=1 "
    r"target=(?P<target>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?) digest=(?P<digest>[0-9a-f]{64})$"
)
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_INTERVAL = "PT1M"
_TASK_EXECUTION_TIME_LIMIT = "PT0S"
_TASK_INSTANCES_POLICY = "Parallel"
_TASK_LOGON_TYPE = "InteractiveToken"
_TASK_RUN_LEVEL = "LeastPrivilege"
_TASK_PRINCIPAL_ID = "Author"
_TASK_START_BOUNDARY = "2020-01-01T00:00:00"
_TASK_ENABLED = "true"
_TASK_STOP_AT_DURATION_END = "false"
_TASK_DISALLOW_ON_BATTERIES = "false"
_TASK_STOP_ON_BATTERIES = "false"
_TASK_START_WHEN_AVAILABLE = "true"
# schtasks reports an absent task as ERROR_FILE_NOT_FOUND or, on some hosts,
# ERROR_PATH_NOT_FOUND; both HRESULTs mean the queried task does not exist.
_WINDOWS_ABSENT_TASK_HRESULTS = frozenset({0x80070002, 0x80070003})
_WINDOWS_SID = re.compile(r"^S-\d-(?:\d+-)+\d+$")
_MAX_TASK_XML_CHARACTERS = 1_000_000


class ScheduleBackendError(RuntimeError):
    """Raised when host scheduler state cannot be inspected or changed safely."""


class ScheduleConflictError(ScheduleBackendError):
    """Raised when scheduler state exists but bridge ownership is not intact."""


class StaleSchedulePlanError(ScheduleBackendError):
    """Raised when scheduler state changed after a plan was reviewed."""


class ScheduleBackendKind(StrEnum):
    """Supported host scheduling mechanisms."""

    LINUX_CRONTAB = "linux-user-crontab"
    WINDOWS_TASK_SCHEDULER = "windows-task-scheduler"


class ScheduleAction(StrEnum):
    """Requested heartbeat lifecycle action."""

    INSTALL = "install"
    REMOVE = "remove"


class ScheduleDisposition(StrEnum):
    """Result of read-only scheduler inspection."""

    CREATE = "create"
    UPDATE = "update"
    REMOVE = "remove"
    NOOP = "noop"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class HeartbeatSpec:
    """One once-per-minute ``agentbridge schedule tick`` heartbeat."""

    agentbridge_executable: Path
    config_path: Path
    target: str
    vendor_executable: Path

    def __post_init__(self) -> None:
        """Reject values that cannot be passed to a scheduler without ambiguity."""

        for label, path in (
            ("agentbridge executable", self.agentbridge_executable),
            ("configuration path", self.config_path),
            ("vendor executable", self.vendor_executable),
        ):
            if not path.is_absolute():
                raise ValueError(f"heartbeat {label} must be absolute: {path}")
            if any(character in os.fspath(path) for character in ("\x00", "\r", "\n")):
                raise ValueError(f"heartbeat {label} contains a control character: {path}")
        if _TARGET_NAME.fullmatch(self.target) is None:
            raise ValueError("heartbeat target must be a lowercase kebab-case identifier")

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the exact heartbeat process argument vector."""

        return (
            str(self.agentbridge_executable),
            "schedule",
            "tick",
            "--config",
            str(self.config_path),
            "--target",
            self.target,
            "--vendor-executable",
            str(self.vendor_executable),
        )


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """Read-only scheduler inspection result used as an apply precondition."""

    backend: ScheduleBackendKind
    action: ScheduleAction
    disposition: ScheduleDisposition
    target: str
    observed_digest: str
    desired_digest: str | None
    managed_digest: str | None
    detail: str

    @property
    def has_conflict(self) -> bool:
        """Return whether applying this plan would cross an ownership boundary."""

        return self.disposition is ScheduleDisposition.CONFLICT


@dataclass(frozen=True, slots=True)
class _CronInspection:
    plan: SchedulePlan
    document: str
    block_span: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _WindowsInspection:
    plan: SchedulePlan
    xml: str | None


def _default_runner(argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class LinuxCronBackend:
    """Manage one owned heartbeat block in the current user's crontab."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _default_runner,
        crontab_executable: str = "crontab",
    ) -> None:
        """Initialize the backend with an injectable subprocess boundary."""

        _validate_command_name(crontab_executable, "crontab executable")
        self._runner = runner
        self._crontab_executable = crontab_executable

    def plan(self, spec: HeartbeatSpec) -> SchedulePlan:
        """Inspect a heartbeat installation without changing the crontab."""

        return self._inspect(spec, ScheduleAction.INSTALL).plan

    def plan_remove(self, spec: HeartbeatSpec) -> SchedulePlan:
        """Inspect heartbeat removal without changing the crontab."""

        return self._inspect(spec, ScheduleAction.REMOVE).plan

    def apply(self, spec: HeartbeatSpec, reviewed_plan: SchedulePlan) -> bool:
        """Apply a reviewed create/update plan and verify convergence."""

        inspection = self._inspect(spec, ScheduleAction.INSTALL)
        _require_reviewed_plan(reviewed_plan, inspection.plan)
        _raise_on_conflict(inspection.plan)
        if inspection.plan.disposition is ScheduleDisposition.NOOP:
            return False

        desired_block = _cron_block(spec)
        if inspection.block_span is None:
            separator = "" if not inspection.document or inspection.document.endswith(("\n", "\r")) else "\n"
            updated = f"{inspection.document}{separator}{desired_block}"
        else:
            lines = inspection.document.splitlines(keepends=True)
            start, stop = inspection.block_span
            updated = "".join((*lines[:start], desired_block, *lines[stop:]))
        self._install_crontab(updated)
        converged = self.plan(spec)
        if converged.disposition is not ScheduleDisposition.NOOP:
            raise ScheduleBackendError("crontab heartbeat update did not converge to the reviewed specification")
        return True

    def remove(self, spec: HeartbeatSpec, reviewed_plan: SchedulePlan) -> bool:
        """Remove a reviewed owned block while preserving unrelated entries."""

        inspection = self._inspect(spec, ScheduleAction.REMOVE)
        _require_reviewed_plan(reviewed_plan, inspection.plan)
        _raise_on_conflict(inspection.plan)
        if inspection.plan.disposition is ScheduleDisposition.NOOP:
            return False
        if inspection.block_span is None:  # pragma: no cover - disposition guarantees a block
            raise ScheduleBackendError("owned crontab heartbeat block disappeared during removal")

        lines = inspection.document.splitlines(keepends=True)
        start, stop = inspection.block_span
        updated = "".join((*lines[:start], *lines[stop:]))
        self._install_crontab(updated)
        converged = self.plan_remove(spec)
        if converged.disposition is not ScheduleDisposition.NOOP:
            raise ScheduleBackendError("crontab heartbeat removal did not converge")
        return True

    def _inspect(self, spec: HeartbeatSpec, action: ScheduleAction) -> _CronInspection:
        document = self._read_crontab()
        observed_digest = _text_digest(document)
        desired_digest = _cron_spec_digest(spec) if action is ScheduleAction.INSTALL else None
        lines = document.splitlines(keepends=True)
        begin = _cron_begin(spec.target)
        end = _cron_end(spec.target)
        begin_indexes = [index for index, line in enumerate(lines) if _line_text(line) == begin]
        end_indexes = [index for index, line in enumerate(lines) if _line_text(line) == end]

        if not begin_indexes and not end_indexes:
            stray_marker = any(
                _line_text(line).startswith("# agent-config-bridge-heartbeat ")
                and _cron_marker_targets(_line_text(line), spec.target)
                for line in lines
            )
            if stray_marker:
                return _CronInspection(
                    plan=_schedule_plan(
                        ScheduleBackendKind.LINUX_CRONTAB,
                        action,
                        ScheduleDisposition.CONFLICT,
                        spec,
                        observed_digest,
                        desired_digest,
                        None,
                        "heartbeat ownership marker exists outside its required marked block",
                    ),
                    document=document,
                    block_span=None,
                )
            disposition = ScheduleDisposition.CREATE if action is ScheduleAction.INSTALL else ScheduleDisposition.NOOP
            detail = (
                "create the owned once-per-minute crontab block"
                if action is ScheduleAction.INSTALL
                else "heartbeat block is absent"
            )
            return _CronInspection(
                plan=_schedule_plan(
                    ScheduleBackendKind.LINUX_CRONTAB,
                    action,
                    disposition,
                    spec,
                    observed_digest,
                    desired_digest,
                    None,
                    detail,
                ),
                document=document,
                block_span=None,
            )

        if len(begin_indexes) != 1 or len(end_indexes) != 1:
            return self._cron_conflict(
                spec,
                action,
                observed_digest,
                desired_digest,
                document,
                "heartbeat block markers are missing or duplicated",
            )
        start = begin_indexes[0]
        end_index = end_indexes[0]
        if end_index != start + 3:
            return self._cron_conflict(
                spec,
                action,
                observed_digest,
                desired_digest,
                document,
                "heartbeat block has an unexpected structure",
            )

        marker_match = _CRON_META.fullmatch(_line_text(lines[start + 1]))
        schedule_line = _line_text(lines[start + 2])
        if marker_match is None or marker_match.group("target") != spec.target:
            return self._cron_conflict(
                spec,
                action,
                observed_digest,
                desired_digest,
                document,
                "heartbeat block has an invalid ownership marker",
            )
        claimed_digest = marker_match.group("digest")
        actual_digest = _cron_payload_digest(spec.target, schedule_line)
        if not schedule_line.startswith("* * * * * ") or claimed_digest != actual_digest:
            return self._cron_conflict(
                spec,
                action,
                observed_digest,
                desired_digest,
                document,
                "heartbeat block content does not match its ownership digest",
                managed_digest=claimed_digest,
            )

        if action is ScheduleAction.REMOVE:
            disposition = ScheduleDisposition.REMOVE
            detail = "remove the intact owned crontab heartbeat block"
        elif claimed_digest == desired_digest:
            disposition = ScheduleDisposition.NOOP
            detail = "crontab heartbeat already matches the desired specification"
        else:
            disposition = ScheduleDisposition.UPDATE
            detail = "replace the intact owned crontab heartbeat block"
        return _CronInspection(
            plan=_schedule_plan(
                ScheduleBackendKind.LINUX_CRONTAB,
                action,
                disposition,
                spec,
                observed_digest,
                desired_digest,
                claimed_digest,
                detail,
            ),
            document=document,
            block_span=(start, end_index + 1),
        )

    def _cron_conflict(
        self,
        spec: HeartbeatSpec,
        action: ScheduleAction,
        observed_digest: str,
        desired_digest: str | None,
        document: str,
        detail: str,
        *,
        managed_digest: str | None = None,
    ) -> _CronInspection:
        return _CronInspection(
            plan=_schedule_plan(
                ScheduleBackendKind.LINUX_CRONTAB,
                action,
                ScheduleDisposition.CONFLICT,
                spec,
                observed_digest,
                desired_digest,
                managed_digest,
                detail,
            ),
            document=document,
            block_span=None,
        )

    def _read_crontab(self) -> str:
        argv = (self._crontab_executable, "-l")
        result = self._runner(argv, None)
        if result.returncode == 0:
            return result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode == 1 and not (result.stdout or "") and "no crontab for" in stderr.casefold():
            return ""
        raise ScheduleBackendError(_command_failure("could not read the current user's crontab", result))

    def _install_crontab(self, document: str) -> None:
        argv = (self._crontab_executable, "-") if document else (self._crontab_executable, "-r")
        result = self._runner(argv, document if argv[-1] == "-" else None)
        if result.returncode != 0:
            raise ScheduleBackendError(_command_failure("could not update the current user's crontab", result))


class WindowsTaskSchedulerBackend:
    """Manage one owned once-per-minute Windows Task Scheduler task."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _default_runner,
        schtasks_executable: str | None = None,
        principal_user: str | None = None,
    ) -> None:
        """Initialize the backend with an injectable subprocess boundary."""

        selected_executable = _select_schtasks_executable(schtasks_executable)
        user = principal_user if principal_user is not None else _default_principal_user()
        if not user or any(character in user for character in ("\x00", "\r", "\n")):
            raise ValueError("Task Scheduler principal user must be a non-empty single-line value")
        self._runner = runner
        self._schtasks_executable = selected_executable
        self._principal_user = user

    @property
    def schtasks_executable(self) -> str:
        """Return the validated command path used for Task Scheduler calls."""

        return self._schtasks_executable

    def plan(self, spec: HeartbeatSpec) -> SchedulePlan:
        """Inspect a heartbeat task without changing Task Scheduler."""

        return self._inspect(spec, ScheduleAction.INSTALL).plan

    def plan_remove(self, spec: HeartbeatSpec) -> SchedulePlan:
        """Inspect heartbeat task removal without changing Task Scheduler."""

        return self._inspect(spec, ScheduleAction.REMOVE).plan

    def apply(self, spec: HeartbeatSpec, reviewed_plan: SchedulePlan) -> bool:
        """Apply a reviewed task create/update and verify convergence."""

        inspection = self._inspect(spec, ScheduleAction.INSTALL)
        _require_reviewed_plan(reviewed_plan, inspection.plan)
        _raise_on_conflict(inspection.plan)
        if inspection.plan.disposition is ScheduleDisposition.NOOP:
            return False

        xml = _windows_task_xml(spec, self._principal_user, self.task_name(spec))
        self._create_task(xml, force=inspection.plan.disposition is ScheduleDisposition.UPDATE, spec=spec)
        converged = self.plan(spec)
        if converged.disposition is not ScheduleDisposition.NOOP:
            raise ScheduleBackendError(
                "Windows heartbeat task update did not converge to the reviewed specification "
                f"({converged.disposition.value}: {converged.detail})"
            )
        return True

    def remove(self, spec: HeartbeatSpec, reviewed_plan: SchedulePlan) -> bool:
        """Remove a reviewed owned task without affecting other scheduled tasks."""

        inspection = self._inspect(spec, ScheduleAction.REMOVE)
        _require_reviewed_plan(reviewed_plan, inspection.plan)
        _raise_on_conflict(inspection.plan)
        if inspection.plan.disposition is ScheduleDisposition.NOOP:
            return False

        argv = (self._schtasks_executable, "/Delete", "/TN", self.task_name(spec), "/F")
        result = self._runner(argv, None)
        if result.returncode != 0:
            raise ScheduleBackendError(_command_failure("could not remove the Windows heartbeat task", result))
        converged = self.plan_remove(spec)
        if converged.disposition is not ScheduleDisposition.NOOP:
            raise ScheduleBackendError("Windows heartbeat task removal did not converge")
        return True

    @staticmethod
    def task_name(spec: HeartbeatSpec) -> str:
        """Return the stable, target-scoped Task Scheduler name."""

        name = f"AgentConfigBridge-Heartbeat-{spec.target}"
        if len(name) > 200:
            raise ValueError("heartbeat target produces an excessively long Task Scheduler name")
        return name

    def _inspect(self, spec: HeartbeatSpec, action: ScheduleAction) -> _WindowsInspection:
        task_name = self.task_name(spec)
        xml = self._query_task(task_name)
        observed_digest = _text_digest(xml if xml is not None else "<absent>")
        desired_digest = (
            _windows_spec_digest(spec, task_name, self._principal_user) if action is ScheduleAction.INSTALL else None
        )
        if xml is None:
            disposition = ScheduleDisposition.CREATE if action is ScheduleAction.INSTALL else ScheduleDisposition.NOOP
            detail = (
                "create the owned once-per-minute scheduled task"
                if action is ScheduleAction.INSTALL
                else "heartbeat task is absent"
            )
            return _WindowsInspection(
                plan=_schedule_plan(
                    ScheduleBackendKind.WINDOWS_TASK_SCHEDULER,
                    action,
                    disposition,
                    spec,
                    observed_digest,
                    desired_digest,
                    None,
                    detail,
                ),
                xml=None,
            )

        try:
            claimed_digest, actual_digest = _inspect_windows_task_xml(xml, spec.target, task_name)
        except ScheduleConflictError as exc:
            return _WindowsInspection(
                plan=_schedule_plan(
                    ScheduleBackendKind.WINDOWS_TASK_SCHEDULER,
                    action,
                    ScheduleDisposition.CONFLICT,
                    spec,
                    observed_digest,
                    desired_digest,
                    None,
                    str(exc),
                ),
                xml=xml,
            )
        if claimed_digest != actual_digest:
            return _WindowsInspection(
                plan=_schedule_plan(
                    ScheduleBackendKind.WINDOWS_TASK_SCHEDULER,
                    action,
                    ScheduleDisposition.CONFLICT,
                    spec,
                    observed_digest,
                    desired_digest,
                    claimed_digest,
                    "scheduled task content does not match its ownership digest",
                ),
                xml=xml,
            )

        if action is ScheduleAction.REMOVE:
            disposition = ScheduleDisposition.REMOVE
            detail = "remove the intact owned scheduled task"
        elif claimed_digest == desired_digest:
            disposition = ScheduleDisposition.NOOP
            detail = "scheduled heartbeat task already matches the desired specification"
        else:
            disposition = ScheduleDisposition.UPDATE
            detail = "replace the intact owned scheduled heartbeat task"
        return _WindowsInspection(
            plan=_schedule_plan(
                ScheduleBackendKind.WINDOWS_TASK_SCHEDULER,
                action,
                disposition,
                spec,
                observed_digest,
                desired_digest,
                claimed_digest,
                detail,
            ),
            xml=xml,
        )

    def _query_task(self, task_name: str) -> str | None:
        argv = (self._schtasks_executable, "/Query", "/TN", task_name, "/XML", "/HResult")
        result = self._runner(argv, None)
        if result.returncode == 0:
            if not (result.stdout or "").strip():
                raise ScheduleBackendError("Task Scheduler returned an empty XML document for an existing task")
            return result.stdout
        if result.returncode & 0xFFFFFFFF in _WINDOWS_ABSENT_TASK_HRESULTS:
            return None
        diagnostic = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
        missing_messages = (
            "cannot find the file specified",
            "cannot find the specified file",
            "the system cannot find",
            "does not exist",
        )
        if result.returncode == 1 and any(message in diagnostic for message in missing_messages):
            return None
        raise ScheduleBackendError(_command_failure("could not query the Windows heartbeat task", result))

    def _create_task(self, xml: bytes, *, force: bool, spec: HeartbeatSpec) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="agentbridge-heartbeat-", suffix=".xml", delete=False) as stream:
                stream.write(xml)
                temporary_path = Path(stream.name)
            argv = (
                self._schtasks_executable,
                "/Create",
                "/TN",
                self.task_name(spec),
                "/XML",
                str(temporary_path),
                *(("/F",) if force else ()),
            )
            result = self._runner(argv, None)
            if result.returncode != 0:
                raise ScheduleBackendError(_command_failure("could not create the Windows heartbeat task", result))
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _validate_command_name(value: str, label: str) -> None:
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} must be a non-empty single-line value")


def _select_schtasks_executable(override: str | None) -> str:
    """Choose Task Scheduler's trusted System32 executable on Windows."""

    if os.name != "nt":
        selected = override or "schtasks.exe"
        _validate_command_name(selected, "schtasks executable")
        return selected

    try:
        trusted = _windows_system_executable("schtasks.exe")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleBackendError("could not resolve the trusted Windows Task Scheduler executable") from exc
    if override is None:
        return str(trusted)
    _validate_command_name(override, "schtasks executable")
    candidate = Path(override)
    if not candidate.is_absolute():
        raise ValueError("schtasks executable override must be an absolute System32 path")
    try:
        candidate_identity = candidate.resolve(strict=True)
        trusted_identity = trusted.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("could not validate the schtasks executable override") from exc
    if candidate_identity != trusted_identity:
        raise ValueError("schtasks executable override must select the genuine System32 executable")
    return str(Path(os.path.abspath(candidate)))


def _windows_system_executable(filename: str) -> Path:
    """Resolve a named executable beneath the OS-reported Windows system directory."""

    if not filename or Path(filename).name != filename or not filename.casefold().endswith(".exe"):
        raise ValueError(f"invalid Windows system executable name: {filename!r}")
    system_directory = _windows_system_directory()
    candidate = system_directory / filename
    try:
        resolved = candidate.resolve(strict=True)
        system_identity = system_directory.resolve(strict=True)
        resolved.relative_to(system_identity)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"could not resolve the trusted Windows system executable: {candidate}") from exc
    if not resolved.is_file():
        raise ValueError(f"trusted Windows system executable is not a file: {candidate}")
    return Path(os.path.abspath(candidate))


def _windows_system_directory() -> Path:
    """Return the genuine Windows system directory without trusting environment data."""

    import ctypes
    from ctypes import wintypes

    try:
        ctypes_api: Any = ctypes
        kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - only reachable on broken Windows runtimes
        raise RuntimeError("Windows system APIs are unavailable") from exc
    kernel32.GetSystemDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32_768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0:
        raise ctypes_api.WinError(ctypes_api.get_last_error())
    if length >= len(buffer):
        raise RuntimeError("Windows system directory exceeds the supported path length")
    system_directory = Path(buffer.value)
    if not system_directory.is_absolute():
        raise RuntimeError("Windows returned a non-absolute system directory")
    return system_directory


def _default_principal_user() -> str:
    if os.name != "nt":
        return getpass.getuser()
    try:
        sid = _windows_current_user_sid()
    except (OSError, RuntimeError) as exc:
        raise ValueError("could not query the current Windows user SID") from exc
    if _WINDOWS_SID.fullmatch(sid) is None:
        raise ValueError("could not query the current Windows user SID")
    return sid


def _windows_current_user_sid() -> str:
    """Return the current process token SID without invoking an external command."""

    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    error_insufficient_buffer = 122

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    try:
        ctypes_api: Any = ctypes
        kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes_api.WinDLL("advapi32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - only reachable on broken Windows runtimes
        raise RuntimeError("Windows security APIs are unavailable") from exc

    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
        raise ctypes_api.WinError(ctypes_api.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user_class, None, 0, ctypes.byref(required))
        if ctypes_api.get_last_error() != error_insufficient_buffer or required.value == 0:
            raise ctypes_api.WinError(ctypes_api.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes_api.WinError(ctypes_api.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid)):
            raise ctypes_api.WinError(ctypes_api.get_last_error())
        try:
            value = sid.value
            if not value:
                raise RuntimeError("Windows returned an empty user SID")
            return value
        finally:
            kernel32.LocalFree(sid)
    finally:
        kernel32.CloseHandle(token)


def _schedule_plan(
    backend: ScheduleBackendKind,
    action: ScheduleAction,
    disposition: ScheduleDisposition,
    spec: HeartbeatSpec,
    observed_digest: str,
    desired_digest: str | None,
    managed_digest: str | None,
    detail: str,
) -> SchedulePlan:
    return SchedulePlan(
        backend=backend,
        action=action,
        disposition=disposition,
        target=spec.target,
        observed_digest=observed_digest,
        desired_digest=desired_digest,
        managed_digest=managed_digest,
        detail=detail,
    )


def _require_reviewed_plan(reviewed: SchedulePlan, current: SchedulePlan) -> None:
    if reviewed != current:
        raise StaleSchedulePlanError("scheduler state or desired heartbeat changed after planning; review a fresh plan")


def _raise_on_conflict(plan: SchedulePlan) -> None:
    if plan.has_conflict:
        raise ScheduleConflictError(plan.detail)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_digest(payload: dict[str, str | int]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _text_digest(serialized)


def _cron_begin(target: str) -> str:
    return f"# BEGIN agent-config-bridge heartbeat {target}"


def _cron_end(target: str) -> str:
    return f"# END agent-config-bridge heartbeat {target}"


def _cron_schedule_line(spec: HeartbeatSpec) -> str:
    # Cron interprets unescaped '%' before invoking /bin/sh, even inside shell
    # quotes.  shlex.join handles shell metacharacters; this second pass handles
    # cron's separate command-field grammar.
    command = shlex.join(spec.argv).replace("%", r"\%")
    return f"* * * * * {command}"


def _cron_payload_digest(target: str, schedule_line: str) -> str:
    return _payload_digest(
        {
            "backend": ScheduleBackendKind.LINUX_CRONTAB.value,
            "schema_version": 1,
            "target": target,
            "schedule_line": schedule_line,
        }
    )


def _cron_spec_digest(spec: HeartbeatSpec) -> str:
    return _cron_payload_digest(spec.target, _cron_schedule_line(spec))


def _cron_block(spec: HeartbeatSpec) -> str:
    schedule_line = _cron_schedule_line(spec)
    digest = _cron_payload_digest(spec.target, schedule_line)
    return (
        f"{_cron_begin(spec.target)}\n"
        f"# agent-config-bridge-heartbeat schema=1 target={spec.target} digest={digest}\n"
        f"{schedule_line}\n"
        f"{_cron_end(spec.target)}\n"
    )


def _line_text(line: str) -> str:
    return line.rstrip("\r\n")


def _cron_marker_targets(line: str, target: str) -> bool:
    return re.search(rf"(?:^| )target={re.escape(target)}(?: |$)", line) is not None


def _windows_arguments(spec: HeartbeatSpec) -> str:
    return subprocess.list2cmdline(spec.argv[1:])


def _windows_payload_digest(
    *,
    task_name: str,
    target: str,
    command: str,
    arguments: str,
    interval: str,
    execution_time_limit: str,
    disallow_on_batteries: str,
    instances_policy: str,
    logon_type: str,
    principal_id: str,
    principal_user: str,
    run_level: str,
    start_boundary: str,
    start_when_available: str,
    stop_on_batteries: str,
    stop_at_duration_end: str,
    task_enabled: str,
    trigger_enabled: str,
) -> str:
    return _payload_digest(
        {
            "backend": ScheduleBackendKind.WINDOWS_TASK_SCHEDULER.value,
            "schema_version": 1,
            "task_name": task_name,
            "target": target,
            "command": command,
            "arguments": arguments,
            "interval": interval,
            "execution_time_limit": execution_time_limit,
            "disallow_on_batteries": disallow_on_batteries,
            "instances_policy": instances_policy,
            "logon_type": logon_type,
            "principal_id": principal_id,
            "principal_user": principal_user,
            "run_level": run_level,
            "start_boundary": start_boundary,
            "start_when_available": start_when_available,
            "stop_on_batteries": stop_on_batteries,
            "stop_at_duration_end": stop_at_duration_end,
            "task_enabled": task_enabled,
            "trigger_enabled": trigger_enabled,
        }
    )


def _windows_spec_digest(spec: HeartbeatSpec, task_name: str, principal_user: str) -> str:
    return _windows_payload_digest(
        task_name=task_name,
        target=spec.target,
        command=str(spec.agentbridge_executable),
        arguments=_windows_arguments(spec),
        interval=_TASK_INTERVAL,
        execution_time_limit=_TASK_EXECUTION_TIME_LIMIT,
        disallow_on_batteries=_TASK_DISALLOW_ON_BATTERIES,
        instances_policy=_TASK_INSTANCES_POLICY,
        logon_type=_TASK_LOGON_TYPE,
        principal_id=_TASK_PRINCIPAL_ID,
        principal_user=principal_user,
        run_level=_TASK_RUN_LEVEL,
        start_boundary=_TASK_START_BOUNDARY,
        start_when_available=_TASK_START_WHEN_AVAILABLE,
        stop_on_batteries=_TASK_STOP_ON_BATTERIES,
        stop_at_duration_end=_TASK_STOP_AT_DURATION_END,
        task_enabled=_TASK_ENABLED,
        trigger_enabled=_TASK_ENABLED,
    )


def _task_tag(name: str) -> str:
    return f"{{{_TASK_NAMESPACE}}}{name}"


def _task_child(parent: ET.Element, name: str, value: str | None = None, **attributes: str) -> ET.Element:
    child = ET.SubElement(parent, _task_tag(name), attributes)
    child.text = value
    return child


def _task_children_are_supported(
    parent: ET.Element,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> bool:
    """Accept only known direct children with safe required/optional cardinality."""

    allowed_tags = {_task_tag(name) for name in (*required, *optional)}
    return (
        all(child.tag in allowed_tags for child in parent)
        and all(len(parent.findall(_task_tag(name))) == 1 for name in required)
        and all(len(parent.findall(_task_tag(name))) <= 1 for name in optional)
    )


def _windows_task_xml(spec: HeartbeatSpec, principal_user: str, task_name: str) -> bytes:
    ET.register_namespace("", _TASK_NAMESPACE)
    root = ET.Element(_task_tag("Task"), {"version": "1.3"})
    registration = _task_child(root, "RegistrationInfo")
    _task_child(registration, "Author", "Agent Config Bridge")
    digest = _windows_spec_digest(spec, task_name, principal_user)
    _task_child(
        registration,
        "Description",
        f"agent-config-bridge-heartbeat schema=1 target={spec.target} digest={digest}",
    )
    _task_child(registration, "URI", f"\\{task_name}")

    triggers = _task_child(root, "Triggers")
    trigger = _task_child(triggers, "TimeTrigger")
    _task_child(trigger, "Enabled", _TASK_ENABLED)
    _task_child(trigger, "StartBoundary", _TASK_START_BOUNDARY)
    repetition = _task_child(trigger, "Repetition")
    _task_child(repetition, "Interval", _TASK_INTERVAL)
    _task_child(repetition, "StopAtDurationEnd", _TASK_STOP_AT_DURATION_END)

    principals = _task_child(root, "Principals")
    principal = _task_child(principals, "Principal", id=_TASK_PRINCIPAL_ID)
    _task_child(principal, "UserId", principal_user)
    _task_child(principal, "LogonType", _TASK_LOGON_TYPE)
    _task_child(principal, "RunLevel", _TASK_RUN_LEVEL)

    settings = _task_child(root, "Settings")
    _task_child(settings, "MultipleInstancesPolicy", _TASK_INSTANCES_POLICY)
    _task_child(settings, "DisallowStartIfOnBatteries", _TASK_DISALLOW_ON_BATTERIES)
    _task_child(settings, "StopIfGoingOnBatteries", _TASK_STOP_ON_BATTERIES)
    _task_child(settings, "StartWhenAvailable", _TASK_START_WHEN_AVAILABLE)
    _task_child(settings, "Enabled", _TASK_ENABLED)
    _task_child(settings, "ExecutionTimeLimit", _TASK_EXECUTION_TIME_LIMIT)

    actions = _task_child(root, "Actions", Context=_TASK_PRINCIPAL_ID)
    execution = _task_child(actions, "Exec")
    _task_child(execution, "Command", str(spec.agentbridge_executable))
    _task_child(execution, "Arguments", _windows_arguments(spec))

    ET.indent(root, space="  ")
    # SchTasks hands task definitions to the Windows XML stack as Unicode.
    # Serializing the import file as BOM-marked UTF-16 keeps its byte stream
    # consistent with the XML declaration and with Task Scheduler exports.
    return cast(bytes, ET.tostring(root, encoding="utf-16", xml_declaration=True))


def _inspect_windows_task_xml(xml: str, expected_target: str, task_name: str) -> tuple[str, str]:
    if len(xml) > _MAX_TASK_XML_CHARACTERS:
        raise ScheduleConflictError("scheduled task XML exceeds the safe inspection limit")
    try:
        root = ET.fromstring(xml.lstrip("\ufeff"))
    except (ET.ParseError, ValueError) as exc:
        raise ScheduleConflictError("scheduled task has invalid XML") from exc
    if root.tag != _task_tag("Task"):
        raise ScheduleConflictError("scheduled task XML has an unexpected root or namespace")

    descriptions = root.findall(f"./{_task_tag('RegistrationInfo')}/{_task_tag('Description')}")
    actions = root.findall(f"./{_task_tag('Actions')}")
    triggers = root.findall(f"./{_task_tag('Triggers')}/{_task_tag('TimeTrigger')}")
    all_trigger_children = root.findall(f"./{_task_tag('Triggers')}/*")
    principals = root.findall(f"./{_task_tag('Principals')}/{_task_tag('Principal')}")
    settings = root.findall(f"./{_task_tag('Settings')}")
    if (
        len(descriptions) != 1
        or len(actions) != 1
        or len(triggers) != 1
        or len(all_trigger_children) != 1
        or len(principals) != 1
        or len(settings) != 1
    ):
        raise ScheduleConflictError("scheduled task does not have the single owned marker, action, and trigger")

    action_children = list(actions[0])
    if len(action_children) != 1 or action_children[0].tag != _task_tag("Exec"):
        raise ScheduleConflictError("scheduled task must contain exactly one Exec action")
    execution = action_children[0]
    if {child.tag for child in execution} != {_task_tag("Command"), _task_tag("Arguments")} or len(execution) != 2:
        raise ScheduleConflictError("scheduled task Exec action has unsupported fields")

    principal = principals[0]
    if not _task_children_are_supported(
        principal,
        required=("UserId", "LogonType"),
        optional=("RunLevel",),
    ):
        raise ScheduleConflictError("scheduled task principal has unsupported fields")

    trigger = triggers[0]
    if not _task_children_are_supported(
        trigger,
        required=("StartBoundary", "Repetition"),
        optional=("Enabled",),
    ):
        raise ScheduleConflictError("scheduled task trigger has unsupported fields")
    repetitions = trigger.findall(f"./{_task_tag('Repetition')}")
    if len(repetitions) != 1:
        raise ScheduleConflictError("scheduled task repetition policy is malformed")
    if not _task_children_are_supported(
        repetitions[0],
        required=("Interval",),
        optional=("StopAtDurationEnd",),
    ):
        raise ScheduleConflictError("scheduled task repetition policy has unsupported fields")

    marker_match = _WINDOWS_MARKER.fullmatch(descriptions[0].text or "")
    if marker_match is None or marker_match.group("target") != expected_target:
        raise ScheduleConflictError("scheduled task has no matching bridge ownership marker")
    claimed_digest = marker_match.group("digest")
    if _DIGEST.fullmatch(claimed_digest) is None:  # pragma: no cover - marker regex guarantees this
        raise ScheduleConflictError("scheduled task has an invalid bridge ownership digest")

    settings_element = settings[0]
    if not _task_children_are_supported(
        settings_element,
        required=(
            "MultipleInstancesPolicy",
            "DisallowStartIfOnBatteries",
            "StopIfGoingOnBatteries",
            "StartWhenAvailable",
            "ExecutionTimeLimit",
        ),
        optional=(
            "Enabled",
            "RunOnlyIfIdle",
            "RunOnlyIfNetworkAvailable",
            "WakeToRun",
            "IdleSettings",
            "UseUnifiedSchedulingEngine",
        ),
    ):
        raise ScheduleConflictError("scheduled task execution settings are missing or duplicated")
    for name in ("RunOnlyIfIdle", "RunOnlyIfNetworkAvailable", "WakeToRun"):
        values = settings_element.findall(f"./{_task_tag(name)}")
        if values and values[0].text != "false":
            raise ScheduleConflictError(f"scheduled task has an unsupported {name} policy")

    idle_settings = settings_element.findall(f"./{_task_tag('IdleSettings')}")
    if idle_settings and (
        not _task_children_are_supported(
            idle_settings[0],
            required=("StopOnIdleEnd", "RestartOnIdle"),
        )
        or (
            idle_settings[0].findtext(_task_tag("StopOnIdleEnd")) != "true"
            or idle_settings[0].findtext(_task_tag("RestartOnIdle")) != "false"
        )
    ):
        raise ScheduleConflictError("scheduled task has unsupported idle defaults")
    unified_engine = settings_element.findall(f"./{_task_tag('UseUnifiedSchedulingEngine')}")
    if unified_engine and unified_engine[0].text != "true":
        raise ScheduleConflictError("scheduled task has an unsupported unified scheduling engine policy")

    command = execution.findtext(_task_tag("Command"))
    arguments = execution.findtext(_task_tag("Arguments"))
    interval = repetitions[0].findtext(_task_tag("Interval"))
    # Task Scheduler omits several elements when they equal their safe schema
    # defaults.  Canonicalize only those allowlisted omissions after validating
    # child cardinality; explicit non-default values still change the digest.
    stop_at_duration_end = repetitions[0].findtext(
        _task_tag("StopAtDurationEnd"),
        _TASK_STOP_AT_DURATION_END,
    )
    trigger_enabled = trigger.findtext(_task_tag("Enabled"), _TASK_ENABLED)
    start_boundary = trigger.findtext(_task_tag("StartBoundary"))
    principal_id = principal.get("id")
    principal_user = principal.findtext(_task_tag("UserId"))
    logon_type = principal.findtext(_task_tag("LogonType"))
    run_level = principal.findtext(_task_tag("RunLevel"), _TASK_RUN_LEVEL)
    actions_context = actions[0].get("Context")
    instances_policy = settings_element.findtext(_task_tag("MultipleInstancesPolicy"))
    execution_time_limit = settings_element.findtext(_task_tag("ExecutionTimeLimit"))
    disallow_on_batteries = settings_element.findtext(_task_tag("DisallowStartIfOnBatteries"))
    stop_on_batteries = settings_element.findtext(_task_tag("StopIfGoingOnBatteries"))
    start_when_available = settings_element.findtext(_task_tag("StartWhenAvailable"))
    task_enabled = settings_element.findtext(_task_tag("Enabled"), _TASK_ENABLED)
    if command is None or arguments is None or interval is None:
        raise ScheduleConflictError("scheduled task action or once-per-minute trigger is malformed")
    if not all(
        isinstance(value, str)
        for value in (
            stop_at_duration_end,
            trigger_enabled,
            start_boundary,
            principal_id,
            principal_user,
            logon_type,
            run_level,
            actions_context,
            instances_policy,
            execution_time_limit,
            disallow_on_batteries,
            stop_on_batteries,
            start_when_available,
            task_enabled,
        )
    ):
        raise ScheduleConflictError("scheduled task principal or execution policy is malformed")
    assert isinstance(stop_at_duration_end, str)
    assert isinstance(trigger_enabled, str)
    assert isinstance(start_boundary, str)
    assert isinstance(principal_id, str)
    assert isinstance(principal_user, str)
    assert isinstance(logon_type, str)
    assert isinstance(run_level, str)
    assert isinstance(actions_context, str)
    assert isinstance(instances_policy, str)
    assert isinstance(execution_time_limit, str)
    assert isinstance(disallow_on_batteries, str)
    assert isinstance(stop_on_batteries, str)
    assert isinstance(start_when_available, str)
    assert isinstance(task_enabled, str)
    if not principal_user or actions_context != principal_id:
        raise ScheduleConflictError("scheduled task action context does not match its principal")
    if not (Path(command).is_absolute() or PureWindowsPath(command).is_absolute()):
        raise ScheduleConflictError("scheduled task agentbridge command is not absolute")

    actual_digest = _windows_payload_digest(
        task_name=task_name,
        target=expected_target,
        command=command,
        arguments=arguments,
        interval=interval,
        execution_time_limit=execution_time_limit,
        disallow_on_batteries=disallow_on_batteries,
        instances_policy=instances_policy,
        logon_type=logon_type,
        principal_id=principal_id,
        principal_user=principal_user,
        run_level=run_level,
        start_boundary=start_boundary,
        start_when_available=start_when_available,
        stop_on_batteries=stop_on_batteries,
        stop_at_duration_end=stop_at_duration_end,
        task_enabled=task_enabled,
        trigger_enabled=trigger_enabled,
    )
    return claimed_digest, actual_digest


def _command_failure(context: str, result: subprocess.CompletedProcess[str]) -> str:
    diagnostic = (result.stderr or result.stdout or "no diagnostic output").strip()
    if len(diagnostic) > 500:
        diagnostic = diagnostic[:497] + "..."
    return f"{context} (exit {result.returncode}): {diagnostic}"
