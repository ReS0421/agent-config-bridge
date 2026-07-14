"""Idempotent host-heartbeat execution for rendered schedule snapshots."""

from __future__ import annotations

import importlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from agent_config_bridge.models import BridgeConfig, Product, TargetConfig
from agent_config_bridge.path_safety import is_directory_reparse_point
from agent_config_bridge.schedule_store import read_schedule_set
from agent_config_bridge.schedules import (
    ScheduleError,
    ScheduleExecutionError,
    TargetScheduleSnapshot,
    VendorExecutionResult,
    VendorInvocation,
    build_vendor_invocation,
    execute_vendor_invocation,
    snapshot_is_due,
)

__all__ = [
    "ScheduleRun",
    "ScheduleRunnerError",
    "TickResult",
    "run_due_schedules",
    "run_named_schedule",
]

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ScheduleRunnerError(RuntimeError):
    """Raised when schedule runtime state is unsafe or unusable."""


@dataclass(frozen=True, slots=True)
class ScheduleRun:
    """The non-sensitive outcome of one attempted schedule invocation."""

    name: str
    succeeded: bool
    returncode: int | None
    error: str | None
    skipped_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TickResult:
    """Summary of one minute heartbeat or manual run."""

    target: str
    minute: str
    runs: tuple[ScheduleRun, ...]
    skipped_reason: str | None = None


ExecutionFunction = Callable[[VendorInvocation], VendorExecutionResult]


def run_due_schedules(
    config: BridgeConfig,
    target: TargetConfig,
    *,
    moment: datetime | None = None,
    environment: Mapping[str, str] | None = None,
    vendor_executable: Path | None = None,
    execute: ExecutionFunction = execute_vendor_invocation,
) -> TickResult:
    """Run schedules due in one UTC minute, at most once per target snapshot."""

    instant = moment or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ScheduleRunnerError("schedule tick moment must be timezone-aware")
    minute = instant.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    rendered = read_schedule_set(config, target)
    if rendered is None:
        return TickResult(target=target.name, minute=minute, runs=(), skipped_reason="no published schedules")

    runtime_root = _runtime_root(config)
    lock_path = runtime_root / f"{target.name}.lock"
    with _TargetLock(lock_path) as acquired:
        if not acquired:
            return TickResult(target=target.name, minute=minute, runs=(), skipped_reason="another tick is active")

        last_tick = runtime_root / f"{target.name}.last.json"
        if _already_processed(last_tick, target.name, minute, rendered.digest):
            return TickResult(target=target.name, minute=minute, runs=(), skipped_reason="minute already processed")
        _write_last_tick(last_tick, target.name, minute, rendered.digest)
        due = tuple(snapshot for snapshot in rendered.schedules if snapshot_is_due(snapshot, instant))

    # The target lock protects only minute claiming. Vendor work runs outside
    # it so a long task cannot make later cron minutes disappear.
    runs = _execute_snapshots(due, target, runtime_root, environment, vendor_executable, execute)
    return TickResult(target=target.name, minute=minute, runs=runs)


def run_named_schedule(
    config: BridgeConfig,
    target: TargetConfig,
    schedule_name: str,
    *,
    environment: Mapping[str, str] | None = None,
    vendor_executable: Path | None = None,
    execute: ExecutionFunction = execute_vendor_invocation,
) -> TickResult:
    """Run one published schedule immediately without changing minute state."""

    if not schedule_name:
        raise ScheduleRunnerError("schedule name must not be empty")
    rendered = read_schedule_set(config, target)
    if rendered is None:
        raise ScheduleRunnerError(f"target {target.name!r} has no published schedules; run apply first")
    matches = tuple(snapshot for snapshot in rendered.schedules if snapshot.schedule_name == schedule_name)
    if len(matches) != 1:
        raise ScheduleRunnerError(f"unknown published schedule {schedule_name!r} for target {target.name!r}")

    runtime_root = _runtime_root(config)
    runs = _execute_snapshots(matches, target, runtime_root, environment, vendor_executable, execute)
    return TickResult(
        target=target.name,
        minute=datetime.now(UTC).replace(second=0, microsecond=0).isoformat(),
        runs=runs,
    )


