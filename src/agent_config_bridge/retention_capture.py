"""Generation-bound atomic capture for destructive Bridge retention (ADR-0008).

The only destructive call in this module removes an object that was first renamed
into a private Bridge-owned run directory and then matched, after the move, on
``(st_dev, st_ino, i_generation)`` plus a marker-inclusive content digest. Every
earlier step is a rename that recovery can undo.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_config_bridge.filesystem import MANAGED_MARKER

__all__ = [
    "CAPTURE_DIR",
    "DEFAULT_CAPTURE_OPS",
    "QUARANTINE_DIR",
    "QUIESCENCE_NS",
    "RETENTION_DIR",
    "CaptureCapability",
    "CaptureError",
    "CaptureFinding",
    "CaptureIdentity",
    "CaptureInspection",
    "CaptureOps",
    "CaptureOutcome",
    "CaptureRequest",
    "RecoveryEvent",
    "RetainedIdentity",
    "TreeObservation",
    "capture_and_delete",
    "default_capture_ops",
    "descriptor_identity",
    "inspect_capture_state",
    "observe_directory",
    "observe_tree",
    "probe_capability",
    "recover_interrupted",
    "retained_identity",
]

QUIESCENCE_NS = 15 * 60 * 1_000_000_000
RETENTION_DIR = "retention"
CAPTURE_DIR = "capture"
QUARANTINE_DIR = "quarantine"
MANIFEST_NAME = "manifest.json"

INTENT = "intent"
CAPTURED = "captured"
VERIFIED = "verified"
DELETED = "deleted"
RESTORED = "restored"
QUARANTINED = "quarantined"
PENDING_CLEANUP = "pending_cleanup"
CLEANED_UP = "cleaned_up"
_STATES = frozenset({INTENT, CAPTURED, VERIFIED, DELETED, RESTORED, QUARANTINED})
_TERMINAL_STATES = frozenset({DELETED, RESTORED, QUARANTINED})

# Generation semantics are verified only for the ext4 driver (which also serves ext2/ext3).
_GENERATION_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4"})
# Machines whose ioctl numbers use the generic _IOC layout verified for FS_IOC_GETVERSION.
# ``platform.machine()`` spells them this way on Linux, the only supported ``sys.platform``.
_GENERIC_IOCTL_MACHINES = frozenset({"x86_64", "aarch64"})
_DIGEST_FORMAT = b"agent-config-bridge/capture-digest/v1"
_MANIFEST_FORMAT = "agent-config-bridge/retention-capture"
_PRIVATE_MODE = 0o700
_MAX_DEPTH = 128
_MAX_ITEMS = 9999
_MAX_MANIFEST_BYTES = 8 << 20
_MAX_MARKER_BYTES = 1 << 16
_CHUNK_BYTES = 1 << 20
_RUN_NAME = re.compile(r"^[0-9a-f]{32}$")
_SLOT_NAME = re.compile(r"^item-[0-9]{4}$")
_MANIFEST_TEMP = re.compile(r"^manifest\.json\.[0-9a-f]{32}\.tmp$")
_OWNER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SNAPSHOT_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# Errors that will not clear on retry; recovery quarantines only on these and retries the rest.
_DEFINITIVE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EEXIST, errno.ENOTEMPTY})
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_ITEM_KEYS = frozenset(
    {
        "bytes",
        "capture_digest",
        "category",
        "device",
        "entry_count",
        "generation",
        "index",
        "inode",
        "name",
        "parent",
        "slot",
        "source_id",
        "state",
    }
)


class CaptureError(RuntimeError):
    """Raised when capture state is unsafe, malformed, or cannot be settled.

    ``outcome`` carries the per-path results recorded before a capture run failed.
    """

    def __init__(self, message: str, outcome: CaptureOutcome | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


class _DeletionRefused(CaptureError):
    """Raised when a slot is not durably recorded as verified for its own run."""


class _MountDetected(_DeletionRefused):
    """Raised when a verified slot crosses a mount or device boundary; it is quarantined, never deleted."""


@dataclass(frozen=True, slots=True)
class CaptureOps:
    """Injectable platform seams for probing, capture, and recovery."""

    os_name: str
    sys_platform: str
    machine: str
    avoids_symlink_attacks: bool
    dir_fd_supported: bool
    filesystem_type: Callable[[int], str | None]
    generation: Callable[[int], int]
    mount_id: Callable[[int], int]
    same_filesystem: Callable[[os.stat_result, os.stat_result], bool]
    rename: Callable[[str, str, int, int], None]
    rmtree: Callable[[str, int], None]
    write_manifest: Callable[[int, bytes], None]
    now_ns: Callable[[], int]
    quiescence_ns: int = QUIESCENCE_NS


@dataclass(frozen=True, slots=True)
class CaptureCapability:
    """Whether generation-bound capture may delete on this host and state filesystem."""

    capable: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class CaptureIdentity:
    """A non-reusable directory identity: device, inode, and inode generation."""

    device: int
    inode: int
    generation: int


@dataclass(frozen=True, slots=True)
class TreeObservation:
    """A descriptor-relative, marker-inclusive observation of one directory tree."""

    capture_digest: str
    entry_count: int
    bytes: int
    newest_ctime_ns: int
    marker_source_id: str | None


@dataclass(frozen=True, slots=True)
class RetainedIdentity:
    """A kept sibling snapshot that must still exist before a candidate moves."""

    name: str
    node_kind: str
    device: int
    inode: int
    generation: int | None


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """One reviewed Skill backup directory to capture, verify, and delete."""

    path: Path
    parent: tuple[str, ...]
    name: str
    identity: CaptureIdentity
    capture_digest: str
    entry_count: int
    bytes: int
    source_id: str
    retained: tuple[RetainedIdentity, ...]


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """Per-path results of one capture run; ``stop_reason`` is set when it stopped early."""

    deleted: tuple[Path, ...] = ()
    restored: tuple[Path, ...] = ()
    quarantined: tuple[Path, ...] = ()
    busy: tuple[Path, ...] = ()
    pending_cleanup: tuple[Path, ...] = ()
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryEvent:
    """One interrupted-run recovery result."""

    path: Path
    outcome: str


@dataclass(frozen=True, slots=True)
class CaptureFinding:
    """A read-only planning finding about private capture state."""

    path: Path
    detail: str


@dataclass(frozen=True, slots=True)
class CaptureInspection:
    """Recovery work (not a blocker) and unsafe capture state (a blocker)."""

    pending: tuple[CaptureFinding, ...] = ()
    blockers: tuple[CaptureFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    index: int
    parent: tuple[str, ...]
    name: str
    identity: CaptureIdentity
    capture_digest: str
    entry_count: int
    bytes: int
    source_id: str
    state: str

    @property
    def slot(self) -> str:
        return _slot_name(self.index)


@dataclass(frozen=True, slots=True)
class _Manifest:
    run_id: str
    items: tuple[_ManifestItem, ...]

    def with_state(self, index: int, state: str) -> _Manifest:
        return replace(
            self,
            items=tuple(replace(item, state=state) if item.index == index else item for item in self.items),
        )


# --- default platform seams -------------------------------------------------


def _fs_ioc_getversion() -> int:
    """Return ``_IOR('v', 1, long)`` for the generic Linux ioctl layout."""

    return (2 << 30) | (struct.calcsize("l") << 16) | (ord("v") << 8) | 1


def _ioctl_generation(descriptor: int) -> int:
    fcntl: Any = importlib.import_module("fcntl")
    buffer = bytearray(struct.calcsize("l"))
    fcntl.ioctl(descriptor, _fs_ioc_getversion(), buffer, True)
    # The kernel stores a native unsigned int at offset zero.
    return int(struct.unpack_from("=I", buffer)[0])


def _fdinfo_mount_id(descriptor: int) -> int:
    """Return the mount ID of an open descriptor from ``/proc/self/fdinfo``."""

    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii", errors="replace") as stream:
        for line in stream:
            key, separator, value = line.partition(":")
            if separator and key == "mnt_id" and value.strip().isdigit():
                return int(value)
    raise OSError(errno.EINVAL, "fdinfo does not report mnt_id")


def _mount_filesystem_type(device: int) -> str | None:
    major, minor = getattr(os, "major", None), getattr(os, "minor", None)
    if major is None or minor is None:
        return None
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    wanted = f"{major(device)}:{minor(device)}"
    types: set[str] = set()
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        fields = left.split()
        filesystem = right.split()
        if separator and len(fields) >= 5 and filesystem and fields[2] == wanted:
            types.add(filesystem[0])
    return next(iter(types)) if len(types) == 1 else None


def _same_device(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev


def _rename_at(source: str, destination: str, source_dir_fd: int, destination_dir_fd: int) -> None:
    os.rename(source, destination, src_dir_fd=source_dir_fd, dst_dir_fd=destination_dir_fd)


def _rmtree_at(name: str, dir_fd: int) -> None:
    shutil.rmtree(name, dir_fd=dir_fd)


def _atomic_write_manifest(dir_fd: int, payload: bytes) -> None:
    temporary = f"{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=dir_fd)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(temporary, MANIFEST_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


def _dir_fd_supported() -> bool:
    required = (os.open, os.rename, os.stat, os.mkdir, os.rmdir, os.unlink, os.readlink)
    return all(function in os.supports_dir_fd for function in required) and os.scandir in os.supports_fd


def default_capture_ops() -> CaptureOps:
    """Return the real platform seams."""

    return CaptureOps(
        os_name=os.name,
        sys_platform=sys.platform,
        machine=platform.machine(),
        avoids_symlink_attacks=bool(getattr(shutil.rmtree, "avoids_symlink_attacks", False)),
        dir_fd_supported=_dir_fd_supported(),
        filesystem_type=_mount_filesystem_type,
        generation=_ioctl_generation,
        mount_id=_fdinfo_mount_id,
        same_filesystem=_same_device,
        rename=_rename_at,
        rmtree=_rmtree_at,
        write_manifest=_atomic_write_manifest,
        now_ns=time.time_ns,
    )


DEFAULT_CAPTURE_OPS = default_capture_ops()


# --- capability, identity, and digest ---------------------------------------


def probe_capability(state_dir: Path, ops: CaptureOps) -> CaptureCapability:
    """Probe capture support without creating, changing, or locking anything."""

    problem = _platform_problem(ops) or _filesystem_problem(state_dir, ops)
    return CaptureCapability(capable=problem is None, reason=problem)


def _platform_problem(ops: CaptureOps) -> str | None:
    if ops.os_name == "nt":
        return "native Windows capture is deferred to phase 2"
    if ops.os_name != "posix":
        return f"unsupported operating system family {ops.os_name!r}"
    if ops.sys_platform != "linux":
        return f"generation-bound capture is verified only on Linux, not {ops.sys_platform!r}"
    if ops.machine.lower() not in _GENERIC_IOCTL_MACHINES:
        return f"FS_IOC_GETVERSION layout is not verified for machine {ops.machine!r}"
    if not ops.avoids_symlink_attacks:
        return "shutil.rmtree is not symlink-attack resistant on this platform"
    if not ops.dir_fd_supported or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return "descriptor-relative no-follow operations are unavailable"
    return None


def _filesystem_problem(state_dir: Path, ops: CaptureOps) -> str | None:
    try:
        descriptor = os.open(state_dir / "backups", _DIR_FLAGS)
    except FileNotFoundError:
        return "Skill backups root is absent"
    except OSError as exc:
        return f"Skill backups root cannot be opened without following links ({_errno_name(exc)})"
    try:
        backups_stat = os.fstat(descriptor)
        filesystem = ops.filesystem_type(backups_stat.st_dev)
        if filesystem not in _GENERATION_FILESYSTEMS:
            return f"state filesystem type {filesystem or 'unknown'} is not in the generation allowlist"
        try:
            ops.generation(descriptor)
        except OSError as exc:
            return f"FS_IOC_GETVERSION is unavailable on the state filesystem ({_errno_name(exc)})"
        try:
            ops.mount_id(descriptor)
        except OSError as exc:
            return f"mount identity (fdinfo mnt_id) is unavailable ({_errno_name(exc)})"
    finally:
        os.close(descriptor)
    return _private_tree_problem(state_dir, backups_stat, ops)


def _private_tree_problem(state_dir: Path, backups_stat: os.stat_result, ops: CaptureOps) -> str | None:
    retention = state_dir / RETENTION_DIR
    try:
        retention_stat = os.lstat(retention)
    except FileNotFoundError:
        if not ops.same_filesystem(os.lstat(state_dir), backups_stat):
            return "retention capture directory would be on another filesystem than Skill backups (EXDEV)"
        return None
    for path, path_stat in _private_tree_stats(retention, retention_stat):
        if path_stat is None:
            return f"{path.name} capture directory must be a real directory"
        if not _is_private(path_stat):
            return f"{path.name} capture directory must be owned by the current user with mode 0700"
        if not ops.same_filesystem(path_stat, backups_stat):
            return f"{path.name} capture directory is on another filesystem than Skill backups (EXDEV)"
    return None


def _private_tree_stats(
    retention: Path,
    retention_stat: os.stat_result,
) -> list[tuple[Path, os.stat_result | None]]:
    if not stat.S_ISDIR(retention_stat.st_mode):
        return [(retention, None)]
    stats: list[tuple[Path, os.stat_result | None]] = [(retention, retention_stat)]
    for name in (CAPTURE_DIR, QUARANTINE_DIR):
        child = retention / name
        try:
            child_stat = os.lstat(child)
        except FileNotFoundError:
            continue
        stats.append((child, child_stat if stat.S_ISDIR(child_stat.st_mode) else None))
    return stats


def _is_private(path_stat: os.stat_result) -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return stat.S_IMODE(path_stat.st_mode) == _PRIVATE_MODE and path_stat.st_uid == geteuid()


def _fchmod(descriptor: int, mode: int) -> None:
    """Call the POSIX-only ``os.fchmod``.

    It, ``os.geteuid``, ``os.major``, and ``os.minor`` are looked up at run time so the module
    also type-checks for Windows, where capture is never capable and never reaches them.
    """

    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        raise CaptureError("descriptor-bound permission change is unavailable on this platform")
    fchmod(descriptor, mode)


def descriptor_identity(descriptor: int, ops: CaptureOps) -> CaptureIdentity:
    """Return the generation-bound identity of an open directory descriptor."""

    descriptor_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(descriptor_stat.st_mode):
        raise CaptureError("capture identity requires a directory descriptor")
    return CaptureIdentity(
        device=descriptor_stat.st_dev,
        inode=descriptor_stat.st_ino,
        generation=ops.generation(descriptor),
    )


def observe_directory(path: Path, ops: CaptureOps) -> tuple[CaptureIdentity, TreeObservation]:
    """Open ``path`` without following its final link and observe identity and tree.

    ``path`` must not be a mount point, and nothing below it may cross a mount.
    """

    parent = os.open(path.parent, _DIR_FLAGS)
    try:
        descriptor = os.open(path.name, _DIR_FLAGS, dir_fd=parent)
        try:
            if _crosses_mount(parent, descriptor, ops):
                raise CaptureError("Skill backup is a mount point or on another device than its group")
            return descriptor_identity(descriptor, ops), observe_tree(descriptor, ops)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def retained_identity(path: Path, node_kind: str, ops: CaptureOps) -> RetainedIdentity:
    """Record a kept snapshot's identity; terminal symlinks bind lstat metadata only."""

    if node_kind == "symlink":
        link_stat = os.lstat(path)
        if not stat.S_ISLNK(link_stat.st_mode):
            raise CaptureError(f"retained snapshot is no longer a symlink: {path}")
        return RetainedIdentity(path.name, node_kind, link_stat.st_dev, link_stat.st_ino, None)
    descriptor = os.open(path, _DIR_FLAGS)
    try:
        identity = descriptor_identity(descriptor, ops)
    finally:
        os.close(descriptor)
    return RetainedIdentity(path.name, node_kind, identity.device, identity.inode, identity.generation)


