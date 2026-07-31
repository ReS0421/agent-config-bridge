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
import re
import shutil
import stat
import time
import tomllib
import uuid
from contextlib import suppress
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
    "codex_profile_allows_runtime_hook_state",
    "inspect_instruction_copy",
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


@dataclass(frozen=True, slots=True)
class InstructionCopyInspection:
    """Validated managed bytes and any opaque product-owned runtime suffix."""

    managed_matches: bool
    runtime_suffix: bytes = b""


@dataclass(frozen=True, slots=True)
class _InstructionCopyObservation:
    """One descriptor-backed observation used for parsing and later checks."""

    inspection: InstructionCopyInspection
    exact_digest: str
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _ExactRegularFileSnapshot:
    """Exact bytes, identity, and mode from one no-follow descriptor read."""

    data: bytes
    identity: tuple[int, int]
    mode: int


_TRUSTED_HOOK_HASH = re.compile(r"sha256:[0-9a-f]{64}")


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


def codex_profile_allows_runtime_hook_state(product: Product, relpath: str) -> bool:
    """Return whether one deployed COPY may carry Codex-owned Hook trust state."""

    path = PurePosixPath(relpath)
    return (
        product is Product.CODEX
        and len(path.parts) == 1
        and path.name != "config.toml"
        and path.name.endswith(".config.toml")
    )


def inspect_instruction_copy(
    path: Path,
    *,
    installed_digest: str,
    allow_runtime_hook_state: bool,
) -> InstructionCopyInspection:
    """Match managed instruction bytes while validating a narrow runtime suffix.

    Generated Catalog profiles remain developer-instruction-only. A deployed
    Codex profile COPY may additionally carry an opaque, provider-owned
    ``[hooks.state]`` suffix. The suffix is accepted only when every child is
    exactly one lowercase SHA-256 ``trusted_hash`` leaf.
    """

    return _observe_instruction_copy(
        path,
        installed_digest=installed_digest,
        allow_runtime_hook_state=allow_runtime_hook_state,
    ).inspection


def _observe_instruction_copy(
    path: Path,
    *,
    installed_digest: str,
    allow_runtime_hook_state: bool,
) -> _InstructionCopyObservation:
    """Bind managed parsing, exact bytes, and identity to one stable read."""

    snapshot = _read_exact_regular_file_snapshot(path)
    return _InstructionCopyObservation(
        inspection=_inspect_instruction_copy_bytes(
            snapshot.data,
            installed_digest=installed_digest,
            allow_runtime_hook_state=allow_runtime_hook_state,
        ),
        exact_digest=hashlib.sha256(snapshot.data).hexdigest(),
        identity=snapshot.identity,
    )


