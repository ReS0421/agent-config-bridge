"""Immutable per-target schedule snapshots below bridge-owned state."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_config_bridge.models import BridgeConfig, TargetConfig
from agent_config_bridge.path_safety import is_directory_reparse_point
from agent_config_bridge.schedules import (
    ScheduleCatalog,
    TargetScheduleSnapshot,
    render_target_snapshots,
)

__all__ = [
    "RenderedScheduleSet",
    "ScheduleStoreError",
    "read_schedule_set",
    "remove_schedule_set",
    "render_schedule_set",
    "schedule_publish_path",
    "schedule_set_digest",
    "schedule_set_is_current",
]

_SNAPSHOT_SCHEMA_VERSION = 1
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class ScheduleStoreError(RuntimeError):
    """Raised when generated schedule state is unsafe or corrupted."""


@dataclass(frozen=True, slots=True)
class RenderedScheduleSet:
    """One validated immutable schedule build and its stable pointer."""

    target: str
    digest: str
    build_file: Path
    published_file: Path
    schedules: tuple[TargetScheduleSnapshot, ...]


def schedule_publish_path(config: BridgeConfig, target: TargetConfig) -> Path:
    """Return the stable per-target pointer read by schedule ticks."""

    return config.state_dir / "schedules" / f"{target.name}.json"


def schedule_set_digest(catalog: ScheduleCatalog, target: TargetConfig) -> str:
    """Return the deterministic digest for one target's rendered schedules."""

    snapshots = render_target_snapshots(catalog, target)
    payload = _snapshot_payload(target, snapshots)
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def schedule_set_is_current(config: BridgeConfig, catalog: ScheduleCatalog, target: TargetConfig) -> bool:
    """Return whether the stable target pointer selects the desired snapshot."""

    published = schedule_publish_path(config, target)
    if not os.path.lexists(published):
        return False
    current = read_schedule_set(config, target)
    return current is not None and current.digest == schedule_set_digest(catalog, target)