@dataclass(frozen=True, slots=True)
class _Boundary:
    """The device and mount every entry of one walked tree must stay on."""

    device: int
    mount: int
    ops: CaptureOps

    @classmethod
    def of(cls, root_fd: int, ops: CaptureOps) -> _Boundary:
        return cls(os.fstat(root_fd).st_dev, ops.mount_id(root_fd), ops)

    def check_entry(self, entry_stat: os.stat_result, relative: bytes) -> None:
        if entry_stat.st_dev != self.device:
            raise CaptureError(f"captured tree crosses a device boundary at {relative.decode(errors='replace')}")

    def check_descriptor(self, descriptor: int, relative: bytes) -> None:
        if self.ops.mount_id(descriptor) != self.mount:
            raise CaptureError(f"captured tree contains a mount point at {relative.decode(errors='replace')}")


def _crosses_mount(parent_fd: int, descriptor: int, ops: CaptureOps) -> bool:
    """Return whether ``descriptor`` is a mount point or on another device than ``parent_fd``."""

    if os.fstat(parent_fd).st_dev != os.fstat(descriptor).st_dev:
        return True
    return ops.mount_id(parent_fd) != ops.mount_id(descriptor)


def observe_tree(root_fd: int, ops: CaptureOps) -> TreeObservation:
    """Digest every entry below ``root_fd``, including the managed marker.

    The encoding starts with a format tag and length-prefixes every field, so
    distinct trees cannot collide by shifting bytes between paths and contents.
    An entry on another device or a descriptor on another mount is rejected, so
    neither the digest nor a later ``rmtree`` crosses into a mounted tree.
    """

    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise CaptureError("capture digest root must be a directory")
    digest = hashlib.sha256()
    _update_field(digest, _DIGEST_FORMAT)
    count, total, newest, source_id = _walk(root_fd, b"", digest, 0, _Boundary.of(root_fd, ops))
    digest.update(struct.pack(">Q", count))
    return TreeObservation(
        capture_digest=digest.hexdigest(),
        entry_count=count,
        bytes=total,
        newest_ctime_ns=max(root_stat.st_ctime_ns, newest),
        marker_source_id=source_id,
    )


