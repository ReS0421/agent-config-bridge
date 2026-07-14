"""Tests for read-only synchronization planning."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.filesystem import apply_copy, tree_digest
from agent_config_bridge.models import Component, LinkMode, Platform, Product
from agent_config_bridge.planner import Disposition, Operation, build_plan
from agent_config_bridge.state import BridgeStateError, write_registered_plugins, write_skill_state
from tests.conftest import make_catalog, make_config, symlink_directory_or_skip


def test_plan_creates_canonical_codex_skill_link(tmp_path: Path) -> None:
    """Codex Skills target the current .agents discovery root."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)

    plan = build_plan(config, discover_catalog(config))

    action = plan.actions[0]
    assert action.operation is Operation.LINK
    assert action.disposition is Disposition.CREATE
    assert action.destination == tmp_path / "home/.agents/skills/hello"


def test_plan_creates_claude_skill_under_claude_home(tmp_path: Path) -> None:
    """Claude Code keeps standalone Skills below .claude/skills."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, product=Product.CLAUDE_CODE)

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].destination == tmp_path / "home/.claude/skills/hello"


def test_plan_honors_custom_claude_config_home_for_skills(tmp_path: Path) -> None:
    """CLAUDE_CONFIG_DIR relocates standalone Skills with other user state."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, product=Product.CLAUDE_CODE)
    target = replace(config.targets[0], config_home=tmp_path / "custom-claude")
    config = replace(config, targets=(target,))

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].destination == tmp_path / "custom-claude/skills/hello"


def test_plan_treats_unmanaged_destination_as_conflict(tmp_path: Path) -> None:
    """Existing user content is never adopted or overwritten implicitly."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    destination = tmp_path / "home/.agents/skills/hello"
    destination.mkdir(parents=True)

    plan = build_plan(config, discover_catalog(config))

    assert plan.has_conflicts
    assert plan.actions[0].disposition is Disposition.CONFLICT


def test_plan_does_not_adopt_manual_canonical_skill_symlink(tmp_path: Path) -> None:
    """A canonical-pointing link is not ownership proof without target state."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    source = catalog / "skills/hello"
    destination = tmp_path / "home/.agents/skills/hello"
    destination.parent.mkdir(parents=True)
    symlink_directory_or_skip(destination, source)

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].disposition is Disposition.CONFLICT
    assert "no matching target ownership state" in plan.actions[0].detail


def test_plan_does_not_adopt_marker_copy_without_target_state(tmp_path: Path) -> None:
    """A valid copy marker alone cannot transfer ownership to a target."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS, mode=LinkMode.COPY)
    source = catalog / "skills/hello"
    destination = tmp_path / "home/.agents/skills/hello"
    source_digest = tree_digest(source)
    apply_copy(
        source,
        destination,
        source_id="skills/hello",
        source_digest=source_digest,
        state_dir=config.state_dir,
        target_name=config.targets[0].name,
        update=False,
    )

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].disposition is Disposition.CONFLICT
    assert "no matching target ownership state" in plan.actions[0].detail


def test_auto_mode_uses_copy_for_windows_target(tmp_path: Path) -> None:
    """Windows defaults to managed copies because symlink privileges vary."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        mode=LinkMode.AUTO,
    )

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].operation is Operation.COPY


def test_plan_includes_render_and_registration_hints(tmp_path: Path) -> None:
    """Plugins and Hooks render first and remain explicitly registered."""

    catalog = make_catalog(
        tmp_path / "catalog",
        plugins=("shared-plugin",),
        hooks=("audit-event",),
    )
    components = frozenset({Component.PLUGINS, Component.HOOKS})
    config = make_config(tmp_path, catalog, components=components)

    plan = build_plan(config, discover_catalog(config))

    assert plan.actions[0].operation is Operation.RENDER
    assert plan.actions[0].destination == config.state_dir / "marketplace"
    assert str(config.state_dir / "marketplace") in plan.commands[0].argv
    assert [hint.argv[0:3] for hint in plan.commands[:1]] == [("codex", "plugin", "marketplace")]
    assert any("shared-plugin@agent-config-bridge" in hint.argv for hint in plan.commands)
    assert any("agent-config-bridge-hooks@agent-config-bridge" in hint.argv for hint in plan.commands)


