"""Tests for portable schedule discovery, evaluation, and execution."""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from agent_config_bridge.models import Platform, Product, Surface, TargetConfig
from agent_config_bridge.schedules import (
    ScheduleError,
    ScheduleExecutionError,
    TargetScheduleSnapshot,
    build_vendor_invocation,
    discover_schedules,
    execute_vendor_invocation,
    parse_cron,
    render_target_snapshot,
    snapshot_is_due,
)


def _write_schedule(
    catalog: Path,
    *,
    name: str = "daily-review",
    cron: str = "0 9 * * 1-5",
    timezone: str = "UTC",
    working_directory: str = "work",
    timeout: object = 900,
    prompt: str = "Review the repository.\n",
    extra: str = "",
) -> Path:
    root = catalog / "schedules" / name
    root.mkdir(parents=True)
    timeout_line = f"timeout_seconds = {str(timeout).lower()}\n" if timeout is not None else ""
    (root / "schedule.toml").write_text(
        "".join(
            (
                "schema_version = 1\n",
                f"cron = {json.dumps(cron)}\n",
                f"timezone = {json.dumps(timezone)}\n",
                f"working_directory = {json.dumps(working_directory)}\n",
                timeout_line,
                extra,
            )
        ),
        encoding="utf-8",
    )
    (root / "PROMPT.md").write_text(prompt, encoding="utf-8")
    return root


def _make_target(
    home: Path,
    *,
    product: Product = Product.CODEX,
    name: str = "local",
    enabled: bool = True,
) -> TargetConfig:
    return TargetConfig(
        name=name,
        product=product,
        platform=Platform.LINUX,
        user_home=home,
        config_home=home / (".codex" if product is Product.CODEX else ".claude"),
        components=frozenset(),
        surfaces=frozenset({Surface.CLI}),
        enabled=enabled,
    )


def _new_york() -> ZoneInfo:
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA America/New_York data is unavailable")


def test_discover_schedules_loads_sorted_strict_definitions(tmp_path: Path) -> None:
    """Canonical schedule files produce deterministic immutable definitions."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, name="zeta", prompt="Zeta prompt without newline")
    _write_schedule(
        catalog,
        name="alpha",
        cron="*/15 8-18 * * 1-5",
        working_directory="projects/alpha",
        timeout=None,
        prompt="Alpha prompt\n\nKeep this spacing.\n",
    )

    inventory = discover_schedules(catalog)

    assert [schedule.name for schedule in inventory.schedules] == ["alpha", "zeta"]
    alpha = inventory.schedules[0]
    assert alpha.cron_expression.source == "*/15 8-18 * * 1-5"
    assert str(alpha.working_directory) == "projects/alpha"
    assert alpha.timeout_seconds == 1_800
    assert alpha.prompt == "Alpha prompt\n\nKeep this spacing.\n"
    with pytest.raises(FrozenInstanceError):
        alpha.name = "changed"  # type: ignore[misc]


def test_discover_schedules_returns_empty_for_missing_group(tmp_path: Path) -> None:
    """Catalogs can omit the optional schedules component."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()

    assert discover_schedules(catalog).schedules == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown", "unknown keys"),
        ("missing", "exactly schedule.toml and PROMPT.md"),
        ("empty-prompt", "must not be empty"),
        ("bad-schema", "schema_version"),
        ("bad-timezone", "unknown IANA timezone"),
    ],
)
def test_discover_schedules_rejects_invalid_catalog_sources(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    """Unknown schema, malformed layout, and unusable prompts fail closed."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    root = _write_schedule(catalog)
    if mutation == "unknown":
        with (root / "schedule.toml").open("a", encoding="utf-8") as stream:
            stream.write("enabled = true\n")
    elif mutation == "missing":
        (root / "PROMPT.md").unlink()
    elif mutation == "empty-prompt":
        (root / "PROMPT.md").write_text(" \n\t", encoding="utf-8")
    elif mutation == "bad-schema":
        document = root / "schedule.toml"
        document.write_text(document.read_text(encoding="utf-8").replace("schema_version = 1", "schema_version = 2"))
    else:
        document = root / "schedule.toml"
        document.write_text(
            document.read_text(encoding="utf-8").replace('timezone = "UTC"', 'timezone = "Mars/Olympus"'),
            encoding="utf-8",
        )

    with pytest.raises(ScheduleError, match=message):
        discover_schedules(catalog)


@pytest.mark.parametrize("timeout", [0, -1, 86_401, True])
def test_discover_schedules_rejects_unbounded_timeout(tmp_path: Path, timeout: object) -> None:
    """Timeouts are integer seconds within the documented safety bound."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, timeout=timeout)

    with pytest.raises(ScheduleError, match="timeout_seconds"):
        discover_schedules(catalog)


def test_discover_schedules_rejects_prompt_null_without_echoing_prompt(tmp_path: Path) -> None:
    """Prompt bytes unsafe for subprocess input are rejected without disclosure."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    secret_prompt = "private-prefix\x00private-suffix"
    _write_schedule(catalog, prompt=secret_prompt)

    with pytest.raises(ScheduleError) as captured:
        discover_schedules(catalog)

    assert secret_prompt not in str(captured.value)


def test_discover_schedules_rejects_source_symlink(tmp_path: Path) -> None:
    """A prompt cannot redirect catalog reads through a symbolic link."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    root = _write_schedule(catalog)
    external = tmp_path / "external.md"
    external.write_text("external prompt", encoding="utf-8")
    (root / "PROMPT.md").unlink()
    try:
        (root / "PROMPT.md").symlink_to(external)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ScheduleError, match="real regular files"):
        discover_schedules(catalog)