def _walk(
    dir_fd: int,
    prefix: bytes,
    digest: Any,
    depth: int,
    boundary: _Boundary,
) -> tuple[int, int, int, str | None]:
    if depth > _MAX_DEPTH:
        raise CaptureError("captured tree is too deep")
    with os.scandir(dir_fd) as iterator:
        entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
    count = total = newest = 0
    source_id: str | None = None
    for entry in entries:
        name = os.fsencode(entry.name)
        relative = prefix + b"/" + name if prefix else name
        entry_stat = entry.stat(follow_symlinks=False)
        boundary.check_entry(entry_stat, relative)
        count += 1
        newest = max(newest, entry_stat.st_ctime_ns)
        _update_field(digest, relative)
        if stat.S_ISDIR(entry_stat.st_mode):
            _update_field(digest, b"directory")
            child_count, child_total, child_newest, _ = _walk_child(
                dir_fd, entry.name, entry_stat, relative, digest, depth, boundary
            )
            count, total, newest = count + child_count, total + child_total, max(newest, child_newest)
        elif stat.S_ISREG(entry_stat.st_mode):
            _update_field(digest, b"file")
            content = _digest_file(
                dir_fd,
                entry.name,
                entry_stat,
                digest,
                keep=not prefix and name == MANAGED_MARKER.encode(),
                boundary=boundary,
                relative=relative,
            )
            total += entry_stat.st_size
            if content is not None:
                source_id = _marker_source_id(content)
        elif stat.S_ISLNK(entry_stat.st_mode):
            _update_field(digest, b"symlink")
            _update_field(digest, os.fsencode(os.readlink(entry.name, dir_fd=dir_fd)))
            total += entry_stat.st_size
        else:
            raise CaptureError(f"unsupported filesystem node in captured tree: {relative.decode(errors='replace')}")
    return count, total, newest, source_id


def _walk_child(
    dir_fd: int,
    name: str,
    entry_stat: os.stat_result,
    relative: bytes,
    digest: Any,
    depth: int,
    boundary: _Boundary,
) -> tuple[int, int, int, str | None]:
    child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        if not os.path.samestat(entry_stat, os.fstat(child)):
            raise CaptureError("captured directory changed while it was digested")
        boundary.check_descriptor(child, relative)
        return _walk(child, relative, digest, depth + 1, boundary)
    finally:
        os.close(child)


