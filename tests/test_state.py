"""Tests for non-secret bridge ownership records."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.state import (
    BridgeStateError,
    find_orphaned_target_states,
    read_registered_plugins,
    read_skill_state,
    write_registered_plugins,
    write_skill_state,
)
from tests.conftest import make_catalog, make_config, symlink_directory_or_skip


def test_registration_state_round_trip(tmp_path: Path) -> None:
    """Plugin ownership state is deterministic and target scoped."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]

    write_registered_plugins(config, target, ("shared-plugin", "agent-config-bridge-hooks"))

    assert read_registered_plugins(config, target) == ("shared-plugin", "agent-config-bridge-hooks")
    payload = json.loads((config.state_dir / "targets/target/plugins.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["identity"]["product"] == "codex"
    assert Path(payload["marketplace_source"]) == config.state_dir / "marketplace"


def test_skill_state_round_trip_uses_stable_source_ids(tmp_path: Path) -> None:
    """Managed Skill identity is catalog-relative rather than host-absolute."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]

    write_skill_state(config, target, discover_catalog(config))

    entries = read_skill_state(config, target)
    assert entries[0].source_id == "skills/hello"
    assert entries[0].link_target == str(catalog.resolve() / "skills/hello")


def test_state_rejects_symlinked_target_directory_escape(tmp_path: Path) -> None:
    """A pre-created state symlink cannot redirect ownership writes."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    outside = tmp_path / "outside"
    outside.mkdir()
    target_dir = config.state_dir / "targets" / config.targets[0].name
    target_dir.parent.mkdir(parents=True)
    symlink_directory_or_skip(target_dir, outside)

    with pytest.raises(BridgeStateError, match="escapes"):
        write_registered_plugins(config, config.targets[0], ("shared-plugin",))

    assert not (outside / "plugins.json").exists()


def test_plugin_state_rejects_reused_name_with_different_home(tmp_path: Path) -> None:
    """A target name cannot transfer Plugin ownership to another product home."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    write_registered_plugins(config, target, ("shared-plugin",))
    changed_target = replace(target, config_home=tmp_path / "different-codex-home")
    changed_config = replace(config, targets=(changed_target,))

    with pytest.raises(BridgeStateError, match="identity does not match"):
        read_registered_plugins(changed_config, changed_target)


def test_skill_state_rejects_reused_name_with_different_root(tmp_path: Path) -> None:
    """A target name cannot transfer Skill ownership to another destination root."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    write_skill_state(config, target, discover_catalog(config))
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    changed_target = replace(target, user_home=other_home)
    changed_config = replace(config, targets=(changed_target,))

    with pytest.raises(BridgeStateError, match="identity does not match"):
        read_skill_state(changed_config, changed_target)


def test_disabled_target_state_is_reported_as_orphaned(tmp_path: Path) -> None:
    """Disabling a target cannot silently hide ownership that still needs cleanup."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    write_skill_state(config, config.targets[0], discover_catalog(config))
    disabled = replace(config.targets[0], enabled=False)

    assert find_orphaned_target_states(replace(config, targets=(disabled,))) == ("target",)


def test_empty_ownership_is_removed_before_target_identity_changes(tmp_path: Path) -> None:
    """A fully reconciled target leaves no stale identity record behind."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    target = config.targets[0]
    write_skill_state(config, target, discover_catalog(config))
    write_registered_plugins(config, target, ("shared-plugin",))

    empty_target = replace(target, components=frozenset())
    empty_config = replace(config, targets=(empty_target,))
    write_skill_state(empty_config, empty_target, discover_catalog(empty_config))
    write_registered_plugins(empty_config, empty_target, ())

    assert not (config.state_dir / "targets/target").exists()
    assert find_orphaned_target_states(replace(config, targets=(replace(target, enabled=False),))) == ()