def test_parse_cron_supports_numeric_vixie_grammar() -> None:
    """Lists, ranges, wildcards, and steps expand to immutable value sets."""

    expression = parse_cron("*/15 0,12 1-10/3 1-12 0,7")

    assert expression.minutes == frozenset({0, 15, 30, 45})
    assert expression.hours == frozenset({0, 12})
    assert expression.days_of_month == frozenset({1, 4, 7, 10})
    assert expression.months == frozenset(range(1, 13))
    assert expression.days_of_week == frozenset({0})


@pytest.mark.parametrize(
    "value",
    [
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "1/2 * * * *",
        "5-1 * * * *",
        "* * * JAN *",
        "* * ? * *",
        "*,1 * * * *",
        "* * * * *\n*",
    ],
)
def test_parse_cron_rejects_non_strict_grammar(value: str) -> None:
    """Extensions, overflow, malformed lists, and extra fields are rejected."""

    with pytest.raises(ScheduleError):
        parse_cron(value)


def test_cron_uses_vixie_or_semantics_for_restricted_day_fields() -> None:
    """Restricted day-of-month and day-of-week fields match with Vixie OR."""

    expression = parse_cron("0 9 15 * 1")

    assert expression.matches(datetime(2026, 7, 15, 9, tzinfo=UTC), "UTC")
    assert expression.matches(datetime(2026, 7, 20, 9, tzinfo=UTC), "UTC")
    assert not expression.matches(datetime(2026, 7, 21, 9, tzinfo=UTC), "UTC")


def test_cron_preserves_syntactic_restriction_for_full_day_range() -> None:
    """A full numeric DOW range remains restricted for Vixie DOM/DOW OR."""

    expression = parse_cron("0 9 15 * 0-6")

    assert expression.day_of_week_unrestricted is False
    assert expression.matches(datetime(2026, 7, 21, 9, tzinfo=UTC), "UTC")


def test_cron_evaluates_in_iana_timezone() -> None:
    """The same instant is matched against the schedule's local wall clock."""

    try:
        zone = ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA Asia/Seoul data is unavailable")
    expression = parse_cron("0 9 * * *")

    assert expression.matches(datetime(2026, 7, 14, 0, tzinfo=UTC), zone)
    assert not expression.matches(datetime(2026, 7, 14, 9, tzinfo=UTC), zone)
    with pytest.raises(ScheduleError, match="aware"):
        expression.matches(datetime(2026, 7, 14, 9), "UTC")


def test_cron_next_after_skips_nonexistent_dst_minute() -> None:
    """Spring-forward gaps never manufacture an imaginary matching instant."""

    zone = _new_york()
    expression = parse_cron("30 2 * * *")

    result = expression.next_after(datetime(2024, 3, 10, 6, 59, tzinfo=UTC), zone)

    assert result == datetime(2024, 3, 11, 2, 30, tzinfo=zone)
    assert result.astimezone(UTC) == datetime(2024, 3, 11, 6, 30, tzinfo=UTC)


def test_cron_next_after_preserves_both_repeated_dst_minutes() -> None:
    """Fall-back's repeated local minute maps to two distinct real instants."""

    zone = _new_york()
    expression = parse_cron("30 1 * * *")

    first = expression.next_after(datetime(2024, 11, 3, 5, 29, tzinfo=UTC), zone)
    second = expression.next_after(first, zone)

    assert first.astimezone(UTC) == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
    assert second.astimezone(UTC) == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    assert first.fold == 0
    assert second.fold == 1


