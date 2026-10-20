"""Tests for bounded Bridge-generated state retention and generation-bound capture."""

from __future__ import annotations

import errno
import json
import os
import pickle
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge import retention, retention_capture
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.filesystem import MANAGED_MARKER, tree_digest
from agent_config_bridge.models import Component, RetentionConfig
from agent_config_bridge.renderer import render_marketplace
from agent_config_bridge.retention import (
    RetentionApplyStopped,
    RetentionError,
    RetentionPlan,
    RetentionResult,
    apply_retention_plan,
    build_retention_plan,
)
from agent_config_bridge.retention_capture import (
    DEFAULT_CAPTURE_OPS,
    QUIESCENCE_NS,
    CaptureInspection,
    CaptureOps,
    observe_tree,
)
from tests.conftest import (
    make_catalog,
    make_config,
    make_skill_backup,
    require_directory_symlink_support,
)

_HOUR_NS = 3600 * 1_000_000_000
# A clock that puts the quiescence cutoff 0.3 s in the past, as in production but compressed.
_TIGHT_CUTOFF = {"now_ns": lambda: time.time_ns() + QUIESCENCE_NS - 300_000_000}
_LOCK_NAME = ".agentbridge-retention.lock"
_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="descriptor-relative capture is POSIX-only")


class _Crash(BaseException):
    """Simulated process death: bypasses every ``except Exception`` handler."""


def _ops(**overrides: object) -> CaptureOps:
    """Real seams with a clock one hour ahead so fresh test snapshots are quiescent."""

    return replace(DEFAULT_CAPTURE_OPS, now_ns=lambda: time.time_ns() + _HOUR_NS, **overrides)


def _marketplace_config(tmp_path: Path, *, build_limit: int = 20):
    catalog = make_catalog(tmp_path / "catalog", plugins=("shared",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.PLUGINS}),
    )
    return replace(
        config,
        retention=RetentionConfig(
            marketplace_builds=build_limit,
            skill_backups=3,
        ),
    )


def _backup_config(tmp_path: Path, *, limit: int = 1):
    catalog = make_catalog(tmp_path / "catalog")
    return replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=limit),
    )