def test_plan_removes_plugins_deselected_after_bridge_registration(tmp_path: Path) -> None:
    """Only plugins recorded by an earlier register run are reconciled away."""

    catalog = make_catalog(
        tmp_path / "catalog",
        plugins=("shared-plugin",),
        hooks=("audit-event",),
    )
    components = frozenset({Component.PLUGINS, Component.HOOKS})
    config = make_config(tmp_path, catalog, components=components)
    inventory = discover_catalog(config)
    write_registered_plugins(
        config,
        config.targets[0],
        ("shared-plugin", "agent-config-bridge-hooks"),
    )
    target = replace(config.targets[0], components=frozenset({Component.HOOKS}))
    config = replace(config, targets=(target,))

    plan = build_plan(config, inventory)

    assert any(
        hint.argv == ("codex", "plugin", "remove", "shared-plugin@agent-config-bridge") for hint in plan.commands
    )
    assert not any(
        hint.argv == ("codex", "plugin", "remove", "agent-config-bridge-hooks@agent-config-bridge")
        for hint in plan.commands
    )


def test_plan_reinstalls_plugins_when_marketplace_source_moves(tmp_path: Path) -> None:
    """A relocated stable marketplace is removed before the new source is added."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    components = frozenset({Component.PLUGINS})
    config = make_config(tmp_path, catalog, components=components)
    write_registered_plugins(config, config.targets[0], ("shared-plugin",))
    moved_state = tmp_path / "moved-state"
    shutil.copytree(config.state_dir, moved_state)
    moved_config = replace(config, state_dir=moved_state)

    plan = build_plan(moved_config, discover_catalog(moved_config))
    argv = [hint.argv for hint in plan.commands]

    assert argv[:3] == [
        ("codex", "plugin", "remove", "shared-plugin@agent-config-bridge"),
        ("codex", "plugin", "marketplace", "remove", "agent-config-bridge"),
        ("codex", "plugin", "marketplace", "add", str(moved_state / "marketplace")),
    ]
    assert argv[3] == ("codex", "plugin", "add", "shared-plugin@agent-config-bridge")


def test_plan_reinstalls_claude_plugins_when_marketplace_source_moves(tmp_path: Path) -> None:
    """Claude relocation uninstalls before replacing the named marketplace."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    components = frozenset({Component.PLUGINS})
    config = make_config(tmp_path, catalog, product=Product.CLAUDE_CODE, components=components)
    write_registered_plugins(config, config.targets[0], ("shared-plugin",))
    moved_state = tmp_path / "moved-state"
    shutil.copytree(config.state_dir, moved_state)
    moved_config = replace(config, state_dir=moved_state)

    plan = build_plan(moved_config, discover_catalog(moved_config))
    argv = [hint.argv for hint in plan.commands]

    assert argv[:3] == [
        (
            "claude",
            "plugin",
            "uninstall",
            "shared-plugin@agent-config-bridge",
            "--scope",
            "user",
            "--keep-data",
        ),
        ("claude", "plugin", "marketplace", "remove", "agent-config-bridge"),
        ("claude", "plugin", "marketplace", "add", str(moved_state / "marketplace")),
    ]
    assert ("claude", "plugin", "install", "shared-plugin@agent-config-bridge", "--scope", "user") in argv


