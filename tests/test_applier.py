"""Tests for conflict-aware plan application."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_config_bridge import applier, filesystem
from agent_config_bridge import renderer as renderer_module
from agent_config_bridge.applier import ApplyError, apply_plan, apply_skill_plan
from agent_config_bridge.catalog import Artifact, CatalogError, discover_catalog
from agent_config_bridge.filesystem import MANAGED_MARKER, FilesystemError, tree_digest
from agent_config_bridge.models import Component, LinkMode, Platform
from agent_config_bridge.planner import Disposition, Operation, build_plan
from agent_config_bridge.provenance import read_skill_root_marker
from agent_config_bridge.renderer import RenderError
from agent_config_bridge.state import BridgeStateError, read_skill_state
from tests.conftest import make_catalog, make_config, require_directory_symlink_support


def test_apply_creates_and_reuses_skill_symlink(tmp_path: Path) -> None:
    """A planned link is created once and becomes a no-op."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    result = apply_plan(config, inventory, build_plan(config, inventory))
    second_plan = build_plan(config, inventory)

    assert len(result.applied) == 1
    assert (tmp_path / "home/.agents/skills/hello").is_symlink()
    assert second_plan.actions[0].disposition is Disposition.NOOP


def test_apply_copy_updates_only_unchanged_managed_destination(tmp_path: Path) -> None:
    """Managed Windows copies update with a retained backup."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        mode=LinkMode.COPY,
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    assert (destination / MANAGED_MARKER).is_file()

    source = catalog / "skills/hello/SKILL.md"
    source.write_text(source.read_text(encoding="utf-8") + "Updated.\n", encoding="utf-8")
    updated_plan = build_plan(config, discover_catalog(config))
    assert updated_plan.actions[0].disposition is Disposition.UPDATE

    result = apply_plan(config, discover_catalog(config), updated_plan)

    assert len(result.backups) == 1
    assert result.backups[0].is_dir()
    assert "Updated." in (destination / "SKILL.md").read_text(encoding="utf-8")


def test_skill_sync_migrates_owned_symlink_to_managed_copy_with_backup(tmp_path: Path) -> None:
    """Copy mode safely replaces only a still-matching Bridge-owned Skill link."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    linked_config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(linked_config)
    apply_plan(linked_config, inventory, build_plan(linked_config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    assert destination.is_symlink()

    copy_config = replace(linked_config, link_mode=LinkMode.COPY)
    plan = build_plan(copy_config, discover_catalog(copy_config))

    assert plan.actions[0].operation is Operation.COPY
    assert plan.actions[0].disposition is Disposition.UPDATE
    assert plan.actions[0].link_mode is LinkMode.SYMLINK

    result = apply_skill_plan(copy_config, discover_catalog(copy_config), plan)

    assert destination.is_dir() and not destination.is_symlink()
    assert (destination / MANAGED_MARKER).is_file()
    assert len(result.backups) == 1
    assert result.backups[0].is_symlink()
    assert read_skill_state(copy_config, copy_config.targets[0])[0].mode is LinkMode.COPY
    assert build_plan(copy_config, discover_catalog(copy_config)).actions[0].disposition is Disposition.NOOP


def test_skill_sync_recovers_after_partial_symlink_to_copy_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry reconciles mixed physical installs left before ownership commit."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog", skills=("alpha", "beta"))
    linked_config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(linked_config)
    apply_plan(linked_config, inventory, build_plan(linked_config, inventory))
    copy_config = replace(linked_config, link_mode=LinkMode.COPY)
    migration_plan = build_plan(copy_config, discover_catalog(copy_config))
    real_apply_copy = applier.apply_copy
    calls = 0

    def fail_second_copy(*args: object, **kwargs: object) -> Path | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FilesystemError("injected second-copy failure")
        return real_apply_copy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(applier, "apply_copy", fail_second_copy)
    with pytest.raises(FilesystemError, match="injected second-copy failure"):
        apply_skill_plan(copy_config, discover_catalog(copy_config), migration_plan)

    root = tmp_path / "home/.agents/skills"
    assert (root / "alpha").is_dir() and not (root / "alpha").is_symlink()
    assert (root / "beta").is_symlink()
    assert all(entry.mode is LinkMode.SYMLINK for entry in read_skill_state(copy_config, copy_config.targets[0]))

    recovery_plan = build_plan(copy_config, discover_catalog(copy_config))
    assert not recovery_plan.has_conflicts
    alpha_action, beta_action = recovery_plan.actions
    assert alpha_action.disposition is Disposition.UPDATE
    assert alpha_action.link_mode is LinkMode.COPY
    assert "recover ownership state" in alpha_action.detail
    assert beta_action.disposition is Disposition.UPDATE
    assert beta_action.link_mode is LinkMode.SYMLINK

    monkeypatch.setattr(applier, "apply_copy", real_apply_copy)
    result = apply_skill_plan(copy_config, discover_catalog(copy_config), recovery_plan)

    assert len(result.applied) == 2
    assert all((root / name).is_dir() and not (root / name).is_symlink() for name in ("alpha", "beta"))
    assert all(entry.mode is LinkMode.COPY for entry in read_skill_state(copy_config, copy_config.targets[0]))
    assert all(
        action.disposition is Disposition.NOOP
        for action in build_plan(copy_config, discover_catalog(copy_config)).actions
    )


def test_skill_sync_recovery_rejects_partial_copy_with_stale_source_digest(tmp_path: Path) -> None:
    """Recovery ownership is not granted when the canonical source has changed."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    linked_config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(linked_config)
    apply_plan(linked_config, inventory, build_plan(linked_config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    source = catalog / "skills/hello"
    destination.unlink()
    applier.apply_copy(
        source,
        destination,
        source_id="skills/hello",
        source_digest=tree_digest(source),
        state_dir=linked_config.state_dir,
        target_name=linked_config.targets[0].name,
        update=False,
    )
    skill_file = source / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "new source revision\n", encoding="utf-8")

    copy_config = replace(linked_config, link_mode=LinkMode.COPY)
    plan = build_plan(copy_config, discover_catalog(copy_config))

    assert plan.has_conflicts
    assert "does not match the canonical source" in plan.actions[0].detail
    assert read_skill_state(copy_config, copy_config.targets[0])[0].mode is LinkMode.SYMLINK


def test_skill_sync_checkpoints_each_copy_create_before_later_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later CREATE failure leaves earlier installs durably owned and retryable."""

    catalog = make_catalog(tmp_path / "catalog", skills=("alpha", "beta"))
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    real_apply_copy = applier.apply_copy
    calls = 0

    def fail_second_copy(*args: object, **kwargs: object) -> Path | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FilesystemError("injected second-create failure")
        return real_apply_copy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(applier, "apply_copy", fail_second_copy)
    with pytest.raises(FilesystemError, match="injected second-create failure"):
        apply_skill_plan(config, inventory, plan)

    root = tmp_path / "home/.agents/skills"
    assert (root / "alpha").is_dir()
    assert not (root / "beta").exists()
    assert [entry.name for entry in read_skill_state(config, config.targets[0])] == ["alpha"]

    recovery_plan = build_plan(config, discover_catalog(config))
    assert not recovery_plan.has_conflicts
    assert [action.disposition for action in recovery_plan.actions] == [Disposition.NOOP, Disposition.CREATE]

    monkeypatch.setattr(applier, "apply_copy", real_apply_copy)
    result = apply_skill_plan(config, discover_catalog(config), recovery_plan)

    assert [action.name for action in result.applied] == ["beta"]
    assert all((root / name).is_dir() for name in ("alpha", "beta"))
    assert [entry.name for entry in read_skill_state(config, config.targets[0])] == ["alpha", "beta"]


@pytest.mark.parametrize("mode", [LinkMode.COPY, LinkMode.SYMLINK])
def test_skill_create_rolls_back_when_ownership_checkpoint_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: LinkMode,
) -> None:
    """COPY and LINK creates never remain marker-only after state-write failure."""

    if mode is LinkMode.SYMLINK:
        require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=mode)
    inventory = discover_catalog(config)
    destination = tmp_path / "home/.agents/skills/hello"

    def fail_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise BridgeStateError("injected checkpoint failure")

    monkeypatch.setattr(applier, "write_skill_state_entries", fail_checkpoint)

    with pytest.raises(ApplyError, match="no filesystem mutation occurred"):
        apply_skill_plan(config, inventory, build_plan(config, inventory))

    assert not destination.exists()
    assert not destination.is_symlink()
    assert read_skill_state(config, config.targets[0]) == ()
    assert not build_plan(config, discover_catalog(config)).has_conflicts


