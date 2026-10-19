"""Plan and apply bounded retention for Bridge-generated operational state."""

from __future__ import annotations

import importlib
import os
import re
import stat
from dataclasses import dataclass, replace
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
from agent_config_bridge.retention_capture import (
    DEFAULT_CAPTURE_OPS,
    PENDING_CLEANUP,
    QUARANTINED,
    RESTORED,
    CaptureCapability,
    CaptureError,
    CaptureIdentity,
    CaptureOps,
    CaptureOutcome,
    CaptureRequest,
    RecoveryEvent,
    RetainedIdentity,
    capture_and_delete,
    inspect_capture_state,
    observe_directory,
    probe_capability,
    recover_interrupted,
    retained_identity,
)

__all__ = [
    "CaptureCapability",
    "RecoveryEvent",
    "RetentionAction",
    "RetentionApplyStopped",
    "RetentionBlocker",
    "RetentionDeferral",
    "RetentionError",
    "RetentionPlan",
    "RetentionRecoveryPending",
    "RetentionResult",
    "RetentionRetained",
    "apply_retention_plan",
    "build_retention_plan",
]

_BUILD_NAME = re.compile(r"^[0-9a-f]{20}$")
_OWNER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SNAPSHOT_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
_PRIVATE_FILE_MODE = 0o600
_BUILD_DEFERRAL = "marketplace builds are kept until render and publish share the retention lock"
_SYMLINK_DEFERRAL = "terminal symlink Skill backups are not captured by generation-bound retention"
_UNPROBED = CaptureCapability(capable=False, reason="capture capability was not probed")
# Machine-readable deferral reasons; ``reason`` stays the human explanation.
_DEFER_BUILD = "marketplace_build"
_DEFER_SYMLINK = "symlink_snapshot"
_DEFER_UNAVAILABLE = "capture_unavailable"
_DEFER_QUIESCENCE = "quiescence"


class RetentionError(RuntimeError):
    """Raised when retention cannot safely inspect or mutate Bridge state."""


@dataclass(frozen=True, slots=True)
class RetentionAction:
    """One verified Skill backup directory selected for generation-bound deletion."""

    category: str
    path: Path
    node_kind: str
    bytes: int
    mtime_ns: int
    device: int
    inode: int
    generation: int | None = None
    capture_digest: str | None = None
    entry_count: int | None = None


@dataclass(frozen=True, slots=True)
class RetentionBlocker:
    """One state entry that prevents every retention deletion."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionDeferral:
    """A retention candidate that is reported and kept; it is never an action.

    ``reason_code`` is one of ``marketplace_build``, ``symlink_snapshot``,
    ``capture_unavailable``, or ``quiescence``.
    """

    category: str
    path: Path
    node_kind: str
    bytes: int
    reason: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class RetentionRecoveryPending:
    """An interrupted capture run that the next apply settles; not a blocker."""

    path: Path
    detail: str


@dataclass(frozen=True, slots=True)
class RetentionRetained:
    """Identity of a kept snapshot whose group has deletion candidates."""

    path: Path
    node_kind: str
    device: int
    inode: int
    generation: int | None


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
    deferred: tuple[RetentionDeferral, ...] = ()
    recovery_pending: tuple[RetentionRecoveryPending, ...] = ()
    retained: tuple[RetentionRetained, ...] = ()
    capability: CaptureCapability = _UNPROBED
    quiescence_seconds: int = 0
    quiescence_cutoff_ns: int = 0

    @property
    def has_changes(self) -> bool:
        """Return whether the plan contains safe deletion candidates; deferrals are not actions."""

        return bool(self.actions)

    @property
    def has_blockers(self) -> bool:
        """Return whether any unsafe state entry blocks all deletion."""

        return bool(self.blockers)

    @property
    def reclaimable_bytes(self) -> int:
        """Return the regular-file bytes represented by deletion candidates."""

        return sum(action.bytes for action in self.actions)

    @property
    def deferred_bytes(self) -> int:
        """Return the bytes represented by deferred candidates."""

        return sum(item.bytes for item in self.deferred)

    @property
    def fully_converged(self) -> bool:
        """Return whether nothing is selected, deferred, blocked, or awaiting recovery."""

        return not (self.actions or self.deferred or self.blockers or self.recovery_pending)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Outcome of one locked apply; ``deleted`` lists only verified captures that were removed.

    ``restored``, ``quarantined``, and ``pending_cleanup`` include the matching
    outcomes of interrupted-run recovery, which ``recovered`` lists in full.
    """

    deleted: tuple[RetentionAction, ...]
    reclaimed_bytes: int
    final_plan: RetentionPlan
    restored: tuple[Path, ...] = ()
    quarantined: tuple[Path, ...] = ()
    busy: tuple[Path, ...] = ()
    pending_cleanup: tuple[Path, ...] = ()
    recovered: tuple[RecoveryEvent, ...] = ()


