"""Focused tests for the public command-line interface."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_config_bridge import cli, marketplace_registry
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.config import load_config
from agent_config_bridge.marketplace_registry import MarketplaceRegistryError
from agent_config_bridge.models import Platform, Product
from agent_config_bridge.planner import CommandHint, build_plan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.retention import (
    RetentionAction,
    RetentionBlocker,
    RetentionPlan,
)
from agent_config_bridge.state import read_registered_plugins, read_skill_state, write_registered_plugins
from tests.conftest import make_catalog


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        (
            "apply",
            "reconcile Skills, Instructions, Settings, Plugin/Hook marketplace builds, and Schedule snapshots",
        ),
        (
            "register",
            "reconcile product Plugin/Hook registrations and host scheduler heartbeats",
        ),
    ),
)
def test_mutating_command_help_names_its_full_reconciliation_scope(
    command: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators can distinguish filesystem apply from external registration."""

    with pytest.raises(SystemExit) as raised:
        cli.main([command, "--help"])

    assert raised.value.code == 0
    assert expected in " ".join(capsys.readouterr().out.split())


def test_state_prune_help_describes_no_change_validation_and_action_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["state", "prune", "--help"])

    assert raised.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "validate a reviewed no-change plan" in output
    assert "action-bearing plans fail closed" in output
    assert "apply the reviewed retention plan" not in output
    assert "delete the reviewed entries" not in output


_CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "codex-marketplace-list"
_DESTRUCTIVE_RETENTION_DISABLED = (
    "destructive retention apply is disabled until generation-bound atomic "
    "candidate capture exists; no entries were deleted"
)


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


@pytest.mark.parametrize(
    ("fixture_name", "platform", "expected"),
    [
        (
            "windows-0.144.4-root-only.json",
            Platform.WINDOWS,
            "c:/users/res/appdata/local/agentconfigbridge/state/marketplace",
        ),
        (
            "wsl-0.144.6-expanded.json",
            Platform.LINUX,
            "/home/res/.local/state/agent-config-bridge/wsl/marketplace",
        ),
    ],
)
def test_codex_marketplace_parser_accepts_supported_cli_schemas(
    tmp_path: Path,
    fixture_name: str,
    platform: Platform,
    expected: str,
) -> None:
    """Captured Codex 0.144.4 and 0.144.6 schemas resolve one source."""

    make_catalog(tmp_path / "catalog")
    target = replace(
        load_config(_write_config(tmp_path, components=("plugins",))).targets[0],
        platform=platform,
    )
    payload = json.loads((_CODEX_FIXTURES / fixture_name).read_text(encoding="utf-8"))

    assert marketplace_registry.parse_marketplace_source(payload, target) == expected


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"marketplaces": None},
        {"marketplaces": ["bad-entry"]},
        {"marketplaces": [{}]},
        {"marketplaces": [{"name": 1}]},
        {
            "marketplaces": [
                {"name": "agent-config-bridge", "root": "/one"},
                {"name": "agent-config-bridge", "root": "/two"},
            ]
        },
        {"marketplaces": [{"name": "agent-config-bridge"}]},
        {"marketplaces": [{"name": "agent-config-bridge", "root": ""}]},
        {"marketplaces": [{"name": "agent-config-bridge", "root": 1}]},
        {"marketplaces": [{"name": "agent-config-bridge", "root": "relative"}]},
        {"marketplaces": [{"name": "agent-config-bridge", "root": "/bad\u0000root"}]},
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": None,
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": [],
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": "local",
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "github", "source": "/bridge"},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"source": "/bridge"},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local"},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local", "source": ""},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local", "source": 1},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local", "source": "/bad\u0000source"},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local", "source": "relative"},
                }
            ]
        },
        {
            "marketplaces": [
                {
                    "name": "agent-config-bridge",
                    "root": "/bridge",
                    "marketplaceSource": {"sourceType": "local", "source": "/other"},
                }
            ]
        },
    ],
)
def test_codex_marketplace_parser_fails_closed_on_unknown_records(
    tmp_path: Path,
    payload: object,
) -> None:
    """Malformed registry records never produce an ownership source."""

    make_catalog(tmp_path / "catalog")
    target = load_config(_write_config(tmp_path, components=("plugins",))).targets[0]

    with pytest.raises(MarketplaceRegistryError):
        marketplace_registry.parse_marketplace_source(payload, target)