def _digest_file(
    dir_fd: int,
    name: str,
    entry_stat: os.stat_result,
    digest: Any,
    *,
    keep: bool,
    boundary: _Boundary,
    relative: bytes,
) -> bytes | None:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or not os.path.samestat(entry_stat, file_stat):
            raise CaptureError("captured file changed while it was digested")
        boundary.check_descriptor(descriptor, relative)
        remaining = file_stat.st_size
        digest.update(struct.pack(">Q", remaining))
        kept = bytearray() if keep and remaining <= _MAX_MARKER_BYTES else None
        while remaining > 0:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                raise CaptureError("captured file shrank while it was digested")
            digest.update(chunk)
            remaining -= len(chunk)
            if kept is not None:
                kept.extend(chunk)
        if os.read(descriptor, 1):
            raise CaptureError("captured file grew while it was digested")
        return bytes(kept) if kept is not None else None
    finally:
        os.close(descriptor)


def _update_field(digest: Any, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _marker_source_id(content: bytes) -> str | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, RecursionError):  # bad UTF-8 or JSON, oversized integers, or deep nesting
        return None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("installed_digest"), str)
        or not payload["installed_digest"]
        or not isinstance(payload.get("source_id"), str)
        or not payload["source_id"]
    ):
        return None
    return str(payload["source_id"])


# --- capture run --------------------------------------------------------------


class _RunContext:
    """Open descriptors and the durable manifest of one capture run."""

    def __init__(
        self,
        *,
        state_dir: Path,
        state_fd: int,
        retention_fd: int,
        capture_fd: int,
        run_fd: int,
        manifest: _Manifest,
        ops: CaptureOps,
    ) -> None:
        self.state_dir = state_dir
        self.state_fd = state_fd
        self.retention_fd = retention_fd
        self.capture_fd = capture_fd
        self.run_fd = run_fd
        self.manifest = manifest
        self.ops = ops

    def transition(self, index: int, state: str) -> None:
        updated = self.manifest.with_state(index, state)
        self.ops.write_manifest(self.run_fd, _encode_manifest(updated))
        self.manifest = updated

    def item_path(self, index: int) -> Path:
        item = self.manifest.items[index]
        return self.state_dir.joinpath(*item.parent, item.name)


def capture_and_delete(state_dir: Path, requests: Sequence[CaptureRequest], ops: CaptureOps) -> CaptureOutcome:
    """Capture, verify, and delete reviewed candidates; the caller holds the retention lock."""

    if not requests:
        return CaptureOutcome()
    if len(requests) > _MAX_ITEMS:
        raise CaptureError("too many retention candidates for one capture run")
    opened: list[int] = []
    try:
        state_fd = _open_directory(None, str(state_dir), opened)
        retention_fd = _ensure_private_directory(state_fd, RETENTION_DIR, opened)
        capture_fd = _ensure_private_directory(retention_fd, CAPTURE_DIR, opened)
        _require_write_capability(state_fd, capture_fd, ops)
        run_id = uuid.uuid4().hex
        run_fd = _ensure_private_directory(capture_fd, run_id, opened, exclusive=True)
        manifest = _Manifest(run_id, tuple(_intent_item(index, request) for index, request in enumerate(requests)))
        ctx = _RunContext(
            state_dir=state_dir,
            state_fd=state_fd,
            retention_fd=retention_fd,
            capture_fd=capture_fd,
            run_fd=run_fd,
            manifest=manifest,
            ops=ops,
        )
        ops.write_manifest(run_fd, _encode_manifest(manifest))
        outcome = _capture_items(ctx, requests)
        try:
            _finalize_run(ctx)
        except OSError as exc:
            message = f"capture run cleanup failed ({_errno_name(exc)}); recovery retries it"
            raise CaptureError(message, outcome) from exc
        return outcome
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _require_write_capability(state_fd: int, capture_fd: int, ops: CaptureOps) -> None:
    backups_fd = os.open("backups", _DIR_FLAGS, dir_fd=state_fd)
    try:
        if not ops.same_filesystem(os.fstat(capture_fd), os.fstat(backups_fd)):
            raise CaptureError("retention capture directory is on another filesystem than Skill backups (EXDEV)")
        try:
            ops.generation(capture_fd)
        except OSError as exc:
            raise CaptureError(f"FS_IOC_GETVERSION failed on the capture directory ({_errno_name(exc)})") from exc
    finally:
        os.close(backups_fd)


def _capture_items(ctx: _RunContext, requests: Sequence[CaptureRequest]) -> CaptureOutcome:
    results: dict[str, list[Path]] = {
        key: [] for key in (DELETED, RESTORED, QUARANTINED, "busy", PENDING_CLEANUP, "blocked")
    }
    stop_reason: str | None = None
    for index, request in enumerate(requests):
        try:
            disposition, stop_reason = _capture_one(ctx, index, request)
        except (OSError, CaptureError, ValueError, RecursionError) as exc:
            # The run directory keeps the unsettled item for recovery; report what already happened.
            raise CaptureError(f"capture of {request.path} failed: {exc}", _outcome(results, None)) from exc
        # Record the result before the manifest write so a failed write still reports the deletion.
        results[disposition].append(request.path)
        if disposition == DELETED:
            try:
                ctx.transition(index, DELETED)
            except OSError as exc:
                raise CaptureError(
                    f"deleted {request.path} but could not record it ({_errno_name(exc)}); recovery records it",
                    _outcome(results, None),
                ) from exc
        if stop_reason is not None:
            break
    return _outcome(results, stop_reason)


def _outcome(results: dict[str, list[Path]], stop_reason: str | None) -> CaptureOutcome:
    return CaptureOutcome(
        deleted=tuple(results[DELETED]),
        restored=tuple(results[RESTORED]),
        quarantined=tuple(results[QUARANTINED]),
        busy=tuple(results["busy"]),
        pending_cleanup=tuple(results[PENDING_CLEANUP]),
        stop_reason=stop_reason,
    )


def _capture_one(ctx: _RunContext, index: int, request: CaptureRequest) -> tuple[str, str | None]:
    try:
        group_fd = _open_relative(ctx.state_fd, request.parent)
    except OSError as exc:
        return "blocked", f"Skill backup group of {request.path} is missing or redirected ({_errno_name(exc)})"
    try:
        changed = _retained_problem(group_fd, request.retained, ctx.ops)
        if changed is not None:
            return "blocked", f"retained snapshot {changed} changed after planning; {request.path} was not moved"
        slot = _slot_name(index)
        try:
            ctx.ops.rename(request.name, slot, group_fd, ctx.run_fd)
        except OSError as exc:
            return "busy", f"could not capture {request.path} ({_errno_name(exc)}); nothing was moved"
        _sync_directories(group_fd, ctx.run_fd)
        ctx.transition(index, CAPTURED)
        mismatch = _verification_problem(ctx.run_fd, slot, request, ctx.ops)
        if mismatch is not None:
            disposition = _settle_unverified(ctx, index, group_fd)
            return disposition, (
                f"captured {request.path} did not match its reviewed identity ({mismatch}); "
                f"it was {disposition} and nothing was deleted"
            )
        # Re-check the kept snapshots last, before the verified record that recovery would finish deleting.
        changed = _retained_problem(group_fd, request.retained, ctx.ops)
        if changed is not None:
            disposition = _settle_unverified(ctx, index, group_fd)
            return disposition, (
                f"retained snapshot {changed} changed during capture; {request.path} was {disposition} "
                "and nothing was deleted"
            )
        ctx.transition(index, VERIFIED)
        try:
            _delete_verified(ctx, index)
        except _MountDetected as exc:
            _quarantine(ctx, index)
            return QUARANTINED, f"{exc}; the capture was quarantined"
        except _DeletionRefused as exc:
            # Demote the durable record before moving the slot back, so a crash after that move
            # never leaves a ``verified`` record that recovery would report as deleted.
            ctx.transition(index, CAPTURED)
            return _settle_unverified(ctx, index, group_fd), str(exc)
        except OSError:
            return PENDING_CLEANUP, None
        return DELETED, None
    finally:
        os.close(group_fd)