class RetentionApplyStopped(RetentionError):
    """Raised when capture stopped safely; ``result`` reports what happened before it stopped."""

    def __init__(self, message: str, result: RetentionResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    path: Path
    node_kind: str
    bytes: int
    mtime_ns: int
    device: int
    inode: int
    ctime_ns: int

    def action(
        self,
        category: str,
        *,
        identity: CaptureIdentity | None = None,
        capture_digest: str | None = None,
        entry_count: int | None = None,
    ) -> RetentionAction:
        return RetentionAction(
            category=category,
            path=self.path,
            node_kind=self.node_kind,
            bytes=self.bytes,
            mtime_ns=self.mtime_ns,
            device=self.device,
            inode=self.inode,
            generation=None if identity is None else identity.generation,
            capture_digest=capture_digest,
            entry_count=entry_count,
        )

    def deferral(self, category: str, reason_code: str, reason: str) -> RetentionDeferral:
        return RetentionDeferral(
            category=category,
            path=self.path,
            node_kind=self.node_kind,
            bytes=self.bytes,
            reason=reason,
            reason_code=reason_code,
        )


def build_retention_plan(
    config: BridgeConfig,
    *,
    ops: CaptureOps | None = None,
    quiescence_cutoff_ns: int | None = None,
) -> RetentionPlan:
    """Inspect generated state without writing files, locks, timestamps, or ``retention/``."""

    capture_ops = ops or DEFAULT_CAPTURE_OPS
    cutoff_ns = (
        capture_ops.now_ns() - capture_ops.quiescence_ns if quiescence_cutoff_ns is None else quiescence_cutoff_ns
    )
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
            ops=capture_ops,
            cutoff_ns=cutoff_ns,
        )
    capability = probe_capability(state_dir, capture_ops)

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
    build_candidates = _select_build_candidates(
        builds,
        limit=config.retention.marketplace_builds,
        published_digest=published_digest,
    )
    deferred = [entry.deferral("marketplace_build", _DEFER_BUILD, _BUILD_DEFERRAL) for entry in build_candidates]
    backup_groups, backup_bytes = _scan_skill_backups(config, blockers, excluded)
    selection = _select_backup_candidates(
        backup_groups,
        limit=config.retention.skill_backups,
        capability=capability,
        cutoff_ns=cutoff_ns,
        ops=capture_ops,
        blockers=blockers,
    )
    inspection = inspect_capture_state(state_dir)
    blockers.extend(RetentionBlocker(finding.path, finding.detail) for finding in inspection.blockers)
    return _plan(
        config.retention,
        builds=builds,
        build_bytes=build_bytes,
        backup_groups=backup_groups,
        backup_bytes=backup_bytes,
        actions=selection.actions,
        deferred=[*deferred, *selection.deferred],
        retained=selection.retained,
        recovery_pending=[RetentionRecoveryPending(item.path, item.detail) for item in inspection.pending],
        blockers=blockers,
        excluded=excluded,
        capability=capability,
        ops=capture_ops,
        cutoff_ns=cutoff_ns,
    )


