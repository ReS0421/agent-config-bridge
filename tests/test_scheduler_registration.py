"""Tests for state-backed host scheduler reconciliation."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component
from agent_config_bridge.schedule_store import render_schedule_set
from agent_config_bridge.scheduler_backends import LinuxCronBackend, ScheduleDisposition
from agent_config_bridge.scheduler_registration import (
    apply_scheduler_registration,
    apply_scheduler_registrations,
    build_scheduler_registration,
)
from agent_config_bridge.schedules import discover_schedules
from agent_config_bridge.state import read_scheduler_state
from tests.conftest import make_catalog, make_config


class _Crontab:
    def __init__(self) -> None:
        self.document: str | None = None

    def __call__(self, argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if argv == ("crontab", "-l"):
            if self.document is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no crontab for test")
            return subprocess.CompletedProcess(argv, 0, stdout=self.document, stderr="")
        if argv == ("crontab", "-"):
            assert stdin is not None
            self.document = stdin
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv == ("crontab", "-r"):
            self.document = None
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)


def _context(tmp_path: Path):  # type: ignore[no-untyped-def]
    catalog = make_catalog(tmp_path / "catalog", skills=(), schedules=("daily",))
    config_path = tmp_path / "agentbridge.toml"
    config_path.write_text("# test identity only\n", encoding="utf-8")
    config = replace(
        make_config(tmp_path, catalog, components=frozenset({Component.SCHEDULES})),
        config_path=config_path,
    )
    target = config.targets[0]
    inventory = discover_catalog(config)
    render_schedule_set(config, discover_schedules(config), target)
    executable = (tmp_path / "bin/agentbridge").resolve()
    runner = _Crontab()
    backend = LinuxCronBackend(runner=runner)
    return config, target, inventory, executable, runner, backend


def test_scheduler_registration_records_ownership_and_is_idempotent(tmp_path: Path) -> None:
    config, target, inventory, executable, _runner, backend = _context(tmp_path)

    plan = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )
    assert plan.plan.disposition is ScheduleDisposition.CREATE
    assert apply_scheduler_registration(config, plan)

    state = read_scheduler_state(config, target)
    assert state is not None
    assert state.heartbeat_digest == plan.plan.desired_digest
    noop = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )
    assert noop.plan.disposition is ScheduleDisposition.NOOP
    assert not noop.has_changes


def test_scheduler_registration_refuses_to_adopt_unrecorded_heartbeat(tmp_path: Path) -> None:
    config, target, inventory, executable, _runner, backend = _context(tmp_path)
    raw = backend.plan(
        build_scheduler_registration(
            config,
            inventory,
            target,
            executable=executable,
            vendor_executable=executable,
            backend=backend,
        ).spec
    )
    spec = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    ).spec
    backend.apply(spec, raw)

    guarded = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )

    assert guarded.plan.disposition is ScheduleDisposition.CONFLICT
    assert "no matching target ownership" in guarded.plan.detail


def test_scheduler_registration_removes_deselected_owned_heartbeat(tmp_path: Path) -> None:
    config, target, inventory, executable, runner, backend = _context(tmp_path)
    install = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )
    apply_scheduler_registration(config, install)
    empty_target = replace(target, components=frozenset())
    empty_config = replace(config, components=frozenset(), targets=(empty_target,))

    removal = build_scheduler_registration(
        empty_config,
        inventory,
        empty_target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )

    assert removal.plan.disposition is ScheduleDisposition.REMOVE
    assert apply_scheduler_registration(empty_config, removal)
    assert runner.document is None
    assert read_scheduler_state(empty_config, empty_target) is None


def test_scheduler_registration_recovers_missing_external_heartbeat_from_state(tmp_path: Path) -> None:
    config, target, inventory, executable, runner, backend = _context(tmp_path)
    initial = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )
    apply_scheduler_registration(config, initial)
    runner.document = None

    recovery = build_scheduler_registration(
        config,
        inventory,
        target,
        executable=executable,
        vendor_executable=executable,
        backend=backend,
    )

    assert recovery.plan.disposition is ScheduleDisposition.CREATE
    assert apply_scheduler_registration(config, recovery)


def test_scheduler_registration_batches_multiple_targets_in_one_crontab(tmp_path: Path) -> None:
    """Each target is safely replanned after an earlier shared-crontab write."""

    config, first, inventory, executable, runner, backend = _context(tmp_path)
    second_home = tmp_path / "second-home"
    second_home.mkdir()
    second = replace(
        first,
        name="second-target",
        user_home=second_home,
        config_home=second_home / ".codex",
    )
    batch_config = replace(config, targets=(first, second))
    render_schedule_set(batch_config, discover_schedules(batch_config), second)
    registrations = tuple(
        build_scheduler_registration(
            batch_config,
            inventory,
            target,
            executable=executable,
            vendor_executable=executable,
            backend=backend,
        )
        for target in batch_config.targets
    )

    assert all(item.plan.disposition is ScheduleDisposition.CREATE for item in registrations)
    assert apply_scheduler_registrations(batch_config, inventory, registrations) == (True, True)
    assert runner.document is not None
    assert "heartbeat target" in runner.document
    assert "heartbeat second-target" in runner.document
    assert read_scheduler_state(batch_config, first) is not None
    assert read_scheduler_state(batch_config, second) is not None
