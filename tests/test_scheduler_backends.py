"""Tests for host heartbeat scheduler ownership and reconciliation."""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent_config_bridge.scheduler_backends import (
    HeartbeatSpec,
    LinuxCronBackend,
    ScheduleBackendError,
    ScheduleConflictError,
    ScheduleDisposition,
    StaleSchedulePlanError,
    WindowsTaskSchedulerBackend,
)

_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class FakeCrontab:
    """In-memory user crontab subprocess boundary."""

    def __init__(self, document: str | None = None) -> None:
        self.document = document
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin))
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
        raise AssertionError(f"unexpected crontab command: {argv}")


class FakeTaskScheduler:
    """In-memory schtasks subprocess boundary that captures imported XML."""

    def __init__(self) -> None:
        self.tasks: dict[str, str] = {}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin))
        action = argv[1]
        task_name = argv[argv.index("/TN") + 1]
        if action == "/Query":
            if task_name not in self.tasks:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="ERROR: The system cannot find the file specified.",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=self.tasks[task_name], stderr="")
        if action == "/Create":
            if task_name in self.tasks and "/F" not in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="task already exists")
            xml_path = Path(argv[argv.index("/XML") + 1])
            self.tasks[task_name] = xml_path.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="SUCCESS", stderr="")
        if action == "/Delete":
            if task_name not in self.tasks:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="task does not exist")
            del self.tasks[task_name]
            return subprocess.CompletedProcess(argv, 0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected schtasks command: {argv}")


def _spec(tmp_path: Path, *, config_name: str = "bridge.toml", target: str = "local-codex") -> HeartbeatSpec:
    return HeartbeatSpec(
        agentbridge_executable=(tmp_path / "bin/agentbridge").resolve(),
        config_path=(tmp_path / config_name).resolve(),
        target=target,
        vendor_executable=(tmp_path / "bin/codex").resolve(),
    )


@pytest.mark.parametrize(
    ("executable", "config", "target"),
    [
        (Path("agentbridge"), Path("/config.toml"), "local"),
        (Path("/agentbridge"), Path("config.toml"), "local"),
        (Path("/agentbridge"), Path("/config.toml"), "../escape"),
    ],
)
def test_heartbeat_spec_rejects_ambiguous_scheduler_arguments(
    executable: Path,
    config: Path,
    target: str,
) -> None:
    """Scheduler commands require absolute paths and a safe target identifier."""

    with pytest.raises(ValueError):
        HeartbeatSpec(executable, config, target, Path("/codex"))


def test_linux_plan_is_read_only_and_apply_is_idempotent(tmp_path: Path) -> None:
    """A missing heartbeat is created once and then becomes a no-op."""

    runner = FakeCrontab()
    backend = LinuxCronBackend(runner=runner)
    spec = _spec(tmp_path)

    plan = backend.plan(spec)

    assert plan.disposition is ScheduleDisposition.CREATE
    assert runner.calls == [(("crontab", "-l"), None)]
    assert backend.apply(spec, plan)
    assert runner.document is not None
    assert "* * * * * " in runner.document
    assert (
        f"{spec.agentbridge_executable} schedule tick --config {spec.config_path} --target {spec.target} "
        f"--vendor-executable {spec.vendor_executable}" in runner.document
    )

    noop = backend.plan(spec)
    write_count = sum(argv != ("crontab", "-l") for argv, _stdin in runner.calls)
    assert noop.disposition is ScheduleDisposition.NOOP
    assert not backend.apply(spec, noop)
    assert sum(argv != ("crontab", "-l") for argv, _stdin in runner.calls) == write_count


def test_linux_apply_and_remove_preserve_unrelated_crontab(tmp_path: Path) -> None:
    """Marked-block lifecycle leaves every unrelated crontab byte intact."""

    unrelated = "MAILTO=ops@example.test\n15 3 * * * /usr/local/bin/backup\n"
    runner = FakeCrontab(unrelated)
    backend = LinuxCronBackend(runner=runner)
    spec = _spec(tmp_path)

    backend.apply(spec, backend.plan(spec))
    assert runner.document is not None and runner.document.startswith(unrelated)
    backend.remove(spec, backend.plan_remove(spec))

    assert runner.document == unrelated
    assert backend.plan_remove(spec).disposition is ScheduleDisposition.NOOP


def test_linux_remove_preserves_whitespace_only_unrelated_crontab(tmp_path: Path) -> None:
    """Removing a block does not collapse pre-existing blank crontab content."""

    unrelated = " \n\n"
    runner = FakeCrontab(unrelated)
    backend = LinuxCronBackend(runner=runner)
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))

    backend.remove(spec, backend.plan_remove(spec))

    assert runner.document == unrelated