def render_schedule_set(
    config: BridgeConfig,
    catalog: ScheduleCatalog,
    target: TargetConfig,
    *,
    expected_digest: str | None = None,
) -> RenderedScheduleSet:
    """Build and publish an immutable target-native schedule snapshot."""

    snapshots = render_target_snapshots(catalog, target)
    if not snapshots:
        raise ScheduleStoreError(f"cannot render an empty schedule set for target {target.name!r}")
    payload = _snapshot_payload(target, snapshots)
    snapshot_bytes = _json_bytes(payload)
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise ScheduleStoreError(f"schedule sources changed after planning for target {target.name!r}")
    build_group = config.state_dir / "schedule-builds"
    _ensure_private_state_directory(config, build_group, "schedule builds directory")
    build_root = build_group / digest / target.name
    build_file = build_root / "snapshot.json"
    _validate_state_directory(config, build_root, "schedule build")

    if os.path.lexists(build_root):
        _read_snapshot_file(build_file, target, digest)
        _set_private_mode(build_root, _PRIVATE_DIRECTORY_MODE, "schedule build")
        _set_private_mode(build_file, _PRIVATE_FILE_MODE, "schedule snapshot")
    else:
        _ensure_private_state_directory(config, build_root.parent, "schedule build digest root")
        temporary = build_root.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            temporary_file = temporary / "snapshot.json"
            _write_private_bytes(temporary_file, snapshot_bytes)
            _read_snapshot_file(temporary_file, target, digest, require_target_parent=False)
            os.replace(temporary, build_root)
            _read_snapshot_file(build_file, target, digest)
        except Exception:
            if temporary.is_dir():
                for child in temporary.iterdir():
                    child.unlink(missing_ok=True)
                temporary.rmdir()
            raise

    published = schedule_publish_path(config, target)
    _validate_state_file(config, published, "published schedule pointer")
    if os.path.lexists(published):
        read_schedule_set(config, target)
    pointer = {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "target": target.name,
        "digest": digest,
        "build_file": str(build_file),
    }
    _ensure_private_state_directory(config, published.parent, "published schedules directory")
    temporary_pointer = published.with_name(f".{published.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_private_bytes(temporary_pointer, _json_bytes(pointer))
        os.replace(temporary_pointer, published)
    except OSError:
        temporary_pointer.unlink(missing_ok=True)
        raise

    return RenderedScheduleSet(
        target=target.name,
        digest=digest,
        build_file=build_file,
        published_file=published,
        schedules=snapshots,
    )


def read_schedule_set(config: BridgeConfig, target: TargetConfig) -> RenderedScheduleSet | None:
    """Read and integrity-check the currently published target snapshot."""

    published = schedule_publish_path(config, target)
    if not os.path.lexists(published):
        return None
    _validate_state_file(config, published, "published schedule pointer")
    try:
        pointer = json.loads(published.read_text(encoding="utf-8"))
        if (
            not isinstance(pointer, dict)
            or pointer.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION
            or pointer.get("target") != target.name
            or not isinstance(pointer.get("digest"), str)
            or not isinstance(pointer.get("build_file"), str)
        ):
            raise ValueError("invalid pointer fields")
        digest = pointer["digest"]
        build_file = Path(pointer["build_file"])
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ScheduleStoreError(f"invalid published schedule pointer: {published}") from exc

    expected = config.state_dir / "schedule-builds" / digest / target.name / "snapshot.json"
    if build_file != expected:
        raise ScheduleStoreError(f"published schedule pointer selects an unexpected build path: {published}")
    snapshots = _read_snapshot_file(build_file, target, digest)
    return RenderedScheduleSet(
        target=target.name,
        digest=digest,
        build_file=build_file,
        published_file=published,
        schedules=snapshots,
    )


def remove_schedule_set(config: BridgeConfig, target: TargetConfig) -> None:
    """Remove only a valid bridge-generated stable schedule pointer."""

    published = schedule_publish_path(config, target)
    if not os.path.lexists(published):
        return
    read_schedule_set(config, target)
    try:
        published.unlink()
        published.parent.rmdir()
    except OSError as exc:
        if published.exists():
            raise ScheduleStoreError(f"could not remove published schedule pointer: {published}") from exc


def _snapshot_payload(
    target: TargetConfig,
    snapshots: tuple[TargetScheduleSnapshot, ...],
) -> dict[str, Any]:
    return {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "target": {
            "name": target.name,
            "product": target.product.value,
            "user_home": str(target.user_home.resolve()),
            "config_home": str(target.config_home.resolve(strict=False)),
        },
        "schedules": [
            {
                "name": snapshot.schedule_name,
                "cron": snapshot.cron,
                "timezone": snapshot.timezone,
                "working_directory": str(snapshot.working_directory),
                "timeout_seconds": snapshot.timeout_seconds,
                "prompt": snapshot.prompt,
            }
            for snapshot in snapshots
        ],
    }


def _read_snapshot_file(
    path: Path,
    target: TargetConfig,
    expected_digest: str,
    *,
    require_target_parent: bool = True,
) -> tuple[TargetScheduleSnapshot, ...]:
    _validate_state_file_for_target(
        path,
        target,
        "schedule snapshot",
        require_target_parent=require_target_parent,
    )
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError("digest mismatch")
        payload = json.loads(raw)
        raw_target = payload["target"]
        raw_schedules = payload["schedules"]
        if payload["schema_version"] != _SNAPSHOT_SCHEMA_VERSION or not isinstance(raw_target, dict):
            raise ValueError("schema mismatch")
        expected_target = {
            "name": target.name,
            "product": target.product.value,
            "user_home": str(target.user_home.resolve()),
            "config_home": str(target.config_home.resolve(strict=False)),
        }
        if raw_target != expected_target or not isinstance(raw_schedules, list):
            raise ValueError("target mismatch")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ScheduleStoreError(f"invalid immutable schedule snapshot: {path}") from exc

    snapshots: list[TargetScheduleSnapshot] = []
    names: set[str] = set()
    for item in raw_schedules:
        if not isinstance(item, dict):
            raise ScheduleStoreError(f"invalid schedule entry in immutable snapshot: {path}")
        try:
            name = item["name"]
            cron = item["cron"]
            timezone = item["timezone"]
            working_directory = Path(item["working_directory"])
            timeout_seconds = item["timeout_seconds"]
            prompt = item["prompt"]
        except (KeyError, TypeError) as exc:
            raise ScheduleStoreError(f"invalid schedule entry in immutable snapshot: {path}") from exc
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(cron, str)
            or not isinstance(timezone, str)
            or type(timeout_seconds) is not int
            or not isinstance(prompt, str)
        ):
            raise ScheduleStoreError(f"invalid schedule entry in immutable snapshot: {path}")
        names.add(name)
        snapshots.append(
            TargetScheduleSnapshot(
                schema_version=_SNAPSHOT_SCHEMA_VERSION,
                schedule_name=name,
                target_name=target.name,
                product=target.product,
                user_home=target.user_home.resolve(),
                cron=cron,
                timezone=timezone,
                working_directory=working_directory,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
            )
        )
    return tuple(snapshots)


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _validate_state_directory(config: BridgeConfig, path: Path, label: str) -> None:
    state_root = config.state_dir.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleStoreError(f"{label} escapes configured state_dir: {path}") from exc
    if not os.path.lexists(path):
        return
    try:
        redirected = path.is_symlink() or is_directory_reparse_point(path)
    except OSError as exc:
        raise ScheduleStoreError(f"cannot inspect {label}: {path}") from exc
    if redirected or not path.is_dir():
        raise ScheduleStoreError(f"{label} must be a real directory: {path}")


def _validate_state_file(config: BridgeConfig, path: Path, label: str) -> None:
    state_root = config.state_dir.resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleStoreError(f"{label} escapes configured state_dir: {path}") from exc
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise ScheduleStoreError(f"{label} must be a real regular file: {path}")


def _validate_state_file_for_target(
    path: Path,
    target: TargetConfig,
    label: str,
    *,
    require_target_parent: bool = True,
) -> None:
    expected_parent = path.parent
    state_root = expected_parent.parents[2] if len(expected_parent.parents) >= 3 else expected_parent
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleStoreError(f"{label} escapes bridge schedule builds: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise ScheduleStoreError(f"{label} must be a real regular file: {path}")
    if require_target_parent and path.parent.name != target.name:
        raise ScheduleStoreError(f"{label} target path does not match {target.name!r}: {path}")


def _ensure_private_state_directory(config: BridgeConfig, path: Path, label: str) -> None:
    _validate_state_directory(config, path, label)
    try:
        path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise ScheduleStoreError(f"could not create {label}: {path}") from exc
    _validate_state_directory(config, path, label)
    _set_private_mode(path, _PRIVATE_DIRECTORY_MODE, label)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _set_private_mode(path, _PRIVATE_FILE_MODE, "staged schedule state")
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise ScheduleStoreError(f"could not stage private schedule state: {path}") from exc


def _set_private_mode(path: Path, mode: int, label: str) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ScheduleStoreError(f"could not restrict {label} permissions: {path}") from exc