def _retained_problem(group_fd: int, retained: Sequence[RetainedIdentity], ops: CaptureOps) -> str | None:
    for kept in retained:
        current: tuple[int, int, int | None] | None
        try:
            if kept.node_kind == "symlink":
                link_stat = os.stat(kept.name, dir_fd=group_fd, follow_symlinks=False)
                current = (link_stat.st_dev, link_stat.st_ino, None) if stat.S_ISLNK(link_stat.st_mode) else None
            else:
                descriptor = os.open(kept.name, _DIR_FLAGS, dir_fd=group_fd)
                try:
                    identity = descriptor_identity(descriptor, ops)
                finally:
                    os.close(descriptor)
                current = (identity.device, identity.inode, identity.generation)
        except (OSError, CaptureError):
            return kept.name
        if current != (kept.device, kept.inode, kept.generation):
            return kept.name
    return None


def _verification_problem(run_fd: int, slot: str, request: CaptureRequest, ops: CaptureOps) -> str | None:
    try:
        descriptor = os.open(slot, _DIR_FLAGS, dir_fd=run_fd)
    except OSError as exc:
        return f"captured entry is not a real directory ({_errno_name(exc)})"
    try:
        identity = descriptor_identity(descriptor, ops)
        if identity != request.identity:
            return "device, inode, or generation differs"
        observation = observe_tree(descriptor, ops)
    except (OSError, CaptureError, ValueError, RecursionError) as exc:
        return f"captured tree could not be verified: {exc}"
    finally:
        os.close(descriptor)
    if observation.capture_digest != request.capture_digest or observation.entry_count != request.entry_count:
        return "capture digest or entry count differs"
    if observation.bytes != request.bytes:
        return "byte count differs"
    if observation.marker_source_id != request.source_id:
        return "managed marker source differs"
    return None


def _settle_unverified(ctx: _RunContext, index: int, group_fd: int | None) -> str:
    """Rename an unverified capture back if it is a real directory and its name is free.

    Anything else is quarantined: renaming a non-directory back could silently replace a
    non-directory created at that name meanwhile, while a directory can replace only an empty
    directory. Only a definitive rename error (the name was re-taken or the group is gone)
    leads to quarantine; a transient one raises and leaves the run for recovery.
    """

    item = ctx.manifest.items[index]
    if group_fd is not None and _is_directory_at(ctx.run_fd, item.slot) and not _lexists_at(group_fd, item.name):
        try:
            ctx.ops.rename(item.slot, item.name, ctx.run_fd, group_fd)
        except OSError as exc:
            if exc.errno not in _DEFINITIVE_ERRNOS:
                raise CaptureError(
                    f"could not restore unverified capture of {ctx.item_path(index)} ({_errno_name(exc)}); "
                    "it remains in the capture run for recovery"
                ) from exc
        else:
            _sync_directories(ctx.run_fd, group_fd)
            ctx.transition(index, RESTORED)
            return RESTORED
    _quarantine(ctx, index)
    return QUARANTINED


def _quarantine(ctx: _RunContext, index: int) -> None:
    opened: list[int] = []
    try:
        quarantine_fd = _ensure_private_directory(ctx.retention_fd, QUARANTINE_DIR, opened)
        run_quarantine_fd = _ensure_private_directory(quarantine_fd, ctx.manifest.run_id, opened)
        slot = ctx.manifest.items[index].slot
        try:
            ctx.ops.rename(slot, slot, ctx.run_fd, run_quarantine_fd)
        except OSError as exc:
            raise CaptureError(
                f"could not restore or quarantine unverified capture of {ctx.item_path(index)} ({_errno_name(exc)}); "
                "it remains in the capture run for recovery"
            ) from exc
        _sync_directories(ctx.run_fd, run_quarantine_fd)
        _record_quarantine(ctx, index, run_quarantine_fd)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _record_quarantine(ctx: _RunContext, index: int, run_quarantine_fd: int) -> None:
    ctx.transition(index, QUARANTINED)
    ctx.ops.write_manifest(run_quarantine_fd, _encode_manifest(ctx.manifest))