def test_copy_update_restores_destination_when_backup_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checked backup failure happens before install and leaves a clean retry."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    old_content = (destination / "SKILL.md").read_text(encoding="utf-8")
    source_file = catalog / "skills/hello/SKILL.md"
    source_file.write_text(source_file.read_text(encoding="utf-8") + "updated source\n", encoding="utf-8")
    update_plan = build_plan(config, discover_catalog(config))
    real_copytree = filesystem.shutil.copytree

    def fail_backup_snapshot(source: object, target: object, *args: object, **kwargs: object) -> object:
        if Path(source) == destination:
            partial = Path(target)
            partial.mkdir()
            (partial / "partial-copy").write_text("incomplete", encoding="utf-8")
            raise OSError("injected backup snapshot failure")
        return real_copytree(source, target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(filesystem.shutil, "copytree", fail_backup_snapshot)
    with pytest.raises(FilesystemError, match="could not snapshot managed Skill backup"):
        apply_skill_plan(config, discover_catalog(config), update_plan)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == old_content
    assert read_skill_state(config, config.targets[0])[0].mode is LinkMode.COPY
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.tmp"))
    backup_parent = config.state_dir / "backups/target/hello"
    assert not backup_parent.exists() or not tuple(backup_parent.iterdir())
    retry_plan = build_plan(config, discover_catalog(config))
    assert not retry_plan.has_conflicts
    assert retry_plan.actions[0].disposition is Disposition.UPDATE

    monkeypatch.setattr(filesystem.shutil, "copytree", real_copytree)
    result = apply_skill_plan(config, discover_catalog(config), retry_plan)

    assert "updated source" in (destination / "SKILL.md").read_text(encoding="utf-8")
    assert len(result.backups) == 1


def test_copy_update_atomically_restores_swap_when_staged_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install failure restores the same-filesystem swap without backup consumption."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    old_content = (destination / "SKILL.md").read_text(encoding="utf-8")
    source_file = catalog / "skills/hello/SKILL.md"
    source_file.write_text(source_file.read_text(encoding="utf-8") + "updated source\n", encoding="utf-8")
    update_plan = build_plan(config, discover_catalog(config))
    real_replace = filesystem.os.replace

    def fail_staged_install(source: object, target: object) -> None:
        source_path = Path(source)
        if source_path.name.endswith(".tmp") and Path(target) == destination:
            raise OSError("injected staged install failure")
        real_replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(filesystem.os, "replace", fail_staged_install)
    with pytest.raises(FilesystemError, match="failed to install staged managed Skill copy"):
        apply_skill_plan(config, discover_catalog(config), update_plan)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == old_content
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.tmp"))
    backup_parent = config.state_dir / "backups/target/hello"
    assert not backup_parent.exists() or not tuple(backup_parent.iterdir())
    assert not build_plan(config, discover_catalog(config)).has_conflicts


def test_copy_update_revalidates_live_destination_after_backup_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User drift during snapshot aborts before swap and remains untouched."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    source_file = catalog / "skills/hello/SKILL.md"
    source_file.write_text(source_file.read_text(encoding="utf-8") + "updated source\n", encoding="utf-8")
    update_plan = build_plan(config, discover_catalog(config))
    real_copytree = filesystem.shutil.copytree

    def drift_after_snapshot(source: object, target: object, *args: object, **kwargs: object) -> object:
        result = real_copytree(source, target, *args, **kwargs)  # type: ignore[arg-type]
        if Path(source) == destination:
            (destination / "user-drift.txt").write_text("preserve me", encoding="utf-8")
        return result

    monkeypatch.setattr(filesystem.shutil, "copytree", drift_after_snapshot)
    with pytest.raises(FilesystemError, match="changed after backup snapshot"):
        apply_skill_plan(config, discover_catalog(config), update_plan)

    assert (destination / "user-drift.txt").read_text(encoding="utf-8") == "preserve me"
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.tmp"))
    backup_parent = config.state_dir / "backups/target/hello"
    assert not backup_parent.exists() or not tuple(backup_parent.iterdir())
    assert build_plan(config, discover_catalog(config)).has_conflicts


def test_copy_remove_snapshot_failure_leaves_destination_untouched_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deselection never destructively moves the only valid copy across filesystems."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    old_digest = tree_digest(destination)
    empty_target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(empty_target,))
    removal_plan = build_plan(deselected, discover_catalog(deselected))
    real_copytree = filesystem.shutil.copytree

    def fail_backup_snapshot(source: object, target: object, *args: object, **kwargs: object) -> object:
        if Path(source) == destination:
            partial = Path(target)
            partial.mkdir()
            (partial / "partial-copy").write_text("incomplete", encoding="utf-8")
            raise OSError("injected removal backup snapshot failure")
        return real_copytree(source, target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(filesystem.shutil, "copytree", fail_backup_snapshot)
    with pytest.raises(FilesystemError, match="could not snapshot deselected Skill backup"):
        apply_skill_plan(deselected, discover_catalog(deselected), removal_plan)

    assert destination.is_dir()
    assert tree_digest(destination) == old_digest
    assert read_skill_state(deselected, empty_target)
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    backup_parent = config.state_dir / "backups/target/hello"
    assert not backup_parent.exists() or not tuple(backup_parent.iterdir())
    retry_plan = build_plan(deselected, discover_catalog(deselected))
    assert not retry_plan.has_conflicts
    assert retry_plan.actions[0].disposition is Disposition.REMOVE

    monkeypatch.setattr(filesystem.shutil, "copytree", real_copytree)
    result = apply_skill_plan(deselected, discover_catalog(deselected), retry_plan)

    assert not destination.exists()
    assert len(result.backups) == 1
    assert tree_digest(result.backups[0]) == old_digest
    assert read_skill_state(deselected, empty_target) == ()


def test_copy_remove_revalidates_live_destination_after_backup_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deselection preserves user drift introduced while its backup is copied."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    empty_target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(empty_target,))
    removal_plan = build_plan(deselected, discover_catalog(deselected))
    real_copytree = filesystem.shutil.copytree

    def drift_after_snapshot(source: object, target: object, *args: object, **kwargs: object) -> object:
        result = real_copytree(source, target, *args, **kwargs)  # type: ignore[arg-type]
        if Path(source) == destination:
            (destination / "user-drift.txt").write_text("preserve me", encoding="utf-8")
        return result

    monkeypatch.setattr(filesystem.shutil, "copytree", drift_after_snapshot)
    with pytest.raises(FilesystemError, match="changed after backup snapshot"):
        apply_skill_plan(deselected, discover_catalog(deselected), removal_plan)

    assert (destination / "user-drift.txt").read_text(encoding="utf-8") == "preserve me"
    assert read_skill_state(deselected, empty_target)
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    backup_parent = config.state_dir / "backups/target/hello"
    assert not backup_parent.exists() or not tuple(backup_parent.iterdir())
    assert build_plan(deselected, discover_catalog(deselected)).has_conflicts


def test_copy_remove_partial_swap_cleanup_restores_from_retained_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially deleted swap is never restored as the owned destination."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_skill_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    old_digest = tree_digest(destination)
    empty_target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(empty_target,))
    removal_plan = build_plan(deselected, discover_catalog(deselected))
    real_remove_path = filesystem._remove_path
    injected = False

    def partially_delete_swap(path: Path) -> None:
        nonlocal injected
        if not injected and path.name.endswith(".old"):
            injected = True
            (path / "SKILL.md").unlink()
            raise OSError("injected partial swap cleanup failure")
        real_remove_path(path)

    monkeypatch.setattr(filesystem, "_remove_path", partially_delete_swap)
    with pytest.raises(FilesystemError, match="restored and verified from retained backup") as raised:
        apply_skill_plan(deselected, discover_catalog(deselected), removal_plan)

    assert destination.is_dir()
    assert tree_digest(destination) == old_digest
    backups = tuple((config.state_dir / "backups/target/hello").iterdir())
    assert len(backups) == 1
    assert tree_digest(backups[0]) == old_digest
    assert str(backups[0]) in str(raised.value)
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.old"))
    assert not tuple(destination.parent.glob(".hello.agentbridge.*.restore.tmp"))
    assert read_skill_state(deselected, empty_target)
    retry_plan = build_plan(deselected, discover_catalog(deselected))
    assert not retry_plan.has_conflicts
    assert retry_plan.actions[0].disposition is Disposition.REMOVE

    result = apply_skill_plan(deselected, discover_catalog(deselected), retry_plan)

    assert not destination.exists()
    assert len(result.backups) == 1
    assert read_skill_state(deselected, empty_target) == ()


