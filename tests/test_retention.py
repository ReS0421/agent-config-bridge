"""Tests for bounded Bridge-generated state retention."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.filesystem import MANAGED_MARKER, tree_digest
from agent_config_bridge.models import Component, RetentionConfig
from agent_config_bridge.renderer import render_marketplace
from agent_config_bridge.retention import (
    RetentionError,
    apply_retention_plan,
    build_retention_plan,
)
from tests.conftest import (
    make_catalog,
    make_config,
    require_directory_symlink_support,
)


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


def _make_backup(
    config,
    index: int,
    *,
    target: str = "target",
    skill: str = "hello",
) -> Path:
    group = config.state_dir / "backups" / target / skill
    group.mkdir(parents=True, exist_ok=True)
    snapshot = group / f"20260723-0000{index:02d}-{index + 1:08x}"
    snapshot.mkdir()
    (snapshot / "SKILL.md").write_text(f"snapshot {index}\n", encoding="utf-8")
    digest = tree_digest(snapshot)
    (snapshot / MANAGED_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": f"skills/{skill}",
                "installed_digest": digest,
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def _state_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes], ...]:
    if not root.exists():
        return ()
    snapshot: list[tuple[str, str, int, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        if path.is_symlink():
            snapshot.append((relative, "symlink", status.st_mtime_ns, os.readlink(path).encode()))
        elif path.is_file():
            snapshot.append((relative, "file", status.st_mtime_ns, path.read_bytes()))
        else:
            snapshot.append((relative, "directory", status.st_mtime_ns, b""))
    return tuple(snapshot)


def test_plan_and_apply_keep_twenty_builds_including_old_published_build(
    tmp_path: Path,
) -> None:
    config = _marketplace_config(tmp_path)
    builds, published = _make_builds(config, 22)

    plan = build_retention_plan(config)

    assert not plan.has_blockers
    assert plan.build_count == 22
    assert len(plan.actions) == 2
    assert published not in {action.path for action in plan.actions}

    if not shutil.rmtree.avoids_symlink_attacks:
        with pytest.raises(RetentionError, match="descriptor-anchored"):
            apply_retention_plan(config, plan)
        assert all(path.is_dir() for path in builds)
        return

    result = apply_retention_plan(config, plan)

    assert len(result.deleted) == 2
    assert sum(path.is_dir() for path in builds) == 20
    assert published.is_dir()
    assert not result.final_plan.has_changes


def test_missing_build_for_published_marketplace_blocks_all_deletion(
    tmp_path: Path,
) -> None:
    config = _marketplace_config(tmp_path, build_limit=1)
    builds, published = _make_builds(config, 3)
    shutil.rmtree(published)

    plan = build_retention_plan(config)

    assert plan.has_changes
    assert plan.has_blockers
    assert any(
        blocker.path == published and "no matching immutable build" in blocker.reason for blocker in plan.blockers
    )
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in builds if path != published)


def test_skill_backup_plan_keeps_latest_three_by_snapshot_name_and_applies(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    backups = [_make_backup(config, index) for index in range(5)]
    for index, path in enumerate(backups):
        timestamp = 1_700_000_000_000_000_000 + (len(backups) - index)
        os.utime(path, ns=(timestamp, timestamp))

    plan = build_retention_plan(config)

    assert [action.path for action in plan.actions] == backups[:2]
    if not shutil.rmtree.avoids_symlink_attacks:
        with pytest.raises(RetentionError, match="descriptor-anchored"):
            apply_retention_plan(config, plan)
        assert all(path.is_dir() for path in backups)
        return

    result = apply_retention_plan(config, plan)
    assert [path.exists() for path in backups] == [False, False, True, True, True]
    assert len(result.deleted) == 2


def test_skill_backup_limit_never_deletes_the_only_snapshot(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    only = _make_backup(config, 0)

    plan = build_retention_plan(config)

    assert not plan.has_changes
    assert only.is_dir()


def test_terminal_symlink_snapshot_is_unlinked_without_following_target(
    tmp_path: Path,
) -> None:
    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    group = config.state_dir / "backups" / "target" / "hello"
    group.mkdir(parents=True)
    old_link = group / "20260723-000000-00000001"
    old_link.symlink_to(outside, target_is_directory=True)
    newest = _make_backup(config, 1)

    plan = build_retention_plan(config)

    assert [action.path for action in plan.actions] == [old_link]
    if not shutil.rmtree.avoids_symlink_attacks:
        with pytest.raises(RetentionError, match="descriptor-anchored"):
            apply_retention_plan(config, plan)
        assert old_link.is_symlink()
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        return

    apply_retention_plan(config, plan)
    assert not old_link.is_symlink()
    assert newest.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_any_unsafe_entry_globally_blocks_all_deletion(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    backups = [_make_backup(config, index) for index in range(3)]
    odd = backups[0].parent / "operator-note.txt"
    odd.write_text("do not delete\n", encoding="utf-8")

    plan = build_retention_plan(config)

    assert plan.has_changes
    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in backups)
    assert odd.is_file()


def test_marker_mismatch_globally_blocks_other_valid_candidates(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    backups = [_make_backup(config, index) for index in range(4)]
    marker = backups[-1] / MANAGED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["installed_digest"] = "0" * 64
    marker.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_retention_plan(config)

    assert plan.has_changes
    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in backups)


def test_redirected_backup_ancestor_blocks_without_following_it(
    tmp_path: Path,
) -> None:
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
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    backups = [_make_backup(config, index) for index in range(3)]
    fifo = backups[0].parent / "20260723-000099-ffffffff"
    os.mkfifo(fifo)

    plan = build_retention_plan(config)

    assert plan.has_blockers
    with pytest.raises(RetentionError, match="contains blockers"):
        apply_retention_plan(config, plan)
    assert all(path.is_dir() for path in backups)
    assert fifo.exists()


def test_candidate_identity_replacement_after_plan_deletes_nothing(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    backups = [_make_backup(config, index) for index in range(3)]
    candidate = backups[0]
    plan = build_retention_plan(config)
    shutil.rmtree(candidate)
    replacement = _make_backup(config, 0)

    expected = "changed after retention planning" if shutil.rmtree.avoids_symlink_attacks else "descriptor-anchored"
    with pytest.raises(RetentionError, match=expected):
        apply_retention_plan(config, plan)

    assert replacement.is_dir()
    assert all(path.is_dir() for path in backups[1:])


def test_apply_blocks_before_deletion_without_descriptor_safe_rmtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = replace(
        make_config(tmp_path, catalog),
        retention=RetentionConfig(marketplace_builds=20, skill_backups=1),
    )
    backups = [_make_backup(config, index) for index in range(3)]
    plan = build_retention_plan(config)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)

    with pytest.raises(RetentionError, match="descriptor-anchored"):
        apply_retention_plan(config, plan)

    assert all(path.is_dir() for path in backups)


def test_dry_run_is_byte_and_metadata_invariant_and_excludes_instructions(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    [_make_backup(config, index) for index in range(4)]
    instructions = config.state_dir / "backups" / "target" / "instructions"
    instructions.mkdir()
    (instructions / "AGENTS.md").write_text("retained\n", encoding="utf-8")
    before = _state_snapshot(config.state_dir)

    plan = build_retention_plan(config)

    assert plan.excluded_instruction_roots == (instructions,)
    assert _state_snapshot(config.state_dir) == before
    assert not (config.state_dir / ".agentbridge-retention.lock").exists()
