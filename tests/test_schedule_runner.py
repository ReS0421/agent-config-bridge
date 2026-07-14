"""Tests for idempotent schedule heartbeat execution."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_config_bridge.models import Component, Product
from agent_config_bridge.schedule_runner import ScheduleRunnerError, run_due_schedules, run_named_schedule
from agent_config_bridge.schedule_store import render_schedule_set
from agent_config_bridge.schedules import (
    ScheduleExecutionError,
    VendorExecutionResult,
    VendorInvocation,
    discover_schedules,
)
from tests.conftest import make_catalog, make_config


def _published_target(tmp_path: Path, *, product: Product = Product.CODEX):  # type: ignore[no-untyped-def]
    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("daily-review",))
    config = make_config(
        tmp_path,
        catalog,
        product=product,
        components=frozenset({Component.SCHEDULES}),
    )
    target = config.targets[0]
    render_schedule_set(config, discover_schedules(config), target)
    return config, target


def test_tick_runs_due_schedule_once_per_snapshot_minute(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path)
    invocations: list[VendorInvocation] = []

    def execute(invocation: VendorInvocation) -> VendorExecutionResult:
        invocations.append(invocation)
        return VendorExecutionResult(argv=invocation.argv, returncode=0, stdout="ok", stderr="")

    moment = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)  # Monday
    first = run_due_schedules(config, target, moment=moment, environment={"PATH": "/bin"}, execute=execute)
    second = run_due_schedules(config, target, moment=moment, environment={"PATH": "/bin"}, execute=execute)

    assert [run.name for run in first.runs] == ["daily-review"]
    assert second.skipped_reason == "minute already processed"
    assert len(invocations) == 1
    assert invocations[0].stdin == "Run the daily-review workflow.\n"
    assert dict(invocations[0].environment or ())["CODEX_HOME"] == str(target.config_home)
    if os.name == "posix":
        runtime = config.state_dir / "schedule-runtime"
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert stat.S_IMODE((runtime / "target.lock").stat().st_mode) == 0o600
        assert stat.S_IMODE((runtime / "target.last.json").stat().st_mode) == 0o600


def test_tick_records_minute_without_replaying_non_due_schedule(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path)
    moment = datetime(2026, 7, 13, 9, 1, tzinfo=UTC)

    result = run_due_schedules(config, target, moment=moment)

    assert result.runs == ()
    state = json.loads((config.state_dir / "schedule-runtime/target.last.json").read_text(encoding="utf-8"))
    assert state["minute"] == "2026-07-13T09:01:00+00:00"


def test_vendor_execution_does_not_hold_the_next_minute_claim_lock(tmp_path: Path) -> None:
    """A long-running minute cannot suppress a later scheduler heartbeat."""

    config, target = _published_target(tmp_path)
    nested_results = []

    def execute(invocation: VendorInvocation) -> VendorExecutionResult:
        nested_results.append(
            run_due_schedules(
                config,
                target,
                moment=datetime(2026, 7, 13, 9, 1, tzinfo=UTC),
                execute=lambda nested: VendorExecutionResult(
                    argv=nested.argv,
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            )
        )
        return VendorExecutionResult(argv=invocation.argv, returncode=0, stdout="", stderr="")

    run_due_schedules(
        config,
        target,
        moment=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        execute=execute,
    )

    assert nested_results[0].skipped_reason is None
    assert nested_results[0].minute == "2026-07-13T09:01:00+00:00"


def test_next_minute_skips_only_the_same_still_running_schedule(tmp_path: Path) -> None:
    """Per-schedule locks prevent overlap without losing the minute claim."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("every-minute",))
    definition = catalog / "schedules/every-minute/schedule.toml"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace('cron = "0 9 * * 1-5"', 'cron = "* * * * *"'),
        encoding="utf-8",
    )
    config = make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES}))
    target = config.targets[0]
    render_schedule_set(config, discover_schedules(config), target)
    nested_results = []

    def execute(invocation: VendorInvocation) -> VendorExecutionResult:
        nested_results.append(
            run_due_schedules(
                config,
                target,
                moment=datetime(2026, 7, 13, 9, 1, tzinfo=UTC),
                execute=lambda nested: VendorExecutionResult(
                    argv=nested.argv,
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            )
        )
        return VendorExecutionResult(argv=invocation.argv, returncode=0, stdout="", stderr="")

    run_due_schedules(
        config,
        target,
        moment=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        execute=execute,
    )

    assert nested_results[0].skipped_reason is None
    assert nested_results[0].runs[0].skipped_reason == "a previous run is still active"


def test_tick_reports_vendor_failure_without_prompt_disclosure(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path)

    def fail(_invocation: VendorInvocation) -> VendorExecutionResult:
        raise ScheduleExecutionError("vendor exited with code 2")

    result = run_due_schedules(
        config,
        target,
        moment=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        execute=fail,
    )

    assert result.runs[0].succeeded is False
    assert result.runs[0].error == "vendor exited with code 2"
    assert "Run the" not in result.runs[0].error


def test_one_invalid_schedule_does_not_abort_other_due_runs(tmp_path: Path) -> None:
    """Invocation validation failures are isolated to their own schedule."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("first", "second"))
    config = make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES}))
    target = config.targets[0]
    for name in ("first", "second"):
        definition = catalog / "schedules" / name / "schedule.toml"
        definition.write_text(
            definition.read_text(encoding="utf-8").replace(
                'working_directory = "."',
                f'working_directory = "{name}"',
            ),
            encoding="utf-8",
        )
        (target.user_home / name).mkdir()
    render_schedule_set(config, discover_schedules(config), target)
    (target.user_home / "first").rmdir()
    seen: list[str] = []

    def execute(invocation: VendorInvocation) -> VendorExecutionResult:
        seen.append(invocation.schedule_name)
        return VendorExecutionResult(argv=invocation.argv, returncode=0, stdout="", stderr="")

    result = run_due_schedules(
        config,
        target,
        moment=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        execute=execute,
    )

    assert result.runs[0].succeeded is False
    assert result.runs[1].succeeded is True
    assert seen == ["second"]


def test_manual_run_uses_claude_default_profile_and_fixed_argv(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path, product=Product.CLAUDE_CODE)
    seen: list[VendorInvocation] = []

    def execute(invocation: VendorInvocation) -> VendorExecutionResult:
        seen.append(invocation)
        return VendorExecutionResult(argv=invocation.argv, returncode=0, stdout="", stderr="")

    result = run_named_schedule(
        config,
        target,
        "daily-review",
        environment={"CLAUDE_CONFIG_DIR": str(tmp_path / "inherited-wrong-profile")},
        execute=execute,
    )

    assert result.runs[0].succeeded
    assert seen[0].argv == ("claude", "--print", "--no-session-persistence")
    assert "CLAUDE_CONFIG_DIR" not in dict(seen[0].environment or ())


def test_manual_run_rejects_unknown_schedule(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path)

    with pytest.raises(ScheduleRunnerError, match="unknown published schedule"):
        run_named_schedule(config, target, "missing")


def test_tick_rejects_forged_last_tick_state(tmp_path: Path) -> None:
    config, target = _published_target(tmp_path)
    runtime = config.state_dir / "schedule-runtime"
    runtime.mkdir(parents=True)
    (runtime / "target.last.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(ScheduleRunnerError, match="invalid last-tick state"):
        run_due_schedules(config, target, moment=datetime(2026, 7, 13, 9, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: {**state, "target": "other-target"},
        lambda state: {**state, "minute": "not-a-minute"},
        lambda state: {**state, "digest": "0" * 63},
        lambda state: {**state, "schema_version": True},
    ],
)
def test_tick_rejects_malformed_last_tick_identity(tmp_path: Path, mutation) -> None:  # type: ignore[no-untyped-def]
    """Corrupted identity fields cannot suppress or redirect a target minute."""

    config, target = _published_target(tmp_path)
    first_moment = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    run_due_schedules(config, target, moment=first_moment)
    state_path = config.state_dir / "schedule-runtime/target.last.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_path.write_text(json.dumps(mutation(state)), encoding="utf-8")

    with pytest.raises(ScheduleRunnerError, match="invalid last-tick state"):
        run_due_schedules(config, target, moment=first_moment)