def _delete_verified(ctx: _RunContext, index: int) -> None:
    """The only destructive call: it requires the on-disk manifest to record ``verified``."""

    path = ctx.item_path(index)
    try:
        persisted = _read_manifest(ctx.run_fd)
    except CaptureError as exc:
        raise _DeletionRefused(f"refusing to delete {path}: capture manifest is invalid ({exc})") from exc
    if persisted.run_id != ctx.manifest.run_id or index >= len(persisted.items):
        raise _DeletionRefused(f"refusing to delete {path}: capture manifest does not belong to this run")
    item = persisted.items[index]
    if item.state != VERIFIED:
        raise _DeletionRefused(f"refusing to delete {path}: its capture is recorded as {item.state}, not verified")
    try:
        descriptor = os.open(item.slot, _DIR_FLAGS, dir_fd=ctx.run_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise _DeletionRefused(f"refusing to delete {path}: capture slot is not a real directory") from exc
        raise  # transient (EACCES, EMFILE, ...): the caller keeps the verified slot as pending cleanup
    try:
        identity = descriptor_identity(descriptor, ctx.ops)
        if identity != item.identity:
            raise _DeletionRefused(f"refusing to delete {path}: capture slot identity changed after verification")
        mounted = _prepare_for_rmtree(ctx.run_fd, descriptor, ctx.ops)
    finally:
        os.close(descriptor)
    if mounted is not None:
        raise _MountDetected(f"refusing to delete {path}: {mounted}")
    ctx.ops.rmtree(item.slot, ctx.run_fd)
    _sync_directories(ctx.run_fd)


def _prepare_for_rmtree(parent_fd: int, root_fd: int, ops: CaptureOps) -> str | None:
    """Walk a verified slot just before ``rmtree``; return why it must not be deleted, or ``None``.

    A directory on another device or mount is reported, and nothing below it is touched.
    Every other directory that lacks owner ``rwx`` gets mode ``0700`` through its own
    no-follow descriptor, so ``rmtree`` can empty a read-only subdirectory. Nothing is
    changed by path and no link is followed. OSError propagates: an unreadable tree is
    retried as pending cleanup, never deleted.
    """

    if _crosses_mount(parent_fd, root_fd, ops):
        return "capture slot is a mount point or on another device"
    try:
        _prepare_tree(root_fd, b"", 0, _Boundary.of(root_fd, ops))
    except CaptureError as exc:
        return str(exc)
    return None


def _prepare_tree(dir_fd: int, prefix: bytes, depth: int, boundary: _Boundary) -> None:
    """Grant owner access to ``dir_fd``, whose boundary was checked, then check and descend."""

    if depth > _MAX_DEPTH:
        raise CaptureError("capture slot is too deep to check for mounts")
    if stat.S_IMODE(os.fstat(dir_fd).st_mode) & _PRIVATE_MODE != _PRIVATE_MODE:
        _fchmod(dir_fd, _PRIVATE_MODE)
    with os.scandir(dir_fd) as iterator:
        entries = list(iterator)
    for entry in entries:
        name = os.fsencode(entry.name)
        relative = prefix + b"/" + name if prefix else name
        entry_stat = entry.stat(follow_symlinks=False)
        boundary.check_entry(entry_stat, relative)
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue
        child = os.open(entry.name, _DIR_FLAGS, dir_fd=dir_fd)
        try:
            if not os.path.samestat(entry_stat, os.fstat(child)):
                raise CaptureError("capture slot changed while it was checked for mounts")
            boundary.check_descriptor(child, relative)
            _prepare_tree(child, relative, depth + 1, boundary)
        finally:
            os.close(child)


def _finalize_run(ctx: _RunContext) -> bool:
    """Remove a run directory once no capture slot remains; keep it for recovery otherwise."""

    names = set(os.listdir(ctx.run_fd))
    if any(_SLOT_NAME.fullmatch(name) for name in names):
        return False
    for name in sorted(names):
        if _MANIFEST_TEMP.fullmatch(name) or name == MANIFEST_NAME:
            os.unlink(name, dir_fd=ctx.run_fd)
    os.rmdir(ctx.manifest.run_id, dir_fd=ctx.capture_fd)
    _sync_directories(ctx.capture_fd)
    return True


# --- recovery -----------------------------------------------------------------


def recover_interrupted(state_dir: Path, ops: CaptureOps) -> tuple[RecoveryEvent, ...]:
    """Settle interrupted runs: restore or quarantine unverified captures, finish verified deletes."""

    platform_problem = _platform_problem(ops)
    if platform_problem is not None:
        if _has_capture_runs(state_dir / RETENTION_DIR / CAPTURE_DIR):
            raise CaptureError(f"retention capture runs cannot be recovered on this platform: {platform_problem}")
        return ()
    opened: list[int] = []
    try:
        try:
            state_fd = _open_directory(None, str(state_dir), opened)
        except FileNotFoundError:
            return ()
        retention_fd = _open_existing_private(state_fd, RETENTION_DIR, opened)
        if retention_fd is None:
            return ()
        capture_fd = _open_existing_private(retention_fd, CAPTURE_DIR, opened)
        events: list[RecoveryEvent] = []
        if capture_fd is not None:
            for run_name in sorted(os.listdir(capture_fd)):
                events.extend(_recover_run(state_dir, state_fd, retention_fd, capture_fd, run_name, ops))
        events.extend(_remove_empty_quarantine_runs(state_dir, retention_fd))
        return tuple(events)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _has_capture_runs(capture: Path) -> bool:
    try:
        with os.scandir(capture) as iterator:
            return next(iterator, None) is not None
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell; refuse rather than ignore interrupted runs


def _remove_empty_quarantine_runs(state_dir: Path, retention_fd: int) -> list[RecoveryEvent]:
    """Remove ``quarantine/<run>`` directories that a crash left empty before their rename."""

    opened: list[int] = []
    try:
        quarantine_fd = _open_existing_private(retention_fd, QUARANTINE_DIR, opened)
        if quarantine_fd is None:
            return []
        events: list[RecoveryEvent] = []
        for name in sorted(os.listdir(quarantine_fd)):
            if _RUN_NAME.fullmatch(name) is None:
                continue
            try:
                os.rmdir(name, dir_fd=quarantine_fd)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.ENOENT):
                    continue  # it holds a quarantined capture (operator review) or is not a directory
                raise
            events.append(RecoveryEvent(state_dir / RETENTION_DIR / QUARANTINE_DIR / name, CLEANED_UP))
        if events:
            _sync_directories(quarantine_fd)
        return events
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _recover_run(
    state_dir: Path,
    state_fd: int,
    retention_fd: int,
    capture_fd: int,
    run_name: str,
    ops: CaptureOps,
) -> list[RecoveryEvent]:
    run_path = state_dir / RETENTION_DIR / CAPTURE_DIR / run_name
    if _RUN_NAME.fullmatch(run_name) is None:
        raise CaptureError(f"unexpected retention capture entry: {run_path}")
    opened: list[int] = []
    try:
        try:
            run_fd = _open_directory(capture_fd, run_name, opened)
        except OSError as exc:
            raise CaptureError(f"capture run must be a real directory: {run_path}") from exc
        names = set(os.listdir(run_fd))
        temporary = {name for name in names if _MANIFEST_TEMP.fullmatch(name)}
        if MANIFEST_NAME not in names:
            if names - temporary:
                raise CaptureError(f"capture run without a manifest contains entries: {run_path}")
            for name in sorted(temporary):
                os.unlink(name, dir_fd=run_fd)
            os.rmdir(run_name, dir_fd=capture_fd)
            return [RecoveryEvent(run_path, CLEANED_UP)]
        manifest = _read_manifest(run_fd)
        if manifest.run_id != run_name:
            raise CaptureError(f"capture manifest run_id does not match its directory: {run_path}")
        if names - temporary - {MANIFEST_NAME} - {item.slot for item in manifest.items}:
            raise CaptureError(f"capture run contains unknown entries: {run_path}")
        ctx = _RunContext(
            state_dir=state_dir,
            state_fd=state_fd,
            retention_fd=retention_fd,
            capture_fd=capture_fd,
            run_fd=run_fd,
            manifest=manifest,
            ops=ops,
        )
        events = [event for item in manifest.items for event in _recover_item(ctx, item.index)]
        if _finalize_run(ctx):
            events.append(RecoveryEvent(run_path, CLEANED_UP))
        return events
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _recover_item(ctx: _RunContext, index: int) -> list[RecoveryEvent]:
    item = ctx.manifest.items[index]
    path = ctx.item_path(index)
    present = _lexists_at(ctx.run_fd, item.slot)
    if not present and item.state not in _TERMINAL_STATES and _recover_quarantined(ctx, index):
        # A crash after the quarantine rename: the slot was moved, not deleted or restored.
        return [RecoveryEvent(path, QUARANTINED)]
    if item.state in (INTENT, CAPTURED):
        if not present:
            return []
        try:
            group_fd: int | None = _open_relative(ctx.state_fd, item.parent)
        except OSError as exc:
            if exc.errno not in _DEFINITIVE_ERRNOS:
                raise CaptureError(
                    f"cannot open the Skill backup group of {path} ({_errno_name(exc)}); "
                    "the capture run is kept for a retry"
                ) from exc
            group_fd = None
        try:
            return [RecoveryEvent(path, _settle_unverified(ctx, index, group_fd))]
        finally:
            if group_fd is not None:
                os.close(group_fd)
    if item.state == VERIFIED:
        if present:
            try:
                _delete_verified(ctx, index)
            except _DeletionRefused:
                # Never restore a tree whose deletion may have started.
                _quarantine(ctx, index)
                return [RecoveryEvent(path, QUARANTINED)]
            except OSError:
                return [RecoveryEvent(path, PENDING_CLEANUP)]
        ctx.transition(index, DELETED)
        return [RecoveryEvent(path, DELETED)]
    if present:
        raise CaptureError(f"capture slot for {path} remains although its manifest records {item.state}")
    return []