def test_skill_sync_rejects_changed_owned_symlink_during_copy_migration(tmp_path: Path) -> None:
    """A recorded link redirected after installation cannot be migrated or overwritten."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    linked_config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(linked_config)
    apply_plan(linked_config, inventory, build_plan(linked_config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    destination.unlink()
    destination.symlink_to(foreign, target_is_directory=True)

    copy_config = replace(linked_config, link_mode=LinkMode.COPY)
    plan = build_plan(copy_config, discover_catalog(copy_config))

    assert plan.has_conflicts
    with pytest.raises(ApplyError, match="full plan has conflicts"):
        apply_skill_plan(copy_config, discover_catalog(copy_config), plan)
    assert destination.is_symlink()


def test_skill_sync_rejects_stale_full_plan_before_mutation(tmp_path: Path) -> None:
    """The scoped apply rebuilds the complete plan before creating any Skill."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    source = catalog / "skills/hello/SKILL.md"
    source.write_text(source.read_text(encoding="utf-8") + "late change\n", encoding="utf-8")

    with pytest.raises(ApplyError, match="changed after planning"):
        apply_skill_plan(config, inventory, plan)

    assert not (tmp_path / "home/.agents/skills/hello").exists()


def test_skill_sync_never_renders_or_writes_non_skill_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Skill NOOP actions and command hints stay completely passive."""

    monkeypatch.setattr("agent_config_bridge.planner.current_platform", lambda: Platform.LINUX)
    catalog = make_catalog(tmp_path / "catalog", plugins=("shared",))
    components = frozenset({Component.SKILLS, Component.PLUGINS})
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY, components=components)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    source = catalog / "skills/hello/SKILL.md"
    source.write_text(source.read_text(encoding="utf-8") + "skill update\n", encoding="utf-8")
    plan = build_plan(config, discover_catalog(config))
    assert any(
        action.component is Component.PLUGINS and action.disposition is Disposition.NOOP for action in plan.actions
    )
    assert plan.commands

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("skill-only sync called a non-Skill writer")

    monkeypatch.setattr(applier, "render_marketplace", unexpected)
    monkeypatch.setattr(applier, "write_instruction_state", unexpected)
    monkeypatch.setattr(applier, "write_settings_state", unexpected)

    result = apply_skill_plan(config, discover_catalog(config), plan)

    assert len(result.applied) == 1
    assert result.marketplace is None
    assert result.schedules == ()


def test_apply_copy_rejects_forged_marker_symlink(tmp_path: Path) -> None:
    """A marker symlink cannot preserve or authorize managed-copy ownership."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        mode=LinkMode.COPY,
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    marker = destination / MANAGED_MARKER
    external = tmp_path / "forged-marker.json"
    external.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
    marker.unlink()
    try:
        marker.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].disposition is Disposition.CONFLICT
    assert "no valid bridge ownership marker" in plan.actions[0].detail
    with pytest.raises(ApplyError, match="conflicts"):
        apply_plan(config, discover_catalog(config), plan)