def test_linux_owned_block_updates_after_config_change(tmp_path: Path) -> None:
    """An intact old owned block may be replaced by a new absolute config path."""

    runner = FakeCrontab()
    backend = LinuxCronBackend(runner=runner)
    old_spec = _spec(tmp_path, config_name="old.toml")
    backend.apply(old_spec, backend.plan(old_spec))
    new_spec = _spec(tmp_path, config_name="new.toml")

    plan = backend.plan(new_spec)

    assert plan.disposition is ScheduleDisposition.UPDATE
    assert backend.apply(new_spec, plan)
    assert runner.document is not None
    assert str(new_spec.config_path) in runner.document
    assert str(old_spec.config_path) not in runner.document


def test_linux_cron_quotes_shell_arguments_and_escapes_percent(tmp_path: Path) -> None:
    """Cron's unavoidable shell boundary receives quoted and percent-safe arguments."""

    unusual = tmp_path / "space and 'quote' % value.toml"
    spec = _spec(tmp_path, config_name=unusual.name)
    runner = FakeCrontab()
    backend = LinuxCronBackend(runner=runner)

    backend.apply(spec, backend.plan(spec))

    assert runner.document is not None
    assert r"\%" in runner.document
    assert "'\"'\"'" in runner.document
    assert runner.document.count("* * * * * ") == 1


def test_linux_forged_or_drifted_marker_is_a_conflict(tmp_path: Path) -> None:
    """A marker whose digest no longer matches its command authorizes no mutation."""

    runner = FakeCrontab()
    backend = LinuxCronBackend(runner=runner)
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))
    assert runner.document is not None
    runner.document = runner.document.replace(str(spec.config_path), "/tmp/forged.toml")

    plan = backend.plan(spec)

    assert plan.disposition is ScheduleDisposition.CONFLICT
    with pytest.raises(ScheduleConflictError):
        backend.apply(spec, plan)
    assert "/tmp/forged.toml" in runner.document


def test_linux_stray_incomplete_marker_is_a_conflict(tmp_path: Path) -> None:
    """A target-scoped ownership marker cannot be adopted without its block."""

    runner = FakeCrontab("# agent-config-bridge-heartbeat schema=1 target=local-codex\n")
    backend = LinuxCronBackend(runner=runner)

    plan = backend.plan(_spec(tmp_path))

    assert plan.disposition is ScheduleDisposition.CONFLICT


def test_linux_stale_plan_cannot_clobber_new_crontab_content(tmp_path: Path) -> None:
    """Unrelated changes after planning invalidate the full-document precondition."""

    runner = FakeCrontab("0 0 * * * /bin/true\n")
    backend = LinuxCronBackend(runner=runner)
    spec = _spec(tmp_path)
    plan = backend.plan(spec)
    runner.document += "1 0 * * * /bin/false\n"

    with pytest.raises(StaleSchedulePlanError):
        backend.apply(spec, plan)


def test_linux_query_error_fails_closed(tmp_path: Path) -> None:
    """An ambiguous crontab read failure is never treated as an empty crontab."""

    def fail(argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="permission denied")

    with pytest.raises(ScheduleBackendError, match="permission denied"):
        LinuxCronBackend(runner=fail).plan(_spec(tmp_path))