def _recover_quarantined(ctx: _RunContext, index: int) -> bool:
    """Record ``quarantined`` when the slot already sits in ``quarantine/<run>``."""

    opened: list[int] = []
    try:
        quarantine_fd = _open_existing_private(ctx.retention_fd, QUARANTINE_DIR, opened)
        run_quarantine_fd = (
            None if quarantine_fd is None else _open_existing_private(quarantine_fd, ctx.manifest.run_id, opened)
        )
        if run_quarantine_fd is None or not _lexists_at(run_quarantine_fd, ctx.manifest.items[index].slot):
            return False
        _record_quarantine(ctx, index, run_quarantine_fd)
        return True
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


# --- read-only inspection -------------------------------------------------------


def inspect_capture_state(state_dir: Path) -> CaptureInspection:
    """Report interrupted runs as pending recovery and unsafe capture state as blockers, read-only."""

    root = state_dir / RETENTION_DIR
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return CaptureInspection()
    except OSError as exc:
        return CaptureInspection(blockers=(CaptureFinding(root, f"cannot inspect retention capture state: {exc}"),))
    if not stat.S_ISDIR(root_stat.st_mode):
        return CaptureInspection(blockers=(CaptureFinding(root, "retention capture root must be a real directory"),))
    if not _private_on_posix(root):
        return CaptureInspection(
            blockers=(CaptureFinding(root, "retention capture root must be owned by the current user with mode 0700"),)
        )
    pending: list[CaptureFinding] = []
    blockers: list[CaptureFinding] = []
    try:
        names = set(os.listdir(root))
        blockers.extend(
            CaptureFinding(root / name, "unexpected retention capture entry")
            for name in sorted(names - {CAPTURE_DIR, QUARANTINE_DIR})
        )
        # Capture runs first: they name the quarantine entries that recovery still has to record.
        awaiting: dict[str, frozenset[str]] = {}
        for name in (CAPTURE_DIR, QUARANTINE_DIR):
            path = root / name
            if name not in names:
                continue
            if not _private_on_posix(path):
                blockers.append(
                    CaptureFinding(path, "capture directory must be owned by the current user with mode 0700")
                )
            elif name == CAPTURE_DIR:
                awaiting = _inspect_runs(path, pending, blockers)
            else:
                _inspect_quarantine(path, awaiting, pending, blockers)
    except OSError as exc:
        blockers.append(CaptureFinding(root, f"cannot inspect retention capture state: {exc}"))
    return CaptureInspection(pending=tuple(pending), blockers=tuple(blockers))


def _private_on_posix(path: Path) -> bool:
    """Return whether ``path`` is private; Windows modes are synthetic, so it never fails there."""

    if os.name != "posix":
        return True
    path_stat = os.lstat(path)
    return not stat.S_ISDIR(path_stat.st_mode) or _is_private(path_stat)


@dataclass(frozen=True, slots=True)
class _RunFinding:
    pending: str | None = None
    blocker: str | None = None
    # Unsettled slots missing from the run; a crash may have left them in ``quarantine/<run>``.
    moved_slots: frozenset[str] = frozenset()


def _inspect_runs(
    root: Path,
    pending: list[CaptureFinding],
    blockers: list[CaptureFinding],
) -> dict[str, frozenset[str]]:
    awaiting: dict[str, frozenset[str]] = {}
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        blockers.append(CaptureFinding(root, "capture directory must be a real directory"))
        return awaiting
    for run_name in sorted(os.listdir(root)):
        run_path = root / run_name
        if _RUN_NAME.fullmatch(run_name) is None:
            blockers.append(CaptureFinding(run_path, "unexpected retention capture entry"))
            continue
        try:
            run_fd = os.open(run_path, _DIR_FLAGS)
        except FileNotFoundError:
            continue  # a concurrent apply finished this run after it was listed
        except OSError:
            blockers.append(CaptureFinding(run_path, "capture run must be a real directory"))
            continue
        try:
            finding = _inspect_run(run_fd, run_name)
        finally:
            os.close(run_fd)
        if finding.blocker is not None:
            blockers.append(CaptureFinding(run_path, finding.blocker))
        elif finding.pending is not None:
            pending.append(CaptureFinding(run_path, finding.pending))
            awaiting[run_name] = finding.moved_slots
    return awaiting


def _inspect_run(run_fd: int, run_name: str) -> _RunFinding:
    """Classify one run; an empty finding means a concurrent apply already removed it."""

    names = set(os.listdir(run_fd))
    if os.fstat(run_fd).st_nlink == 0:
        return _RunFinding()
    temporary = {name for name in names if _MANIFEST_TEMP.fullmatch(name)}
    if MANIFEST_NAME not in names:
        if names - temporary:
            return _RunFinding(blocker="capture run without a manifest contains entries")
        return _RunFinding(pending="interrupted capture run has no captured entries")
    try:
        manifest = _read_manifest(run_fd)
    except FileNotFoundError:
        return _RunFinding()
    except (OSError, CaptureError) as exc:
        return _RunFinding(blocker=f"malformed capture manifest: {exc}")
    if manifest.run_id != run_name:
        return _RunFinding(blocker="capture manifest run_id does not match its directory")
    if names - temporary - {MANIFEST_NAME} - {item.slot for item in manifest.items}:
        return _RunFinding(blocker="capture run contains unknown entries")
    for item in manifest.items:
        if item.state in _TERMINAL_STATES and item.slot in names:
            # Recovery refuses this run; report it before an apply instead of after.
            return _RunFinding(blocker=f"capture slot {item.slot} remains although its manifest records {item.state}")
    counts = Counter(item.state for item in manifest.items)
    summary = ", ".join(f"{state}={count}" for state, count in sorted(counts.items()))
    moved = frozenset(
        item.slot for item in manifest.items if item.state not in _TERMINAL_STATES and item.slot not in names
    )
    return _RunFinding(pending=f"interrupted capture run awaiting recovery ({summary})", moved_slots=moved)