def test_apply_rejects_plan_with_conflict(tmp_path: Path) -> None:
    """No safe actions run while any conflict remains."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    destination = tmp_path / "home/.agents/skills/hello"
    destination.mkdir(parents=True)
    inventory = discover_catalog(config)

    with pytest.raises(ApplyError, match="conflicts"):
        apply_plan(config, inventory, build_plan(config, inventory))


def test_apply_rejects_orphaned_target_ownership(tmp_path: Path) -> None:
    """Another target cannot adopt destinations while old ownership is orphaned."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    disabled = replace(config.targets[0], enabled=False)
    orphaned_config = replace(config, targets=(disabled,))
    orphaned_plan = build_plan(orphaned_config, inventory)

    with pytest.raises(ApplyError, match="no enabled target"):
        apply_plan(orphaned_config, inventory, orphaned_plan)


def test_skill_root_handoff_requires_completed_empty_reconciliation(tmp_path: Path) -> None:
    """A replacement target can claim a root only after the old state is empty."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(
        tmp_path,
        catalog,
        target_name="old",
        platform=Platform.WINDOWS,
        mode=LinkMode.COPY,
    )
    inventory = discover_catalog(config)
    old_target = config.targets[0]
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    assert destination.is_dir()

    cleanup_target = replace(old_target, components=frozenset())
    new_target = replace(old_target, name="new")
    unsafe_handoff = replace(config, targets=(cleanup_target, new_target))

    with pytest.raises(BridgeStateError, match="reconcile 'old' alone"):
        build_plan(unsafe_handoff, inventory)
    assert destination.is_dir()

    cleanup = replace(
        config,
        components=frozenset(),
        targets=(cleanup_target,),
    )
    apply_plan(cleanup, inventory, build_plan(cleanup, inventory))
    assert read_skill_state(cleanup, cleanup_target) == ()
    assert not destination.exists()

    safe_handoff = replace(config, targets=(new_target,))
    safe_plan = build_plan(safe_handoff, inventory)
    assert any(action.target == "new" and action.disposition is Disposition.CREATE for action in safe_plan.actions)

    apply_plan(safe_handoff, inventory, safe_plan)
    assert destination.is_dir()
    assert read_skill_state(safe_handoff, new_target)


def test_apply_rejects_stale_plan_after_source_change(tmp_path: Path) -> None:
    """Apply rechecks the catalog after the user reviewed a plan."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    skill = catalog / "skills/hello/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Changed after plan.\n", encoding="utf-8")

    with pytest.raises(ApplyError, match="changed after planning"):
        apply_plan(config, inventory, plan)

    assert not (tmp_path / "home/.agents/skills/hello").exists()


