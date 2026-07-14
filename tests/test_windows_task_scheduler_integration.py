"""Opt-in integration test for the real Windows Task Scheduler boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_config_bridge.scheduler_backends import (
    HeartbeatSpec,
    ScheduleBackendError,
    ScheduleDisposition,
    WindowsTaskSchedulerBackend,
)

_ENABLED = os.name == "nt" and os.environ.get("AGENTBRIDGE_RUN_WINDOWS_SCHEDULER_INTEGRATION") == "1"


@pytest.mark.windows_scheduler_integration
@pytest.mark.skipif(not _ENABLED, reason="requires an explicitly enabled Windows Task Scheduler host")
def test_windows_task_scheduler_import_export_round_trip(tmp_path: Path) -> None:
    """Real Task Scheduler normalization preserves the bridge's owned semantics."""

    target = f"ci-{uuid.uuid4().hex[:16]}"
    backend = WindowsTaskSchedulerBackend()
    agentbridge = Path(sys.executable).resolve().parent / "agentbridge.exe"
    assert agentbridge.is_file()
    config_path = (tmp_path / "bridge.toml").resolve()
    config_path.write_text("# integration identity\n", encoding="utf-8")
    spec = HeartbeatSpec(
        agentbridge_executable=agentbridge,
        config_path=config_path,
        target=target,
        vendor_executable=Path(sys.executable).resolve(),
    )
    task_name = backend.task_name(spec)

    try:
        create = backend.plan(spec)
        assert create.disposition is ScheduleDisposition.CREATE
        try:
            assert backend.apply(spec, create)
        except ScheduleBackendError as exc:
            exported = subprocess.run(
                (backend.schtasks_executable, "/Query", "/TN", task_name, "/XML"),
                capture_output=True,
                check=False,
                text=True,
            )
            pytest.fail(
                f"{exc}\nTask Scheduler export (exit {exported.returncode}):\n{exported.stdout}\n{exported.stderr}"
            )
        assert backend.plan(spec).disposition is ScheduleDisposition.NOOP

        removal = backend.plan_remove(spec)
        assert removal.disposition is ScheduleDisposition.REMOVE
        assert backend.remove(spec, removal)
        assert backend.plan_remove(spec).disposition is ScheduleDisposition.NOOP
    finally:
        # The UUID-scoped task belongs only to this test. This fallback also
        # cleans up when Task Scheduler normalizes XML in an unsupported way.
        subprocess.run(
            (backend.schtasks_executable, "/Delete", "/TN", task_name, "/F"),
            capture_output=True,
            check=False,
            text=True,
        )