def apply_retention_plan(
    config: BridgeConfig,
    reviewed_plan: RetentionPlan,
    *,
    ops: CaptureOps | None = None,
) -> RetentionResult:
    """Recover, revalidate, then capture, verify, and delete reviewed candidates under the lock.

    A returned result always has a converged final plan: no deletion candidates
    and no blockers. Every apply that ends otherwise raises
    ``RetentionApplyStopped`` carrying what it did, so ``--yes`` exits 0 with
    ``applied: true`` only when ``converged`` is true.
    """

    capture_ops = ops or DEFAULT_CAPTURE_OPS
    if reviewed_plan.has_blockers:
        raise RetentionError("retention plan contains blockers; no entries were deleted")
    cutoff_ns = reviewed_plan.quiescence_cutoff_ns
    with _OperationLock(config.state_dir):
        recovered = _recover(config, capture_ops)
        fresh_plan = build_retention_plan(config, ops=capture_ops, quiescence_cutoff_ns=cutoff_ns)
        if _comparable(fresh_plan) != _comparable(reviewed_plan):
            if not recovered:
                raise RetentionError("generated state changed after retention planning; review a fresh plan")
            # Recovery changed the candidates; this run only recovers and reports the fresh plan.
            return _require_converged(
                _recovery_result(fresh_plan, recovered),
                "interrupted retention capture recovery changed the reviewed plan; no reviewed candidate was "
                "deleted; review a fresh plan",
            )
        if fresh_plan.has_blockers:
            raise RetentionError("retention state became unsafe; no entries were deleted")
        if not fresh_plan.actions:
            final_plan = build_retention_plan(config, ops=capture_ops, quiescence_cutoff_ns=cutoff_ns)
            return _require_converged(
                _recovery_result(final_plan, recovered),
                "retention state did not remain unchanged during no-change validation; "
                "no reviewed candidate was deleted",
            )
        try:
            outcome = capture_and_delete(config.state_dir, _capture_requests(config, fresh_plan), capture_ops)
        except (OSError, CaptureError) as exc:
            partial = exc.outcome if isinstance(exc, CaptureError) else None
            message = f"retention capture failed: {exc}; review a fresh plan before retrying"
            if partial is None:
                raise RetentionError(message) from exc
            final_plan = build_retention_plan(config, ops=capture_ops, quiescence_cutoff_ns=cutoff_ns)
            raise RetentionApplyStopped(message, _capture_result(fresh_plan, partial, final_plan, recovered)) from exc
        final_plan = build_retention_plan(config, ops=capture_ops, quiescence_cutoff_ns=cutoff_ns)
        result = _capture_result(fresh_plan, outcome, final_plan, recovered)
        if outcome.stop_reason is not None:
            raise RetentionApplyStopped(
                f"{outcome.stop_reason}; retention stopped after deleting {len(result.deleted)} verified entries",
                result,
            )
        return _require_converged(result, "retention state changed during apply")


def _require_converged(result: RetentionResult, message: str) -> RetentionResult:
    """Enforce the apply contract: success means no candidates and no blockers remain."""

    final_plan = result.final_plan
    if final_plan.has_changes or final_plan.has_blockers:
        raise RetentionApplyStopped(
            f"{message} ({len(final_plan.actions)} deletion candidates and {len(final_plan.blockers)} blockers remain)",
            result,
        )
    return result


def _recover(config: BridgeConfig, ops: CaptureOps) -> tuple[RecoveryEvent, ...]:
    try:
        recovered = recover_interrupted(config.state_dir, ops)
    except CaptureError as exc:
        raise RetentionError(f"interrupted retention capture needs operator review: {exc}") from exc
    except OSError as exc:
        raise RetentionError(
            f"interrupted retention capture recovery failed: {exc}; the capture state is kept for a retry"
        ) from exc
    if any(event.outcome == QUARANTINED for event in recovered):
        raise RetentionApplyStopped(
            "interrupted retention capture recovery quarantined entries that need operator review",
            _recovery_result(build_retention_plan(config, ops=ops), recovered),
        )
    return recovered


def _recovered_paths(recovered: tuple[RecoveryEvent, ...], outcome: str) -> tuple[Path, ...]:
    return tuple(event.path for event in recovered if event.outcome == outcome)


