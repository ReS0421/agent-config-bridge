"""Tests for visible root-level provenance markers."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component, LinkMode, Platform, Product
from agent_config_bridge.provenance import (
    ROOT_MARKER_FILENAME,
    read_skill_root_marker,
    remove_skill_root_marker,
    write_skill_root_marker,
)
from agent_config_bridge.state import BridgeStateError, skill_root_for_target
from tests.conftest import make_catalog, make_config


def test_marker_round_trip_describes_the_projection(tmp_path: Path) -> None:
    """A written marker names the target, catalog, state_dir, and mode."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)
    skill_root_for_target(target).mkdir(parents=True)

    marker = write_skill_root_marker(config, target, inventory)

    assert marker == skill_root_for_target(target) / ROOT_MARKER_FILENAME
    payload = read_skill_root_marker(target)
    assert payload is not None
    assert payload["managed_by"] == "agent-config-bridge"
    assert payload["role"] == "deployment-projection"
    assert payload["target"] == target.name
    assert payload["product"] == "codex"
    assert payload["mode"] == "symlink"
    assert payload["skill_count"] == 1
    assert Path(payload["catalog_root"]) == catalog.resolve()
    assert Path(payload["state_dir"]) == config.state_dir.resolve()
    assert any("not accidental duplicates" in line for line in payload["notice"])


def test_marker_reports_copy_mode_for_windows_auto(tmp_path: Path) -> None:
    """AUTO link mode resolves to the projection mode observers actually see."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS, mode=LinkMode.AUTO)
    target = config.targets[0]
    inventory = discover_catalog(config)
    skill_root_for_target(target).mkdir(parents=True)

    write_skill_root_marker(config, target, inventory)

    payload = read_skill_root_marker(target)
    assert payload is not None
    assert payload["mode"] == "copy"


def test_marker_is_skipped_when_root_does_not_exist(tmp_path: Path) -> None:
    """The marker never creates a skill root by itself."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)

    assert write_skill_root_marker(config, target, inventory) is None
    assert not skill_root_for_target(target).exists()
    assert read_skill_root_marker(target) is None


def test_marker_is_removed_when_skills_are_deselected(tmp_path: Path) -> None:
    """Deselecting the skills component clears the stale marker."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)
    skill_root_for_target(target).mkdir(parents=True)
    write_skill_root_marker(config, target, inventory)

    deselected = replace(target, components=frozenset())
    assert write_skill_root_marker(config, deselected, inventory) is None
    assert read_skill_root_marker(target) is None


def test_marker_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    """A pre-planted symlink at the marker path cannot redirect the write."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)
    root = skill_root_for_target(target)
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    try:
        (root / ROOT_MARKER_FILENAME).symlink_to(outside)
    except OSError as exc:  # pragma: no cover - Windows symlink policy
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")

    with pytest.raises(BridgeStateError):
        write_skill_root_marker(config, target, inventory)
    with pytest.raises(BridgeStateError):
        read_skill_root_marker(target)


def test_read_rejects_a_foreign_or_malformed_marker(tmp_path: Path) -> None:
    """A marker not written by the bridge is reported, not silently trusted."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    root = skill_root_for_target(target)
    root.mkdir(parents=True)
    marker = root / ROOT_MARKER_FILENAME

    marker.write_text("not json\n", encoding="utf-8")
    with pytest.raises(BridgeStateError):
        read_skill_root_marker(target)

    marker.write_text(json.dumps({"schema_version": 1, "managed_by": "someone-else"}), encoding="utf-8")
    with pytest.raises(BridgeStateError):
        read_skill_root_marker(target)


def test_remove_is_idempotent(tmp_path: Path) -> None:
    """Removing an absent marker is a no-op."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]

    remove_skill_root_marker(target)
    skill_root_for_target(target).mkdir(parents=True)
    remove_skill_root_marker(target)


def test_claude_code_target_uses_config_home_skill_root(tmp_path: Path) -> None:
    """Claude Code roots live under config_home/skills, Codex under ~/.agents/skills."""

    catalog = make_catalog(tmp_path / "catalog")
    codex = make_config(tmp_path, catalog).targets[0]
    claude = make_config(tmp_path, catalog, product=Product.CLAUDE_CODE).targets[0]

    assert skill_root_for_target(codex) == codex.user_home / ".agents" / "skills"
    assert skill_root_for_target(claude) == claude.config_home / "skills"
    assert Component.SKILLS in codex.components


def test_deselected_target_leaves_another_targets_marker_alone(tmp_path: Path) -> None:
    """Two targets sharing one skill root: the non-skill target must not clobber."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    owner = config.targets[0]
    inventory = discover_catalog(config)
    skill_root_for_target(owner).mkdir(parents=True)
    write_skill_root_marker(config, owner, inventory)

    sibling = replace(owner, name="sibling", components=frozenset({Component.SETTINGS}))
    assert write_skill_root_marker(config, sibling, inventory) is None

    payload = read_skill_root_marker(owner)
    assert payload is not None
    assert payload["target"] == owner.name


def test_deselection_still_removes_this_targets_own_marker(tmp_path: Path) -> None:
    """A target that stops managing skills removes only the marker it owns."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)
    skill_root_for_target(target).mkdir(parents=True)
    write_skill_root_marker(config, target, inventory)

    deselected = replace(target, components=frozenset())
    write_skill_root_marker(config, deselected, inventory)

    assert read_skill_root_marker(target) is None
