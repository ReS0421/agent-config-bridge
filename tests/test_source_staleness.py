"""Regression tests for reviewed source snapshots at apply boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import agent_config_bridge.applier as applier_module
from agent_config_bridge.applier import ApplyError, apply_plan
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component
from agent_config_bridge.planner import build_plan
from agent_config_bridge.schedule_store import ScheduleStoreError, schedule_publish_path
from agent_config_bridge.schedules import discover_schedules
from agent_config_bridge.settings import discover_settings_fragments, setting_value_digest
from tests.conftest import make_catalog, make_config


def test_apply_rejects_unreviewed_settings_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mutation uses the same settings leaf set whose digest appeared in plan."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), settings=("reviewed",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SETTINGS}))
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    fragments = discover_settings_fragments(catalog)
    original_leaf = fragments[0].leaves[0]
    changed_leaf = replace(
        original_leaf,
        value="unreviewed",
        digest=setting_value_digest("unreviewed"),
    )
    changed_fragment = replace(fragments[0], leaves=(changed_leaf,))
    monkeypatch.setattr(
        applier_module,
        "discover_settings_fragments",
        lambda _root: (changed_fragment,),
    )

    with pytest.raises(ApplyError, match="settings sources changed after planning"):
        apply_plan(config, inventory, plan)

    assert not (config.targets[0].config_home / "config.toml").exists()


def test_apply_rejects_unreviewed_schedule_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A changed prompt cannot be published after an earlier plan was reviewed."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("reviewed",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES}))
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    schedules = discover_schedules(config)
    changed_definition = replace(schedules.schedules[0], prompt="Unreviewed prompt.\n")
    changed_catalog = replace(schedules, schedules=(changed_definition,))
    monkeypatch.setattr(applier_module, "discover_schedules", lambda _config: changed_catalog)

    with pytest.raises(ScheduleStoreError, match="schedule sources changed after planning"):
        apply_plan(config, inventory, plan)

    assert not schedule_publish_path(config, config.targets[0]).exists()
