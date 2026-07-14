"""Focused tests for the public command-line interface."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_config_bridge import cli
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.config import load_config
from agent_config_bridge.models import Platform, Product
from agent_config_bridge.planner import CommandHint, build_plan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.state import read_registered_plugins, write_registered_plugins
from tests.conftest import make_catalog


def _write_config(
    tmp_path: Path,
    *,
    components: tuple[str, ...],
    product: str = "codex",
    target_name: str = "local",
    target_extra: str = "",
) -> Path:
    """Write an integration-test config whose paths stay below ``tmp_path``."""

    catalog = tmp_path / "catalog"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config_path = tmp_path / "agentbridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = {json.dumps(catalog.as_posix())}
state_dir = {json.dumps((tmp_path / "state").as_posix())}
link_mode = "auto"
components = {json.dumps(components)}

[[targets]]
name = {json.dumps(target_name)}
product = {json.dumps(product)}
platform = {json.dumps(current_platform().value)}
user_home = {json.dumps(home.as_posix())}
surfaces = ["cli", "desktop"]
enabled = true
{target_extra}
""",
        encoding="utf-8",
    )
    return config_path


def _successful_product_run(
    argv: tuple[str, ...],
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Return empty marketplace JSON for preflights and success otherwise."""

    if argv == ("codex", "plugin", "marketplace", "list", "--json"):
        return subprocess.CompletedProcess(argv, 0, stdout='{"marketplaces": []}', stderr="")
    if argv == ("claude", "plugin", "marketplace", "list", "--json"):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _write_launcher(path: Path) -> Path:
    """Create a host-executable test product launcher."""

    if current_platform() is Platform.WINDOWS:
        path = path.with_suffix(".exe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_init_creates_starter_config_and_refuses_unconfirmed_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Initialization is local to the requested path and protects existing config."""

    config_path = tmp_path / "bridge.toml"

    assert cli.main(["init", "--config", str(config_path)]) == 0
    assert config_path.is_file()
    assert all((tmp_path / "catalog" / group).is_dir() for group in ("skills", "plugins", "hooks"))

    assert cli.main(["init", "--config", str(config_path)]) == 2
    assert "config already exists" in capsys.readouterr().err


def test_validate_json_reports_catalog_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The validate command emits stable machine-readable inventory counts."""

    make_catalog(tmp_path / "catalog", skills=("alpha", "beta"), hooks=("audit",))
    config_path = _write_config(tmp_path, components=("skills", "hooks"))

    assert cli.main(["validate", "--config", str(config_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "catalog": str((tmp_path / "catalog").resolve()),
        "hooks": 1,
        "plugins": 0,
        "schedules": 0,
        "settings": 0,
        "skills": 2,
        "valid": True,
    }


def test_plan_json_returns_one_for_unmanaged_destination_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Plan surfaces an unmanaged target as a conflict without modifying it."""

    make_catalog(tmp_path / "catalog", skills=("hello",))
    config_path = _write_config(tmp_path, components=("skills",))
    unmanaged = tmp_path / "home" / ".agents" / "skills" / "hello"
    unmanaged.mkdir(parents=True)
    sentinel = unmanaged / "owned-by-user.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    assert cli.main(["plan", "--config", str(config_path), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["has_conflicts"] is True
    assert payload["actions"][0]["disposition"] == "conflict"
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_plan_json_models_claude_default_profile_environment_removal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine-readable commands make the default Claude profile selection explicit."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",), product="claude-code")

    assert cli.main(["plan", "--config", str(config_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"]
    assert all(command["environment"] == {} for command in payload["commands"])
    assert all(command["environment_unsets"] == ["CLAUDE_CONFIG_DIR"] for command in payload["commands"])


def test_register_requires_confirmation_before_running_product_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin registration cannot start subprocesses before explicit confirmation."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_confirm", lambda _prompt, _confirmed: False)

    assert cli.main(["register", "--config", str(config_path), "--target", "local"]) == 2
    run.assert_not_called()


def test_register_confirmed_passes_scoped_home_to_product_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmed registration runs only planned commands with the target config home."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    assert run.call_count == 3
    preflight_call, marketplace_call, install_call = run.call_args_list
    assert preflight_call.args[0] == ("codex", "plugin", "marketplace", "list", "--json")
    assert marketplace_call.args[0][:4] == ("codex", "plugin", "marketplace", "add")
    assert install_call.args[0] == ("codex", "plugin", "add", "shared@agent-config-bridge")
    for invocation in run.call_args_list:
        assert invocation.kwargs["check"] is True
        assert invocation.kwargs["env"]["CODEX_HOME"] == str(tmp_path / "home" / ".codex")
    config = load_config(config_path)
    assert read_registered_plugins(config, config.targets[0]) == ("shared",)


def test_register_uses_claude_default_profile_without_config_dir_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default Claude registration inherits its normal top-level profile layout."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",), product="claude-code")
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "inherited-wrong-profile"))
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    assert run.call_count == 5
    for invocation in run.call_args_list:
        assert "CLAUDE_CONFIG_DIR" not in invocation.kwargs["env"]