def test_marketplace_probe_decodes_utf8_stdout_independently_of_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-ASCII UTF-8 warning on stderr cannot trigger host-locale decoding."""

    calls: list[dict[str, object]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=b'{"marketplaces": []}',
            stderr="경고".encode(),
        )

    monkeypatch.setattr(marketplace_registry.subprocess, "run", run)

    assert marketplace_registry.run_utf8_json_command(("codex", "list"), {}) == {"marketplaces": []}
    assert calls == [{"check": False, "capture_output": True, "env": {}, "timeout": 5}]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (subprocess.CompletedProcess(("codex",), 1, stdout=b"{}", stderr=b""), "exited with status 1"),
        (subprocess.CompletedProcess(("codex",), 0, stdout=b"\xec", stderr=b""), "non-UTF-8"),
        (subprocess.CompletedProcess(("codex",), 0, stdout=b"not-json", stderr=b""), "invalid JSON"),
    ],
)
def test_marketplace_probe_fails_closed_on_process_or_decode_errors(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[bytes],
    message: str,
) -> None:
    """Process failures and undecodable output cannot authorize ownership."""

    monkeypatch.setattr(marketplace_registry.subprocess, "run", lambda *_args, **_kwargs: result)

    with pytest.raises(MarketplaceRegistryError, match=message):
        marketplace_registry.run_utf8_json_command(("codex", "list"), {})


def test_marketplace_probe_fails_closed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded read-only probe reports timeout deterministically."""

    def timeout(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(marketplace_registry.subprocess, "run", timeout)

    with pytest.raises(MarketplaceRegistryError, match="timed out"):
        marketplace_registry.run_utf8_json_command(("codex", "list"), {})


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
        "instructions": 0,
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
    assert payload["schema_version"] == 1
    assert payload["has_conflicts"] is True
    assert payload["actions"][0]["disposition"] == "conflict"
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_sync_skills_requires_confirmation_only_when_skills_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public command confirms mutations but converged runs do not prompt."""

    make_catalog(tmp_path / "catalog", skills=("hello",))
    config_path = _write_config(tmp_path, components=("skills",))
    destination = tmp_path / "home/.agents/skills/hello"

    assert cli.main(["sync-skills", "-c", str(config_path)]) == 2
    assert not destination.exists()
    capsys.readouterr()

    assert cli.main(["sync-skills", "-c", str(config_path), "--yes", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["scope"] == "skills"
    assert first["skill_only"] is True
    assert first["applied"] == 1
    assert first["no_op"] is False

    monkeypatch.setattr(cli, "_confirm", Mock(side_effect=AssertionError("no-op must not confirm")))
    assert cli.main(["sync-skills", "-c", str(config_path), "--json"]) == 0
    converged = json.loads(capsys.readouterr().out)
    assert converged == {
        "applied": 0,
        "backups": [],
        "no_op": True,
        "scope": "skills",
        "skill_only": True,
        "warnings": [],
    }


def test_sync_skills_noop_reconciles_stale_ownership_after_completed_remove(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No-op execution clears ownership left after interruption following removal."""

    make_catalog(tmp_path / "catalog", skills=("hello",))
    config_path = _write_config(tmp_path, components=("skills",))
    assert cli.main(["sync-skills", "-c", str(config_path), "--yes"]) == 0
    capsys.readouterr()
    configured = load_config(config_path)
    destination = tmp_path / "home/.agents/skills/hello"
    if destination.is_symlink():
        destination.unlink()
    else:
        shutil.rmtree(destination)
    assert read_skill_state(configured, configured.targets[0])

    _write_config(tmp_path, components=())
    deselected = load_config(config_path)
    assert cli.main(["sync-skills", "-c", str(config_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == 0
    assert payload["no_op"] is True
    assert read_skill_state(deselected, deselected.targets[0]) == ()


def test_sync_skills_rejects_pending_non_skill_changes_before_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full-plan marketplace change blocks the otherwise safe Skill create."""

    make_catalog(tmp_path / "catalog", skills=("hello",), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("skills", "plugins"))
    destination = tmp_path / "home/.agents/skills/hello"

    assert cli.main(["sync-skills", "-c", str(config_path), "--yes"]) == 2

    captured = capsys.readouterr()
    assert "non-skill changes are pending" in captured.err
    assert "plugins:marketplace" in captured.err
    assert not destination.exists()
    assert not (tmp_path / "state/marketplace").exists()


def test_plan_json_models_claude_default_profile_environment_removal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machine-readable commands make the default Claude profile selection explicit."""

    make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config_path = _write_config(tmp_path, components=("plugins",), product="claude-code")

    assert cli.main(["plan", "--config", str(config_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert {
        "actions",
        "commands",
        "has_changes",
        "has_conflicts",
        "reviews",
        "schema_version",
        "warnings",
    } <= set(payload)
    assert payload["schema_version"] == 1
    assert payload["commands"]
    assert all(
        {
            "argv",
            "environment",
            "environment_unsets",
            "reason",
            "target",
        }
        <= set(command)
        for command in payload["commands"]
    )
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
    assert preflight_call.kwargs["check"] is False
    assert preflight_call.kwargs["capture_output"] is True
    assert preflight_call.kwargs["timeout"] == 5
    assert "text" not in preflight_call.kwargs
    for invocation in (marketplace_call, install_call):
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


def test_removal_probe_fails_closed_on_non_utf8_vendor_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undecodable idempotence output never turns a failed removal into success."""

    command = CommandHint(
        target="codex",
        platform=current_platform(),
        environment=(),
        argv=("codex", "plugin", "marketplace", "remove", "agent-config-bridge"),
        reason="test",
    )
    monkeypatch.setattr(
        marketplace_registry.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout=b"\xec", stderr=b""),
    )

    assert not cli._removal_is_already_satisfied(command, {}, Product.CODEX)


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

    with pytest.raises(cli.ConfigError, match="invalid Codex bridge marketplace root"):
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


def test_register_scheduler_only_does_not_crash_on_governed_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register with scheduler changes but no plugin commands still records ownership.

    Regression: the governance-gated ownership write dereferenced a resolved
    inventory that was only computed when plugin commands existed, so a
    scheduler-only registration raised UnboundLocalError before it could run.
    """

    from types import SimpleNamespace

    make_catalog(tmp_path / "catalog", skills=(), schedules=("daily",))
    config_path = _write_config(tmp_path, components=("schedules",))
    config = load_config(config_path)
    target = config.targets[0]

    plan = SimpleNamespace(
        target=target,
        spec=SimpleNamespace(agentbridge_executable="ab", vendor_executable="v", config_path="c"),
        plan=SimpleNamespace(
            disposition=SimpleNamespace(value="create"), backend=SimpleNamespace(value="cron"), detail="d"
        ),
        desired="desired",
        previous_state=None,
        has_conflict=False,
        has_changes=True,
    )
    monkeypatch.setattr(cli, "_build_scheduler_registrations", lambda *args, **kwargs: (plan,))
    applied: list[object] = []
    monkeypatch.setattr(cli, "apply_scheduler_registrations", lambda *args: applied.append(args))

    exit_code = cli.main(["register", "--config", str(config_path), "--target", "local", "--yes"])

    assert exit_code == 0
    assert applied  # the scheduler path ran instead of crashing
    assert read_registered_plugins(config, target) == ()


def _retention_plan(
    config_path: Path,
    *,
    actions: tuple[RetentionAction, ...] = (),
    blockers: tuple[RetentionBlocker, ...] = (),
) -> RetentionPlan:
    config = load_config(config_path)
    return RetentionPlan(
        limits=config.retention,
        build_count=2,
        build_bytes=20,
        skill_backup_group_count=1,
        skill_backup_snapshot_count=4,
        skill_backup_bytes=40,
        actions=actions,
        blockers=blockers,
        excluded_instruction_roots=(config.state_dir / "backups" / "local" / "instructions",),
    )


def test_state_prune_is_a_read_only_json_plan_without_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())
    action_path = tmp_path / "state" / "builds" / ("a" * 20)
    action = RetentionAction(
        category="marketplace_build",
        path=action_path,
        node_kind="directory",
        bytes=10,
        mtime_ns=1,
        device=2,
        inode=3,
    )
    plan = _retention_plan(config_path, actions=(action,))
    monkeypatch.setattr(cli, "build_retention_plan", lambda _config: plan)
    monkeypatch.setattr(
        cli,
        "apply_retention_plan",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dry-run must not apply")),
    )

    exit_code = cli.main(["state", "prune", "-c", str(config_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["applied"] is False
    assert payload["has_changes"] is True
    assert payload["actions"] == [
        {
            "bytes": 10,
            "category": "marketplace_build",
            "device": 2,
            "inode": 3,
            "mtime_ns": 1,
            "node_kind": "directory",
            "path": str(action_path),
        }
    ]
    assert payload["deleted"] == []
    assert payload["reclaimed_bytes"] == 0
    assert payload["converged"] is False


def test_state_prune_human_dry_run_labels_candidates_and_disabled_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())
    action_path = tmp_path / "state" / "builds" / ("a" * 20)
    action = RetentionAction(
        category="marketplace_build",
        path=action_path,
        node_kind="directory",
        bytes=10,
        mtime_ns=1,
        device=2,
        inode=3,
    )
    plan = _retention_plan(config_path, actions=(action,))
    monkeypatch.setattr(cli, "build_retention_plan", lambda _config: plan)

    exit_code = cli.main(["state", "prune", "-c", str(config_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "1 deletion candidates" in captured.out
    assert f"CANDIDATE  marketplace_build: {action_path}" in captured.out
    assert "automated deletion is disabled" in captured.out
    assert captured.err == ""


def test_state_prune_yes_rejects_action_plan_without_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())
    action_path = tmp_path / "state" / "backups" / "local" / "seo" / "20260723-120000-1234abcd"
    action_path.mkdir(parents=True)
    sentinel = action_path / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    action = RetentionAction(
        category="skill_backup",
        path=action_path,
        node_kind="directory",
        bytes=10,
        mtime_ns=1,
        device=2,
        inode=3,
    )
    reviewed = _retention_plan(config_path, actions=(action,))
    monkeypatch.setattr(cli, "build_retention_plan", lambda _config: reviewed)

    exit_code = cli.main(["state", "prune", "-c", str(config_path), "--yes", "--json"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_DESTRUCTIVE_RETENTION_DISABLED}\n"
    assert action_path.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_state_prune_yes_validates_no_action_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())

    exit_code = cli.main(["state", "prune", "-c", str(config_path), "--yes", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["applied"] is True
    assert payload["actions"] == []
    assert payload["deleted"] == []
    assert payload["reclaimed_bytes"] == 0
    assert payload["converged"] is True


def test_state_prune_yes_human_output_reports_validation_not_deletion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())

    exit_code = cli.main(["state", "prune", "-c", str(config_path), "--yes"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "retention validation completed: the reviewed no-change plan remains current\n"
    assert "deleted" not in captured.out
    assert captured.err == ""


def test_state_prune_blocker_fails_closed_without_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_catalog(tmp_path / "catalog")
    config_path = _write_config(tmp_path, components=())
    blocker = RetentionBlocker(tmp_path / "state" / "builds" / "odd", "unexpected entry")
    plan = _retention_plan(config_path, blockers=(blocker,))
    monkeypatch.setattr(cli, "build_retention_plan", lambda _config: plan)
    monkeypatch.setattr(
        cli,
        "apply_retention_plan",
        lambda *_args: (_ for _ in ()).throw(AssertionError("blocked plan must not apply")),
    )

    exit_code = cli.main(["state", "prune", "-c", str(config_path), "--yes", "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["safe"] is False
    assert payload["has_blockers"] is True
    assert payload["deleted"] == []
