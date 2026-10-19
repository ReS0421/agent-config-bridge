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
from agent_config_bridge.path_safety import (
    is_directory_reparse_point,
    path_comparison_key,
    read_symlink_target,
)

__all__ = [
    "MANAGED_MARKER",
    "FilesystemError",
    "apply_copy",
    "apply_link",
    "apply_remove",
    "is_bytecode_cache_file",
    "managed_copy_digest_matches",
    "read_managed_marker",
    "tree_digest",
]

MANAGED_MARKER = ".agent-config-bridge.json"


class FilesystemError(RuntimeError):
    """Raised when a filesystem operation cannot be completed safely."""


_BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})
_BYTECODE_DIRECTORY = "__pycache__"


def _is_real_regular_file(path: Path) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISREG(status.st_mode) and not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT


def is_bytecode_cache_file(path: Path) -> bool:
    """Return whether ``path`` is a regular bytecode file CPython writes itself.

    Only a real (non-symlink) ``.pyc``/``.pyo`` file whose immediate parent is a
    directory named exactly ``__pycache__`` qualifies. Sourceless ``.pyc`` files
    elsewhere are importable code and are never excluded.
    """

    return (
        path.parent.name == _BYTECODE_DIRECTORY
        and path.suffix.casefold() in _BYTECODE_SUFFIXES
        and _is_real_regular_file(path)
    )


def is_pure_bytecode_cache_directory(path: Path) -> bool:
    """Return whether ``path`` is a real ``__pycache__`` directory holding only cache files."""

    if path.name != _BYTECODE_DIRECTORY:
        return False
    try:
        if path.is_symlink() or not path.is_dir() or is_directory_reparse_point(path):
            return False
        with os.scandir(path) as entries:
            return all(is_bytecode_cache_file(path / entry.name) for entry in entries)
    except OSError:
        return False