def test_register_sets_claude_config_dir_for_custom_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom Claude homes remain scoped consistently across preflight and mutations."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    custom_home = tmp_path / "claude-profile"
    config_path = _write_config(
        tmp_path,
        components=("plugins",),
        product="claude-code",
        target_extra=f"config_home = {json.dumps(custom_home.as_posix())}",
    )
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "inherited-wrong-profile"))
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    for invocation in run.call_args_list:
        assert invocation.kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(custom_home)


def test_register_uses_validated_explicit_executable_for_preflight_and_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit target launcher replaces the PATH command throughout registration."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    executable = _write_launcher(tmp_path / "tools/codex-custom")
    config_path = _write_config(
        tmp_path,
        components=("plugins",),
        target_extra=f"executable = {json.dumps(executable.as_posix())}",
    )
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:] == ("plugin", "marketplace", "list", "--json"):
            return subprocess.CompletedProcess(argv, 0, stdout='{"marketplaces": []}', stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    assert calls[0] == (str(executable), "plugin", "marketplace", "list", "--json")
    assert calls
    assert {argv[0] for argv in calls} == {str(executable)}


def test_register_rejects_missing_explicit_executable_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Plugin registration applies the same real-file validation as scheduled runs."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    missing = tmp_path / "tools/missing-codex"
    config_path = _write_config(
        tmp_path,
        components=("plugins",),
        target_extra=f"executable = {json.dumps(missing.as_posix())}",
    )
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 2

    run.assert_not_called()
    assert "could not resolve the codex executable" in capsys.readouterr().err


def test_register_reconciles_deselected_plugins_recorded_by_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later empty selection removes only the plugin recorded by register."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    config_path = _write_config(tmp_path, components=())
    run.reset_mock()
    run.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(
        argv,
        0,
        stdout='{"marketplaces": []}' if argv == ("codex", "plugin", "marketplace", "list", "--json") else "",
        stderr="",
    )
    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    commands = [invocation.args[0] for invocation in run.call_args_list]
    assert ("codex", "plugin", "remove", "shared@agent-config-bridge") in commands
    assert ("codex", "plugin", "marketplace", "remove", "agent-config-bridge") in commands
    config = load_config(config_path)
    assert read_registered_plugins(config, config.targets[0]) == ()


def test_windows_command_preview_uses_powershell_syntax() -> None:
    """Windows plans print a copyable PowerShell command, not POSIX env syntax."""

    command = CommandHint(
        target="windows-codex",
        platform=Platform.WINDOWS,
        environment=(("CODEX_HOME", r"C:\Users\Res\.codex"),),
        argv=("codex", "plugin", "list"),
        reason="test",
    )

    preview = cli._format_command(command)

    assert preview == "$env:CODEX_HOME = 'C:\\Users\\Res\\.codex'; & 'codex' 'plugin' 'list'"


def test_windows_command_preview_quotes_explicit_executable() -> None:
    """A selected Windows launcher remains a safe copyable PowerShell invocation."""

    command = CommandHint(
        target="windows-codex",
        platform=Platform.WINDOWS,
        environment=(("CODEX_HOME", r"C:\Users\Res\.codex"),),
        argv=(r"C:\Program Files\Codex\codex.exe", "plugin", "list"),
        reason="test",
    )

    preview = cli._format_command(command)

    assert preview == (
        "$env:CODEX_HOME = 'C:\\Users\\Res\\.codex'; & 'C:\\Program Files\\Codex\\codex.exe' 'plugin' 'list'"
    )


def test_posix_command_preview_unsets_claude_default_profile_override() -> None:
    """Linux previews cannot inherit a caller's custom Claude profile."""

    command = CommandHint(
        target="linux-claude",
        platform=Platform.LINUX,
        environment=(),
        argv=("claude", "plugin", "list"),
        reason="test",
        environment_unsets=("CLAUDE_CONFIG_DIR",),
    )

    assert cli._format_command(command) == "env -u CLAUDE_CONFIG_DIR claude plugin list"


def test_windows_command_preview_unsets_claude_default_profile_override() -> None:
    """Windows previews remove the inherited profile before invoking Claude."""

    command = CommandHint(
        target="windows-claude",
        platform=Platform.WINDOWS,
        environment=(),
        argv=("claude.exe", "plugin", "list"),
        reason="test",
        environment_unsets=("CLAUDE_CONFIG_DIR",),
    )

    assert cli._format_command(command) == (
        "Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue; & 'claude.exe' 'plugin' 'list'"
    )


def test_register_treats_confirmed_missing_claude_removals_as_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial Claude cleanup can be retried after uninstall/remove already succeeded."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=(), product="claude-code")
    config = load_config(config_path)
    write_registered_plugins(config, config.targets[0], ("shared",))
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ("claude", "plugin", "uninstall"):
            raise subprocess.CalledProcessError(1, argv)
        if argv == ("claude", "plugin", "list", "--json"):
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if argv[:4] == ("claude", "plugin", "marketplace", "remove"):
            raise subprocess.CalledProcessError(1, argv)
        if argv == ("claude", "plugin", "marketplace", "list", "--json"):
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0

    assert ("claude", "plugin", "list", "--json") in calls
    assert ("claude", "plugin", "marketplace", "list", "--json") in calls
    assert read_registered_plugins(config, config.targets[0]) == ()


def test_register_without_target_selects_only_current_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared config can register its local targets without failing on another OS target."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    config = load_config(config_path)
    local_target = config.targets[0]
    other_platform = Platform.WINDOWS if current_platform() is Platform.LINUX else Platform.LINUX
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    other_target = replace(
        local_target,
        name="other-platform",
        platform=other_platform,
        user_home=other_home,
        config_home=other_home / ".codex",
    )
    mixed_config = replace(config, targets=(local_target, other_target))
    inventory = discover_catalog(mixed_config)
    plan = build_plan(mixed_config, inventory)
    run = Mock(side_effect=_successful_product_run)
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli._command_register(mixed_config, inventory, plan, (), True) == 0

    assert run.call_count == 3
    assert read_registered_plugins(mixed_config, local_target) == ("shared",)
    assert read_registered_plugins(mixed_config, other_target) == ()


def test_register_treats_confirmed_missing_codex_marketplace_as_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried Codex cleanup can pass a marketplace removal that already succeeded."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=())
    config = load_config(config_path)
    write_registered_plugins(config, config.targets[0], ("shared",))

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ("codex", "plugin", "remove"):
            return subprocess.CompletedProcess(argv, 0)
        if argv[:4] == ("codex", "plugin", "marketplace", "remove"):
            raise subprocess.CalledProcessError(1, argv)
        if argv == ("codex", "plugin", "marketplace", "list", "--json"):
            return subprocess.CompletedProcess(argv, 0, stdout='{"marketplaces": []}', stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 0
    assert read_registered_plugins(config, config.targets[0]) == ()


def test_removal_probe_fails_closed_on_unknown_vendor_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed vendor list schemas never erase bridge ownership by assumption."""

    command = CommandHint(
        target="claude",
        platform=current_platform(),
        environment=(),
        argv=(
            "claude",
            "plugin",
            "uninstall",
            "shared@agent-config-bridge",
            "--scope",
            "user",
            "--keep-data",
        ),
        reason="test",
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(command.argv, 0, stdout='[{"plugin": "shared"}]'),
    )

    assert not cli._removal_is_already_satisfied(command, {}, Product.CLAUDE_CODE)


def test_removal_retry_reuses_explicit_product_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotence probes never fall back to a different PATH-discovered CLI."""

    executable = "/opt/claude/claude"
    command = CommandHint(
        target="claude",
        platform=current_platform(),
        environment=(),
        argv=(executable, "plugin", "marketplace", "remove", "agent-config-bridge"),
        reason="test",
    )
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    cli._run_registration_command(command, {}, Product.CLAUDE_CODE)

    assert calls == [
        command.argv,
        (executable, "plugin", "marketplace", "list", "--json"),
    ]


def test_register_refuses_cleanup_when_named_marketplace_has_another_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Recorded plugin names do not authorize deleting another checkout's marketplace."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=())
    config = load_config(config_path)
    write_registered_plugins(config, config.targets[0], ("shared",))
    foreign_source = (tmp_path / "foreign-checkout" / "marketplace").resolve()
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == ("codex", "plugin", "marketplace", "list", "--json")
        payload = {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": str(foreign_source),
                    "marketplaceSource": {
                        "sourceType": "local",
                        "source": str(foreign_source),
                    },
                }
            ]
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 2
    assert calls == [("codex", "plugin", "marketplace", "list", "--json")]
    assert "unowned source" in capsys.readouterr().err
    assert read_registered_plugins(config, config.targets[0]) == ("shared",)


def test_register_refuses_initial_claude_add_over_another_marketplace_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Claude's replacing marketplace add cannot take over a foreign named source."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",), product="claude-code")
    config = load_config(config_path)
    foreign_source = (tmp_path / "foreign-checkout" / "marketplace").resolve()
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == ("claude", "plugin", "marketplace", "list", "--json")
        payload = [
            {
                "name": "agent-config-bridge",
                "source": "directory",
                "path": str(foreign_source),
                "installLocation": str(foreign_source),
            }
        ]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"]) == 2
    assert calls == [("claude", "plugin", "marketplace", "list", "--json")]
    assert "unowned source" in capsys.readouterr().err
    assert read_registered_plugins(config, config.targets[0]) == ()


def test_registration_preflight_allows_desired_source_after_partial_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry accepts the desired source if an earlier relocation reached marketplace add."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    config = load_config(config_path)
    target = config.targets[0]
    write_registered_plugins(config, target, ("shared",))
    state_path = config.state_dir / "targets" / target.name / "plugins.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["marketplace_source"] = str((tmp_path / "old-state" / "marketplace").resolve())
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    desired_source = (config.state_dir / "marketplace").resolve()

    def run(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        marketplace = {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": str(desired_source),
                    "marketplaceSource": {
                        "sourceType": "local",
                        "source": str(desired_source),
                    },
                }
            ]
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(marketplace), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    destructive = CommandHint(
        target=target.name,
        platform=target.platform,
        environment=(),
        argv=("codex", "plugin", "remove", "shared@agent-config-bridge"),
        reason="test partial relocation",
    )

    cli._preflight_registration_ownership(config, target, (destructive,), {})


def test_registration_preflight_fails_closed_on_unknown_marketplace_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor schema change cannot silently authorize destructive reconciliation."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",))
    config = load_config(config_path)
    target = config.targets[0]
    write_registered_plugins(config, target, ("shared",))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"marketplaces": [{"name": "agent-config-bridge", "uri": "elsewhere"}]}',
            stderr="",
        ),
    )
    destructive = CommandHint(
        target=target.name,
        platform=target.platform,
        environment=(),
        argv=("codex", "plugin", "remove", "shared@agent-config-bridge"),
        reason="test schema failure",
    )

    with pytest.raises(cli.ConfigError, match="unexpected Codex bridge marketplace source"):
        cli._preflight_registration_ownership(config, target, (destructive,), {})