@pytest.mark.parametrize("working_directory", ["../outside", "/tmp", "C:\\outside", "safe/../../outside"])
def test_schedule_rejects_lexical_working_directory_escape(
    tmp_path: Path,
    working_directory: str,
) -> None:
    """Canonical work paths are relative, normalized, and portable."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, working_directory=working_directory)

    with pytest.raises(ScheduleError, match="working_directory"):
        discover_schedules(catalog)


@pytest.mark.parametrize("working_directory", ["con.txt", "safe/COM1.log", "NUL.any"])
def test_schedule_rejects_windows_device_names_with_extensions(
    tmp_path: Path,
    working_directory: str,
) -> None:
    """DOS device names stay reserved even when a suffix is present."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, working_directory=working_directory)

    with pytest.raises(ScheduleError, match="Windows reserved name"):
        discover_schedules(catalog)


def test_render_target_snapshot_resolves_contained_directory(tmp_path: Path) -> None:
    """Rendering freezes a real native directory beneath the target home."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, working_directory="projects/review")
    home = tmp_path / "home"
    working_directory = home / "projects" / "review"
    working_directory.mkdir(parents=True)
    schedule = discover_schedules(catalog).schedules[0]

    snapshot = render_target_snapshot(schedule, _make_target(home))

    assert snapshot.schema_version == 1
    assert snapshot.user_home == home.resolve()
    assert snapshot.working_directory == working_directory.resolve()
    assert snapshot.cron == "0 9 * * 1-5"
    assert snapshot_is_due(snapshot, datetime(2026, 7, 14, 9, tzinfo=UTC))
    with pytest.raises(FrozenInstanceError):
        snapshot.prompt = "changed"  # type: ignore[misc]


def test_render_target_snapshot_rejects_symlink_escape(tmp_path: Path) -> None:
    """A relative path cannot escape through an existing directory symlink."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, working_directory="linked")
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (home / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    schedule = discover_schedules(catalog).schedules[0]

    with pytest.raises(ScheduleError, match="escapes target.user_home"):
        render_target_snapshot(schedule, _make_target(home))


def test_render_target_snapshot_rejects_disabled_target(tmp_path: Path) -> None:
    """Disabled targets never produce runnable schedule data."""

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_schedule(catalog, working_directory=".")
    home = tmp_path / "home"
    home.mkdir()
    schedule = discover_schedules(catalog).schedules[0]

    with pytest.raises(ScheduleError, match="disabled"):
        render_target_snapshot(schedule, _make_target(home, enabled=False))


def test_build_codex_invocation_uses_exact_argv_and_prompt_stdin(tmp_path: Path) -> None:
    """Shell-looking prompt content remains inert standard-input text."""

    prompt = "Review `$(touch /tmp/pwned)` and $HOME; do not interpolate.\n"
    snapshot = _snapshot(tmp_path, Product.CODEX, prompt=prompt)
    source_environment = {"PATH": "/safe/bin", "LOCAL_TOKEN": "runtime-only"}

    invocation = build_vendor_invocation(snapshot, environment=source_environment)
    source_environment["PATH"] = "/changed"

    assert invocation.argv == (
        "codex",
        "exec",
        "--ephemeral",
        "-C",
        str(snapshot.working_directory),
        "-",
    )
    assert invocation.stdin == prompt
    assert invocation.env == {"LOCAL_TOKEN": "runtime-only", "PATH": "/safe/bin"}
    assert all(prompt not in argument for argument in invocation.argv)
    assert "dangerously" not in " ".join(invocation.argv)


def test_build_claude_invocation_uses_nonpersistent_print_mode(tmp_path: Path) -> None:
    """Claude scheduled runs use print mode without permission bypass flags."""

    snapshot = _snapshot(tmp_path, Product.CLAUDE_CODE)

    invocation = build_vendor_invocation(snapshot)

    assert invocation.argv == ("claude", "--print", "--no-session-persistence")
    assert invocation.environment is None
    assert invocation.working_directory == snapshot.working_directory
    assert "dangerously-skip-permissions" not in invocation.argv


def test_build_invocation_uses_registered_absolute_vendor_executable(tmp_path: Path) -> None:
    """Host scheduler runs do not depend on cron or Task Scheduler PATH."""

    executable = (tmp_path / "bin/codex").resolve()
    invocation = build_vendor_invocation(
        _snapshot(tmp_path, Product.CODEX),
        vendor_executable=executable,
    )

    assert invocation.argv[0] == str(executable)
    assert invocation.argv[1:] == (
        "exec",
        "--ephemeral",
        "-C",
        str(invocation.working_directory),
        "-",
    )


def test_execute_vendor_invocation_passes_argv_env_and_stdin_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor passes structured process data and never enables a shell."""

    prompt = "literal; $(not-a-command)\n"
    invocation = build_vendor_invocation(
        _snapshot(tmp_path, Product.CODEX, prompt=prompt),
        environment={"PATH": "/safe"},
    )
    captured: dict[str, Any] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="done\n", stderr="")

    monkeypatch.setattr("agent_config_bridge.schedules.subprocess.run", fake_run)

    result = execute_vendor_invocation(invocation)

    assert captured["argv"] == invocation.argv
    assert captured["input"] == prompt
    assert captured["env"] == {"PATH": "/safe"}
    assert captured["cwd"] == invocation.working_directory
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "strict"
    assert captured["shell"] is False
    assert captured["check"] is False
    assert result.stdout == "done\n"


def test_execute_vendor_invocation_reports_timeout_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout failures are bounded and do not disclose the submitted prompt."""

    secret_prompt = "SECRET-PROMPT-CONTENT"
    invocation = build_vendor_invocation(_snapshot(tmp_path, Product.CLAUDE_CODE, prompt=secret_prompt))

    def timeout(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("agent_config_bridge.schedules.subprocess.run", timeout)

    with pytest.raises(ScheduleExecutionError, match="timed out") as captured:
        execute_vendor_invocation(invocation)

    assert secret_prompt not in str(captured.value)


def test_execute_vendor_invocation_sanitizes_utf8_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locale-independent UTF-8 failures do not disclose the prompt."""

    secret_prompt = "비공개-프롬프트-🔒"
    invocation = build_vendor_invocation(_snapshot(tmp_path, Product.CODEX, prompt=secret_prompt))

    def fail_encoding(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise UnicodeEncodeError("utf-8", secret_prompt, 0, 1, "test failure")

    monkeypatch.setattr("agent_config_bridge.schedules.subprocess.run", fail_encoding)

    with pytest.raises(ScheduleExecutionError, match="UTF-8") as captured:
        execute_vendor_invocation(invocation)

    assert secret_prompt not in str(captured.value)


def test_execute_vendor_invocation_rejects_nonzero_exit_without_output_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vendor failures surface an exit code without copying potentially secret output."""

    invocation = build_vendor_invocation(_snapshot(tmp_path, Product.CODEX))

    def fail(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 7, stdout="", stderr="private vendor output")

    monkeypatch.setattr("agent_config_bridge.schedules.subprocess.run", fail)

    with pytest.raises(ScheduleExecutionError, match="code 7") as captured:
        execute_vendor_invocation(invocation)

    assert "private vendor output" not in str(captured.value)


def test_execute_vendor_invocation_revalidates_working_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-render symlink swap is rejected before starting the vendor."""

    invocation = build_vendor_invocation(_snapshot(tmp_path, Product.CODEX))
    original_directory = invocation.working_directory
    moved_directory = original_directory.with_name("moved-work")
    original_directory.rename(moved_directory)
    outside = tmp_path / "outside-work"
    outside.mkdir()
    try:
        original_directory.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        moved_directory.rename(original_directory)
        pytest.skip(f"directory symlinks unavailable: {error}")

    def must_not_run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected process execution: {argv!r}, {kwargs!r}")

    monkeypatch.setattr("agent_config_bridge.schedules.subprocess.run", must_not_run)

    with pytest.raises(ScheduleExecutionError, match="no longer safe"):
        execute_vendor_invocation(invocation)


def test_execute_vendor_invocation_rejects_forged_timeout(tmp_path: Path) -> None:
    """Directly constructed invocation data cannot bypass timeout bounds."""

    invocation = build_vendor_invocation(_snapshot(tmp_path, Product.CLAUDE_CODE))

    with pytest.raises(ScheduleExecutionError, match="invalid timeout"):
        execute_vendor_invocation(replace(invocation, timeout_seconds=True))


def _snapshot(tmp_path: Path, product: Product, *, prompt: str = "Run checks.\n") -> TargetScheduleSnapshot:
    home = tmp_path / f"{product.value}-home"
    working_directory = home / "work"
    working_directory.mkdir(parents=True)
    return TargetScheduleSnapshot(
        schema_version=1,
        schedule_name="daily-review",
        target_name=f"local-{product.value}",
        product=product,
        user_home=home.resolve(),
        cron="0 9 * * *",
        timezone="UTC",
        working_directory=working_directory.resolve(),
        timeout_seconds=5,
        prompt=prompt,
    )