def test_apply_rejects_stale_hook_source_before_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed Hook plan is invalidated before rendering any marketplace."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("native-event",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.HOOKS}),
    )
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    hook = catalog / "hooks/native-event/common/hooks.json"
    payload = json.loads(hook.read_text(encoding="utf-8"))
    payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "python changed-after-plan.py"
    hook.write_text(json.dumps(payload), encoding="utf-8")
    render = Mock(side_effect=AssertionError("stale Hook plans must fail before render"))
    monkeypatch.setattr(applier, "render_marketplace", render)

    with pytest.raises(ApplyError, match="changed after planning"):
        apply_plan(config, inventory, plan)

    render.assert_not_called()
    assert not config.state_dir.exists()


def test_apply_never_renders_transient_hook_source_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed A -> transient B -> A Hook tree cannot publish B."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("native-event",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.HOOKS}),
    )
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    hook = catalog / "hooks/native-event/common/hooks.json"
    original_bytes = hook.read_bytes()
    payload = json.loads(original_bytes)
    transient_command = "python transient-b-must-never-render.py"
    payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] = transient_command
    transient_bytes = json.dumps(payload).encode()
    original_freeze = renderer_module._freeze_artifact_tree
    freeze_calls = 0
    injected = False

    def freeze_with_source_aba(artifact: Artifact) -> renderer_module._FrozenArtifactTree:
        nonlocal freeze_calls, injected
        freeze_calls += 1
        if freeze_calls == 2 and artifact.name == "native-event":
            hook.write_bytes(transient_bytes)
            try:
                return original_freeze(artifact)
            finally:
                hook.write_bytes(original_bytes)
                injected = True
        return original_freeze(artifact)

    unexpected_render = Mock(side_effect=AssertionError("digest-mismatched B must not reach Hook rendering"))
    monkeypatch.setattr(renderer_module, "_freeze_artifact_tree", freeze_with_source_aba)
    monkeypatch.setattr(renderer_module, "_render_hook_plugin", unexpected_render)

    with pytest.raises(RenderError, match="reviewed marketplace digest"):
        apply_plan(config, inventory, plan)

    assert injected is True
    assert freeze_calls == 2
    assert hook.read_bytes() == original_bytes
    unexpected_render.assert_not_called()
    assert not (config.state_dir / "marketplace").exists()
    assert not any(
        transient_command.encode() in path.read_bytes() for path in config.state_dir.rglob("*") if path.is_file()
    )


