"""Plan and apply bounded retention for Bridge-generated operational state."""

from __future__ import annotations

import importlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from agent_config_bridge.filesystem import read_managed_marker, tree_digest
from agent_config_bridge.models import BridgeConfig, RetentionConfig
from agent_config_bridge.renderer import (
    RenderError,
    published_marketplace_digest,
    validate_marketplace_build,
)

__all__ = [
    "RetentionAction",
    "RetentionBlocker",
    "RetentionError",
    "RetentionPlan",
    "RetentionResult",
    "apply_retention_plan",
    "build_retention_plan",
]

_BUILD_NAME = re.compile(r"^[0-9a-f]{20}$")
_OWNER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SNAPSHOT_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
_PRIVATE_FILE_MODE = 0o600


class RetentionError(RuntimeError):
    """Raised when retention cannot safely inspect or mutate Bridge state."""


@dataclass(frozen=True, slots=True)
class RetentionAction:
    """One verified generated-state entry selected for deletion."""

    category: str
    path: Path
    node_kind: str
    bytes: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class RetentionBlocker:
    """One state entry that prevents every retention deletion."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """A deterministic, read-only snapshot of bounded retention decisions."""

    limits: RetentionConfig
    build_count: int
    build_bytes: int
    skill_backup_group_count: int
    skill_backup_snapshot_count: int
    skill_backup_bytes: int
    actions: tuple[RetentionAction, ...]
    blockers: tuple[RetentionBlocker, ...]
    excluded_instruction_roots: tuple[Path, ...]

    @property
    def has_changes(self) -> bool:
        """Return whether the plan contains safe deletion candidates."""

        return bool(self.actions)

    @property
    def has_blockers(self) -> bool:
        """Return whether any unsafe state entry blocks all deletion."""

        return bool(self.blockers)

    @property
    def reclaimable_bytes(self) -> int:
        """Return the regular-file bytes represented by deletion candidates."""

        return sum(action.bytes for action in self.actions)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """The entries deleted by a successful apply and its converged final plan."""

    deleted: tuple[RetentionAction, ...]
    reclaimed_bytes: int
    final_plan: RetentionPlan


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    path: Path
    node_kind: str
    bytes: int
    mtime_ns: int
    device: int
    inode: int

    def action(self, category: str) -> RetentionAction:
        return RetentionAction(
            category=category,
            path=self.path,
            node_kind=self.node_kind,
            bytes=self.bytes,
            mtime_ns=self.mtime_ns,
            device=self.device,
            inode=self.inode,
        )


def build_retention_plan(config: BridgeConfig) -> RetentionPlan:
    """Inspect generated state without writing files, locks, or timestamps."""

    blockers: list[RetentionBlocker] = []
    excluded: list[Path] = []
    state_dir = config.state_dir
    if os.path.lexists(state_dir) and not _is_real_directory(state_dir):
        blockers.append(
            RetentionBlocker(
                path=state_dir,
                reason="configured state_dir must be a real directory",
            )
        )
        return _plan(
            config.retention,
            blockers=blockers,
            excluded=excluded,
        )

    published_digest: str | None = None
    try:
        published_digest = published_marketplace_digest(config)
    except (OSError, RenderError) as exc:
        blockers.append(
            RetentionBlocker(
                path=config.state_dir / "marketplace",
                reason=f"published marketplace is unsafe or invalid: {exc}",
            )
        )

    builds, build_bytes = _scan_builds(config, blockers)
    if published_digest is not None and all(build.path.name != published_digest for build in builds):
        blockers.append(
            RetentionBlocker(
                path=config.state_dir / "builds" / published_digest,
                reason="published marketplace has no matching immutable build",
            )
        )
    build_actions = _select_build_actions(
        builds,
        limit=config.retention.marketplace_builds,
        published_digest=published_digest,
    )
    backup_groups, backup_bytes = _scan_skill_backups(config, blockers, excluded)
    backup_actions = _select_backup_actions(
        backup_groups,
        limit=config.retention.skill_backups,
    )
    actions = sorted(
        (*build_actions, *backup_actions),
        key=lambda action: (action.category, action.path.as_posix()),
    )
    return _plan(
        config.retention,
        builds=builds,
        build_bytes=build_bytes,
        backup_groups=backup_groups,
        backup_bytes=backup_bytes,
        actions=actions,
        blockers=blockers,
        excluded=excluded,
    )


def apply_retention_plan(
    config: BridgeConfig,
    reviewed_plan: RetentionPlan,
) -> RetentionResult:
    """Apply one reviewed plan under an exclusive lock after full revalidation."""

    if reviewed_plan.has_blockers:
        raise RetentionError("retention plan contains blockers; no entries were deleted")
    with _OperationLock(config.state_dir):
        if reviewed_plan.actions and not shutil.rmtree.avoids_symlink_attacks:
            raise RetentionError(
                "this platform lacks descriptor-anchored directory removal; retention apply is blocked"
            )
        fresh_plan = build_retention_plan(config)
        if fresh_plan != reviewed_plan:
            raise RetentionError("generated state changed after retention planning; review a fresh plan")
        if fresh_plan.has_blockers:
            raise RetentionError("retention state became unsafe; no entries were deleted")
        for action in fresh_plan.actions:
            _revalidate_action(config, action)
        for action in fresh_plan.actions:
            _revalidate_action(config, action)
            _remove_action_anchored(config, action)

        final_plan = build_retention_plan(config)
        if final_plan.has_blockers or final_plan.has_changes:
            raise RetentionError("retention did not converge after deleting its reviewed candidates")
        return RetentionResult(
            deleted=fresh_plan.actions,
            reclaimed_bytes=fresh_plan.reclaimable_bytes,
            final_plan=final_plan,
        )


def _plan(
    limits: RetentionConfig,
    *,
    builds: list[_ScannedEntry] | None = None,
    build_bytes: int = 0,
    backup_groups: dict[tuple[str, str], list[_ScannedEntry]] | None = None,
    backup_bytes: int = 0,
    actions: list[RetentionAction] | None = None,
    blockers: list[RetentionBlocker],
    excluded: list[Path],
) -> RetentionPlan:
    builds = builds or []
    backup_groups = backup_groups or {}
    return RetentionPlan(
        limits=limits,
        build_count=len(builds),
        build_bytes=build_bytes,
        skill_backup_group_count=len(backup_groups),
        skill_backup_snapshot_count=sum(len(group) for group in backup_groups.values()),
        skill_backup_bytes=backup_bytes,
        actions=tuple(actions or ()),
        blockers=tuple(sorted(blockers, key=lambda item: (item.path.as_posix(), item.reason))),
        excluded_instruction_roots=tuple(sorted(set(excluded), key=lambda path: path.as_posix())),
    )


def _scan_builds(
    config: BridgeConfig,
    blockers: list[RetentionBlocker],
) -> tuple[list[_ScannedEntry], int]:
    root = config.state_dir / "builds"
    entries = _directory_entries(root, blockers, "marketplace builds root")
    builds: list[_ScannedEntry] = []
    total_bytes = 0
    for entry in entries:
        path = Path(entry.path)
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            blockers.append(RetentionBlocker(path, f"cannot inspect build entry: {exc}"))
            continue
        if _BUILD_NAME.fullmatch(entry.name) is None:
            blockers.append(RetentionBlocker(path, "unexpected marketplace build entry name"))
            continue
        if not stat.S_ISDIR(entry_stat.st_mode) or _entry_is_reparse(entry_stat):
            blockers.append(RetentionBlocker(path, "marketplace build must be a real directory"))
            continue
        try:
            validate_marketplace_build(config, path, entry.name)
            entry_bytes = _tree_regular_bytes(path, allow_symlinks=False)
        except (OSError, RenderError, RetentionError) as exc:
            blockers.append(RetentionBlocker(path, f"invalid marketplace build: {exc}"))
            continue
        item = _scanned_entry(path, entry_stat, "directory", entry_bytes)
        builds.append(item)
        total_bytes += entry_bytes
    builds.sort(key=lambda item: (item.mtime_ns, item.path.name), reverse=True)
    return builds, total_bytes


def _select_build_actions(
    builds: list[_ScannedEntry],
    *,
    limit: int,
    published_digest: str | None,
) -> list[RetentionAction]:
    kept = list(builds[:limit])
    published = next(
        (entry for entry in builds if entry.path.name == published_digest),
        None,
    )
    if published is not None and published not in kept:
        kept[-1] = published
    keep_paths = {entry.path for entry in kept}
    return [entry.action("marketplace_build") for entry in builds if entry.path not in keep_paths]


def _scan_skill_backups(
    config: BridgeConfig,
    blockers: list[RetentionBlocker],
    excluded: list[Path],
) -> tuple[dict[tuple[str, str], list[_ScannedEntry]], int]:
    root = config.state_dir / "backups"
    target_entries = _directory_entries(root, blockers, "Skill backups root")
    groups: dict[tuple[str, str], list[_ScannedEntry]] = {}
    total_bytes = 0
    for target_entry in target_entries:
        target_path = Path(target_entry.path)
        if not _valid_named_directory(target_entry, _OWNER_NAME):
            blockers.append(
                RetentionBlocker(
                    target_path,
                    "Skill backup target must be a real lowercase kebab-case directory",
                )
            )
            continue
        skill_entries = _directory_entries(
            target_path,
            blockers,
            "Skill backup target directory",
        )
        for skill_entry in skill_entries:
            skill_path = Path(skill_entry.path)
            if skill_entry.name == "instructions":
                if _valid_named_directory(skill_entry, _OWNER_NAME):
                    excluded.append(skill_path)
                else:
                    blockers.append(
                        RetentionBlocker(
                            skill_path,
                            "instruction backup root must be a real directory",
                        )
                    )
                continue
            if not _valid_named_directory(skill_entry, _OWNER_NAME):
                blockers.append(
                    RetentionBlocker(
                        skill_path,
                        "Skill backup group must be a real lowercase kebab-case directory",
                    )
                )
                continue
            snapshots = _scan_backup_group(
                target_entry.name,
                skill_entry.name,
                skill_path,
                blockers,
            )
            groups[(target_entry.name, skill_entry.name)] = snapshots
            total_bytes += sum(snapshot.bytes for snapshot in snapshots)
    return groups, total_bytes


def _scan_backup_group(
    target: str,
    skill: str,
    root: Path,
    blockers: list[RetentionBlocker],
) -> list[_ScannedEntry]:
    entries = _directory_entries(root, blockers, "Skill backup group")
    snapshots: list[_ScannedEntry] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            blockers.append(RetentionBlocker(path, f"cannot inspect Skill backup: {exc}"))
            continue
        if _SNAPSHOT_NAME.fullmatch(entry.name) is None:
            blockers.append(RetentionBlocker(path, "unexpected Skill backup snapshot name"))
            continue
        if stat.S_ISLNK(entry_stat.st_mode):
            snapshots.append(_scanned_entry(path, entry_stat, "symlink", entry_stat.st_size))
            continue
        if not stat.S_ISDIR(entry_stat.st_mode) or _entry_is_reparse(entry_stat):
            blockers.append(
                RetentionBlocker(path, "Skill backup snapshot must be a real directory or terminal symlink")
            )
            continue
        marker = read_managed_marker(path)
        if (
            marker is None
            or marker.get("source_id") != f"skills/{skill}"
            or tree_digest(path) != marker.get("installed_digest")
        ):
            blockers.append(
                RetentionBlocker(
                    path,
                    f"Skill backup ownership or digest does not match {target}:{skill}",
                )
            )
            continue
        try:
            entry_bytes = _tree_regular_bytes(path, allow_symlinks=True)
        except (OSError, RetentionError) as exc:
            blockers.append(RetentionBlocker(path, f"invalid Skill backup tree: {exc}"))
            continue
        snapshots.append(_scanned_entry(path, entry_stat, "directory", entry_bytes))
    snapshots.sort(key=lambda item: item.path.name, reverse=True)
    return snapshots


def _select_backup_actions(
    groups: dict[tuple[str, str], list[_ScannedEntry]],
    *,
    limit: int,
) -> list[RetentionAction]:
    actions: list[RetentionAction] = []
    for group in groups.values():
        actions.extend(snapshot.action("skill_backup") for snapshot in group[limit:])
    return actions


def _directory_entries(
    root: Path,
    blockers: list[RetentionBlocker],
    label: str,
) -> list[os.DirEntry[str]]:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        blockers.append(RetentionBlocker(root, f"cannot inspect {label}: {exc}"))
        return []
    if not stat.S_ISDIR(root_stat.st_mode) or _entry_is_reparse(root_stat):
        blockers.append(RetentionBlocker(root, f"{label} must be a real directory"))
        return []
    try:
        return sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError as exc:
        blockers.append(RetentionBlocker(root, f"cannot read {label}: {exc}"))
        return []


def _valid_named_directory(
    entry: os.DirEntry[str],
    pattern: re.Pattern[str],
) -> bool:
    if pattern.fullmatch(entry.name) is None:
        return False
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(entry_stat.st_mode) and not _entry_is_reparse(entry_stat)


def _tree_regular_bytes(root: Path, *, allow_symlinks: bool) -> int:
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise RetentionError(f"cannot read generated directory: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RetentionError(f"cannot inspect generated entry: {path}") from exc
            if stat.S_ISREG(entry_stat.st_mode):
                total += entry_stat.st_size
            elif stat.S_ISDIR(entry_stat.st_mode) and not _entry_is_reparse(entry_stat):
                pending.append(path)
            elif stat.S_ISLNK(entry_stat.st_mode) and allow_symlinks:
                total += entry_stat.st_size
            else:
                raise RetentionError(f"unsupported or redirected generated entry: {path}")
    return total


def _revalidate_action(config: BridgeConfig, action: RetentionAction) -> None:
    root = config.state_dir / "builds" if action.category == "marketplace_build" else config.state_dir / "backups"
    _require_real_ancestors(action.path, root)
    try:
        current = action.path.lstat()
    except OSError as exc:
        raise RetentionError(f"retention candidate changed before deletion: {action.path}") from exc
    if current.st_dev != action.device or current.st_ino != action.inode or current.st_mtime_ns != action.mtime_ns:
        raise RetentionError(f"retention candidate identity changed before deletion: {action.path}")
    if action.node_kind == "symlink":
        if not stat.S_ISLNK(current.st_mode):
            raise RetentionError(f"terminal backup link changed before deletion: {action.path}")
        return
    if not stat.S_ISDIR(current.st_mode) or _entry_is_reparse(current):
        raise RetentionError(f"retention directory changed or became redirected: {action.path}")
    if action.category == "marketplace_build":
        validate_marketplace_build(config, action.path, action.path.name)
        return
    skill = action.path.parent.name
    marker = read_managed_marker(action.path)
    if (
        marker is None
        or marker.get("source_id") != f"skills/{skill}"
        or tree_digest(action.path) != marker.get("installed_digest")
    ):
        raise RetentionError(f"Skill backup changed before deletion: {action.path}")


def _remove_action_anchored(config: BridgeConfig, action: RetentionAction) -> None:
    """Remove one candidate relative to its verified parent directory handle."""

    parent = action.path.parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise RetentionError(f"retention candidate parent changed before deletion: {parent}") from exc
    try:
        current = os.stat(action.path.name, dir_fd=parent_fd, follow_symlinks=False)
        if current.st_dev != action.device or current.st_ino != action.inode or current.st_mtime_ns != action.mtime_ns:
            raise RetentionError(f"retention candidate identity changed before deletion: {action.path}")
        if action.node_kind == "symlink":
            if not stat.S_ISLNK(current.st_mode):
                raise RetentionError(f"terminal backup link changed before deletion: {action.path}")
            os.unlink(action.path.name, dir_fd=parent_fd)
        else:
            if not stat.S_ISDIR(current.st_mode) or _entry_is_reparse(current):
                raise RetentionError(f"retention directory changed or became redirected: {action.path}")
            shutil.rmtree(action.path.name, dir_fd=parent_fd)
    except OSError as exc:
        raise RetentionError(f"retention candidate could not be removed safely: {action.path}") from exc
    finally:
        os.close(parent_fd)


def _require_real_ancestors(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RetentionError(f"retention candidate escapes its generated-state root: {path}") from exc
    candidate = path.parent
    while True:
        if not _is_real_directory(candidate):
            raise RetentionError(f"retention candidate ancestor is redirected: {candidate}")
        if candidate == root:
            return
        if candidate == candidate.parent:
            raise RetentionError(f"retention candidate root is unreachable: {path}")
        candidate = candidate.parent


def _scanned_entry(
    path: Path,
    entry_stat: os.stat_result,
    node_kind: str,
    entry_bytes: int,
) -> _ScannedEntry:
    return _ScannedEntry(
        path=path,
        node_kind=node_kind,
        bytes=entry_bytes,
        mtime_ns=entry_stat.st_mtime_ns,
        device=entry_stat.st_dev,
        inode=entry_stat.st_ino,
    )


def _is_real_directory(path: Path) -> bool:
    try:
        path_stat = path.lstat()
        return stat.S_ISDIR(path_stat.st_mode) and not _entry_is_reparse(path_stat)
    except OSError:
        return False


def _entry_is_reparse(entry_stat: os.stat_result) -> bool:
    attributes = getattr(entry_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class _OperationLock:
    """Cross-platform exclusive lock for one retention apply operation."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._path = state_dir / ".agentbridge-retention.lock"
        self._stream: BinaryIO | None = None
        self._acquired = False

    def __enter__(self) -> _OperationLock:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if not _is_real_directory(self._state_dir):
            raise RetentionError(f"configured state_dir must be a real directory: {self._state_dir}")
        if os.path.lexists(self._path):
            try:
                lock_stat = self._path.lstat()
            except OSError as exc:
                raise RetentionError(f"cannot inspect retention lock: {self._path}") from exc
            if not stat.S_ISREG(lock_stat.st_mode) or _entry_is_reparse(lock_stat):
                raise RetentionError(f"retention lock must be a real regular file: {self._path}")
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, _PRIVATE_FILE_MODE)
            self._stream = os.fdopen(descriptor, "a+b")
            if os.name != "nt":
                os.chmod(self._path, _PRIVATE_FILE_MODE)
            self._acquired = _lock_windows(self._stream) if os.name == "nt" else _lock_posix(self._stream)
        except OSError as exc:
            if self._stream is not None:
                self._stream.close()
            raise RetentionError(f"could not acquire retention lock: {self._path}") from exc
        if not self._acquired:
            self._stream.close()
            self._stream = None
            raise RetentionError(f"another retention operation holds the lock: {self._path}")
        return self

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