def test_plan_warns_about_disabled_target_ownership_state(tmp_path: Path) -> None:
    """Target removal remains visible until its recorded components are reconciled."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    write_skill_state(config, config.targets[0], inventory)
    disabled = replace(config.targets[0], enabled=False)

    plan = build_plan(replace(config, targets=(disabled,)), inventory)

    assert any("ownership state has no enabled target" in warning for warning in plan.warnings)


def test_plan_reserves_skill_root_through_physical_directory_alias(tmp_path: Path) -> None:
    """An existing owner's physical root alias cannot be claimed during cleanup."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, target_name="old")
    inventory = discover_catalog(config)
    old_target = config.targets[0]
    write_skill_state(config, old_target, inventory)
    alias_home = tmp_path / "home-alias"
    symlink_directory_or_skip(alias_home, old_target.user_home)
    cleanup_target = replace(old_target, components=frozenset())
    new_target = replace(old_target, name="new", user_home=alias_home)
    handoff = replace(config, targets=(cleanup_target, new_target))

    with pytest.raises(BridgeStateError, match="remains reserved by target 'old'"):
        build_plan(handoff, inventory)


def test_plan_reserves_windows_skill_root_case_insensitively(tmp_path: Path) -> None:
    """Windows path spelling cannot bypass staged ownership reconciliation."""

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
    write_skill_state(config, old_target, inventory)
    cleanup_target = replace(old_target, components=frozenset())
    case_variant_home = Path(str(old_target.user_home).upper())
    new_target = replace(old_target, name="new", user_home=case_variant_home)
    handoff = replace(config, targets=(cleanup_target, new_target))

    with pytest.raises(BridgeStateError, match="remains reserved by target 'old'"):
        build_plan(handoff, inventory)


def test_plan_reserves_skill_root_against_nested_discovery_root(tmp_path: Path) -> None:
    """A replacement discovery root cannot be nested below retained ownership."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, target_name="old")
    inventory = discover_catalog(config)
    old_target = config.targets[0]
    write_skill_state(config, old_target, inventory)
    cleanup_target = replace(old_target, components=frozenset())
    old_root = old_target.user_home / ".agents/skills"
    nested_target = replace(
        old_target,
        name="nested",
        product=Product.CLAUDE_CODE,
        config_home=old_root / "claude-runtime",
        components=frozenset({Component.PLUGINS}),
    )
    handoff = replace(config, targets=(cleanup_target, nested_target))

    with pytest.raises(BridgeStateError, match="remains reserved by target 'old'"):
        build_plan(handoff, inventory)


def test_plan_reserves_skill_root_against_other_config_home(tmp_path: Path) -> None:
    """A new vendor runtime home cannot occupy a retained Skill destination."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, target_name="old")
    inventory = discover_catalog(config)
    old_target = config.targets[0]
    write_skill_state(config, old_target, inventory)
    cleanup_target = replace(old_target, components=frozenset())
    second_home = tmp_path / "second-home"
    second_home.mkdir()
    runtime_target = replace(
        old_target,
        name="runtime",
        user_home=second_home,
        config_home=old_target.user_home / ".agents/skills/vendor-runtime",
        components=frozenset({Component.PLUGINS}),
    )
    handoff = replace(config, targets=(cleanup_target, runtime_target))

    with pytest.raises(BridgeStateError, match="remains reserved by target 'old'.*overlaps"):
        build_plan(handoff, inventory)


def test_plan_omits_registration_commands_for_another_host_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux process never prints Linux paths as copyable Windows commands."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        components=frozenset({Component.PLUGINS}),
    )
    monkeypatch.setattr("agent_config_bridge.planner.current_platform", lambda: Platform.LINUX)

    plan = build_plan(config, discover_catalog(config))

    assert plan.commands == ()
    assert any("registration commands are omitted" in warning for warning in plan.warnings)


def test_plan_warns_when_cross_host_cleanup_commands_are_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A components-empty cleanup still tells the user to register on its target OS."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        components=frozenset(),
    )
    write_registered_plugins(config, config.targets[0], ("shared-plugin",))
    monkeypatch.setattr("agent_config_bridge.planner.current_platform", lambda: Platform.LINUX)

    plan = build_plan(config, discover_catalog(config))

    assert plan.commands == ()
    assert any("registration commands are omitted" in warning for warning in plan.warnings)