def test_plan_marketplace_digest_and_review_share_frozen_hook_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewed Hook command comes from the exact tree behind its digest."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("native-event",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.HOOKS}),
    )
    inventory = discover_catalog(config)
    hook = catalog / "hooks/native-event/common/hooks.json"
    original_bytes = hook.read_bytes()
    payload = json.loads(original_bytes)
    original_command = payload["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    transient_command = "python reviewed-transient-b.py"
    payload["hooks"]["SessionStart"][0]["hooks"][0]["command"] = transient_command
    transient_bytes = json.dumps(payload).encode()
    original_freeze = renderer_module._freeze_artifact_tree
    injected = False

    def freeze_with_source_aba(artifact: Artifact) -> renderer_module._FrozenArtifactTree:
        nonlocal injected
        if not injected and artifact.name == "native-event":
            hook.write_bytes(transient_bytes)
            try:
                return original_freeze(artifact)
            finally:
                hook.write_bytes(original_bytes)
                injected = True
        return original_freeze(artifact)

    monkeypatch.setattr(renderer_module, "_freeze_artifact_tree", freeze_with_source_aba)

    plan = build_plan(config, inventory)
    marketplace_action = next(action for action in plan.actions if action.target == "marketplace")
    live_digest = renderer_module.marketplace_digest(config, discover_catalog(config))

    assert injected is True
    assert marketplace_action.source_digest != live_digest
    assert any(transient_command in review for review in plan.reviews)
    assert not any(original_command in review for review in plan.reviews)


def test_plan_and_apply_reject_invalid_frozen_hook_snapshot_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-valid A cannot authorize an invalid frozen B that later reverts to A."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("native-event",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.HOOKS}),
    )
    inventory = discover_catalog(config)
    reviewed_plan = build_plan(config, inventory)
    hook = catalog / "hooks/native-event/common/hooks.json"
    original_bytes = hook.read_bytes()
    payload = json.loads(original_bytes)
    payload["hooks"]["SessionStart"][0]["hooks"][0]["async"] = "NOT_A_BOOLEAN"
    invalid_bytes = json.dumps(payload).encode()
    original_freeze = renderer_module._freeze_artifact_tree
    freeze_calls = 0

    def freeze_invalid_b_then_restore_a(artifact: Artifact) -> renderer_module._FrozenArtifactTree:
        nonlocal freeze_calls
        if artifact.name == "native-event":
            freeze_calls += 1
            hook.write_bytes(invalid_bytes)
            try:
                return original_freeze(artifact)
            finally:
                hook.write_bytes(original_bytes)
        return original_freeze(artifact)

    unexpected_render = Mock(side_effect=AssertionError("invalid frozen Hook B must not reach rendering"))
    monkeypatch.setattr(renderer_module, "_freeze_artifact_tree", freeze_invalid_b_then_restore_a)
    monkeypatch.setattr(applier, "render_marketplace", unexpected_render)

    with pytest.raises(CatalogError, match="hook async.*boolean"):
        build_plan(config, discover_catalog(config))
    assert hook.read_bytes() == original_bytes

    with pytest.raises(CatalogError, match="hook async.*boolean"):
        apply_plan(config, inventory, reviewed_plan)

    assert freeze_calls == 2
    assert hook.read_bytes() == original_bytes
    unexpected_render.assert_not_called()
    assert not (config.state_dir / "marketplace").exists()


