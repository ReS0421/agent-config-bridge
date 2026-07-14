"""Tests for conflict-aware plan application."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.applier import ApplyError, apply_plan
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.filesystem import MANAGED_MARKER
from agent_config_bridge.models import Component, LinkMode, Platform
from agent_config_bridge.planner import Disposition, build_plan
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