def _inspect_quarantine(
    root: Path,
    awaiting: dict[str, frozenset[str]],
    pending: list[CaptureFinding],
    blockers: list[CaptureFinding],
) -> None:
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        blockers.append(CaptureFinding(root, "quarantine directory must be a real directory"))
        return
    for name in sorted(os.listdir(root)):
        path = root / name
        try:
            entries = _directory_names(path) if _RUN_NAME.fullmatch(name) else None
        except FileNotFoundError:
            continue  # a concurrent apply removed an empty quarantine directory
        if entries is not None and not entries:
            pending.append(CaptureFinding(path, "empty quarantine directory from an interrupted capture"))
        elif entries is not None and name in awaiting and entries <= awaiting[name]:
            pending.append(
                CaptureFinding(path, "interrupted capture moved entries here; recovery records them and stops")
            )
        else:
            blockers.append(CaptureFinding(path, "quarantined capture requires operator review and manual removal"))


def _directory_names(path: Path) -> frozenset[str] | None:
    """Return the entries of a real directory, ``None`` for anything else; FileNotFoundError propagates."""

    try:
        descriptor = os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        raise
    except OSError:
        return None
    try:
        return frozenset(os.listdir(descriptor))
    finally:
        os.close(descriptor)


# --- manifest -------------------------------------------------------------------


def _intent_item(index: int, request: CaptureRequest) -> _ManifestItem:
    return _ManifestItem(
        index=index,
        parent=request.parent,
        name=request.name,
        identity=request.identity,
        capture_digest=request.capture_digest,
        entry_count=request.entry_count,
        bytes=request.bytes,
        source_id=request.source_id,
        state=INTENT,
    )


def _encode_manifest(manifest: _Manifest) -> bytes:
    payload = {
        "schema_version": 1,
        "format": _MANIFEST_FORMAT,
        "run_id": manifest.run_id,
        "items": [
            {
                "index": item.index,
                "slot": item.slot,
                "category": "skill_backup",
                "parent": list(item.parent),
                "name": item.name,
                "device": item.identity.device,
                "inode": item.identity.inode,
                "generation": item.identity.generation,
                "capture_digest": item.capture_digest,
                "entry_count": item.entry_count,
                "bytes": item.bytes,
                "source_id": item.source_id,
                "state": item.state,
            }
            for item in manifest.items
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_manifest(run_fd: int) -> _Manifest:
    descriptor = os.open(MANIFEST_NAME, _FILE_FLAGS, dir_fd=run_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CaptureError("capture manifest must be a regular file")
        data = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_MANIFEST_BYTES:
        raise CaptureError("capture manifest is too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # bad UTF-8 or JSON, oversized integers, or deep nesting
        raise CaptureError("capture manifest is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "format", "run_id", "items"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["format"] != _MANIFEST_FORMAT
        or not isinstance(payload["run_id"], str)
        or _RUN_NAME.fullmatch(payload["run_id"]) is None
        or not isinstance(payload["items"], list)
        or len(payload["items"]) > _MAX_ITEMS
    ):
        raise CaptureError("capture manifest has an unsupported shape")
    items = tuple(_parse_item(position, raw) for position, raw in enumerate(payload["items"]))
    return _Manifest(run_id=payload["run_id"], items=items)


def _parse_item(position: int, raw: object) -> _ManifestItem:
    if not isinstance(raw, dict) or set(raw) != _ITEM_KEYS:
        raise CaptureError(f"capture manifest item {position} has an unsupported shape")
    integers = ("index", "device", "inode", "generation", "entry_count", "bytes")
    parent = raw["parent"]
    if (
        any(type(raw[key]) is not int or raw[key] < 0 for key in integers)
        or raw["index"] != position
        or raw["slot"] != _slot_name(position)
        or raw["category"] != "skill_backup"
        or raw["state"] not in _STATES
        or not isinstance(parent, list)
        or len(parent) != 3
        or parent[0] != "backups"
        or not all(isinstance(part, str) and _OWNER_NAME.fullmatch(part) for part in parent[1:])
        or parent[2] == "instructions"
        or not isinstance(raw["name"], str)
        or _SNAPSHOT_NAME.fullmatch(raw["name"]) is None
        or not isinstance(raw["capture_digest"], str)
        or _HEX_DIGEST.fullmatch(raw["capture_digest"]) is None
        or not isinstance(raw["source_id"], str)
        or not raw["source_id"]
    ):
        raise CaptureError(f"capture manifest item {position} is invalid")
    return _ManifestItem(
        index=position,
        parent=tuple(parent),
        name=raw["name"],
        identity=CaptureIdentity(raw["device"], raw["inode"], raw["generation"]),
        capture_digest=raw["capture_digest"],
        entry_count=raw["entry_count"],
        bytes=raw["bytes"],
        source_id=raw["source_id"],
        state=raw["state"],
    )


# --- descriptor helpers -----------------------------------------------------------


def _slot_name(index: int) -> str:
    return f"item-{index:04d}"


def _open_directory(dir_fd: int | None, name: str, opened: list[int]) -> int:
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    opened.append(descriptor)
    return descriptor


def _open_relative(base_fd: int, parts: Sequence[str]) -> int:
    """Open ``parts`` below ``base_fd`` one no-follow component at a time."""

    current: int | None = None
    try:
        for part in parts:
            following = os.open(part, _DIR_FLAGS, dir_fd=base_fd if current is None else current)
            if current is not None:
                os.close(current)
            current = following
    except BaseException:
        if current is not None:
            os.close(current)
        raise
    if current is None:
        raise CaptureError("capture parent must have at least one component")
    return current


def _ensure_private_directory(parent_fd: int, name: str, opened: list[int], *, exclusive: bool = False) -> int:
    created = False
    try:
        os.mkdir(name, _PRIVATE_MODE, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        if exclusive:
            raise
    try:
        descriptor = _open_directory(parent_fd, name, opened)
    except OSError as exc:
        raise CaptureError(f"retention capture directory {name} must be a real directory") from exc
    if created:
        _fchmod(descriptor, _PRIVATE_MODE)
        _sync_directories(parent_fd)
    if not _is_private(os.fstat(descriptor)):
        raise CaptureError(f"retention capture directory {name} must be owned by the current user with mode 0700")
    return descriptor


def _open_existing_private(parent_fd: int, name: str, opened: list[int]) -> int | None:
    try:
        descriptor = _open_directory(parent_fd, name, opened)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CaptureError(f"retention capture directory {name} must be a real directory") from exc
    if not _is_private(os.fstat(descriptor)):
        raise CaptureError(f"retention capture directory {name} must be owned by the current user with mode 0700")
    return descriptor


def _is_directory_at(dir_fd: int, name: str) -> bool:
    return stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)


def _lexists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _sync_directories(*descriptors: int) -> None:
    for descriptor in descriptors:
        os.fsync(descriptor)


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))
