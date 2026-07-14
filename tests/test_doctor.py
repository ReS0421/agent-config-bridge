"""Focused tests for read-only environment diagnostics."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.doctor import CheckLevel, run_doctor
from agent_config_bridge.models import Component, Platform, Product
from agent_config_bridge.planner import build_plan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.state import write_skill_state
from tests.conftest import make_catalog, make_config, symlink_directory_or_skip


def test_doctor_reports_healthy_catalog_executable_and_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable target produces positive baseline diagnostics."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    executable = Path(sys.executable).resolve()
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: str(executable))

    checks = run_doctor(config, inventory, plan)
    by_code = {check.code: check for check in checks}

    assert by_code["catalog.valid"].level is CheckLevel.OK
    assert by_code["plan.conflicts"].level is CheckLevel.OK
    assert by_code["target.executable"].level is CheckLevel.OK
    assert str(executable) in by_code["target.executable"].message
    assert "version: Python" in by_code["target.executable"].message
    assert by_code["target.home"].level is CheckLevel.OK


def test_doctor_honors_configured_executable_and_reports_its_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit target launcher is validated and probed without consulting PATH."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    executable = Path(sys.executable).resolve()
    target = replace(config.targets[0], executable=executable)
    config = replace(config, targets=(target,))

    def unexpected_path_probe(_name: str) -> str:
        raise AssertionError("an explicit executable must bypass PATH")

    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", unexpected_path_probe)
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    executable_check = next(check for check in checks if check.code == "target.executable")
    assert executable_check.level is CheckLevel.OK
    assert f"configured): {executable}" in executable_check.message
    assert "version: Python" in executable_check.message


def test_doctor_errors_for_missing_configured_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing explicit launcher is a target configuration error, not a PATH warning."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    missing = tmp_path / "custom/missing-codex"
    config = replace(config, targets=(replace(config.targets[0], executable=missing),))
    monkeypatch.setattr(
        "agent_config_bridge.doctor.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("an explicit executable must bypass PATH")),
    )
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    executable_check = next(check for check in checks if check.code == "target.executable")
    assert executable_check.level is CheckLevel.ERROR
    assert "configured executable is invalid" in executable_check.message
    assert str(missing) in executable_check.message
    assert "version probe skipped" in executable_check.message


def test_doctor_warns_for_invalid_path_selected_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken ambient PATH entry is diagnosed without making the target declaration invalid."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    missing = tmp_path / "tools/missing-codex"
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: str(missing))
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    executable_check = next(check for check in checks if check.code == "target.executable")
    assert executable_check.level is CheckLevel.WARNING
    assert "PATH-selected executable is invalid" in executable_check.message
    assert str(missing) in executable_check.message


def test_doctor_warns_when_codex_hooks_are_disabled_and_home_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex feature flags and process-home mismatches are diagnosed together."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("audit",))
    config = make_config(
        tmp_path,
        catalog,
        platform=current_platform(),
        components=frozenset({Component.HOOKS}),
    )
    config_home = config.targets[0].config_home
    config_home.mkdir(parents=True)
    (config_home / "config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "another-codex-home"))
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    assert any(check.code == "codex.home-mismatch" and check.level is CheckLevel.WARNING for check in checks)
    assert any(check.code == "hooks.disabled" and check.level is CheckLevel.WARNING for check in checks)
    assert any(check.code == "target.executable" and check.level is CheckLevel.WARNING for check in checks)


def test_doctor_compares_windows_codex_home_case_insensitively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent Windows path casing does not produce a false mismatch warning."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS)
    target = replace(config.targets[0], config_home=tmp_path / "HOME/.CODEX")
    config = replace(config, targets=(target,))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home/.codex"))
    monkeypatch.setattr("agent_config_bridge.doctor.current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    assert not any(check.code == "codex.home-mismatch" for check in checks)


def test_doctor_skips_product_probes_for_opposite_platform_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote-platform diagnostics inspect paths without probing local product state."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("audit",))
    config = make_config(
        tmp_path,
        catalog,
        platform=Platform.WINDOWS,
        components=frozenset({Component.HOOKS}),
    )
    monkeypatch.setattr("agent_config_bridge.doctor.current_platform", lambda: Platform.LINUX)

    def unexpected_probe(_name: str) -> str:
        raise AssertionError("opposite-platform target must not probe the current PATH")

    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", unexpected_probe)

    def unexpected_settings_probe(*_args: object) -> tuple[object, ...]:
        raise AssertionError("opposite-platform target must not read product settings")

    monkeypatch.setattr("agent_config_bridge.doctor._hook_feature_checks", unexpected_settings_probe)
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    assert any(check.code == "target.platform-mismatch" and check.level is CheckLevel.INFO for check in checks)
    assert any(check.code == "target.home" for check in checks)
    assert any(check.code == "target.config-home" for check in checks)
    assert not any(check.code in {"target.executable", "hooks.enabled", "hooks.disabled"} for check in checks)


def test_doctor_reports_creatable_custom_config_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing custom config home is healthy when an ancestor is writable."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    target = replace(config.targets[0], config_home=tmp_path / "home/custom/deep/config")
    config = replace(config, targets=(target,))
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    config_home = next(check for check in checks if check.code == "target.config-home")
    assert config_home.level is CheckLevel.OK
    assert "can be created" in config_home.message


def test_doctor_warns_when_claude_skill_root_redirects_to_another_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single target still reports a discovery root redirected outside its profile."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        platform=current_platform(),
    )
    config_home = config.targets[0].config_home
    config_home.mkdir(parents=True)
    physical_root = tmp_path / "windows-profile/.claude/skills"
    physical_root.mkdir(parents=True)
    declared_root = config_home / "skills"
    symlink_directory_or_skip(declared_root, physical_root)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    checks = run_doctor(config, inventory, plan)

    assert not plan.has_conflicts
    redirected = next(check for check in checks if check.code == "skills.discovery-root-redirected")
    assert redirected.level is CheckLevel.WARNING
    assert redirected.target == "target"
    assert str(declared_root) in redirected.message
    assert str(physical_root.resolve()) in redirected.message


def test_doctor_warns_when_existing_skill_root_parent_is_redirected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink in the existing discovery-root parent chain is visible."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    physical_agents = tmp_path / "shared-agents"
    (physical_agents / "skills").mkdir(parents=True)
    redirected_parent = config.targets[0].user_home / ".agents"
    symlink_directory_or_skip(redirected_parent, physical_agents)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    redirected = next(check for check in checks if check.code == "skills.discovery-root-redirected")
    assert redirected.level is CheckLevel.WARNING
    assert str(redirected_parent) in redirected.message
    assert str((physical_agents / "skills").resolve()) in redirected.message


def test_doctor_ignores_symlinks_below_skill_discovery_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intentional child Skill links are not mistaken for root redirection."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    skill_root = config.targets[0].user_home / ".agents/skills"
    skill_root.mkdir(parents=True)
    manual_skill = tmp_path / "manual-skill"
    manual_skill.mkdir()
    symlink_directory_or_skip(skill_root / "manual", manual_skill)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    assert not any(check.code == "skills.discovery-root-redirected" for check in checks)


def test_doctor_treats_windows_directory_reparse_root_as_redirected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows junction-like roots are diagnosed even when is_symlink is false."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=Platform.WINDOWS)
    skill_root = config.targets[0].user_home / ".agents/skills"
    skill_root.mkdir(parents=True)
    monkeypatch.setattr("agent_config_bridge.doctor.current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "agent_config_bridge.doctor.is_directory_reparse_point",
        lambda path: path == skill_root,
    )

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    redirected = next(check for check in checks if check.code == "skills.discovery-root-redirected")
    assert redirected.level is CheckLevel.WARNING
    assert str(skill_root) in redirected.message


def test_doctor_warns_when_custom_config_home_parent_is_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing config home reports an ancestor that cannot create it."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog, platform=current_platform())
    existing_parent = tmp_path / "home"
    target = replace(config.targets[0], config_home=existing_parent / "custom/deep/config")
    config = replace(config, targets=(target,))
    real_access = os.access

    def access(path: Path, mode: int) -> bool:
        if Path(path) == existing_parent and mode == os.W_OK | os.X_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr("agent_config_bridge.doctor.os.access", access)
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    config_home = next(check for check in checks if check.code == "target.config-home")
    assert config_home.level is CheckLevel.WARNING
    assert "not writable" in config_home.message


def test_doctor_warns_when_claude_disables_all_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude's global hook kill switch is visible as a warning."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("audit",))
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        platform=current_platform(),
        components=frozenset({Component.HOOKS}),
    )
    config_home = config.targets[0].config_home
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text('{"disableAllHooks": true}\n', encoding="utf-8")
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: "/tools/claude")

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    disabled = next(check for check in checks if check.code == "hooks.disabled")
    assert disabled.level is CheckLevel.WARNING
    assert disabled.target == "target"
    assert "disableAllHooks" in disabled.message


def test_doctor_flags_missing_home_and_windows_desktop_plugin_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing homes are errors while the known Desktop limitation is informational."""

    catalog = make_catalog(tmp_path / "catalog", skills=(), plugins=("shared",))
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        platform=Platform.WINDOWS,
        components=frozenset({Component.PLUGINS}),
    )
    config.targets[0].user_home.rmdir()
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: "/tools/claude")

    inventory = discover_catalog(config)
    checks = run_doctor(config, inventory, build_plan(config, inventory))

    assert any(check.code == "target.home" and check.level is CheckLevel.ERROR for check in checks)
    assert any(check.code == "claude.desktop-session-limit" and check.level is CheckLevel.INFO for check in checks)