def test_windows_hook_review_lists_command_and_command_windows_from_common_snapshot(
    tmp_path: Path,
) -> None:
    """A Windows override is a separate labeled review item beside command."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("native-event",))
    hook = catalog / "hooks/native-event/common/hooks.json"
    payload = json.loads(hook.read_text(encoding="utf-8"))
    handler = payload["hooks"]["SessionStart"][0]["hooks"][0]
    handler["command"] = "python benign.py"
    handler["commandWindows"] = "powershell -File dangerous.ps1"
    hook.write_text(json.dumps(payload), encoding="utf-8")
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        components=frozenset({Component.HOOKS}),
    )

    plan = build_plan(config, discover_catalog(config))
    hook_reviews = tuple(review for review in plan.reviews if "hook native-event/common event=SessionStart" in review)

    assert any("command='python benign.py'" in review for review in hook_reviews)
    assert any("commandWindows='powershell -File dangerous.ps1'" in review for review in hook_reviews)
    assert len(hook_reviews) == 2


def test_apply_removes_deselected_managed_symlink(tmp_path: Path) -> None:
    """Deselecting Skills removes a link recorded by an earlier apply."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    assert destination.is_symlink()

    target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(target,))
    plan = build_plan(deselected, discover_catalog(deselected))

    assert plan.actions[0].disposition is Disposition.REMOVE
    apply_plan(deselected, discover_catalog(deselected), plan)
    assert not destination.is_symlink()