def _inspect_instruction_copy_bytes(
    data: bytes,
    *,
    installed_digest: str,
    allow_runtime_hook_state: bool,
) -> InstructionCopyInspection:
    """Validate managed content and a runtime suffix from exact observed bytes."""

    normalized = _normalize_newlines(data)
    if hashlib.sha256(normalized).hexdigest() == installed_digest:
        return InstructionCopyInspection(managed_matches=True)
    if not allow_runtime_hook_state:
        return InstructionCopyInspection(managed_matches=False)

    for marker_start in _hook_state_marker_offsets(data):
        for managed_end in _possible_managed_ends(data, marker_start):
            managed = _normalize_newlines(data[:managed_end])
            if hashlib.sha256(managed).hexdigest() != installed_digest:
                continue
            suffix = data[managed_end:]
            if _valid_runtime_hook_state_suffix(suffix):
                return InstructionCopyInspection(managed_matches=True, runtime_suffix=suffix)
    return InstructionCopyInspection(managed_matches=False)


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
    allow_runtime_hook_state: bool = False,
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
        if allow_runtime_hook_state and os.name != "nt":
            temporary.chmod(0o600)
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
        _install_staged_copy_without_replacement(temporary, destination)
        return None

    if destination.is_symlink() or not destination.is_file():
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed update destination is no longer a regular file: {destination}")
    if installed_digest is None:
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")
    observation = _observe_instruction_copy(
        destination,
        installed_digest=installed_digest,
        allow_runtime_hook_state=allow_runtime_hook_state,
    )
    if not observation.inspection.managed_matches:
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")
    if observation.inspection.runtime_suffix:
        try:
            with temporary.open("ab") as stream:
                stream.write(observation.inspection.runtime_suffix)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise FilesystemError(f"failed to preserve product-owned Hook state: {destination}: {exc}") from exc
        staged = inspect_instruction_copy(
            temporary,
            installed_digest=source_digest,
            allow_runtime_hook_state=allow_runtime_hook_state,
        )
        if not staged.managed_matches or staged.runtime_suffix != observation.inspection.runtime_suffix:
            temporary.unlink(missing_ok=True)
            raise FilesystemError(f"staged copy did not preserve product-owned Hook state: {destination}")

    if not _exact_regular_file_matches(
        destination,
        expected_digest=observation.exact_digest,
        expected_identity=observation.identity,
    ):
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")

    expected_replacement_digest = _exact_file_digest(temporary)
    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    backup = _backup_path(state_dir, target_name, relpath)
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, displaced)
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed during update validation: {destination}")
        if allow_runtime_hook_state and os.name != "nt":
            _chmod_regular_file_identity(
                displaced,
                expected_identity=observation.identity,
                mode=0o600,
            )
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed during update validation: {destination}")
        _retain_displaced_copy(
            displaced=displaced,
            backup=backup,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        )
        expected_replacement_identity = _install_staged_copy_without_replacement(temporary, destination)
        if not _exact_regular_file_matches(
            destination,
            expected_digest=expected_replacement_digest,
            expected_identity=expected_replacement_identity,
        ):
            raise FilesystemError(f"installed managed instruction replacement changed: {destination}")
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed after replacement install: {destination}")
        if _exact_file_digest(backup) != observation.exact_digest:
            raise FilesystemError(f"managed instruction copy changed during update backup: {destination}")
        if not _exact_regular_file_matches(
            destination,
            expected_digest=expected_replacement_digest,
            expected_identity=expected_replacement_identity,
        ):
            raise FilesystemError(f"installed managed instruction replacement changed: {destination}")
        _remove_displaced_copy(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        )
        if _exact_file_digest(backup) != observation.exact_digest:
            raise FilesystemError(f"managed instruction copy changed during update backup: {destination}")
        if not _exact_regular_file_matches(
            destination,
            expected_digest=expected_replacement_digest,
            expected_identity=expected_replacement_identity,
        ):
            raise FilesystemError(f"installed managed instruction replacement changed: {destination}")
    except FilesystemError:
        _recover_instruction_update(
            destination=destination,
            candidates=(displaced, backup),
        )
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, shutil.Error) as exc:
        _recover_instruction_update(
            destination=destination,
            candidates=(displaced, backup),
        )
        temporary.unlink(missing_ok=True)
        raise FilesystemError(f"failed to swap managed instruction copy at {destination}: {exc}") from exc
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
    allow_runtime_hook_state: bool = False,
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
    if installed_digest is None:
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")
    observation = _observe_instruction_copy(
        destination,
        installed_digest=installed_digest,
        allow_runtime_hook_state=allow_runtime_hook_state,
    )
    if not observation.inspection.managed_matches:
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")
    if not _exact_regular_file_matches(
        destination,
        expected_digest=observation.exact_digest,
        expected_identity=observation.identity,
    ):
        raise FilesystemError(f"managed instruction copy changed after planning: {destination}")

    backup = _backup_path(state_dir, target_name, relpath)
    backup.parent.mkdir(parents=True, exist_ok=True)
    displaced = destination.with_name(f".{destination.name}.agentbridge.{uuid.uuid4().hex}.old")
    try:
        os.replace(destination, displaced)
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed during removal validation: {destination}")
        if allow_runtime_hook_state and os.name != "nt":
            _chmod_regular_file_identity(
                displaced,
                expected_identity=observation.identity,
                mode=0o600,
            )
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed during removal validation: {destination}")
        _retain_displaced_copy(
            displaced=displaced,
            backup=backup,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        )
        if os.path.lexists(destination):
            raise FilesystemError(f"managed instruction copy destination reappeared during removal: {destination}")
        if not _exact_regular_file_matches(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        ):
            raise FilesystemError(f"managed instruction copy changed during removal backup: {destination}")
        if _exact_file_digest(backup) != observation.exact_digest:
            raise FilesystemError(f"managed instruction copy changed during removal backup: {destination}")
        _remove_displaced_copy(
            displaced,
            expected_digest=observation.exact_digest,
            expected_identity=observation.identity,
        )
        if _exact_file_digest(backup) != observation.exact_digest:
            raise FilesystemError(f"managed instruction copy changed during removal backup: {destination}")
        if os.path.lexists(destination):
            raise FilesystemError(f"managed instruction copy destination reappeared during removal: {destination}")
    except FilesystemError:
        _restore_displaced_copy_if_absent(
            destination=destination,
            candidates=(displaced, backup),
        )
        raise
    except (OSError, shutil.Error) as exc:
        _restore_displaced_copy_if_absent(
            destination=destination,
            candidates=(displaced, backup),
        )
        raise FilesystemError(f"could not retain deselected instruction backup at {backup}: {exc}") from exc
    return backup