def test_doctor_skips_environment_probes_for_disabled_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled targets are reported without probing executables or homes."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    disabled = replace(config.targets[0], enabled=False)
    config = replace(config, targets=(disabled,))

    def unexpected_probe(_name: str) -> str:
        raise AssertionError("disabled target must not probe PATH")

    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", unexpected_probe)
    inventory = discover_catalog(config)

    checks = run_doctor(config, inventory, build_plan(config, inventory))

    target_checks = [check for check in checks if check.target == disabled.name]
    assert target_checks == [
        next(check for check in target_checks if check.code == "target.disabled" and check.level is CheckLevel.INFO)
    ]


def test_doctor_warns_about_orphaned_disabled_target_state(tmp_path: Path) -> None:
    """A disabled target cannot hide bridge-owned content that remains installed."""

    catalog = make_catalog(tmp_path / "catalog")
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    write_skill_state(config, config.targets[0], inventory)
    disabled = replace(config.targets[0], enabled=False)
    disabled_config = replace(config, targets=(disabled,))
    plan = build_plan(disabled_config, inventory)

    checks = run_doctor(disabled_config, inventory, plan)

    orphan = next(check for check in checks if check.code == "state.orphaned-target")
    assert orphan.level is CheckLevel.ERROR
    assert orphan.target == disabled.name