def test_register_preflights_every_target_before_any_product_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later target conflict is discovered before an earlier target is changed."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=())
    config = load_config(config_path)
    first_target = config.targets[0]
    second_home = tmp_path / "second-home"
    second_home.mkdir()
    second_target = replace(
        first_target,
        name="z-foreign",
        user_home=second_home,
        config_home=second_home / ".codex",
    )
    config = replace(config, targets=(first_target, second_target))
    for target in config.targets:
        write_registered_plugins(config, target, ("shared",))
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    foreign_source = (tmp_path / "foreign-checkout" / "marketplace").resolve()
    calls: list[tuple[str, ...]] = []

    def run(
        argv: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == ("codex", "plugin", "marketplace", "list", "--json")
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        if environment["CODEX_HOME"] == str(first_target.config_home):
            payload: object = {"marketplaces": []}
        else:
            payload = {
                "marketplaces": [
                    {
                        "name": "agent-config-bridge",
                        "root": str(foreign_source),
                        "marketplaceSource": {
                            "sourceType": "local",
                            "source": str(foreign_source),
                        },
                    }
                ]
            }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    with pytest.raises(cli.ConfigError, match="unowned source"):
        cli._command_register(config, inventory, plan, (first_target.name, second_target.name), True)

    assert calls == [
        ("codex", "plugin", "marketplace", "list", "--json"),
        ("codex", "plugin", "marketplace", "list", "--json"),
    ]
