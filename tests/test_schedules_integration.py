"""Integration tests for catalog-driven host schedule snapshot reconciliation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_config_bridge.applier import apply_plan
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component
from agent_config_bridge.planner import Disposition, Operation, build_plan
from agent_config_bridge.schedule_store import read_schedule_set, schedule_publish_path
from tests.conftest import make_catalog, make_config


def test_schedule_snapshot_render_is_idempotent_and_deselection_removes_pointer(tmp_path: Path) -> None:
    """The full planner/applier flow publishes immutable builds and removes only their pointer."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("weekday-review",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES}))
    target = config.targets[0]

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    action = next(action for action in plan.actions if action.component is Component.SCHEDULES)

    assert len(inventory.schedules) == 1
    assert action.operation is Operation.RENDER
    assert action.disposition is Disposition.CREATE
    assert not plan.has_conflicts

    result = apply_plan(config, inventory, plan)
    rendered = result.schedules[0]
    loaded = read_schedule_set(config, target)

    assert action in result.applied
    assert loaded == rendered
    assert rendered.published_file == schedule_publish_path(config, target)
    assert rendered.build_file.is_file()
    assert rendered.schedules[0].schedule_name == "weekday-review"
    assert rendered.schedules[0].working_directory == target.user_home.resolve()
    assert rendered.schedules[0].prompt == "Run the weekday-review workflow.\n"

    unchanged_inventory = discover_catalog(config)
    unchanged_plan = build_plan(config, unchanged_inventory)
    unchanged_action = next(action for action in unchanged_plan.actions if action.component is Component.SCHEDULES)

    assert unchanged_action.disposition is Disposition.NOOP
    assert not unchanged_plan.has_changes
    unchanged_result = apply_plan(config, unchanged_inventory, unchanged_plan)
    assert unchanged_result.applied == ()
    assert unchanged_result.schedules == (rendered,)

    prompt = catalog / "schedules" / "weekday-review" / "PROMPT.md"
    prompt.write_text("Run the updated weekday review.\n", encoding="utf-8")
    updated_inventory = discover_catalog(config)
    updated_plan = build_plan(config, updated_inventory)
    updated_action = next(action for action in updated_plan.actions if action.component is Component.SCHEDULES)

    assert updated_action.disposition is Disposition.UPDATE
    updated_result = apply_plan(config, updated_inventory, updated_plan)
    updated = updated_result.schedules[0]
    assert updated.digest != rendered.digest
    assert updated.schedules[0].prompt == "Run the updated weekday review.\n"
    assert rendered.build_file.is_file()

    deselected_target = replace(target, components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(deselected_target,))
    deselected_inventory = discover_catalog(deselected)
    removal_plan = build_plan(deselected, deselected_inventory)
    removal_action = next(action for action in removal_plan.actions if action.component is Component.SCHEDULES)

    assert removal_action.operation is Operation.REMOVE
    assert removal_action.disposition is Disposition.REMOVE
    removal_result = apply_plan(deselected, deselected_inventory, removal_plan)

    assert removal_action in removal_result.applied
    assert not schedule_publish_path(deselected, deselected_target).exists()
    assert updated.build_file.is_file()
    final_plan = build_plan(deselected, discover_catalog(deselected))
    assert all(action.component is not Component.SCHEDULES for action in final_plan.actions)
    assert not final_plan.has_changes
