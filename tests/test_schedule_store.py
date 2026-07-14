"""Tests for immutable per-target schedule snapshot storage."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_config_bridge.models import Component
from agent_config_bridge.schedule_store import (
    ScheduleStoreError,
    read_schedule_set,
    remove_schedule_set,
    render_schedule_set,
    schedule_publish_path,
    schedule_set_digest,
    schedule_set_is_current,
)
from agent_config_bridge.schedules import discover_schedules
from tests.conftest import make_catalog, make_config


def _schedule_context(tmp_path: Path):  # type: ignore[no-untyped-def]
    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("daily-review",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES}))
    target = config.targets[0]
    return config, target, discover_schedules(config)


def test_render_schedule_set_publishes_integrity_checked_snapshot(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)

    rendered = render_schedule_set(config, schedules, target)
    loaded = read_schedule_set(config, target)

    assert loaded == rendered
    assert loaded is not None
    assert loaded.schedules[0].schedule_name == "daily-review"
    assert loaded.schedules[0].working_directory == target.user_home.resolve()
    assert schedule_set_is_current(config, schedules, target)
    if os.name == "posix":
        assert stat.S_IMODE((config.state_dir / "schedule-builds").stat().st_mode) == 0o700
        assert stat.S_IMODE(rendered.build_file.parents[1].stat().st_mode) == 0o700
        assert stat.S_IMODE(rendered.build_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(rendered.build_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(rendered.published_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(rendered.published_file.stat().st_mode) == 0o600


def test_render_schedule_set_reuses_immutable_build(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)

    first = render_schedule_set(config, schedules, target)
    second = render_schedule_set(config, schedules, target)

    assert second == first
    assert tuple((config.state_dir / "schedule-builds").iterdir()) == (first.build_file.parents[1],)


def test_schedule_set_changes_after_canonical_prompt_change(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)
    first = render_schedule_set(config, schedules, target)
    (config.catalog / "schedules/daily-review/PROMPT.md").write_text("Use the updated prompt.\n", encoding="utf-8")
    updated_catalog = discover_schedules(config)

    assert not schedule_set_is_current(config, updated_catalog, target)
    second = render_schedule_set(config, updated_catalog, target)

    assert second.digest != first.digest
    assert second.schedules[0].prompt == "Use the updated prompt.\n"


def test_schedule_set_is_stable_across_source_line_endings(tmp_path: Path) -> None:
    """A Windows checkout does not create a different immutable build."""

    config, target, schedules = _schedule_context(tmp_path)
    first = render_schedule_set(config, schedules, target)
    prompt = config.catalog / "schedules/daily-review/PROMPT.md"
    prompt.write_bytes(b"Run the daily-review workflow.\r\n")

    rediscovered = discover_schedules(config)

    assert rediscovered.schedules[0].prompt == "Run the daily-review workflow.\n"
    assert schedule_set_digest(rediscovered, target) == first.digest
    assert schedule_set_is_current(config, rediscovered, target)
    assert render_schedule_set(config, rediscovered, target) == first


def test_read_schedule_set_rejects_modified_snapshot(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)
    rendered = render_schedule_set(config, schedules, target)
    rendered.build_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ScheduleStoreError, match="invalid immutable"):
        read_schedule_set(config, target)


def test_read_schedule_set_rejects_redirected_pointer(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)
    rendered = render_schedule_set(config, schedules, target)
    published = rendered.published_file
    published.unlink()
    published.symlink_to(rendered.build_file)

    with pytest.raises(ScheduleStoreError, match="real regular file"):
        read_schedule_set(config, target)


def test_read_schedule_set_rejects_unexpected_build_path(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)
    rendered = render_schedule_set(config, schedules, target)
    payload = json.loads(rendered.published_file.read_text(encoding="utf-8"))
    payload["build_file"] = str(tmp_path / "outside.json")
    rendered.published_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ScheduleStoreError, match="unexpected build path"):
        read_schedule_set(config, target)


def test_remove_schedule_set_removes_only_stable_pointer(tmp_path: Path) -> None:
    config, target, schedules = _schedule_context(tmp_path)
    rendered = render_schedule_set(config, schedules, target)

    remove_schedule_set(config, target)

    assert not schedule_publish_path(config, target).exists()
    assert rendered.build_file.is_file()
    remove_schedule_set(config, target)