def _normalize_newlines(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _exact_file_digest(path: Path) -> str:
    """Hash exact bytes for post-displacement change detection."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FilesystemError(f"cannot read instruction file for exact validation: {path}: {exc}") from exc


def _read_exact_regular_file_snapshot(path: Path) -> _ExactRegularFileSnapshot:
    """Read one exact regular-file snapshot with stable path identity."""

    before = _regular_file_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FilesystemError(f"cannot open instruction file for validation: {path}: {exc}") from exc

    try:
        opened = os.fstat(descriptor)
        opened_identity = opened.st_dev, opened.st_ino
        if not stat.S_ISREG(opened.st_mode) or opened_identity != before:
            raise FilesystemError(f"instruction file changed while opening for validation: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after_read.st_mode)
            or (after_read.st_dev, after_read.st_ino) != before
            or after_read.st_size != len(data)
        ):
            raise FilesystemError(f"instruction file changed while reading for validation: {path}")
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError(f"cannot read instruction file for validation: {path}: {exc}") from exc
    finally:
        os.close(descriptor)

    if _regular_file_identity(path) != before:
        raise FilesystemError(f"instruction file changed after validation read: {path}")
    return _ExactRegularFileSnapshot(
        data=data,
        identity=before,
        mode=stat.S_IMODE(opened.st_mode),
    )


def _chmod_regular_file_identity(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    mode: int,
) -> None:
    """Change mode through a no-follow descriptor bound to one expected file."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FilesystemError(f"cannot open managed instruction file for permission repair: {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != expected_identity:
            raise FilesystemError(f"managed instruction file changed before permission repair: {path}")
        os.fchmod(descriptor, mode)
        repaired = os.fstat(descriptor)
        if (
            not stat.S_ISREG(repaired.st_mode)
            or (repaired.st_dev, repaired.st_ino) != expected_identity
            or stat.S_IMODE(repaired.st_mode) != mode
        ):
            raise FilesystemError(f"managed instruction permission repair did not persist: {path}")
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError(f"cannot repair managed instruction permissions: {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    if _regular_file_identity(path) != expected_identity:
        raise FilesystemError(f"managed instruction file changed after permission repair: {path}")


def _install_staged_copy_without_replacement(temporary: Path, destination: Path) -> tuple[int, int]:
    """Install a staged regular file only while the destination remains absent.

    A hard link provides atomic publication when the destination filesystem
    supports it. Filesystems without hard links use an exclusive-create copy;
    final identity and exact-byte validation detect path replacement without
    deleting whatever is found at the active destination.
    """

    staged_identity = _regular_file_identity(temporary)
    staged_digest = _exact_file_digest(temporary)
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise FilesystemError(f"refusing to replace concurrently created destination: {destination}") from exc
    except OSError as link_exc:
        try:
            copied_digest, copied_source_identity, destination_identity = _copy_regular_file_exclusive(
                temporary,
                destination,
            )
        except FileExistsError as exc:
            raise FilesystemError(f"refusing to replace concurrently created destination: {destination}") from exc
        except FilesystemError as exc:
            raise FilesystemError(
                f"failed to install staged managed copy at {destination}; "
                f"hard-link publication failed first: {link_exc}; exclusive copy failed: {exc}"
            ) from exc
        if copied_digest != staged_digest or copied_source_identity != staged_identity:
            raise FilesystemError(f"staged managed copy changed during exclusive install: {temporary}") from link_exc
    else:
        destination_identity = staged_identity

    if not _exact_regular_file_matches(
        destination,
        expected_digest=staged_digest,
        expected_identity=destination_identity,
    ):
        raise FilesystemError(f"installed staged managed copy changed during publication: {destination}")
    with suppress(OSError):
        temporary.unlink()
    return destination_identity


def _regular_file_identity(path: Path) -> tuple[int, int]:
    """Return a regular file's device/inode identity without following links."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FilesystemError(f"cannot inspect managed instruction file identity: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise FilesystemError(f"managed instruction path is not a regular file: {path}")
    return metadata.st_dev, metadata.st_ino


def _exact_regular_file_matches(
    path: Path,
    *,
    expected_digest: str,
    expected_identity: tuple[int, int],
) -> bool:
    """Match exact bytes and path identity across one final validation read."""

    try:
        before = _regular_file_identity(path)
        if before != expected_identity or _exact_file_digest(path) != expected_digest:
            return False
        return _regular_file_identity(path) == before
    except FilesystemError:
        return False


def _copy_regular_file_exclusive(
    source: Path,
    destination: Path,
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Copy one stable regular-file snapshot into an exclusively created path.

    The destination is deliberately left in place on any ambiguous post-create
    failure. Pathname cleanup could otherwise delete a concurrent replacement.
    Callers retain the source as their recoverable candidate.
    """

    source_snapshot = _read_exact_regular_file_snapshot(source)
    source_digest = hashlib.sha256(source_snapshot.data).hexdigest()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        destination_fd = os.open(destination, flags, source_snapshot.mode)
    except FileExistsError:
        raise
    except OSError as exc:
        raise FilesystemError(f"cannot exclusively create managed instruction path: {destination}: {exc}") from exc

    try:
        destination_metadata = os.fstat(destination_fd)
        if not stat.S_ISREG(destination_metadata.st_mode):
            raise FilesystemError(f"exclusive managed instruction path is not a regular file: {destination}")
        destination_identity = destination_metadata.st_dev, destination_metadata.st_ino
        remaining = memoryview(source_snapshot.data)
        while remaining:
            written = os.write(destination_fd, remaining)
            if written <= 0:
                raise OSError("zero-byte write while copying managed instruction")
            remaining = remaining[written:]
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError(
            f"cannot finish exclusive managed instruction copy at {destination}; "
            "the destination and source candidate were preserved"
        ) from exc
    finally:
        os.close(destination_fd)

    if not _exact_regular_file_matches(
        destination,
        expected_digest=source_digest,
        expected_identity=destination_identity,
    ):
        raise FilesystemError(f"exclusive managed instruction copy changed during publication: {destination}")
    if not _exact_regular_file_matches(
        source,
        expected_digest=source_digest,
        expected_identity=source_snapshot.identity,
    ):
        raise FilesystemError(f"managed instruction copy source changed while being copied: {source}")
    return source_digest, source_snapshot.identity, destination_identity


def _retain_displaced_copy(
    *,
    displaced: Path,
    backup: Path,
    expected_digest: str,
    expected_identity: tuple[int, int],
) -> None:
    """Retain an old inode without consuming its local recovery name."""

    try:
        os.link(displaced, backup, follow_symlinks=False)
    except FileExistsError as exc:
        raise FilesystemError(f"refusing to replace existing instruction backup: {backup}") from exc
    except OSError as link_exc:
        try:
            copied_digest, copied_source_identity, backup_identity = _copy_regular_file_exclusive(
                displaced,
                backup,
            )
        except FileExistsError as exc:
            raise FilesystemError(f"refusing to replace existing instruction backup: {backup}") from exc
        if copied_digest != expected_digest or copied_source_identity != expected_identity:
            raise FilesystemError(f"managed instruction copy changed while retaining backup: {displaced}") from link_exc
    else:
        backup_identity = expected_identity

    if not _exact_regular_file_matches(
        displaced,
        expected_digest=expected_digest,
        expected_identity=expected_identity,
    ):
        raise FilesystemError(f"managed instruction copy changed while retaining backup: {displaced}")
    if not _exact_regular_file_matches(
        backup,
        expected_digest=expected_digest,
        expected_identity=backup_identity,
    ):
        raise FilesystemError(f"retained instruction backup failed validation: {backup}")


def _remove_displaced_copy(
    displaced: Path,
    *,
    expected_digest: str,
    expected_identity: tuple[int, int],
) -> None:
    """Release a private old-file name only at its final validation boundary."""

    if not _exact_regular_file_matches(
        displaced,
        expected_digest=expected_digest,
        expected_identity=expected_identity,
    ):
        raise FilesystemError(f"managed instruction copy changed before old-file cleanup: {displaced}")
    try:
        displaced.unlink()
    except OSError as exc:
        raise FilesystemError(
            f"managed instruction old-file candidate could not be released: {displaced}: {exc}"
        ) from exc


def _recover_instruction_update(
    *,
    destination: Path,
    candidates: tuple[Path, ...],
) -> None:
    """Recover an absent destination without ever removing an active path."""

    if os.path.lexists(destination):
        return
    _restore_displaced_copy_if_absent(destination=destination, candidates=candidates)


def _restore_displaced_copy_if_absent(
    *,
    destination: Path,
    candidates: tuple[Path, ...],
) -> None:
    """Restore the first recoverable copy without overwriting a raced file."""

    if os.path.lexists(destination):
        return
    for candidate in candidates:
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FilesystemError(
                f"managed instruction recovery candidate could not be inspected at {candidate}: {exc}"
            ) from exc
        if not stat.S_ISREG(candidate_metadata.st_mode):
            raise FilesystemError(f"managed instruction recovery candidate is not a regular file: {candidate}")
        candidate_identity = candidate_metadata.st_dev, candidate_metadata.st_ino
        candidate_digest = _exact_file_digest(candidate)
        try:
            os.link(candidate, destination, follow_symlinks=False)
        except FileExistsError:
            return
        except OSError as link_exc:
            try:
                copied_digest, copied_source_identity, destination_identity = _copy_regular_file_exclusive(
                    candidate,
                    destination,
                )
            except FileExistsError:
                return
            except FilesystemError as exc:
                raise FilesystemError(
                    f"managed instruction copy could not be restored at {destination}; "
                    f"hard-link restoration failed first: {link_exc}; exclusive copy failed: {exc}"
                ) from exc
            if copied_digest != candidate_digest or copied_source_identity != candidate_identity:
                raise FilesystemError(
                    f"managed instruction recovery candidate changed while copied: {candidate}"
                ) from link_exc
        else:
            destination_identity = candidate_identity

        if not _exact_regular_file_matches(
            destination,
            expected_digest=candidate_digest,
            expected_identity=destination_identity,
        ):
            raise FilesystemError(f"restored managed instruction copy changed during publication: {destination}")
        return


def _hook_state_marker_offsets(data: bytes) -> tuple[int, ...]:
    """Return line-start offsets for exact ``[hooks.state]`` table headers."""

    return tuple(match.start() for match in re.finditer(rb"(?m)^\[hooks\.state\](?:\r?\n|\Z)", data))


def _possible_managed_ends(data: bytes, marker_start: int) -> tuple[int, ...]:
    """Return boundaries that retain zero or more separator newlines in suffix."""

    ends = [marker_start]
    cursor = marker_start
    while cursor > 0 and data[cursor - 1] == 0x0A:
        cursor -= 1
        if cursor > 0 and data[cursor - 1] == 0x0D:
            cursor -= 1
        ends.append(cursor)
    return tuple(ends)


def _valid_runtime_hook_state_suffix(suffix: bytes) -> bool:
    try:
        payload = tomllib.loads(suffix.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    if set(payload) != {"hooks"}:
        return False
    hooks = payload["hooks"]
    if not isinstance(hooks, dict) or set(hooks) != {"state"}:
        return False
    state = hooks["state"]
    if not isinstance(state, dict):
        return False
    return all(
        isinstance(identifier, str)
        and bool(identifier)
        and isinstance(entry, dict)
        and set(entry) == {"trusted_hash"}
        and isinstance(entry["trusted_hash"], str)
        and _TRUSTED_HOOK_HASH.fullmatch(entry["trusted_hash"]) is not None
        for identifier, entry in state.items()
    )


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
