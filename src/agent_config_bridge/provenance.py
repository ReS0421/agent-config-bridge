"""Visible root-level provenance markers for bridge-managed runtime roots.

The hidden per-skill copy marker (``filesystem.MANAGED_MARKER``) exists for the
bridge's own update/removal safety. This module solves a different problem:
a human or agent listing a deployed skill root has no way to tell that the
directory is a managed projection of one canonical catalog, so parallel roots
on other hosts or products read as accidental duplication. The root marker is
deliberately visible and self-explanatory so the directory answers that
question by itself.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_config_bridge.catalog import Artifact
from agent_config_bridge.instructions import managed_instruction_directories
from agent_config_bridge.models import BridgeConfig, Component, TargetConfig
from agent_config_bridge.state import BridgeStateError, effective_link_mode, skill_root_for_target

__all__ = [
    "ROOT_MARKER_FILENAME",
    "read_instruction_dir_marker",
    "read_skill_root_marker",
    "remove_skill_root_marker",
    "write_instruction_dir_markers",
    "write_skill_root_marker",
]

ROOT_MARKER_FILENAME = "AGENTBRIDGE-MANAGED.json"

_MANAGED_BY = "agent-config-bridge"
_NOTICE = (
    "This directory is a deployment projection managed by agent-config-bridge.",
    "Skills here are links or managed copies of one canonical catalog; "
    "hand edits are reverted by the next `agentbridge apply`.",
    "Similar-looking skill roots on other hosts or products are projections "
    "of the same catalog, not accidental duplicates.",
    "Review with `agentbridge plan`; ownership records live in the state_dir recorded below.",
)


def write_skill_root_marker(
    config: BridgeConfig,
    target: TargetConfig,
    skills: tuple[Artifact, ...],
) -> Path | None:
    """Write or refresh the visible marker at one target's skill root.

    Returns the marker path, or ``None`` when the target deploys no standalone
    Skills (in which case any stale marker is removed) or the root does not
    exist yet. The root is never created just to hold a marker.

    Raises:
        BridgeStateError: If a symlink squats on the marker path or the write fails.
    """

    if Component.SKILLS not in target.components or not skills:
        _remove_marker_if_owned(target)
        return None
    root = skill_root_for_target(target)
    if not root.is_dir():
        return None
    marker = root / ROOT_MARKER_FILENAME
    if marker.is_symlink():
        raise BridgeStateError(f"refusing to write a provenance marker through a symlink: {marker}")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "managed_by": _MANAGED_BY,
        "role": "deployment-projection",
        "notice": list(_NOTICE),
        "target": target.name,
        "product": target.product.value,
        "platform": target.platform.value,
        "mode": effective_link_mode(config.link_mode, target.platform).value,
        "catalog_root": str(config.catalog.resolve()),
        "state_dir": str(config.state_dir.resolve()),
        "skill_count": len(skills),
        "applied_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
    }
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, marker)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BridgeStateError(f"could not write provenance marker: {marker}: {exc}") from exc
    return marker


def read_skill_root_marker(target: TargetConfig) -> dict[str, Any] | None:
    """Read one target's root marker, or ``None`` when it is absent.

    Raises:
        BridgeStateError: If the marker is a symlink, unreadable, malformed,
            or was not written by the bridge.
    """

    return _read_marker(skill_root_for_target(target) / ROOT_MARKER_FILENAME)


def _read_marker(marker: Path) -> dict[str, Any] | None:
    if not marker.exists():
        if marker.is_symlink():
            raise BridgeStateError(f"provenance marker is a broken symlink: {marker}")
        return None
    if marker.is_symlink() or not marker.is_file():
        raise BridgeStateError(f"provenance marker is not a regular file: {marker}")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeStateError(f"invalid provenance marker: {marker}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("managed_by") != _MANAGED_BY
        or not isinstance(payload.get("target"), str)
    ):
        raise BridgeStateError(f"invalid provenance marker: {marker}")
    return payload


def remove_skill_root_marker(target: TargetConfig) -> None:
    """Remove one target's root marker if present; absent markers are a no-op.

    Raises:
        BridgeStateError: If the marker path is a symlink or cannot be removed.
    """

    marker = skill_root_for_target(target) / ROOT_MARKER_FILENAME
    if marker.is_symlink():
        raise BridgeStateError(f"cannot remove a symlinked provenance marker: {marker}")
    if not marker.is_file():
        return
    try:
        marker.unlink()
    except OSError as exc:
        raise BridgeStateError(f"could not remove provenance marker: {marker}: {exc}") from exc


_INSTRUCTION_NOTICE = (
    "This directory holds instruction files managed by agent-config-bridge.",
    "Managed files here are links or managed copies of one canonical catalog; "
    "hand edits are reverted by the next `agentbridge apply`.",
    "Similar-looking instruction directories on other hosts or products are "
    "projections of the same catalog, not accidental duplicates.",
    "Review with `agentbridge plan`; ownership records live in the state_dir recorded below.",
)


def write_instruction_dir_markers(
    config: BridgeConfig,
    target: TargetConfig,
    relpaths: tuple[str, ...],
    previous_relpaths: tuple[str, ...],
) -> tuple[Path, ...]:
    """Write markers for managed instruction directories; drop stale ones.

    Per ADR-5 only managed *directories* (``rules/``, ``agents/``,
    ``commands/``) carry a marker — root-level single files such as
    ``AGENTS.md`` do not, because the config home as a whole is not
    bridge-owned. Directories this target stopped managing lose their marker
    only when this target wrote it. Directories are never created just to
    hold a marker.

    Raises:
        BridgeStateError: If a symlink squats on a marker path or a write fails.
    """

    current_dirs = managed_instruction_directories(relpaths)
    for stale in set(managed_instruction_directories(previous_relpaths)) - set(current_dirs):
        _remove_instruction_marker_if_owned(target, stale)

    written: list[Path] = []
    # Informational marker metadata only; catalog validation guarantees a
    # root-level file name never collides with a managed directory name.
    file_counts = {
        directory: sum(relpath.split("/", 1)[0] == directory and "/" in relpath for relpath in relpaths)
        for directory in current_dirs
    }
    for directory in current_dirs:
        root = target.config_home / directory
        if not root.is_dir():
            continue
        marker = root / ROOT_MARKER_FILENAME
        if marker.is_symlink():
            raise BridgeStateError(f"refusing to write a provenance marker through a symlink: {marker}")
        payload: dict[str, Any] = {
            "schema_version": 1,
            "managed_by": _MANAGED_BY,
            "role": "deployment-projection",
            "component": "instructions",
            "notice": list(_INSTRUCTION_NOTICE),
            "target": target.name,
            "product": target.product.value,
            "platform": target.platform.value,
            "mode": effective_link_mode(config.link_mode, target.platform).value,
            "catalog_root": str(config.catalog.resolve()),
            "state_dir": str(config.state_dir.resolve()),
            "file_count": file_counts[directory],
            "applied_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, marker)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise BridgeStateError(f"could not write provenance marker: {marker}: {exc}") from exc
        written.append(marker)
    return tuple(written)


def read_instruction_dir_marker(target: TargetConfig, directory: str) -> dict[str, Any] | None:
    """Read one managed instruction directory's marker, or ``None`` when absent.

    Raises:
        BridgeStateError: If the marker is a symlink, unreadable, malformed,
            or was not written by the bridge.
    """

    return _read_marker(target.config_home / directory / ROOT_MARKER_FILENAME)


def _remove_instruction_marker_if_owned(target: TargetConfig, directory: str) -> None:
    try:
        payload = read_instruction_dir_marker(target, directory)
    except BridgeStateError:
        return
    if payload is None or payload["target"] != target.name:
        return
    marker = target.config_home / directory / ROOT_MARKER_FILENAME
    if marker.is_symlink() or not marker.is_file():
        return
    try:
        marker.unlink()
    except OSError as exc:
        raise BridgeStateError(f"could not remove provenance marker: {marker}: {exc}") from exc


def _remove_marker_if_owned(target: TargetConfig) -> None:
    """Remove the root marker only when this target wrote it.

    Multiple targets can deploy into one skill root (e.g. two Codex targets
    sharing a user home). A target that does not manage skills must not clobber
    the marker the managing target just wrote. Markers that cannot be read are
    also left in place for ``doctor`` to report rather than destroyed blindly.
    """

    try:
        payload = read_skill_root_marker(target)
    except BridgeStateError:
        return
    if payload is not None and payload["target"] == target.name:
        remove_skill_root_marker(target)