def test_windows_task_create_is_xml_based_and_idempotent(tmp_path: Path) -> None:
    """Task creation uses separate command/arguments and a one-minute trigger."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="TEST\\Res")
    spec = _spec(tmp_path)
    task_name = backend.task_name(spec)

    plan = backend.plan(spec)
    assert plan.disposition is ScheduleDisposition.CREATE
    assert backend.apply(spec, plan)

    root = ET.fromstring(runner.tasks[task_name])
    namespace = {"t": _TASK_NAMESPACE}
    assert root.attrib["version"] == "1.3"
    trigger = root.find(".//t:TimeTrigger", namespaces=namespace)
    assert trigger is not None
    assert [child.tag.rsplit("}", maxsplit=1)[-1] for child in trigger] == [
        "Enabled",
        "StartBoundary",
        "Repetition",
    ]
    assert root.findtext(".//t:Interval", namespaces=namespace) == "PT1M"
    assert root.findtext(".//t:MultipleInstancesPolicy", namespaces=namespace) == "Parallel"
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=namespace) == "PT0S"
    assert root.findtext(".//t:LogonType", namespaces=namespace) == "InteractiveToken"
    assert root.findtext(".//t:RunLevel", namespaces=namespace) == "LeastPrivilege"
    assert root.findtext(".//t:Command", namespaces=namespace) == str(spec.agentbridge_executable)
    arguments = root.findtext(".//t:Arguments", namespaces=namespace)
    assert arguments is not None
    assert arguments.startswith("schedule tick --config ")
    assert str(spec.config_path) in arguments
    assert arguments.endswith(f"--target {spec.target} --vendor-executable {spec.vendor_executable}")
    assert root.findtext(".//t:Description", namespaces=namespace).startswith("agent-config-bridge-heartbeat schema=1")

    noop = backend.plan(spec)
    create_count = sum(argv[1] == "/Create" for argv, _stdin in runner.calls)
    assert noop.disposition is ScheduleDisposition.NOOP
    assert not backend.apply(spec, noop)
    assert sum(argv[1] == "/Create" for argv, _stdin in runner.calls) == create_count


@pytest.mark.skipif(os.name != "nt", reason="requires Windows system-directory APIs")
def test_windows_backend_ignores_environment_and_uses_absolute_system_schtasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned SystemRoot or current directory cannot replace schtasks.exe."""

    poisoned_root = tmp_path / "fake-windows"
    fake = poisoned_root / "System32/schtasks.exe"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"not an executable")
    monkeypatch.setenv("SYSTEMROOT", str(poisoned_root))
    monkeypatch.chdir(tmp_path)

    backend = WindowsTaskSchedulerBackend(runner=FakeTaskScheduler(), principal_user="S-1-5-21-1")
    selected = Path(backend.schtasks_executable)

    assert selected.is_absolute()
    assert selected.is_file()
    assert selected.resolve(strict=True) != fake.resolve(strict=True)
    with pytest.raises(ValueError, match="absolute System32 path"):
        WindowsTaskSchedulerBackend(
            runner=FakeTaskScheduler(),
            schtasks_executable="schtasks.exe",
            principal_user="S-1-5-21-1",
        )


def test_windows_owned_task_updates_with_force(tmp_path: Path) -> None:
    """Only an intact owned task is replaced with schtasks /F."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    old_spec = _spec(tmp_path, config_name="old.toml")
    backend.apply(old_spec, backend.plan(old_spec))
    new_spec = _spec(tmp_path, config_name="new.toml")

    plan = backend.plan(new_spec)
    assert plan.disposition is ScheduleDisposition.UPDATE
    assert backend.apply(new_spec, plan)

    update_commands = [argv for argv, _stdin in runner.calls if argv[1] == "/Create" and "/F" in argv]
    assert len(update_commands) == 1
    assert str(new_spec.config_path) in runner.tasks[backend.task_name(new_spec)]


def test_windows_forged_marker_or_action_is_a_conflict(tmp_path: Path) -> None:
    """Task action drift invalidates the embedded ownership digest."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))
    task_name = backend.task_name(spec)
    runner.tasks[task_name] = runner.tasks[task_name].replace("schedule tick", "schedule forged")

    plan = backend.plan(spec)

    assert plan.disposition is ScheduleDisposition.CONFLICT
    with pytest.raises(ScheduleConflictError):
        backend.remove(spec, backend.plan_remove(spec))
    assert task_name in runner.tasks


