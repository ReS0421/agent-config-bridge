"""Per-file instruction deployment machinery (ADR-5).

Instructions are always-loaded policy files (Claude ``CLAUDE.md``/``rules``/
``agents``/``commands``, Codex ``AGENTS.md``/``agents`` and generated
developer-instruction profiles) deployed from
``catalog/instructions/<bundle>/<product>/`` overlays to overlay-relative paths
under a target ``config_home``. Delivery reuses the standalone Skill link/copy
semantics at file granularity: Linux targets link, Windows targets receive
managed copies, and removal touches only bridge-recorded files.

Content identity normalizes CRLF/CR to LF (like Schedule prompts) so a
line-ending-only difference between checkouts never reads as drift; the
deployed bytes themselves are exact copies of the catalog source.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_config_bridge.catalog import Artifact
from agent_config_bridge.filesystem import FilesystemError
from agent_config_bridge.models import LinkMode, Product
from agent_config_bridge.path_safety import path_comparison_key, read_symlink_target

__all__ = [
    "InstructionFile",
    "apply_instruction_copy",
    "apply_instruction_link",
    "apply_instruction_remove",
    "instruction_digest",
    "instruction_files",
    "instruction_source_id",
    "managed_instruction_directories",
]


@dataclass(frozen=True, slots=True)
class InstructionFile:
    """One catalog instruction file and its destination-relative POSIX path."""

    bundle: str
    relpath: str
    source: Path


def instruction_files(bundle: Artifact, product: Product) -> tuple[InstructionFile, ...]:
    """Enumerate one bundle's files for a product, sorted by relative path."""

    overlay = bundle.path / product.value
    if not overlay.is_dir():
        return ()
    files = sorted(
        (path for path in overlay.rglob("*") if not path.is_dir()),
        key=lambda path: path.relative_to(overlay).as_posix(),
    )
    return tuple(
        InstructionFile(
            bundle=bundle.name,
            relpath=path.relative_to(overlay).as_posix(),
            source=path,
        )
        for path in files
    )


def instruction_source_id(bundle_name: str) -> str:
    """Return the governance artifact ref that owns one bundle's files."""

    return f"instructions/{bundle_name}"


def instruction_digest(path: Path) -> str:
    """Digest one instruction file with CRLF/CR normalized to LF."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FilesystemError(f"cannot read instruction file for digesting: {path}: {exc}") from exc
    return hashlib.sha256(_normalize_newlines(data)).hexdigest()


def managed_instruction_directories(relpaths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the sorted top-level directories holding nested instruction files.

    Root-level single files (``AGENTS.md``, ``CLAUDE.md``) intentionally yield
    nothing: per ADR-5 they carry no provenance marker because the config home
    as a whole is not bridge-owned.
    """

    return tuple(sorted({parts[0] for relpath in relpaths if len(parts := PurePosixPath(relpath).parts) > 1}))


def apply_instruction_link(source: Path, destination: Path) -> None:
    """Create a file symlink without replacing existing content."""

    if os.path.lexists(destination):
        raise FilesystemError(f"refusing to replace existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(source, target_is_directory=False)
    except OSError as exc:
        raise FilesystemError(f"failed to create symlink {destination} -> {source}: {exc}") from exc


def apply_instruction_copy(
    source: Path,
    destination: Path,
    *,
    source_digest: str,
    installed_digest: str | None,
    state_dir: Path,
    target_name: str,
    relpath: str,
    update: bool,
) -> Path | None:
    """Create or safely update one managed instruction file copy.

    Updates are staged next to the destination, swapped on the same
    filesystem, and the previous managed file is retained below the bridge
    state directory.

    Returns:
        The backup path for an update, otherwise ``None``.
    """

    if instruction_digest(source) != source_digest:
        raise FilesystemError(f"canonical source changed after planning: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if instruction_digest(temporary) != source_digest:
            raise FilesystemError(f"staged copy does not match its planned source: {source}")
    except FilesystemError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"failed to stage managed copy {source} -> {destination}: {exc}") from exc

    if not update:
        if os.path.lexists(destination):
            temporary.unlink(missing_ok=True)
            raise FilesystemError(f"refusing to replace existing destination: {destination}")
        os.replace(temporary, destination)
        return None

    if destination.is_symlink() or not destination.is_file():
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed update destination is no longer a regular file: {destination}")
    if installed_digest is None or instruction_digest(destination) != installed_digest:
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")

    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    os.replace(destination, displaced)
    try:
        os.replace(temporary, destination)
    except OSError:
        os.replace(displaced, destination)
        temporary.unlink(missing_ok=True)
        raise

    backup = _backup_path(state_dir, target_name, relpath)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(displaced), str(backup))
    except (OSError, shutil.Error) as exc:
        raise FilesystemError(
            f"updated instruction file is installed but its backup could not be retained at {backup}: {exc}"
        ) from exc
    return backup


def apply_instruction_remove(
    destination: Path,
    *,
    mode: LinkMode,
    expected_link_target: Path | None,
    installed_digest: str | None,
    state_dir: Path,
    target_name: str,
    relpath: str,
    windows_path_semantics: bool,
) -> Path | None:
    """Remove only a still-matching bridge-managed instruction file.

    Symlinks are unlinked. Managed copies are retained in the backup tree.
    """

    if mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or expected_link_target is None:
            raise FilesystemError(f"managed instruction link changed after planning: {destination}")
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
            raise FilesystemError(f"managed instruction link target changed after planning: {destination}")
        destination.unlink()
        return None

    if mode is not LinkMode.COPY or destination.is_symlink() or not destination.is_file():
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")
    if installed_digest is None or instruction_digest(destination) != installed_digest:
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")

    backup = _backup_path(state_dir, target_name, relpath)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(destination), str(backup))
    except (OSError, shutil.Error) as exc:
        raise FilesystemError(f"could not retain deselected instruction backup at {backup}: {exc}") from exc
    return backup


def _normalize_newlines(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _backup_path(state_dir: Path, target_name: str, relpath: str) -> Path:
    backup = (
        state_dir
        / "backups"
        / target_name
        / "instructions"
        / Path(*PurePosixPath(relpath).parts)
        / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    try:
        backup.parent.resolve().relative_to(state_dir.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise FilesystemError(f"backup path escapes configured state directory: {backup}") from exc
    return backup