def test_deselection_uses_recorded_mode_after_link_mode_changes(tmp_path: Path) -> None:
    """Cleanup remains possible without restoring the former global link mode."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"

    empty_target = replace(config.targets[0], components=frozenset())
    cleanup = replace(config, link_mode=LinkMode.COPY, components=frozenset(), targets=(empty_target,))
    plan = build_plan(cleanup, discover_catalog(cleanup))

    assert plan.actions[0].disposition is Disposition.REMOVE
    apply_plan(cleanup, discover_catalog(cleanup), plan)
    assert not destination.exists()


def test_deselection_removes_managed_dangling_symlink(tmp_path: Path) -> None:
    """A deleted canonical Skill does not strand its recorded discovery link."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, mode=LinkMode.SYMLINK)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    shutil.rmtree(catalog / "skills/hello")

    empty_inventory = discover_catalog(config)
    plan = build_plan(config, empty_inventory)

    assert plan.actions[0].disposition is Disposition.REMOVE
    apply_plan(config, empty_inventory, plan)
    assert not destination.is_symlink()


def test_apply_backs_up_deselected_managed_copy(tmp_path: Path) -> None:
    """Deselecting an unchanged managed copy retains it as a backup."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"

    target = replace(config.targets[0], components=frozenset({Component.PLUGINS}))
    deselected = replace(config, components=frozenset({Component.PLUGINS}), targets=(target,))
    plan = build_plan(deselected, discover_catalog(deselected))
    result = apply_plan(deselected, discover_catalog(deselected), plan)

    assert not destination.exists()
    assert len(result.backups) == 1
    assert (result.backups[0] / "SKILL.md").is_file()


def test_deselection_conflicts_with_modified_managed_copy(tmp_path: Path) -> None:
    """Deselecting never deletes a managed copy changed by the user."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS, mode=LinkMode.COPY)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = tmp_path / "home/.agents/skills/hello"
    (destination / "SKILL.md").write_text("user change\n", encoding="utf-8")

    target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(target,))
    plan = build_plan(deselected, discover_catalog(deselected))

    assert plan.has_conflicts
    assert destination.is_dir()


def test_apply_writes_visible_root_provenance_marker(tmp_path: Path) -> None:
    """Every apply leaves a self-explanatory marker at the skill root."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    apply_plan(config, inventory, build_plan(config, inventory))

    payload = read_skill_root_marker(config.targets[0])
    assert payload is not None
    assert payload["target"] == "target"
    assert payload["mode"] == "symlink"
    assert payload["skill_count"] == 1


def test_deselection_apply_removes_root_provenance_marker(tmp_path: Path) -> None:
    """A root that stops being bridge-managed stops claiming to be."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    assert read_skill_root_marker(config.targets[0]) is not None

    target = replace(config.targets[0], components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(target,))
    apply_plan(deselected, discover_catalog(deselected), build_plan(deselected, discover_catalog(deselected)))

    assert read_skill_root_marker(target) is None


def test_apply_survives_a_squatted_marker_path_with_a_warning(tmp_path: Path) -> None:
    """A symlink squatting on the marker path degrades to a warning, not a failed apply."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    root = tmp_path / "home/.agents/skills"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (root / "AGENTBRIDGE-MANAGED.json").symlink_to(outside)
    inventory = discover_catalog(config)

    result = apply_plan(config, inventory, build_plan(config, inventory))

    assert (root / "hello").is_symlink()
    assert read_skill_state(config, config.targets[0])
    assert len(result.warnings) == 1
    assert "provenance marker was not updated" in result.warnings[0]