def test_windows_drifted_execution_policy_is_a_conflict(tmp_path: Path) -> None:
    """Task runtime policy is covered by the ownership digest."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))
    task_name = backend.task_name(spec)
    runner.tasks[task_name] = runner.tasks[task_name].replace(
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
        "<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>",
    )

    assert backend.plan(spec).disposition is ScheduleDisposition.CONFLICT


@pytest.mark.parametrize(
    "mutate",
    [
        lambda xml: xml.replace(
            "</Actions>",
            "<ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler></Actions>",
        ),
        lambda xml: xml.replace("<Enabled>true</Enabled>", "<Enabled>false</Enabled>", 1),
        lambda xml: xml.replace("<UserId>test</UserId>", "<UserId>attacker</UserId>"),
        lambda xml: xml.replace("</Exec>", "<WorkingDirectory>C:\\</WorkingDirectory></Exec>"),
        lambda xml: xml.replace('Context="Author"', 'Context="Other"'),
        lambda xml: xml.replace(
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
        ),
        lambda xml: xml.replace("</Settings>", "<RunOnlyIfIdle>true</RunOnlyIfIdle></Settings>"),
    ],
)
def test_windows_rejects_unowned_task_semantic_drift(tmp_path: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    """Extra actions, disabled triggers, principals, and cwd drift all conflict."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))
    task_name = backend.task_name(spec)
    runner.tasks[task_name] = mutate(runner.tasks[task_name])

    assert backend.plan(spec).disposition is ScheduleDisposition.CONFLICT


def test_windows_remove_preserves_unrelated_tasks(tmp_path: Path) -> None:
    """Removal addresses only the target-scoped owned Task Scheduler name."""

    runner = FakeTaskScheduler()
    runner.tasks["Unrelated-Backup"] = "<unrelated />"
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    spec = _spec(tmp_path)
    backend.apply(spec, backend.plan(spec))

    assert backend.remove(spec, backend.plan_remove(spec))

    assert runner.tasks == {"Unrelated-Backup": "<unrelated />"}
    assert backend.plan_remove(spec).disposition is ScheduleDisposition.NOOP


def test_windows_unmanaged_task_name_is_a_conflict(tmp_path: Path) -> None:
    """An existing task without the bridge marker is never overwritten."""

    runner = FakeTaskScheduler()
    backend = WindowsTaskSchedulerBackend(runner=runner, principal_user="test")
    spec = _spec(tmp_path)
    runner.tasks[backend.task_name(spec)] = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" />'

    plan = backend.plan(spec)

    assert plan.disposition is ScheduleDisposition.CONFLICT
    with pytest.raises(ScheduleConflictError):
        backend.apply(spec, plan)
    assert not any(argv[1] == "/Create" for argv, _stdin in runner.calls)


def test_windows_query_error_fails_closed(tmp_path: Path) -> None:
    """A permission failure is not confused with an absent scheduled task."""

    def fail(argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ERROR: Access is denied.")

    backend = WindowsTaskSchedulerBackend(runner=fail, principal_user="test")

    with pytest.raises(ScheduleBackendError, match="Access is denied"):
        backend.plan(_spec(tmp_path))


def test_windows_hresult_detects_missing_task_without_localized_text(tmp_path: Path) -> None:
    """Task absence uses the HRESULT instead of an English diagnostic."""

    def missing(argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        assert "/HResult" in argv
        return subprocess.CompletedProcess(argv, 0x80070002, stdout="", stderr="localized diagnostic")

    backend = WindowsTaskSchedulerBackend(runner=missing, principal_user="test")

    assert backend.plan(_spec(tmp_path)).disposition is ScheduleDisposition.CREATE