def _ignore_bytecode_caches(directory: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ignore callback that skips only CPython-written caches."""

    parent = Path(directory)
    return {
        name
        for name in names
        if is_bytecode_cache_file(parent / name) or is_pure_bytecode_cache_directory(parent / name)
    }


def managed_copy_digest_matches(destination: Path, installed_digest: str) -> bool:
    """Compare a live installed Skill copy with its recorded digest.

    Bytecode caches are ignored, but a recorded digest that covers them (copies
    made before caches were excluded) still verifies against the strict digest.
    """

    return (
        tree_digest(destination, ignore_bytecode_caches=True) == installed_digest
        or tree_digest(destination) == installed_digest
    )


def tree_digest(root: Path, *, ignore_bytecode_caches: bool = False) -> str:
    """Compute a deterministic digest of a file or directory tree.

    The bridge ownership marker is excluded so copied trees can be compared with
    their canonical sources. With ``ignore_bytecode_caches`` (live installed
    Skill copies and Skill sources only), regular ``.pyc``/``.pyo`` files
    directly inside a ``__pycache__`` directory, and a ``__pycache__`` directory
    holding nothing else, are also excluded. Everything else is digested.
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
        if ignore_bytecode_caches and (is_bytecode_cache_file(path) or is_pure_bytecode_cache_directory(path)):
            continue
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
    previous_mode: LinkMode = LinkMode.COPY,
    expected_link_target: Path | None = None,
    windows_path_semantics: bool = False,
) -> Path | None:
    """Create or safely update a managed directory copy.

    Updates are staged next to the destination, swapped on the same filesystem,
    and the previous managed copy is retained below the bridge state directory.

    Returns:
        The backup path for an update, otherwise ``None``.
    """

    if tree_digest(source, ignore_bytecode_caches=True) != source_digest:
        raise FilesystemError(f"canonical source changed after planning: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copytree(source, temporary, symlinks=True, ignore=_ignore_bytecode_caches)
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

    backup_digest = source_digest
    if previous_mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or expected_link_target is None:
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"managed Skill link changed after planning: {destination}")
        try:
            actual_target = read_symlink_target(destination)
            target_matches = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                expected_link_target,
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            target_matches = False
        if not target_matches:
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"managed Skill link target changed after planning: {destination}")
    elif previous_mode is LinkMode.COPY:
        if not destination.is_dir() or destination.is_symlink() or is_directory_reparse_point(destination):
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"managed update destination is no longer a directory: {destination}")

        marker = read_managed_marker(destination)
        if marker is None or marker.get("source_id") != source_id:
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"managed copy ownership changed after planning: {destination}")
        installed_digest = marker.get("installed_digest")
        if not isinstance(installed_digest, str) or not managed_copy_digest_matches(destination, installed_digest):
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(f"managed copy changed after planning: {destination}")
        backup_digest = installed_digest
    else:  # pragma: no cover - LinkMode is currently exhaustive
        shutil.rmtree(temporary, ignore_errors=True)
        raise FilesystemError(f"unsupported previous managed mode: {previous_mode.value}")

    backup = _backup_path(state_dir, target_name, destination.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        _snapshot_managed_destination(
            destination,
            backup,
            mode=previous_mode,
            source_id=source_id,
            installed_digest=backup_digest,
            expected_link_target=expected_link_target,
            windows_path_semantics=windows_path_semantics,
        )
    except (FilesystemError, OSError, shutil.Error) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if os.path.lexists(backup):
            _remove_path(backup)
        raise FilesystemError(f"could not snapshot managed Skill backup at {backup}: {exc}") from exc

    try:
        _validate_managed_destination(
            destination,
            mode=previous_mode,
            source_id=source_id,
            installed_digest=backup_digest,
            expected_link_target=expected_link_target,
            windows_path_semantics=windows_path_semantics,
        )
    except FilesystemError:
        shutil.rmtree(temporary, ignore_errors=True)
        _remove_path(backup)
        raise

    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    try:
        os.replace(destination, displaced)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        _remove_path(backup)
        raise FilesystemError(f"could not stage managed Skill destination swap at {destination}: {exc}") from exc

    try:
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.replace(displaced, destination)
        except OSError as restore_exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise FilesystemError(
                f"new Skill install failed and same-filesystem swap could not be restored at "
                f"{destination}: {restore_exc}"
            ) from exc
        shutil.rmtree(temporary, ignore_errors=True)
        _remove_path(backup)
        raise FilesystemError(f"failed to install staged managed Skill copy at {destination}: {exc}") from exc

    try:
        _remove_path(displaced)
    except OSError as exc:
        raise FilesystemError(
            f"updated Skill and retained backup are valid but old same-filesystem swap could not be removed: "
            f"{displaced}: {exc}"
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
    windows_path_semantics: bool,
) -> Path | None:
    """Remove only a still-matching bridge-managed Skill.

    Symlinks are unlinked. Managed copies are retained in the backup tree.
    """

    if mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or expected_link_target is None:
            raise FilesystemError(f"managed Skill link changed after planning: {destination}")
        try:
            actual_target = read_symlink_target(destination)
            expected_target = expected_link_target
            target_matches = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                expected_target,
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            target_matches = False
        if not target_matches:
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
        or not managed_copy_digest_matches(destination, marker_digest)
    ):
        raise FilesystemError(f"managed Skill copy changed after planning: {destination}")

    backup = _backup_path(state_dir, target_name, destination.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        _snapshot_managed_destination(
            destination,
            backup,
            mode=LinkMode.COPY,
            source_id=source_id,
            installed_digest=marker_digest,
            expected_link_target=None,
            windows_path_semantics=windows_path_semantics,
        )
    except (FilesystemError, OSError, shutil.Error) as exc:
        if os.path.lexists(backup):
            _remove_path(backup)
        raise FilesystemError(f"could not snapshot deselected Skill backup at {backup}: {exc}") from exc

    try:
        _validate_managed_destination(
            destination,
            mode=LinkMode.COPY,
            source_id=source_id,
            installed_digest=marker_digest,
            expected_link_target=None,
            windows_path_semantics=windows_path_semantics,
        )
    except FilesystemError:
        _remove_path(backup)
        raise

    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    try:
        os.replace(destination, displaced)
    except OSError as exc:
        _remove_path(backup)
        raise FilesystemError(f"could not stage deselected Skill removal at {destination}: {exc}") from exc
    try:
        _remove_path(displaced)
    except OSError as exc:
        try:
            _restore_copy_backup_to_destination(
                backup,
                destination,
                source_id=source_id,
                installed_digest=marker_digest,
            )
        except (FilesystemError, OSError, shutil.Error) as restore_exc:
            raise FilesystemError(
                f"could not remove deselected Skill; verified backup is retained at {backup}, "
                f"damaged swap remains at {displaced}, and destination restore failed: {restore_exc}"
            ) from exc
        try:
            _remove_path(displaced)
        except OSError as cleanup_exc:
            raise FilesystemError(
                f"could not remove deselected Skill; destination was restored from verified backup {backup}, "
                f"but orphaned swap remains at {displaced}: {cleanup_exc}"
            ) from exc
        raise FilesystemError(
            f"could not remove deselected Skill; destination was restored and verified from retained backup {backup}"
        ) from exc
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


def _validate_managed_destination(
    destination: Path,
    *,
    mode: LinkMode,
    source_id: str,
    installed_digest: str,
    expected_link_target: Path | None,
    windows_path_semantics: bool,
    live: bool = True,
) -> None:
    """Revalidate reviewed ownership immediately before destructive mutation."""

    if mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or expected_link_target is None:
            raise FilesystemError(f"managed Skill link changed after backup snapshot: {destination}")
        try:
            actual_target = read_symlink_target(destination)
            target_matches = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                expected_link_target,
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            target_matches = False
        if not target_matches:
            raise FilesystemError(f"managed Skill link target changed after backup snapshot: {destination}")
        return

    if (
        mode is not LinkMode.COPY
        or destination.is_symlink()
        or not destination.is_dir()
        or is_directory_reparse_point(destination)
    ):
        raise FilesystemError(f"managed Skill copy changed after backup snapshot: {destination}")
    marker = read_managed_marker(destination)
    if (
        marker is None
        or marker.get("source_id") != source_id
        or marker.get("installed_digest") != installed_digest
        or not (
            managed_copy_digest_matches(destination, installed_digest)
            if live
            else tree_digest(destination) == installed_digest
        )
    ):
        raise FilesystemError(f"managed Skill copy content changed after backup snapshot: {destination}")


def _restore_copy_backup_to_destination(
    backup: Path,
    destination: Path,
    *,
    source_id: str,
    installed_digest: str,
) -> None:
    """Reconstruct and atomically install a verified copy from retained backup."""

    _validate_managed_destination(
        backup,
        mode=LinkMode.COPY,
        source_id=source_id,
        installed_digest=installed_digest,
        expected_link_target=None,
        windows_path_semantics=False,
        live=False,
    )
    if os.path.lexists(destination):
        raise FilesystemError(f"cannot restore managed Skill over an existing destination: {destination}")
    restore = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.restore.tmp")
    try:
        shutil.copytree(backup, restore, symlinks=True)
        _validate_managed_destination(
            restore,
            mode=LinkMode.COPY,
            source_id=source_id,
            installed_digest=installed_digest,
            expected_link_target=None,
            windows_path_semantics=False,
            live=False,
        )
        os.replace(restore, destination)
    except (FilesystemError, OSError, shutil.Error):
        if os.path.lexists(restore):
            _remove_path(restore)
        raise


def _snapshot_managed_destination(
    destination: Path,
    backup: Path,
    *,
    mode: LinkMode,
    source_id: str,
    installed_digest: str,
    expected_link_target: Path | None,
    windows_path_semantics: bool,
) -> None:
    """Create and verify a non-destructive final backup snapshot."""

    if mode is LinkMode.SYMLINK:
        if expected_link_target is None:
            raise FilesystemError(f"managed Skill link backup has no expected target: {destination}")
        actual_target = read_symlink_target(destination)
        backup.symlink_to(actual_target, target_is_directory=True)
        backup_target = read_symlink_target(backup)
        if path_comparison_key(backup_target, windows=windows_path_semantics) != path_comparison_key(
            expected_link_target,
            windows=windows_path_semantics,
        ):
            raise FilesystemError(f"managed Skill link backup target verification failed: {backup}")
        return

    if mode is not LinkMode.COPY:
        raise FilesystemError(f"unsupported managed Skill backup mode: {mode.value}")
    # Backups of a copy whose recorded digest ignores caches omit them; a legacy
    # copy whose recorded digest covers them is preserved exactly as recorded.
    ignore = (
        _ignore_bytecode_caches if tree_digest(destination, ignore_bytecode_caches=True) == installed_digest else None
    )
    shutil.copytree(destination, backup, symlinks=True, ignore=ignore)
    marker = read_managed_marker(backup)
    if (
        marker is None
        or marker.get("source_id") != source_id
        or marker.get("installed_digest") != installed_digest
        or tree_digest(backup) != installed_digest
    ):
        raise FilesystemError(f"managed Skill copy backup verification failed: {backup}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _backup_path(state_dir: Path, target_name: str, name: str) -> Path:
    backup = state_dir / "backups" / target_name / name / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        backup.parent.resolve().relative_to(state_dir.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise FilesystemError(f"backup path escapes configured state directory: {backup}") from exc
    return backup