def _recovery_result(final_plan: RetentionPlan, recovered: tuple[RecoveryEvent, ...]) -> RetentionResult:
    """A result for a run that deleted nothing new; recovery outcomes also fill the per-path lists."""

    return RetentionResult(
        deleted=(),
        reclaimed_bytes=0,
        final_plan=final_plan,
        restored=_recovered_paths(recovered, RESTORED),
        quarantined=_recovered_paths(recovered, QUARANTINED),
        pending_cleanup=_recovered_paths(recovered, PENDING_CLEANUP),
        recovered=recovered,
    )


def _comparable(plan: RetentionPlan) -> RetentionPlan:
    return replace(plan, recovery_pending=())


def _capture_requests(config: BridgeConfig, plan: RetentionPlan) -> tuple[CaptureRequest, ...]:
    requests: list[CaptureRequest] = []
    for action in plan.actions:
        parts = action.path.relative_to(config.state_dir).parts
        retained = tuple(
            RetainedIdentity(kept.path.name, kept.node_kind, kept.device, kept.inode, kept.generation)
            for kept in plan.retained
            if kept.path.parent == action.path.parent
        )
        if (
            action.category != "skill_backup"
            or action.node_kind != "directory"
            or len(parts) != 4
            or parts[0] != "backups"
            or action.generation is None
            or action.capture_digest is None
            or action.entry_count is None
            or not retained
        ):
            raise RetentionError(f"retention action is not bound to a capturable identity: {action.path}")
        requests.append(
            CaptureRequest(
                path=action.path,
                parent=parts[:-1],
                name=parts[-1],
                identity=CaptureIdentity(action.device, action.inode, action.generation),
                capture_digest=action.capture_digest,
                entry_count=action.entry_count,
                bytes=action.bytes,
                source_id=f"skills/{parts[2]}",
                retained=retained,
            )
        )
    return tuple(requests)


def _capture_result(
    plan: RetentionPlan,
    outcome: CaptureOutcome,
    final_plan: RetentionPlan,
    recovered: tuple[RecoveryEvent, ...],
) -> RetentionResult:
    deleted_paths = set(outcome.deleted)
    deleted = tuple(action for action in plan.actions if action.path in deleted_paths)
    return RetentionResult(
        deleted=deleted,
        reclaimed_bytes=sum(action.bytes for action in deleted),
        final_plan=final_plan,
        restored=_recovered_paths(recovered, RESTORED) + outcome.restored,
        quarantined=outcome.quarantined,
        busy=outcome.busy,
        pending_cleanup=_recovered_paths(recovered, PENDING_CLEANUP) + outcome.pending_cleanup,
        recovered=recovered,
    )