def _make_builds(config, count: int) -> tuple[list[Path], Path]:
    rendered = render_marketplace(config, discover_catalog(config))
    base = rendered.build_root
    builds = config.state_dir / "builds"
    marker = json.loads((base / "bridge-build.json").read_text(encoding="utf-8"))
    paths = [base]
    for index in range(count - 1):
        digest = f"{index + 1:020x}"
        if digest == base.name:
            digest = f"{index + count + 1:020x}"
        destination = builds / digest
        shutil.copytree(base, destination)
        copied_marker = {**marker, "digest": digest}
        (destination / "bridge-build.json").write_text(
            json.dumps(copied_marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths.append(destination)
    ordered = [base, *sorted((path for path in paths if path != base), key=lambda path: path.name)]
    for index, path in enumerate(ordered):
        timestamp = 1_700_000_000_000_000_000 + index
        os.utime(path, ns=(timestamp, timestamp))
    return ordered, base


def _make_backup(config, index: int, *, target: str = "target", skill: str = "hello") -> Path:
    return make_skill_backup(config.state_dir, index, target=target, skill=skill)


def _state_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes], ...]:
    if not root.exists():
        return ()
    snapshot: list[tuple[str, str, int, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == _LOCK_NAME:
            continue
        status = path.lstat()
        if path.is_symlink():
            snapshot.append((relative, "symlink", status.st_mtime_ns, os.readlink(path).encode()))
        elif path.is_file():
            snapshot.append((relative, "file", status.st_mtime_ns, path.read_bytes()))
        else:
            snapshot.append((relative, "directory", status.st_mtime_ns, b""))
    return tuple(snapshot)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _capable_plan(config, ops: CaptureOps) -> RetentionPlan:
    plan = build_retention_plan(config, ops=ops)
    if not plan.capability.capable:
        pytest.skip(f"generation-bound capture is unavailable on the test filesystem: {plan.capability.reason}")
    return plan


def _capture_runs(config) -> list[str]:
    root = config.state_dir / "retention" / "capture"
    return sorted(os.listdir(root)) if root.exists() else []


def _quarantine_entries(config) -> list[str]:
    root = config.state_dir / "retention" / "quarantine"
    return sorted(os.listdir(root)) if root.exists() else []


def _first_capture(
    name: str,
    *,
    before: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> Callable[[str, str, int, int], None]:
    """Return a rename seam that runs hooks around the first capture rename of ``name``."""

    fired: list[bool] = []

    def rename(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        trigger = not fired and source == name and destination.startswith("item-")
        if trigger:
            fired.append(True)
            if before is not None:
                before()
        DEFAULT_CAPTURE_OPS.rename(source, destination, source_fd, destination_fd)
        if trigger and after is not None:
            after()

    return rename


def _run(config, ops: CaptureOps) -> RetentionResult:
    return apply_retention_plan(config, build_retention_plan(config, ops=ops), ops=ops)


def _run_or_stopped(config, ops: CaptureOps) -> tuple[RetentionResult, bool]:
    """Apply a fresh plan; return its result and whether the apply stopped instead of converging."""

    try:
        return _run(config, ops), False
    except RetentionApplyStopped as exc:
        return exc.result, True


# --- planning and category gating ----------------------------------------------


def test_marketplace_plan_defers_excess_builds_and_apply_preserves_all(tmp_path: Path) -> None:
    config = _marketplace_config(tmp_path)
    builds, published = _make_builds(config, 22)

    plan = build_retention_plan(config)

    assert not plan.has_blockers
    assert plan.build_count == 22
    assert plan.actions == ()
    assert len(plan.deferred) == 2
    assert {item.category for item in plan.deferred} == {"marketplace_build"}
    assert {item.reason_code for item in plan.deferred} == {"marketplace_build"}
    assert all("share the retention lock" in item.reason for item in plan.deferred)
    assert published not in {item.path for item in plan.deferred}

    before = _state_snapshot(config.state_dir)
    result = apply_retention_plan(config, plan)

    assert result.deleted == ()
    assert result.final_plan.deferred == plan.deferred
    assert _state_snapshot(config.state_dir) == before
    assert all(path.is_dir() for path in builds)
    assert not (config.state_dir / "retention").exists()


def test_missing_build_for_published_marketplace_blocks_all_deletion(tmp_path: Path) -> None:
    config = _marketplace_config(tmp_path, build_limit=1)
    builds, published = _make_builds(config, 3)
    shutil.rmtree(published)

    plan = build_retention_plan(config)

    assert plan.deferred
    assert plan.has_blockers
    assert any(
        blocker.path == published and "no matching immutable build" in blocker.reason for blocker in plan.blockers
    )
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in builds if path != published)


def test_skill_backup_apply_deletes_only_verified_oldest_captures(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=3)
    backups = [_make_backup(config, index) for index in range(5)]
    kept = {path: _tree_bytes(path) for path in backups[2:]}
    ops = _ops()

    plan = _capable_plan(config, ops)

    assert [action.path for action in plan.actions] == backups[:2]
    assert all(action.generation is not None for action in plan.actions)
    assert all(action.entry_count == 2 for action in plan.actions)  # SKILL.md and the marker
    assert [item.path for item in plan.retained] == backups[2:]

    result = apply_retention_plan(config, plan, ops=ops)

    assert result.deleted == plan.actions
    assert result.reclaimed_bytes == plan.reclaimable_bytes
    assert not any(path.exists() for path in backups[:2])
    assert {path: _tree_bytes(path) for path in backups[2:]} == kept
    assert result.final_plan.actions == ()
    assert result.final_plan.fully_converged
    assert _capture_runs(config) == []
    assert _quarantine_entries(config) == []


def test_skill_backup_limit_never_deletes_the_only_snapshot(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    only = _make_backup(config, 0)

    plan = build_retention_plan(config, ops=_ops())

    assert not plan.has_changes
    assert plan.deferred == ()
    assert only.is_dir()


def test_terminal_symlink_snapshot_is_deferred_and_never_unlinked(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    config = _backup_config(tmp_path, limit=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    group = config.state_dir / "backups" / "target" / "hello"
    group.mkdir(parents=True)
    old_link = group / "20260723-000000-00000001"
    old_link.symlink_to(outside, target_is_directory=True)
    newest = _make_backup(config, 1)
    ops = _ops()

    plan = build_retention_plan(config, ops=ops)

    assert plan.actions == ()
    assert [(item.path, item.node_kind) for item in plan.deferred] == [(old_link, "symlink")]
    assert plan.deferred[0].reason_code == "symlink_snapshot"
    assert "terminal symlink" in plan.deferred[0].reason
    result = apply_retention_plan(config, plan, ops=ops)

    assert result.deleted == ()
    assert old_link.is_symlink()
    assert newest.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_any_unsafe_entry_globally_blocks_all_deletion(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    odd = backups[0].parent / "operator-note.txt"
    odd.write_text("do not delete\n", encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    assert plan.actions or plan.deferred
    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan, ops=_ops())
    assert all(path.is_dir() for path in backups)
    assert odd.is_file()


def test_marker_mismatch_globally_blocks_other_valid_candidates(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(4)]
    marker = backups[-1] / MANAGED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["installed_digest"] = "0" * 64
    marker.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    assert plan.actions or plan.deferred
    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan, ops=_ops())
    assert all(path.is_dir() for path in backups)


def test_redirected_backup_ancestor_blocks_without_following_it(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    config.state_dir.mkdir()
    (config.state_dir / "backups").symlink_to(outside, target_is_directory=True)

    plan = build_retention_plan(config)

    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_special_backup_node_is_a_global_blocker(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    fifo = backups[0].parent / "20260723-000099-ffffffff"
    os.mkfifo(fifo)

    plan = build_retention_plan(config)

    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in backups)
    assert fifo.exists()


def test_recreated_candidate_after_planning_fails_the_locked_replan(tmp_path: Path) -> None:
    """Replaces the old refusal test: a byte-identical recreate no longer matches the plan."""

    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    candidate = backups[0]
    ops = _ops()
    plan = _capable_plan(config, ops)
    original = _tree_bytes(candidate)
    mtime = candidate.stat().st_mtime_ns
    shutil.rmtree(candidate)
    replacement = _make_backup(config, 0)
    os.utime(replacement, ns=(mtime, mtime))

    before = _state_snapshot(config.state_dir)
    with pytest.raises(RetentionError, match="changed after retention planning"):
        apply_retention_plan(config, plan, ops=ops)

    assert _state_snapshot(config.state_dir) == before
    assert _tree_bytes(replacement) == original
    assert all(path.is_dir() for path in backups[1:])
    assert not (config.state_dir / "retention").exists()


def test_no_action_apply_succeeds_and_releases_lock(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    plan = build_retention_plan(config)

    assert not plan.has_blockers
    assert not plan.has_changes

    first = apply_retention_plan(config, plan)
    second = apply_retention_plan(config, plan)

    for result in (first, second):
        assert result.deleted == ()
        assert result.reclaimed_bytes == 0
        assert not result.final_plan.has_changes
        assert not result.final_plan.has_blockers

    lock_path = config.state_dir / _LOCK_NAME
    assert lock_path.is_file()
    lock_path.unlink()
    assert not lock_path.exists()


def test_dry_run_is_byte_and_metadata_invariant_and_excludes_instructions(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    [_make_backup(config, index) for index in range(4)]
    instructions = config.state_dir / "backups" / "target" / "instructions"
    instructions.mkdir()
    (instructions / "AGENTS.md").write_text("retained\n", encoding="utf-8")
    before = _state_snapshot(config.state_dir)

    plan = build_retention_plan(config, ops=_ops())

    assert plan.excluded_instruction_roots == (instructions,)
    assert _state_snapshot(config.state_dir) == before
    assert not (config.state_dir / _LOCK_NAME).exists()
    assert not (config.state_dir / "retention").exists()


def _backup_with_marker(config, build, *, ignore: bool) -> Path:
    snapshot = _make_backup(config, 0)
    build(snapshot)
    digest = tree_digest(snapshot, ignore_bytecode_caches=ignore)
    marker = snapshot / MANAGED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["installed_digest"] = digest
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return snapshot


def _plant_plain_cache(snapshot: Path) -> None:
    (snapshot / "__pycache__").mkdir()
    (snapshot / "__pycache__/x.cpython-312.pyc").write_bytes(b"cache")


@pytest.mark.parametrize("build", [_plant_plain_cache])
def test_backup_whose_marker_ignores_planted_or_cache_files_is_a_blocker(tmp_path: Path, build) -> None:
    """Retention verifies backups strictly; a cache-ignoring marker digest is not accepted."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    _backup_with_marker(config, build, ignore=True)

    assert build_retention_plan(config).has_blockers


def test_backup_recorded_with_caches_strictly_remains_valid(tmp_path: Path) -> None:
    """Backups recorded before caches were excluded keep verifying."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    _backup_with_marker(config, _plant_plain_cache, ignore=False)

    assert not build_retention_plan(config).has_blockers


# --- capture digest -------------------------------------------------------------


def _digest(root: Path):
    descriptor = os.open(root, os.O_RDONLY)
    try:
        return observe_tree(descriptor, DEFAULT_CAPTURE_OPS)
    finally:
        os.close(descriptor)


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


@_POSIX_ONLY
def test_capture_digest_is_length_prefixed_against_naive_concatenation(tmp_path: Path) -> None:
    # The tree_digest-style framing (path, NUL, type, content) collides for these trees.
    first = _tree(tmp_path / "first", {"a": b"foo", "b": b"bar"})
    second = _tree(tmp_path / "second", {"a": b"foob\0Fbar"})

    def naive(root: Path) -> bytes:
        return b"".join(path.name.encode() + b"\0F" + path.read_bytes() for path in sorted(root.iterdir()))

    assert naive(first) == naive(second)
    assert _digest(first).capture_digest != _digest(second).capture_digest

    # These trees have equal entry counts and collide under this digest's own record
    # framing (path, type tag, content) once every length prefix is removed.
    shifted = _tree(tmp_path / "shifted", {"a": b"foocfilebaz", "b": b"bar"})
    unshifted = _tree(tmp_path / "unshifted", {"a": b"foo", "c": b"bazbfilebar"})

    def unprefixed(root: Path) -> bytes:
        return b"".join(path.name.encode() + b"file" + path.read_bytes() for path in sorted(root.iterdir()))

    assert unprefixed(shifted) == unprefixed(unshifted)
    assert _digest(shifted).entry_count == _digest(unshifted).entry_count
    assert _digest(shifted).capture_digest != _digest(unshifted).capture_digest


@_POSIX_ONLY
def test_capture_digest_covers_the_marker_and_symlinked_directories(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    base = tmp_path / "base"
    base.mkdir()
    (base / "SKILL.md").write_text("same\n", encoding="utf-8")
    (base / MANAGED_MARKER).write_text('{"source_id": "skills/a"}', encoding="utf-8")
    other_marker = tmp_path / "other-marker"
    shutil.copytree(base, other_marker)
    (other_marker / MANAGED_MARKER).write_text('{"source_id": "skills/b"}', encoding="utf-8")
    with_link = tmp_path / "with-link"
    shutil.copytree(base, with_link)
    (tmp_path / "outside").mkdir()
    (with_link / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)

    observed = _digest(base)

    assert observed.entry_count == 2
    assert _digest(other_marker).capture_digest != observed.capture_digest
    assert _digest(with_link).entry_count == 3
    assert _digest(with_link).capture_digest != observed.capture_digest


# --- capability and quiescence deferral ----------------------------------------


def _enotty(_descriptor: int) -> int:
    raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")


def _no_mnt_id(_descriptor: int) -> int:
    raise OSError(errno.EINVAL, "fdinfo does not report mnt_id")


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"generation": _enotty}, "ENOTTY"),
        ({"avoids_symlink_attacks": False}, "symlink-attack"),
        ({"same_filesystem": lambda _first, _second: False}, "EXDEV"),
        ({"os_name": "nt"}, "native Windows"),
        ({"sys_platform": "darwin"}, "only on Linux"),
        ({"machine": "amd64"}, "not verified for machine"),
        ({"mount_id": _no_mnt_id}, "mnt_id"),
        ({"filesystem_type": lambda _device: "tmpfs"}, "generation allowlist"),
        ({"filesystem_type": lambda _device: "xfs"}, "generation allowlist"),
    ),
    ids=(
        "ioctl-enotty",
        "rmtree-not-symlink-safe",
        "exdev",
        "native-windows",
        "non-linux-posix",
        "unverified-machine-spelling",
        "no-mount-id",
        "filesystem-allowlist-tmpfs",
        "filesystem-allowlist-xfs",
    ),
)
def test_capability_failure_defers_every_candidate(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    ops = _ops(**overrides)

    plan = build_retention_plan(config, ops=ops)

    assert not plan.capability.capable
    assert reason in (plan.capability.reason or "")
    assert plan.actions == ()
    assert [item.path for item in plan.deferred] == backups[:2]
    assert all(reason in item.reason for item in plan.deferred)
    assert {item.reason_code for item in plan.deferred} == {"capture_unavailable"}
    result = apply_retention_plan(config, plan, ops=ops)

    assert result.deleted == ()
    assert all(path.is_dir() for path in backups)
    assert not (config.state_dir / "retention").exists()


def test_snapshot_inside_quiescence_window_is_deferred(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    ops = replace(DEFAULT_CAPTURE_OPS, now_ns=time.time_ns)

    plan = build_retention_plan(config, ops=ops)

    if not plan.capability.capable:
        pytest.skip(f"generation-bound capture is unavailable: {plan.capability.reason}")
    assert plan.actions == ()
    assert [item.path for item in plan.deferred] == backups[:2]
    assert {item.reason for item in plan.deferred} == {"Skill backup changed within the 900-second quiescence window"}
    assert {item.reason_code for item in plan.deferred} == {"quiescence"}
    assert apply_retention_plan(config, plan, ops=ops).deleted == ()
    assert all(path.is_dir() for path in backups)


# --- ABA, swaps, and ancestry ---------------------------------------------------


def test_byte_identical_recreate_during_capture_is_restored_not_deleted(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate = backups[0]
    original = _tree_bytes(candidate)
    scratch = tmp_path / "scratch-copy"

    def recreate() -> None:
        times = candidate.stat()
        shutil.copytree(candidate, scratch, symlinks=True)
        shutil.rmtree(candidate)
        shutil.copytree(scratch, candidate, symlinks=True)
        os.utime(candidate, ns=(times.st_atime_ns, times.st_mtime_ns))

    ops = _ops(rename=_first_capture(candidate.name, before=recreate))
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="did not match its reviewed identity") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.restored == (candidate,)
    assert _tree_bytes(candidate) == original
    assert backups[1].is_dir()
    assert _capture_runs(config) == []
    assert _quarantine_entries(config) == []


def test_same_inode_with_new_generation_is_restored_not_deleted(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate = backups[0]
    candidate_inode = candidate.stat().st_ino
    captured: list[bool] = []

    def generation(descriptor: int) -> int:
        real = DEFAULT_CAPTURE_OPS.generation(descriptor)
        return real ^ 1 if captured and os.fstat(descriptor).st_ino == candidate_inode else real

    ops = _ops(
        rename=_first_capture(candidate.name, after=lambda: captured.append(True)),
        generation=generation,
    )
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="device, inode, or generation differs") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.restored == (candidate,)
    assert candidate.is_dir()
    assert candidate.stat().st_ino == candidate_inode
    assert _capture_runs(config) == []


def test_concurrent_swap_inside_rename_with_reoccupied_name_is_quarantined(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate = backups[0]
    original = _tree_bytes(candidate)
    displaced = tmp_path / "displaced-original"

    def swap_in_impostor() -> None:
        candidate.rename(displaced)
        candidate.mkdir()
        (candidate / "impostor.txt").write_text("not the reviewed snapshot\n", encoding="utf-8")

    def reoccupy() -> None:
        candidate.mkdir()
        (candidate / "late.txt").write_text("created after capture\n", encoding="utf-8")

    ops = _ops(rename=_first_capture(candidate.name, before=swap_in_impostor, after=reoccupy))
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="quarantined") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.quarantined == (candidate,)
    assert _tree_bytes(displaced) == original
    assert (candidate / "late.txt").is_file()
    [run_id] = _quarantine_entries(config)
    quarantined = config.state_dir / "retention" / "quarantine" / run_id
    assert (quarantined / "item-0000" / "impostor.txt").is_file()
    assert json.loads((quarantined / "manifest.json").read_text(encoding="utf-8"))["items"][0]["state"] == "quarantined"
    follow_up = build_retention_plan(config, ops=_ops())
    assert any("operator review" in blocker.reason for blocker in follow_up.blockers)


def test_candidate_swapped_for_symlink_is_quarantined_never_renamed_back(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate = backups[0]
    moved = tmp_path / "outside" / "moved"
    moved.parent.mkdir()
    original = _tree_bytes(candidate)
    restores: list[str] = []

    def swap_for_symlink() -> None:
        candidate.rename(moved)
        candidate.symlink_to(moved, target_is_directory=True)

    def rename(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        if destination == candidate.name:
            restores.append(source)
            os.symlink("written-by-another-tool", candidate)  # the name is re-taken in the restore window
        capture(source, destination, source_fd, destination_fd)

    capture = _first_capture(candidate.name, before=swap_for_symlink)
    ops = _ops(rename=rename)
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="not a real directory") as raised:
        apply_retention_plan(config, plan, ops=ops)

    # A non-directory could silently replace the non-directory now at the name, so it is not renamed back.
    assert restores == []
    assert raised.value.result.quarantined == (candidate,)
    assert raised.value.result.restored == ()
    assert raised.value.result.deleted == ()
    assert not os.path.lexists(candidate)
    [run_id] = _quarantine_entries(config)
    assert os.readlink(config.state_dir / "retention" / "quarantine" / run_id / "item-0000") == str(moved)
    assert _tree_bytes(moved) == original
    assert _capture_runs(config) == []


def test_ancestor_swapped_for_symlink_stops_before_moving_its_candidate(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    config = _backup_config(tmp_path, limit=1)
    first = [_make_backup(config, index, target="t1") for index in range(2)]
    second = [_make_backup(config, index, target="t2") for index in range(2)]
    second_target = config.state_dir / "backups" / "t2"
    moved_target = tmp_path / "outside" / "t2-moved"
    moved_target.parent.mkdir()
    second_original = _tree_bytes(second[0])

    def swap_ancestor() -> None:
        second_target.rename(moved_target)
        second_target.symlink_to(moved_target, target_is_directory=True)

    ops = _ops(rename=_first_capture(first[0].name, before=swap_ancestor))
    plan = _capable_plan(config, ops)
    assert [action.path for action in plan.actions] == [first[0], second[0]]

    with pytest.raises(RetentionApplyStopped, match="missing or redirected") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert [action.path for action in raised.value.result.deleted] == [first[0]]
    assert second_target.is_symlink()
    assert _tree_bytes(moved_target / "hello" / second[0].name) == second_original
    assert first[1].is_dir()


def test_kept_snapshot_removed_between_captures_stops_before_the_next_move(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    kept = backups[2]

    def rmtree_then_remove_kept(name: str, dir_fd: int) -> None:
        DEFAULT_CAPTURE_OPS.rmtree(name, dir_fd)
        if kept.exists():
            shutil.rmtree(kept)

    ops = _ops(rmtree=rmtree_then_remove_kept)
    plan = _capable_plan(config, ops)
    assert [action.path for action in plan.actions] == backups[:2]

    with pytest.raises(RetentionApplyStopped, match="changed after planning; .* was not moved") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert [action.path for action in raised.value.result.deleted] == [backups[0]]
    assert backups[1].is_dir()
    assert len(list(backups[1].parent.iterdir())) == config.retention.skill_backups


def test_kept_snapshot_removed_during_capture_restores_before_the_verified_record(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    candidate, kept = backups[0], backups[2]
    original = _tree_bytes(candidate)
    states: list[str] = []

    def record_states(dir_fd: int, payload: bytes) -> None:
        states.extend(item["state"] for item in json.loads(payload)["items"][:1])
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)

    # The pre-move check passes; the kept snapshot disappears while its sibling is captured.
    ops = _ops(rename=_first_capture(candidate.name, after=lambda: shutil.rmtree(kept)), write_manifest=record_states)
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="changed during capture") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.restored == (candidate,)
    assert _tree_bytes(candidate) == original
    assert backups[1].is_dir()
    assert "verified" not in states
    assert _capture_runs(config) == []


def test_exdev_capture_rename_reports_busy_and_moves_nothing(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]

    def exdev(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        if destination.startswith("item-"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        DEFAULT_CAPTURE_OPS.rename(source, destination, source_fd, destination_fd)

    ops = _ops(rename=exdev)
    plan = _capable_plan(config, ops)
    before = _tree_bytes(config.state_dir / "backups")

    with pytest.raises(RetentionApplyStopped, match="EXDEV") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.busy == (backups[0],)
    assert raised.value.result.deleted == ()
    assert _tree_bytes(config.state_dir / "backups") == before
    assert _capture_runs(config) == []


def test_symlink_inside_deleted_capture_is_not_followed(tmp_path: Path) -> None:
    require_directory_symlink_support(tmp_path)
    config = _backup_config(tmp_path, limit=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    candidate = config.state_dir / "backups" / "target" / "hello" / "20260723-000000-00000001"
    candidate.mkdir(parents=True)
    (candidate / "SKILL.md").write_text("old\n", encoding="utf-8")
    (candidate / "linked").symlink_to(outside, target_is_directory=True)
    (candidate / MANAGED_MARKER).write_text(
        json.dumps({"schema_version": 1, "source_id": "skills/hello", "installed_digest": tree_digest(candidate)}),
        encoding="utf-8",
    )
    _make_backup(config, 1)
    ops = _ops()
    plan = _capable_plan(config, ops)
    assert [action.path for action in plan.actions] == [candidate]

    result = apply_retention_plan(config, plan, ops=ops)

    assert [action.path for action in result.deleted] == [candidate]
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


# --- only durably verified captures are deleted, and interrupted runs recover --------


def test_unpersisted_verified_state_refuses_deletion_and_restores(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate = backups[0]
    original = _tree_bytes(candidate)

    def drop_verified(dir_fd: int, payload: bytes) -> None:
        if any(item["state"] == "verified" for item in json.loads(payload)["items"]):
            return  # a lost, non-durable write
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)

    ops = _ops(write_manifest=drop_verified)
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="not verified") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.restored == (candidate,)
    assert _tree_bytes(candidate) == original
    assert _capture_runs(config) == []


def _crash_after_manifest_state(state: str) -> Callable[[int, bytes], None]:
    fired: list[bool] = []

    def write(dir_fd: int, payload: bytes) -> None:
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)
        if not fired and any(item["state"] == state for item in json.loads(payload)["items"]):
            fired.append(True)
            raise _Crash(state)

    return write


def _crash_after_rename(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
    DEFAULT_CAPTURE_OPS.rename(source, destination, source_fd, destination_fd)
    if destination.startswith("item-"):
        raise _Crash("renamed")


def _crash_mid_rmtree(name: str, dir_fd: int) -> None:
    os.unlink(f"{name}/SKILL.md", dir_fd=dir_fd)
    raise _Crash("rmtree")


_CRASH_POINTS: dict[str, Callable[[], dict[str, object]]] = {
    "intent": lambda: {"write_manifest": _crash_after_manifest_state("intent")},
    "renamed-before-captured": lambda: {"rename": _crash_after_rename},
    "captured": lambda: {"write_manifest": _crash_after_manifest_state("captured")},
    "verified": lambda: {"write_manifest": _crash_after_manifest_state("verified")},
    "mid-rmtree": lambda: {"rmtree": _crash_mid_rmtree},
    "deleted": lambda: {"write_manifest": _crash_after_manifest_state("deleted")},
}
_EXPECTED_FIRST_RECOVERY = {
    "intent": {"cleaned_up"},
    "renamed-before-captured": {"restored", "cleaned_up"},
    "captured": {"restored", "cleaned_up"},
    "verified": {"deleted", "cleaned_up"},
    "mid-rmtree": {"deleted", "cleaned_up"},
    "deleted": {"cleaned_up"},
}


@pytest.mark.parametrize("crash_point", tuple(_CRASH_POINTS))
def test_crash_at_each_manifest_state_recovers_idempotently(tmp_path: Path, crash_point: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    candidate, kept = backups
    original = _tree_bytes(candidate)
    kept_bytes = _tree_bytes(kept)
    crashing = _ops(**_CRASH_POINTS[crash_point]())
    plan = _capable_plan(config, crashing)

    with pytest.raises(_Crash):
        apply_retention_plan(config, plan, ops=crashing)

    ops = _ops()
    interrupted = build_retention_plan(config, ops=ops)
    assert not interrupted.has_blockers
    assert len(interrupted.recovery_pending) == 1

    first, stopped = _run_or_stopped(config, ops)
    outcomes = {event.outcome for event in first.recovered}
    assert outcomes == _EXPECTED_FIRST_RECOVERY[crash_point]
    # A restored candidate changes the plan, so only that run stops without converging.
    assert stopped == ("restored" in outcomes)
    if "restored" in outcomes:
        # Unverified captures come back byte-identical; they are never deleted by recovery.
        assert first.deleted == ()
        assert first.restored == (candidate,)
        assert _tree_bytes(candidate) == original
    if crash_point in {"verified", "mid-rmtree", "deleted"}:
        assert not candidate.exists()

    runs = [_run(config, ops) for _ in range(2)]

    assert runs[-1].deleted == ()
    assert runs[-1].recovered == ()
    assert runs[-1].final_plan.fully_converged
    assert not candidate.exists()
    assert _tree_bytes(kept) == kept_bytes
    assert _capture_runs(config) == []
    assert _quarantine_entries(config) == []


@_POSIX_ONLY
def test_malformed_manifest_and_unknown_capture_entries_are_blockers(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    [_make_backup(config, index) for index in range(2)]
    run = config.state_dir / "retention" / "capture" / ("a" * 32)
    run.mkdir(parents=True)
    for path in (run.parent.parent, run.parent, run):
        path.chmod(0o700)
    (run / "manifest.json").write_text("{not json", encoding="utf-8")
    (config.state_dir / "retention" / "stray").write_text("x", encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    reasons = {blocker.path.name: blocker.reason for blocker in plan.blockers}
    assert "malformed capture manifest" in reasons["a" * 32]
    assert reasons["stray"] == "unexpected retention capture entry"
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan, ops=_ops())


@_POSIX_ONLY
def test_non_private_capture_tree_is_a_planning_blocker(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    retention = config.state_dir / "retention"
    retention.mkdir()
    retention.chmod(0o755)

    plan = build_retention_plan(config, ops=_ops())

    assert not plan.capability.capable
    assert [item.path for item in plan.deferred] == backups[:1]
    assert [(blocker.path, blocker.reason) for blocker in plan.blockers] == [
        (retention, "retention capture root must be owned by the current user with mode 0700")
    ]


@_POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permissions")
def test_transient_slot_open_error_keeps_verified_capture_pending_not_quarantined(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    crashing = _ops(write_manifest=_crash_after_manifest_state("verified"))
    plan = _capable_plan(config, crashing)
    with pytest.raises(_Crash):
        apply_retention_plan(config, plan, ops=crashing)
    [run_id] = _capture_runs(config)
    slot = config.state_dir / "retention" / "capture" / run_id / "item-0000"
    slot.chmod(0o000)
    ops = _ops()
    try:
        blocked = _run(config, ops)
    finally:
        slot.chmod(0o700)

    assert [event.outcome for event in blocked.recovered] == ["pending_cleanup"]
    assert _quarantine_entries(config) == []
    assert len(build_retention_plan(config, ops=ops).recovery_pending) == 1

    retried = _run(config, ops)

    assert {event.outcome for event in retried.recovered} == {"deleted", "cleaned_up"}
    assert not candidate.exists()
    assert kept.is_dir()
    assert _capture_runs(config) == []


def test_mid_run_failure_reports_earlier_deletions_and_recovers(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    second = backups[1]
    second_bytes = _tree_bytes(second)

    def fail_second_capture(dir_fd: int, payload: bytes) -> None:
        if json.loads(payload)["items"][-1]["state"] == "captured":
            raise OSError(errno.EIO, "simulated manifest write failure")
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)

    failing = _ops(write_manifest=fail_second_capture)
    plan = _capable_plan(config, failing)
    assert [action.path for action in plan.actions] == backups[:2]

    with pytest.raises(RetentionApplyStopped, match="retention capture failed") as raised:
        apply_retention_plan(config, plan, ops=failing)

    assert [action.path for action in raised.value.result.deleted] == [backups[0]]
    assert not second.exists()  # still inside the interrupted capture run
    ops = _ops()
    with pytest.raises(RetentionApplyStopped, match="recovery changed the reviewed plan") as stopped:
        _run(config, ops)
    recovered = stopped.value.result
    assert [event.outcome for event in recovered.recovered] == ["restored", "cleaned_up"]
    assert recovered.restored == (second,)
    assert recovered.deleted == ()
    assert [action.path for action in recovered.final_plan.actions] == [second]
    assert _tree_bytes(second) == second_bytes
    assert [action.path for action in _run(config, ops).deleted] == [second]
    assert backups[2].is_dir()
    assert _capture_runs(config) == []


@_POSIX_ONLY
def test_manifest_naming_a_parent_outside_backups_is_a_blocker(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    _make_backup(config, 0)
    run_id = "c" * 32
    run = config.state_dir / "retention" / "capture" / run_id
    run.mkdir(parents=True)
    for path in (run.parent.parent, run.parent, run):
        path.chmod(0o700)
    item = {
        "index": 0,
        "slot": "item-0000",
        "category": "skill_backup",
        "parent": ["targets", "target", "hello"],
        "name": "20260723-000000-00000001",
        "device": 1,
        "inode": 2,
        "generation": 3,
        "capture_digest": "0" * 64,
        "entry_count": 2,
        "bytes": 10,
        "source_id": "skills/hello",
        "state": "captured",
    }
    manifest = {
        "schema_version": 1,
        "format": "agent-config-bridge/retention-capture",
        "run_id": run_id,
        "items": [item],
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    assert [(blocker.path, blocker.reason) for blocker in plan.blockers] == [
        (run, "malformed capture manifest: capture manifest item 0 is invalid")
    ]
    item["parent"] = ["backups", "target", "instructions"]
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert [blocker.reason for blocker in build_retention_plan(config, ops=_ops()).blockers] == [
        "malformed capture manifest: capture manifest item 0 is invalid"
    ]
    item["parent"] = ["backups", "target", "hello"]
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert build_retention_plan(config, ops=_ops()).blockers == ()


def _private_run_dir(config, *parts: str) -> Path:
    path = config.state_dir.joinpath("retention", *parts)
    path.mkdir(parents=True)
    current = path
    while current != config.state_dir:
        current.chmod(0o700)
        current = current.parent
    return path


def _after_state(state: str) -> tuple[Callable[[int, bytes], None], Callable[[], bool]]:
    """Return a manifest writer and a flag that turns on once the first item records ``state``."""

    seen: list[bool] = []

    def write(dir_fd: int, payload: bytes) -> None:
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)
        if json.loads(payload)["items"][0]["state"] == state:
            seen.append(True)

    return write, lambda: bool(seen)


def _flip_generation(inode: int, active: Callable[[], bool] = lambda: True) -> Callable[[int], int]:
    """Return a generation seam that reports a reused inode for ``inode`` while ``active``."""

    def generation(descriptor: int) -> int:
        real = DEFAULT_CAPTURE_OPS.generation(descriptor)
        return real ^ 1 if active() and os.fstat(descriptor).st_ino == inode else real

    return generation


# --- the apply contract: success always converges --------------------------------


@pytest.mark.parametrize("crash_point", ("intent", "verified"))
def test_recovery_that_only_cleans_up_or_finishes_a_delete_still_applies_the_plan(
    tmp_path: Path,
    crash_point: str,
) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    crashing = _ops(write_manifest=_crash_after_manifest_state(crash_point))
    plan = _capable_plan(config, crashing)
    assert [action.path for action in plan.actions] == backups[:2]
    with pytest.raises(_Crash):
        apply_retention_plan(config, plan, ops=crashing)

    result = _run(config, _ops())

    expected = {"intent": {"cleaned_up"}, "verified": {"deleted", "cleaned_up"}}[crash_point]
    assert {event.outcome for event in result.recovered} == expected
    remaining = backups[:2] if crash_point == "intent" else backups[1:2]
    assert [action.path for action in result.deleted] == remaining
    assert result.final_plan.fully_converged
    assert not any(path.exists() for path in backups[:2])
    assert backups[2].is_dir()


def test_restored_capture_is_deferred_when_the_cutoff_precedes_the_restore(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    original = _tree_bytes(backups[0])
    time.sleep(0.4)  # the snapshots are now older than the tight cutoff; a later rename is not
    crashing = replace(DEFAULT_CAPTURE_OPS, rename=_crash_after_rename, **_TIGHT_CUTOFF)
    plan = _capable_plan(config, crashing)
    assert [action.path for action in plan.actions] == backups[:2]
    with pytest.raises(_Crash):
        apply_retention_plan(config, plan, ops=crashing)

    with pytest.raises(RetentionApplyStopped, match="recovery changed the reviewed plan") as raised:
        _run(config, replace(DEFAULT_CAPTURE_OPS, **_TIGHT_CUTOFF))

    result = raised.value.result
    assert result.restored == (backups[0],)
    assert result.deleted == ()
    assert [(item.path, item.reason_code) for item in result.final_plan.deferred] == [(backups[0], "quiescence")]
    assert [action.path for action in result.final_plan.actions] == [backups[1]]
    assert _tree_bytes(backups[0]) == original


# ``st_ctime`` is a status-change time only on POSIX; on Windows it is the creation time.
@_POSIX_ONLY
def test_recent_change_deep_inside_a_snapshot_defers_it(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(3)]
    root = config.state_dir / "backups"
    settled = max(path.lstat().st_ctime_ns for path in root.rglob("*"))
    nested = backups[0] / "SKILL.md"
    times = nested.stat()
    for _attempt in range(200):  # wait out the filesystem timestamp granularity
        os.utime(nested, ns=(times.st_atime_ns, times.st_mtime_ns))  # changes only the nested ctime
        if nested.stat().st_ctime_ns > settled:
            break
        time.sleep(0.01)
    assert nested.stat().st_ctime_ns > settled
    assert backups[0].stat().st_ctime_ns <= settled

    # The cutoff sits between every other change and the nested one, independent of wall-clock timing.
    plan = build_retention_plan(config, ops=_ops(), quiescence_cutoff_ns=settled)

    if not plan.capability.capable:
        pytest.skip(f"generation-bound capture is unavailable: {plan.capability.reason}")
    assert [action.path for action in plan.actions] == [backups[1]]
    assert [(item.path, item.reason_code) for item in plan.deferred] == [(backups[0], "quiescence")]


@_POSIX_ONLY
def test_capture_directory_capability_is_rechecked_before_any_move(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    backups = [_make_backup(config, index) for index in range(2)]
    capture = config.state_dir / "retention" / "capture"

    def generation(descriptor: int) -> int:
        if capture.exists() and os.path.samestat(os.fstat(descriptor), capture.stat()):
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        return DEFAULT_CAPTURE_OPS.generation(descriptor)

    ops = _ops(generation=generation)
    plan = _capable_plan(config, ops)
    before = _tree_bytes(config.state_dir / "backups")

    with pytest.raises(RetentionError, match="FS_IOC_GETVERSION failed on the capture directory"):
        apply_retention_plan(config, plan, ops=ops)

    assert _tree_bytes(config.state_dir / "backups") == before
    assert all(path.is_dir() for path in backups)
    assert _capture_runs(config) == []


def test_slot_identity_change_after_verification_refuses_deletion(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    original = _tree_bytes(candidate)
    write, verified = _after_state("verified")
    ops = _ops(write_manifest=write, generation=_flip_generation(candidate.stat().st_ino, verified))
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="identity changed after verification") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.restored == (candidate,)
    assert _tree_bytes(candidate) == original
    assert kept.is_dir()


def test_crash_after_restoring_a_refused_delete_is_not_reported_as_deleted(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    original = _tree_bytes(candidate)
    write, verified = _after_state("verified")

    def crash_after_restore(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        DEFAULT_CAPTURE_OPS.rename(source, destination, source_fd, destination_fd)
        if destination == candidate.name:
            raise _Crash("restored")

    crashing = _ops(
        write_manifest=write,
        generation=_flip_generation(candidate.stat().st_ino, verified),  # the verified delete is refused
        rename=crash_after_restore,
    )
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    [run_id] = _capture_runs(config)
    manifest = json.loads((config.state_dir / "retention" / "capture" / run_id / "manifest.json").read_bytes())
    assert manifest["items"][0]["state"] == "captured"  # demoted before the restore move
    assert _tree_bytes(candidate) == original

    result = _run(config, _ops())

    assert {event.outcome for event in result.recovered} == {"cleaned_up"}
    assert [action.path for action in result.deleted] == [candidate]  # by a fresh, verified capture
    assert kept.is_dir()


@_POSIX_ONLY
def test_read_only_subdirectory_inside_a_verified_capture_is_deleted(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    sub = _add_subdirectory(candidate)
    (sub / "deeper").mkdir()
    _resign_marker(candidate)
    sub.chmod(0o555)
    try:
        ops = _ops()
        plan = _capable_plan(config, ops)
        assert [action.path for action in plan.actions] == [candidate]

        result = apply_retention_plan(config, plan, ops=ops)
    finally:
        if sub.exists():
            sub.chmod(0o755)

    assert result.deleted == plan.actions
    assert result.pending_cleanup == ()
    assert result.final_plan.fully_converged
    assert not candidate.exists()
    assert _capture_runs(config) == []
    assert kept.is_dir()


def test_failed_deleted_record_still_reports_the_deletion_and_recovers(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]

    def fail_deleted(dir_fd: int, payload: bytes) -> None:
        if json.loads(payload)["items"][0]["state"] == "deleted":
            raise OSError(errno.EIO, "simulated manifest write failure")
        DEFAULT_CAPTURE_OPS.write_manifest(dir_fd, payload)

    failing = _ops(write_manifest=fail_deleted)
    plan = _capable_plan(config, failing)

    with pytest.raises(RetentionApplyStopped, match="could not record it") as raised:
        apply_retention_plan(config, plan, ops=failing)

    assert raised.value.result.deleted == plan.actions
    assert raised.value.result.reclaimed_bytes == plan.reclaimable_bytes
    assert not candidate.exists()
    result = _run(config, _ops())
    assert {event.outcome for event in result.recovered} == {"deleted", "cleaned_up"}
    assert result.final_plan.fully_converged
    assert kept.is_dir()


def test_capture_run_cleanup_failure_still_reports_deletions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    ops = _ops()
    plan = _capable_plan(config, ops)

    def fail_cleanup(_ctx: object) -> bool:
        raise OSError(errno.EIO, "simulated cleanup failure")

    monkeypatch.setattr(retention_capture, "_finalize_run", fail_cleanup)
    with pytest.raises(RetentionApplyStopped, match="cleanup failed") as raised:
        apply_retention_plan(config, plan, ops=ops)
    monkeypatch.undo()

    assert raised.value.result.deleted == plan.actions
    assert not candidate.exists()
    assert {event.outcome for event in _run(config, ops).recovered} == {"cleaned_up"}
    assert _capture_runs(config) == []
    assert kept.is_dir()


def test_os_error_during_recovery_becomes_a_retention_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _backup_config(tmp_path, limit=1)
    _make_backup(config, 0)

    def failing(_state_dir: Path, _ops: CaptureOps) -> tuple[()]:
        raise PermissionError(errno.EACCES, "simulated recovery failure")

    monkeypatch.setattr(retention, "recover_interrupted", failing)
    plan = build_retention_plan(config, ops=_ops())

    with pytest.raises(RetentionError, match="recovery failed: .*kept for a retry") as raised:
        apply_retention_plan(config, plan, ops=_ops())

    assert isinstance(raised.value.__cause__, PermissionError)


# --- recovery stops instead of guessing ------------------------------------------


@_POSIX_ONLY
def test_recovery_quarantines_a_verified_slot_whose_delete_is_refused(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    candidate_inode = candidate.stat().st_ino
    crashing = _ops(write_manifest=_crash_after_manifest_state("verified"))
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    ops = _ops(generation=_flip_generation(candidate_inode))

    with pytest.raises(RetentionApplyStopped, match="quarantined entries") as raised:
        _run(config, ops)

    assert raised.value.result.quarantined == (candidate,)
    assert raised.value.result.restored == ()
    assert not candidate.exists()  # a verified capture is never restored
    [run_id] = _quarantine_entries(config)
    assert (config.state_dir / "retention" / "quarantine" / run_id / "item-0000" / "SKILL.md").is_file()
    assert kept.is_dir()


@_POSIX_ONLY
@pytest.mark.parametrize("obstacle", ("name-retaken", "group-missing"))
def test_recovery_that_cannot_restore_quarantines_and_stops(tmp_path: Path, obstacle: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    original = _tree_bytes(candidate)
    crashing = _ops(write_manifest=_crash_after_manifest_state("captured"))
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    moved_group = tmp_path / "moved-group"
    if obstacle == "name-retaken":
        shutil.copytree(kept, candidate)  # a valid managed snapshot now holds the name
    else:
        candidate.parent.rename(moved_group)
    ops = _ops()
    reviewed = build_retention_plan(config, ops=ops)
    assert not reviewed.has_blockers

    with pytest.raises(RetentionApplyStopped, match="quarantined entries") as raised:
        apply_retention_plan(config, reviewed, ops=ops)

    assert raised.value.result.quarantined == (candidate,)
    assert raised.value.result.deleted == ()
    [run_id] = _quarantine_entries(config)
    assert _tree_bytes(config.state_dir / "retention" / "quarantine" / run_id / "item-0000") == original
    if obstacle == "name-retaken":
        assert _tree_bytes(candidate) == _tree_bytes(kept)  # the reviewed action was not deleted
    else:
        assert (moved_group / kept.name).is_dir()


_Rename = Callable[[str, str, int, int], None]


def _crash_after_quarantine_rename(rename: _Rename) -> _Rename:
    def wrapped(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        rename(source, destination, source_fd, destination_fd)
        if source == destination:  # only the quarantine rename keeps the slot name
            raise _Crash("quarantined")

    return wrapped


@_POSIX_ONLY
@pytest.mark.parametrize("state", ("verified", "captured"))
def test_crash_after_the_quarantine_rename_recovers_as_quarantined(tmp_path: Path, state: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    sub = _add_subdirectory(candidate)
    if state == "verified":
        write, verified = _after_state("verified")
        crashing = _ops(
            write_manifest=write,
            mount_id=_mount_at({sub.stat().st_ino}, verified),
            rename=_crash_after_quarantine_rename(DEFAULT_CAPTURE_OPS.rename),
        )
    else:
        captured: list[bool] = []

        def reoccupy() -> None:
            captured.append(True)
            candidate.mkdir()

        crashing = _ops(
            generation=_flip_generation(candidate.stat().st_ino, lambda: bool(captured)),
            rename=_crash_after_quarantine_rename(_first_capture(candidate.name, after=reoccupy)),
        )
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    if state == "captured":
        candidate.rmdir()  # the operator removes the empty directory that re-took the name
    ops = _ops()
    interrupted = build_retention_plan(config, ops=ops)
    assert not interrupted.has_blockers
    assert len(interrupted.recovery_pending) == 2  # the run and the entry it moved to quarantine

    with pytest.raises(RetentionApplyStopped, match="quarantined entries") as raised:
        apply_retention_plan(config, interrupted, ops=ops)

    assert raised.value.result.quarantined == (candidate,)
    assert {event.outcome for event in raised.value.result.recovered} == {"quarantined", "cleaned_up"}
    assert not candidate.exists()
    [run_id] = _quarantine_entries(config)
    quarantine = config.state_dir / "retention" / "quarantine" / run_id
    assert json.loads((quarantine / "manifest.json").read_text(encoding="utf-8"))["items"][0]["state"] == "quarantined"
    assert (quarantine / "item-0000" / "sub" / "data.txt").is_file()
    assert _capture_runs(config) == []
    assert [blocker.path for blocker in build_retention_plan(config, ops=ops).blockers] == [quarantine]


@_POSIX_ONLY
def test_empty_quarantine_directory_is_recovery_work_not_a_permanent_blocker(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    _make_backup(config, 0)
    empty = _private_run_dir(config, "quarantine", "d" * 32)
    ops = _ops()

    plan = build_retention_plan(config, ops=ops)

    assert not plan.has_blockers
    assert [item.path for item in plan.recovery_pending] == [empty]
    result = apply_retention_plan(config, plan, ops=ops)
    assert [(event.path, event.outcome) for event in result.recovered] == [(empty, "cleaned_up")]
    assert _quarantine_entries(config) == []
    assert result.final_plan.fully_converged


@_POSIX_ONLY
@pytest.mark.parametrize("state", ("deleted", "restored", "quarantined"))
def test_terminal_manifest_state_with_its_slot_present_is_a_planning_blocker(tmp_path: Path, state: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    [_make_backup(config, index) for index in range(2)]
    crashing = _ops(write_manifest=_crash_after_manifest_state("verified"))
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    [run_id] = _capture_runs(config)
    run = config.state_dir / "retention" / "capture" / run_id
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["items"][0]["state"] = state
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    assert [(blocker.path, blocker.reason) for blocker in plan.blockers] == [
        (run, f"capture slot item-0000 remains although its manifest records {state}")
    ]
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan, ops=_ops())
    assert (run / "item-0000").is_dir()


@_POSIX_ONLY
def test_recovery_refuses_capture_runs_only_when_they_exist_on_an_unsupported_platform(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    crashing = _ops(write_manifest=_crash_after_manifest_state("captured"))
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    unsupported = _ops(sys_platform="darwin")
    plan = build_retention_plan(config, ops=unsupported)
    assert not plan.capability.capable
    assert not plan.has_blockers
    before = _state_snapshot(config.state_dir)

    with pytest.raises(RetentionError, match="cannot be recovered on this platform"):
        apply_retention_plan(config, plan, ops=unsupported)

    assert _state_snapshot(config.state_dir) == before
    _run_or_stopped(config, _ops())  # a supported host restores the capture
    assert _capture_runs(config) == []
    result = _run(config, unsupported)  # an empty capture tree no longer refuses
    assert result.deleted == ()
    assert result.recovered == ()
    assert candidate.is_dir()
    assert kept.is_dir()


@_POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permissions")
def test_transient_group_error_during_recovery_keeps_the_run_instead_of_quarantining(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, _kept = [_make_backup(config, index) for index in range(2)]
    original = _tree_bytes(candidate)
    crashing = _ops(write_manifest=_crash_after_manifest_state("captured"))
    with pytest.raises(_Crash):
        apply_retention_plan(config, _capable_plan(config, crashing), ops=crashing)
    ops = _ops()
    plan = build_retention_plan(config, ops=ops)
    target = config.state_dir / "backups" / "target"
    target.chmod(0o600)
    try:
        with pytest.raises(RetentionError, match="EACCES.*kept for a retry"):
            apply_retention_plan(config, plan, ops=ops)
    finally:
        target.chmod(0o755)

    assert _quarantine_entries(config) == []
    assert len(_capture_runs(config)) == 1
    retried, _stopped = _run_or_stopped(config, ops)
    assert retried.restored == (candidate,)
    assert _tree_bytes(candidate) == original


def test_transient_restore_error_leaves_the_capture_for_recovery_not_quarantine(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, _kept = [_make_backup(config, index) for index in range(2)]
    original = _tree_bytes(candidate)
    captured: list[bool] = []

    def rename(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
        if destination == candidate.name:
            raise OSError(errno.EIO, "simulated I/O error")
        DEFAULT_CAPTURE_OPS.rename(source, destination, source_fd, destination_fd)
        captured.append(True)

    ops = _ops(rename=rename, generation=_flip_generation(candidate.stat().st_ino, lambda: bool(captured)))
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="EIO.*remains in the capture run") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.quarantined == ()
    assert _quarantine_entries(config) == []
    assert len(_capture_runs(config)) == 1
    retried, _stopped = _run_or_stopped(config, _ops())
    assert retried.restored == (candidate,)
    assert _tree_bytes(candidate) == original


# --- malformed and racing capture state ------------------------------------------


@_POSIX_ONLY
@pytest.mark.parametrize(
    "payload",
    ('{"schema_version": ' + "9" * 5000 + "}", "[" * 100_000 + "]" * 100_000),
    ids=("oversized-integer", "deep-nesting"),
)
def test_manifest_that_breaks_the_json_decoder_is_a_blocker(tmp_path: Path, payload: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    _make_backup(config, 0)
    run = _private_run_dir(config, "capture", "f" * 32)
    (run / "manifest.json").write_text(payload, encoding="utf-8")

    plan = build_retention_plan(config, ops=_ops())

    assert [(blocker.path, blocker.reason) for blocker in plan.blockers] == [
        (run, "malformed capture manifest: capture manifest is not valid JSON")
    ]


@_POSIX_ONLY
def test_capture_digest_tolerates_a_marker_with_an_oversized_integer(tmp_path: Path) -> None:
    root = _tree(tmp_path / "root", {MANAGED_MARKER: b'{"schema_version": ' + b"9" * 5000 + b"}"})

    assert _digest(root).marker_source_id is None


@_POSIX_ONLY
def test_inspection_treats_a_run_removed_after_it_was_opened_as_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _backup_config(tmp_path, limit=1)
    run = _private_run_dir(config, "capture", "e" * 32)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    real_open = os.open

    def open_then_finish(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == os.fspath(run):
            # A concurrent apply finalizes the run while inspection holds its descriptor.
            (run / "manifest.json").unlink()
            run.rmdir()
        return descriptor

    with monkeypatch.context() as patched:
        patched.setattr(os, "open", open_then_finish)
        inspection = retention_capture.inspect_capture_state(config.state_dir)

    assert inspection == CaptureInspection()


@_POSIX_ONLY
def test_inspection_treats_a_run_finished_concurrently_as_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _backup_config(tmp_path, limit=1)
    run = _private_run_dir(config, "capture", "e" * 32)
    (run / "manifest.json").write_text("{}", encoding="utf-8")

    def finished(_run_fd: int) -> object:
        raise FileNotFoundError(errno.ENOENT, "removed by a concurrent apply")

    monkeypatch.setattr(retention_capture, "_read_manifest", finished)

    assert retention_capture.inspect_capture_state(config.state_dir) == CaptureInspection()


# --- mounts inside candidates ------------------------------------------------------


def _add_subdirectory(snapshot: Path) -> Path:
    """Give a managed snapshot a nested directory and re-sign its marker."""

    sub = snapshot / "sub"
    sub.mkdir()
    (sub / "data.txt").write_text("nested\n", encoding="utf-8")
    _resign_marker(snapshot)
    return sub


def _resign_marker(snapshot: Path) -> None:
    marker = snapshot / MANAGED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["installed_digest"] = tree_digest(snapshot)
    marker.write_text(json.dumps(payload), encoding="utf-8")


def _mount_at(inodes: set[int], active: Callable[[], bool] = lambda: True) -> Callable[[int], int]:
    """Return a mount-ID seam that reports another mount for descriptors of ``inodes``."""

    def mount_id(descriptor: int) -> int:
        real = DEFAULT_CAPTURE_OPS.mount_id(descriptor)
        return real + 1000 if active() and os.fstat(descriptor).st_ino in inodes else real

    return mount_id


@_POSIX_ONLY
@pytest.mark.parametrize("mounted", ("nested", "candidate"))
def test_mount_inside_or_at_a_candidate_is_a_planning_blocker(tmp_path: Path, mounted: str) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    sub = _add_subdirectory(candidate)
    # Everything below a mount point shares its mount, so only the boundary itself differs.
    mount_root = sub if mounted == "nested" else candidate
    ops = _ops(mount_id=_mount_at({path.stat().st_ino for path in (mount_root, *mount_root.rglob("*"))}))

    plan = _capable_plan(config, ops)

    assert plan.actions == ()
    assert [blocker.path for blocker in plan.blockers] == [candidate]
    assert "mount point" in plan.blockers[0].reason
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan, ops=ops)
    assert (sub / "data.txt").read_text(encoding="utf-8") == "nested\n"
    assert kept.is_dir()


@_POSIX_ONLY
def test_mount_appearing_in_a_verified_slot_is_quarantined_never_deleted(tmp_path: Path) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    sub = _add_subdirectory(candidate)
    write, verified = _after_state("verified")
    ops = _ops(write_manifest=write, mount_id=_mount_at({sub.stat().st_ino}, verified))
    plan = _capable_plan(config, ops)

    with pytest.raises(RetentionApplyStopped, match="mount point at sub") as raised:
        apply_retention_plan(config, plan, ops=ops)

    assert raised.value.result.deleted == ()
    assert raised.value.result.quarantined == (candidate,)
    assert raised.value.result.restored == ()
    [run_id] = _quarantine_entries(config)
    slot = config.state_dir / "retention" / "quarantine" / run_id / "item-0000"
    assert (slot / "sub" / "data.txt").read_text(encoding="utf-8") == "nested\n"
    assert kept.is_dir()


def _user_namespaces_available() -> bool:
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if os.name != "posix" or unshare is None or mount is None:
        return False
    try:
        return subprocess.run([unshare, "-rm", "true"], capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_MOUNT_SCRIPT = textwrap.dedent(
    """
    import json, pickle, subprocess, sys, time
    from dataclasses import replace
    from pathlib import Path

    from agent_config_bridge.filesystem import MANAGED_MARKER, tree_digest
    from agent_config_bridge.retention import RetentionError, apply_retention_plan, build_retention_plan
    from agent_config_bridge.retention_capture import DEFAULT_CAPTURE_OPS

    config_file, kind, source, candidate = sys.argv[1:5]
    mount_point = Path(candidate) / "sub"
    if kind == "bind":
        command = ["mount", "--bind", source, str(mount_point)]
    else:
        command = ["mount", "-t", "tmpfs", "none", str(mount_point)]
    if subprocess.run(command, check=False).returncode != 0:
        sys.exit(77)  # mounting is not permitted in this namespace
    marker = Path(candidate) / MANAGED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["installed_digest"] = tree_digest(Path(candidate))  # an attacker re-signs the mounted view
    marker.write_text(json.dumps(payload), encoding="utf-8")
    config = pickle.loads(Path(config_file).read_bytes())
    ops = replace(DEFAULT_CAPTURE_OPS, now_ns=lambda: time.time_ns() + 3600 * 10**9)
    plan = build_retention_plan(config, ops=ops)
    report = {
        "capable": plan.capability.capable,
        "reason": plan.capability.reason,
        "actions": [str(action.path) for action in plan.actions],
        "blockers": [[str(blocker.path), blocker.reason] for blocker in plan.blockers],
    }
    try:
        apply_retention_plan(config, plan, ops=ops)
        report["error"] = None
    except RetentionError as exc:
        report["error"] = str(exc)
    print(json.dumps(report))
    """
)


@pytest.mark.skipif(not _user_namespaces_available(), reason="unprivileged user and mount namespaces are unavailable")
@pytest.mark.parametrize(("kind", "detail"), (("bind", "mount point at sub"), ("tmpfs", "device boundary at sub")))
def test_real_mount_inside_a_candidate_blocks_planning_in_a_user_namespace(
    tmp_path: Path,
    kind: str,
    detail: str,
) -> None:
    config = _backup_config(tmp_path, limit=1)
    candidate, kept = [_make_backup(config, index) for index in range(2)]
    _add_subdirectory(candidate)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("victim\n", encoding="utf-8")
    config_file = tmp_path / "config.pickle"
    config_file.write_bytes(pickle.dumps(config))
    environment = {**os.environ, "PYTHONPATH": str(Path(retention.__file__).resolve().parents[1])}

    completed = subprocess.run(
        ["unshare", "-rm", sys.executable, "-c", _MOUNT_SCRIPT, str(config_file), kind, str(victim), str(candidate)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )

    if completed.returncode == 77:
        pytest.skip(f"{kind} mounts are not permitted in an unprivileged user namespace")
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    if not report["capable"]:
        pytest.skip(f"generation-bound capture is unavailable in the namespace: {report['reason']}")
    assert report["actions"] == []
    assert [path for path, _reason in report["blockers"]] == [str(candidate)]
    assert detail in report["blockers"][0][1]
    assert "contains blockers" in report["error"]
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "victim\n"
    assert (candidate / "sub" / "data.txt").is_file()  # the mount ended with the namespace
    assert kept.is_dir()
