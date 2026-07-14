"""Integration tests for catalog-driven product settings reconciliation."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.applier import ApplyError, apply_plan
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component, Product
from agent_config_bridge.planner import Disposition, Operation, build_plan
from agent_config_bridge.state import read_settings_state, settings_state_path
from tests.conftest import make_catalog, make_config


def _deselect_settings(config):  # type: ignore[no-untyped-def]
    target = replace(config.targets[0], components=frozenset())
    return replace(config, components=frozenset(), targets=(target,))


def test_codex_settings_preserve_unmanaged_content_and_reconcile_owned_leaves(tmp_path: Path) -> None:
    """Codex TOML comments and unrelated leaves survive apply, update, and removal."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), settings=())
    fragment = catalog / "settings" / "team-defaults" / "codex" / "config.toml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        'model = "gpt-initial"\n\n[features]\nweb_search_request = true\n',
        encoding="utf-8",
    )
    config = make_config(tmp_path, catalog, components=frozenset({Component.SETTINGS}))
    target = config.targets[0]
    destination = target.config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        '# Keep this user comment.\napproval_policy = "on-request"\n\n[features]\nshell_snapshot = false\n',
        encoding="utf-8",
    )

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)

    assert len(inventory.settings) == 1
    action = next(action for action in plan.actions if action.component is Component.SETTINGS)
    assert action.operation is Operation.PATCH
    assert action.disposition is Disposition.UPDATE
    assert not plan.has_conflicts

    result = apply_plan(config, inventory, plan)
    document = tomllib.loads(destination.read_text(encoding="utf-8"))
    state = read_settings_state(config, target)

    assert action in result.applied
    assert destination.read_text(encoding="utf-8").startswith("# Keep this user comment.\n")
    assert document == {
        "approval_policy": "on-request",
        "model": "gpt-initial",
        "features": {"shell_snapshot": False, "web_search_request": True},
    }
    assert state.file_created is False
    assert {entry.path for entry in state.entries} == {("model",), ("features", "web_search_request")}

    unchanged_inventory = discover_catalog(config)
    unchanged_plan = build_plan(config, unchanged_inventory)
    unchanged_action = next(action for action in unchanged_plan.actions if action.component is Component.SETTINGS)

    assert unchanged_action.disposition is Disposition.NOOP
    assert not unchanged_plan.has_changes
    assert apply_plan(config, unchanged_inventory, unchanged_plan).applied == ()

    fragment.write_text(
        'model = "gpt-updated"\n\n[features]\nweb_search_request = false\n',
        encoding="utf-8",
    )
    updated_inventory = discover_catalog(config)
    updated_plan = build_plan(config, updated_inventory)
    updated_action = next(action for action in updated_plan.actions if action.component is Component.SETTINGS)

    assert updated_action.disposition is Disposition.UPDATE
    apply_plan(config, updated_inventory, updated_plan)
    updated_document = tomllib.loads(destination.read_text(encoding="utf-8"))
    assert updated_document["model"] == "gpt-updated"
    assert updated_document["features"] == {"shell_snapshot": False, "web_search_request": False}

    deselected = _deselect_settings(config)
    deselected_inventory = discover_catalog(deselected)
    removal_plan = build_plan(deselected, deselected_inventory)
    removal_action = next(action for action in removal_plan.actions if action.component is Component.SETTINGS)

    assert removal_action.operation is Operation.PATCH
    assert removal_action.disposition is Disposition.REMOVE
    apply_plan(deselected, deselected_inventory, removal_plan)

    cleaned_text = destination.read_text(encoding="utf-8")
    assert cleaned_text.startswith("# Keep this user comment.\n")
    assert tomllib.loads(cleaned_text) == {
        "approval_policy": "on-request",
        "features": {"shell_snapshot": False},
    }
    assert not settings_state_path(deselected, deselected.targets[0]).exists()
    assert not build_plan(deselected, discover_catalog(deselected)).has_changes


def test_claude_settings_preserve_sibling_values_through_apply_and_deselection(tmp_path: Path) -> None:
    """Claude JSON reconciliation owns only fragment leaves, not their sibling object."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), settings=())
    fragment = catalog / "settings" / "team-defaults" / "claude-code" / "settings.json"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        json.dumps(
            {
                "effortLevel": "high",
                "permissions": {"deny": ["Bash(rm *)"]},
                "statusLine": {"command": "agentbridge-status", "type": "command"},
            }
        ),
        encoding="utf-8",
    )
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        components=frozenset({Component.SETTINGS}),
    )
    target = config.targets[0]
    destination = target.config_home / "settings.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        json.dumps({"theme": "dark", "permissions": {"allow": ["Read"]}}, indent=2) + "\n",
        encoding="utf-8",
    )

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    action = next(action for action in plan.actions if action.component is Component.SETTINGS)

    assert action.disposition is Disposition.UPDATE
    apply_plan(config, inventory, plan)
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "effortLevel": "high",
        "permissions": {"allow": ["Read"], "deny": ["Bash(rm *)"]},
        "statusLine": {"command": "agentbridge-status", "type": "command"},
    }
    assert not build_plan(config, discover_catalog(config)).has_changes

    deselected = _deselect_settings(config)
    deselected_inventory = discover_catalog(deselected)
    removal_plan = build_plan(deselected, deselected_inventory)

    assert any(
        action.component is Component.SETTINGS and action.disposition is Disposition.REMOVE
        for action in removal_plan.actions
    )
    apply_plan(deselected, deselected_inventory, removal_plan)
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "permissions": {"allow": ["Read"]},
    }


def test_unmanaged_equal_setting_is_a_conflict_and_apply_refuses_it(tmp_path: Path) -> None:
    """An equal value cannot be silently adopted without bridge ownership state."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), settings=("gpt-managed",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SETTINGS}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text('model = "gpt-managed"\n', encoding="utf-8")

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    action = next(action for action in plan.actions if action.component is Component.SETTINGS)

    assert action.disposition is Disposition.CONFLICT
    assert plan.has_conflicts
    with pytest.raises(ApplyError, match="refusing to apply a plan with conflicts"):
        apply_plan(config, inventory, plan)
    assert destination.read_text(encoding="utf-8") == 'model = "gpt-managed"\n'
    assert not settings_state_path(config, config.targets[0]).exists()


def test_apply_clears_ownership_when_deselected_leaf_is_already_absent(tmp_path: Path) -> None:
    """A destination no-op still reconciles stale digest-only ownership state."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), settings=("gpt-managed",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SETTINGS}))
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    destination = config.targets[0].config_home / "config.toml"
    destination.write_text("# The user already removed the managed leaf.\n", encoding="utf-8")

    deselected = _deselect_settings(config)
    deselected_inventory = discover_catalog(deselected)
    plan = build_plan(deselected, deselected_inventory)
    action = next(action for action in plan.actions if action.component is Component.SETTINGS)

    assert action.disposition is Disposition.NOOP
    assert not plan.has_changes
    apply_plan(deselected, deselected_inventory, plan)
    assert not settings_state_path(deselected, deselected.targets[0]).exists()
    assert destination.read_text(encoding="utf-8") == "# The user already removed the managed leaf.\n"
