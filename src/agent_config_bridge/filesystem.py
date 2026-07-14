"""Safe filesystem inspection and application primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from agent_config_bridge.models import LinkMode
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "MANAGED_MARKER",
    "FilesystemError",
    "apply_copy",
    "apply_link",
    "apply_remove",
    "read_managed_marker",
    "tree_digest",
]

MANAGED_MARKER = ".agent-config-bridge.json"


class FilesystemError(RuntimeError):
    """Raised when a filesystem operation cannot be completed safely."""


def tree_digest(root: Path) -> str:
    """Compute a deterministic digest of a file or directory tree.

    The bridge ownership marker is excluded so copied trees can be compared with
    their canonical sources.
    """

    try:
        if is_directory_reparse_point(root):
            raise FilesystemError(f"cannot digest directory reparse point: {root}")
    except OSError as exc:
        raise FilesystemError(f"cannot inspect path before digesting it: {root}: {exc}") from exc

    digest = hashlib.sha256()
    if root.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.readlink(root).encode())
        return digest.hexdigest()
    if root.is_file():
        digest.update(b"file\0")
        digest.update(root.read_bytes())
        return digest.hexdigest()
    if not root.is_dir():
        raise FilesystemError(f"cannot digest missing or unsupported path: {root}")

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            if is_directory_reparse_point(path):
                raise FilesystemError(f"cannot digest directory reparse point: {path}")
        except OSError as exc:
            raise FilesystemError(f"cannot inspect path before digesting it: {path}: {exc}") from exc
        relative = path.relative_to(root)
        if relative.as_posix() == MANAGED_MARKER:
            try:
                marker_status = path.lstat()
            except OSError as exc:
                raise FilesystemError(f"cannot inspect managed marker before digesting it: {path}: {exc}") from exc
            marker_attributes = getattr(marker_status, "st_file_attributes", 0)
            if not stat.S_ISREG(marker_status.st_mode) or marker_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise FilesystemError(f"managed marker is not a real regular file: {path}")
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
        else:
            raise FilesystemError(f"cannot digest unsupported filesystem node: {path}")
    return digest.hexdigest()


def read_managed_marker(destination: Path) -> dict[str, Any] | None:
    """Read a bridge copy marker, returning ``None`` when it is absent or invalid."""

    marker = destination / MANAGED_MARKER
    try:
        marker_status = marker.lstat()
    except OSError:
        return None
    marker_attributes = getattr(marker_status, "st_file_attributes", 0)
    if not stat.S_ISREG(marker_status.st_mode) or marker_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("source_id"), str)
        or not payload["source_id"]
        or not isinstance(payload.get("installed_digest"), str)
        or not payload["installed_digest"]
    ):
        return None
    return payload


def apply_link(source: Path, destination: Path) -> None:
    """Create a directory symlink without replacing existing content."""

    if os.path.lexists(destination):
        raise FilesystemError(f"refusing to replace existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        raise FilesystemError(f"failed to create symlink {destination} -> {source}: {exc}") from exc


def apply_copy(
    source: Path,
    destination: Path,
    *,
    source_id: str,
    source_digest: str,
    state_dir: Path,
    target_name: str,
    update: bool,
) -> Path | None:
    """Create or safely update a managed directory copy.

    Updates are staged next to the destination, swapped on the same filesystem,
    and the previous managed copy is retained below the bridge state directory.

    Returns:
        The backup path for an update, otherwise ``None``.
    """

    if tree_digest(source) != source_digest:
        raise FilesystemError(f"canonical source changed after planning: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(source, temporary, symlinks=True)
        if tree_digest(temporary) != source_digest:
            raise FilesystemError(f"staged copy does not match its planned source: {source}")
        _write_marker(temporary, source_id=source_id, digest=source_digest)
    except FilesystemError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise FilesystemError(f"failed to stage managed copy {source} -> {destination}: {exc}") from exc

    if not update:
        if os.path.lexists(destination):
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"refusing to replace existing destination: {destination}")
        os.replace(temporary, destination)
        return None

    if not destination.is_dir() or destination.is_symlink() or is_directory_reparse_point(destination):
        shutil.rmtree(temporary, ignore_errors=True)
        raise FilesystemError(f"managed update destination is no longer a directory: {destination}")

    marker = read_managed_marker(destination)
    if marker is None or marker.get("source_id") != source_id:
        shutil.rmtree(temporary, ignore_errors=True)
        raise FilesystemError(f"managed copy ownership changed after planning: {destination}")
    installed_digest = marker.get("installed_digest")
    if not isinstance(installed_digest, str) or tree_digest(destination) != installed_digest:
        shutil.rmtree(temporary, ignore_errors=True)
        raise FilesystemError(f"managed copy changed after planning: {destination}")

    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    os.replace(destination, displaced)
    try:
        os.replace(temporary, destination)
    except OSError:
        os.replace(displaced, destination)
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    backup = _backup_path(state_dir, target_name, destination.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(displaced), str(backup))
    except (OSError, shutil.Error) as exc:
        raise FilesystemError(
            f"updated Skill is installed but its backup could not be retained at {backup}: {exc}"
        ) from exc
    return backup


def apply_remove(
    destination: Path,
    *,
    mode: LinkMode,
    source_id: str,
    expected_link_target: Path | None,
    installed_digest: str | None,
    state_dir: Path,
    target_name: str,
) -> Path | None:
    """Remove only a still-matching bridge-managed Skill.

    Symlinks are unlinked. Managed copies are retained in the backup tree.
    """

    if mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or expected_link_target is None:
            raise FilesystemError(f"managed Skill link changed after planning: {destination}")
        actual_target = Path(os.path.abspath(destination.parent / os.readlink(destination)))
        if actual_target != expected_link_target:
            raise FilesystemError(f"managed Skill link target changed after planning: {destination}")
        destination.unlink()
        return None

    if (
        mode is not LinkMode.COPY
        or destination.is_symlink()
        or not destination.is_dir()
        or is_directory_reparse_point(destination)
    ):
        raise FilesystemError(f"managed Skill copy changed after planning: {destination}")
    marker = read_managed_marker(destination)
    if marker is None or marker.get("source_id") != source_id:
        raise FilesystemError(f"managed Skill copy ownership changed after planning: {destination}")
    marker_digest = marker.get("installed_digest")
    if (
        not isinstance(marker_digest, str)
        or installed_digest != marker_digest
        or tree_digest(destination) != marker_digest
    ):
        raise FilesystemError(f"managed Skill copy changed after planning: {destination}")

    backup = _backup_path(state_dir, target_name, destination.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(destination), str(backup))
    except (OSError, shutil.Error) as exc:
        raise FilesystemError(f"could not retain deselected Skill backup at {backup}: {exc}") from exc
    return backup


def _write_marker(destination: Path, *, source_id: str, digest: str) -> None:
    payload = {
        "schema_version": 1,
        "source_id": source_id,
        "installed_digest": digest,
    }
    (destination / MANAGED_MARKER).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _backup_path(state_dir: Path, target_name: str, name: str) -> Path:
    backup = state_dir / "backups" / target_name / name / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        backup.parent.resolve().relative_to(state_dir.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise FilesystemError(f"backup path escapes configured state directory: {backup}") from exc
    return backup