def _plan(
    limits: RetentionConfig,
    *,
    builds: list[_ScannedEntry] | None = None,
    build_bytes: int = 0,
    backup_groups: dict[tuple[str, str], list[_ScannedEntry]] | None = None,
    backup_bytes: int = 0,
    actions: list[RetentionAction] | None = None,
    deferred: list[RetentionDeferral] | None = None,
    retained: list[RetentionRetained] | None = None,
    recovery_pending: list[RetentionRecoveryPending] | None = None,
    blockers: list[RetentionBlocker],
    excluded: list[Path],
    capability: CaptureCapability = _UNPROBED,
    ops: CaptureOps,
    cutoff_ns: int,
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
        actions=tuple(sorted(actions or (), key=lambda item: (item.category, item.path.as_posix()))),
        blockers=tuple(sorted(blockers, key=lambda item: (item.path.as_posix(), item.reason))),
        excluded_instruction_roots=tuple(sorted(set(excluded), key=lambda path: path.as_posix())),
        deferred=tuple(sorted(deferred or (), key=lambda item: (item.category, item.path.as_posix()))),
        recovery_pending=tuple(sorted(recovery_pending or (), key=lambda item: item.path.as_posix())),
        retained=tuple(sorted(retained or (), key=lambda item: item.path.as_posix())),
        capability=capability,
        quiescence_seconds=ops.quiescence_ns // 1_000_000_000,
        quiescence_cutoff_ns=cutoff_ns,
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


def _select_build_candidates(
    builds: list[_ScannedEntry],
    *,
    limit: int,
    published_digest: str | None,
) -> list[_ScannedEntry]:
    kept = list(builds[:limit])
    published = next(
        (entry for entry in builds if entry.path.name == published_digest),
        None,
    )
    if published is not None and published not in kept:
        kept[-1] = published
    keep_paths = {entry.path for entry in kept}
    return [entry for entry in builds if entry.path not in keep_paths]


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


@dataclass(frozen=True, slots=True)
class _BackupSelection:
    actions: list[RetentionAction]
    deferred: list[RetentionDeferral]
    retained: list[RetentionRetained]


def _select_backup_candidates(
    groups: dict[tuple[str, str], list[_ScannedEntry]],
    *,
    limit: int,
    capability: CaptureCapability,
    cutoff_ns: int,
    ops: CaptureOps,
    blockers: list[RetentionBlocker],
) -> _BackupSelection:
    actions: list[RetentionAction] = []
    deferred: list[RetentionDeferral] = []
    retained: list[RetentionRetained] = []
    quiescence = f"Skill backup changed within the {ops.quiescence_ns // 1_000_000_000}-second quiescence window"
    for (_target, skill), group in groups.items():
        group_actions: list[RetentionAction] = []
        for snapshot in group[limit:]:
            if snapshot.node_kind == "symlink":
                deferred.append(snapshot.deferral("skill_backup", _DEFER_SYMLINK, _SYMLINK_DEFERRAL))
            elif not capability.capable:
                unavailable = f"generation-bound capture is unavailable: {capability.reason}"
                deferred.append(snapshot.deferral("skill_backup", _DEFER_UNAVAILABLE, unavailable))
            elif snapshot.ctime_ns > cutoff_ns:
                deferred.append(snapshot.deferral("skill_backup", _DEFER_QUIESCENCE, quiescence))
            else:
                selected = _bind_capture_identity(snapshot, skill, cutoff_ns, ops)
                if isinstance(selected, RetentionAction):
                    group_actions.append(selected)
                elif selected is None:
                    deferred.append(snapshot.deferral("skill_backup", _DEFER_QUIESCENCE, quiescence))
                else:
                    blockers.append(selected)
        if group_actions:
            try:
                retained.extend(_retained(snapshot, ops) for snapshot in group[:limit])
            except (OSError, CaptureError) as exc:
                blockers.append(RetentionBlocker(group_actions[0].path.parent, f"cannot bind retained identity: {exc}"))
                continue
            actions.extend(group_actions)
    return _BackupSelection(actions=actions, deferred=deferred, retained=retained)


def _bind_capture_identity(
    snapshot: _ScannedEntry,
    skill: str,
    cutoff_ns: int,
    ops: CaptureOps,
) -> RetentionAction | RetentionBlocker | None:
    """Bind a candidate to its generation identity and digest; ``None`` means it is not quiescent."""

    try:
        identity, observation = observe_directory(snapshot.path, ops)
    except (OSError, CaptureError) as exc:
        return RetentionBlocker(snapshot.path, f"cannot bind Skill backup capture identity: {exc}")
    if (
        (identity.device, identity.inode) != (snapshot.device, snapshot.inode)
        or observation.bytes != snapshot.bytes
        or observation.marker_source_id != f"skills/{skill}"
    ):
        return RetentionBlocker(snapshot.path, "Skill backup changed while its capture identity was recorded")
    if observation.newest_ctime_ns > cutoff_ns:
        return None
    return snapshot.action(
        "skill_backup",
        identity=identity,
        capture_digest=observation.capture_digest,
        entry_count=observation.entry_count,
    )


def _retained(snapshot: _ScannedEntry, ops: CaptureOps) -> RetentionRetained:
    kept = retained_identity(snapshot.path, snapshot.node_kind, ops)
    return RetentionRetained(snapshot.path, kept.node_kind, kept.device, kept.inode, kept.generation)


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
        ctime_ns=entry_stat.st_ctime_ns,
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