def _execute_snapshots(
    snapshots: tuple[TargetScheduleSnapshot, ...],
    target: TargetConfig,
    runtime_root: Path,
    environment: Mapping[str, str] | None,
    vendor_executable: Path | None,
    execute: ExecutionFunction,
) -> tuple[ScheduleRun, ...]:
    local_environment = dict(os.environ if environment is None else environment)
    if target.product is Product.CODEX:
        local_environment["CODEX_HOME"] = str(target.config_home)
    else:
        local_environment["CLAUDE_CONFIG_DIR"] = str(target.config_home)

    runs: list[ScheduleRun] = []
    for snapshot in snapshots:
        run_lock = runtime_root / f"{target.name}.{snapshot.schedule_name}.run.lock"
        with _TargetLock(run_lock) as acquired:
            if not acquired:
                runs.append(
                    ScheduleRun(
                        name=snapshot.schedule_name,
                        succeeded=False,
                        returncode=None,
                        error=None,
                        skipped_reason="a previous run is still active",
                    )
                )
                continue
            try:
                invocation = build_vendor_invocation(
                    snapshot,
                    environment=local_environment,
                    vendor_executable=vendor_executable,
                )
                result = execute(invocation)
            except (ScheduleError, ScheduleExecutionError) as exc:
                runs.append(ScheduleRun(name=snapshot.schedule_name, succeeded=False, returncode=None, error=str(exc)))
            else:
                runs.append(
                    ScheduleRun(
                        name=snapshot.schedule_name,
                        succeeded=True,
                        returncode=result.returncode,
                        error=None,
                    )
                )
    return tuple(runs)


def _runtime_root(config: BridgeConfig) -> Path:
    root = config.state_dir / "schedule-runtime"
    state_root = config.state_dir.resolve(strict=False)
    try:
        root.resolve(strict=False).relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleRunnerError(f"schedule runtime root escapes state_dir: {root}") from exc
    if os.path.lexists(root):
        try:
            redirected = root.is_symlink() or is_directory_reparse_point(root)
        except OSError as exc:
            raise ScheduleRunnerError(f"cannot inspect schedule runtime root: {root}") from exc
        if redirected or not root.is_dir():
            raise ScheduleRunnerError(f"schedule runtime root must be a real directory: {root}")
    else:
        root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True)
    try:
        os.chmod(root, _PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise ScheduleRunnerError(f"could not restrict schedule runtime permissions: {root}") from exc
    return root


def _already_processed(path: Path, target: str, minute: str, digest: str) -> bool:
    if not os.path.lexists(path):
        return False
    if path.is_symlink() or not path.is_file():
        raise ScheduleRunnerError(f"last-tick state must be a real regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScheduleRunnerError(f"invalid last-tick state: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "target", "minute", "digest"}:
        raise ScheduleRunnerError(f"invalid last-tick state: {path}")
    stored_minute = payload.get("minute")
    stored_digest = payload.get("digest")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("target") != target
        or not isinstance(stored_minute, str)
        or not _valid_utc_minute(stored_minute)
        or not isinstance(stored_digest, str)
        or _DIGEST.fullmatch(stored_digest) is None
    ):
        raise ScheduleRunnerError(f"invalid last-tick state: {path}")
    return stored_minute == minute and stored_digest == digest


def _valid_utc_minute(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == UTC.utcoffset(parsed)
        and parsed.second == 0
        and parsed.microsecond == 0
        and parsed.isoformat() == value
    )


def _write_last_tick(path: Path, target: str, minute: str, digest: str) -> None:
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise ScheduleRunnerError(f"last-tick state must be a real regular file: {path}")
    payload = {
        "schema_version": 1,
        "target": target,
        "minute": minute,
        "digest": digest,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _PRIVATE_FILE_MODE)
        os.replace(temporary, path)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise ScheduleRunnerError(f"could not update last-tick state: {path}") from exc


class _TargetLock:
    """A non-blocking one-byte/file lock implemented with the standard library."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None
        self._acquired = False

    def __enter__(self) -> bool:
        if os.path.lexists(self._path) and (self._path.is_symlink() or not self._path.is_file()):
            raise ScheduleRunnerError(f"schedule lock must be a real regular file: {self._path}")
        try:
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags, _PRIVATE_FILE_MODE)
            self._stream = os.fdopen(descriptor, "a+b")
            os.chmod(self._path, _PRIVATE_FILE_MODE)
            if os.name == "nt":
                self._acquired = _lock_windows(self._stream)
            else:
                self._acquired = _lock_posix(self._stream)
        except OSError as exc:
            if self._stream is not None:
                self._stream.close()
            raise ScheduleRunnerError(f"could not acquire schedule lock: {self._path}") from exc
        return self._acquired

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is None:
            return
        try:
            if self._acquired:
                if os.name == "nt":
                    _unlock_windows(self._stream)
                else:
                    _unlock_posix(self._stream)
        finally:
            self._stream.close()


def _lock_posix(stream: BinaryIO) -> bool:
    fcntl: Any = importlib.import_module("fcntl")

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_posix(stream: BinaryIO) -> None:
    fcntl: Any = importlib.import_module("fcntl")

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _lock_windows(stream: BinaryIO) -> bool:
    msvcrt: Any = importlib.import_module("msvcrt")

    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock_windows(stream: BinaryIO) -> None:
    msvcrt: Any = importlib.import_module("msvcrt")

    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
