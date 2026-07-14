"""Command-line interface for Agent Config Bridge."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_config_bridge.applier import ApplyError, apply_plan
from agent_config_bridge.catalog import CatalogError, CatalogInventory, discover_catalog
from agent_config_bridge.config import ConfigError, load_config
from agent_config_bridge.doctor import CheckLevel, run_doctor
from agent_config_bridge.filesystem import FilesystemError
from agent_config_bridge.models import BridgeConfig, Platform, Product, TargetConfig
from agent_config_bridge.path_safety import path_comparison_key
from agent_config_bridge.planner import CommandHint, SyncPlan, build_plan
from agent_config_bridge.platforms import UnsupportedPlatformError, current_platform
from agent_config_bridge.renderer import RenderError, render_marketplace
from agent_config_bridge.state import (
    BridgeStateError,
    desired_plugin_names,
    find_orphaned_target_states,
    read_registration_state,
    registration_marketplace_source,
    write_registered_plugins,
)

__all__ = ["main"]

_DEFAULT_CONFIG = "agentbridge.toml"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Agent Config Bridge command-line interface."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _command_init(args)
        config, inventory = _load_context(Path(args.config))
        if args.command == "validate":
            return _command_validate(inventory, args.json)
        if args.command == "render":
            return _command_render(config, inventory, args.json)
        plan = build_plan(config, inventory)
        if args.command == "plan":
            return _command_plan(plan, args.json)
        if args.command == "doctor":
            return _command_doctor(config, inventory, plan, args.json)
        if args.command == "apply":
            return _command_apply(config, inventory, plan, args.yes, args.json)
        if args.command == "register":
            return _command_register(config, inventory, plan, tuple(args.target), args.yes)
        parser.error(f"unknown command: {args.command}")
    except (
        ApplyError,
        CatalogError,
        ConfigError,
        FilesystemError,
        RenderError,
        BridgeStateError,
        UnsupportedPlatformError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbridge",
        description="Share canonical skills, plugins, and hooks across Codex and Claude Code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a starter config and empty catalog")
    init_parser.add_argument("--config", default=_DEFAULT_CONFIG)
    init_parser.add_argument("--force", action="store_true", help="replace only the generated config file")

    for command, help_text in (
        ("validate", "validate config and catalog structure"),
        ("plan", "show a read-only synchronization plan"),
        ("doctor", "diagnose target discovery and configuration"),
        ("render", "build the immutable dual plugin marketplace"),
        ("apply", "apply safe skill links/copies and render the marketplace"),
        ("register", "run product CLI commands to register and install rendered plugins"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
        if command in {"validate", "plan", "doctor", "render", "apply"}:
            command_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        if command in {"apply", "register"}:
            command_parser.add_argument("-y", "--yes", action="store_true", help="confirm state-changing actions")
        if command == "register":
            command_parser.add_argument(
                "--target",
                action="append",
                default=[],
                help="register one configured target; repeat to select multiple",
            )
    return parser


def _load_context(config_path: Path) -> tuple[BridgeConfig, CatalogInventory]:
    config = load_config(config_path)
    inventory = discover_catalog(config)
    return config, inventory


def _command_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if config_path.exists() and not args.force:
        raise ConfigError(f"config already exists (use --force to replace it): {config_path}")

    catalog = config_path.parent / "catalog"
    for group in ("skills", "plugins", "hooks"):
        (catalog / group).mkdir(parents=True, exist_ok=True)
    template = """schema_version = 1

[bridge]
catalog = "./catalog"
state_dir = "./.agentbridge"
link_mode = "auto"
components = ["skills", "plugins", "hooks"]

[[targets]]
name = "local-codex"
product = "codex"
platform = "auto"
user_home = "~"
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "local-claude-code"
product = "claude-code"
platform = "auto"
user_home = "~"
surfaces = ["cli", "desktop"]
enabled = true
"""
    config_path.write_text(template, encoding="utf-8")
    print(f"created {config_path}")
    print(f"created catalog at {catalog}")
    return 0


def _command_validate(inventory: CatalogInventory, as_json: bool) -> int:
    payload = {
        "catalog": str(inventory.root),
        "skills": len(inventory.skills),
        "plugins": len(inventory.plugins),
        "hooks": len(inventory.hooks),
        "valid": True,
    }
    if as_json:
        _print_json(payload)
    else:
        print(
            f"valid: {payload['skills']} skills, {payload['plugins']} plugins, "
            f"{payload['hooks']} hook bundles in {payload['catalog']}"
        )
    return 0


def _command_plan(plan: SyncPlan, as_json: bool) -> int:
    if as_json:
        _print_json(_plan_payload(plan))
    else:
        _print_plan(plan)
    return 1 if plan.has_conflicts else 0


def _command_doctor(
    config: BridgeConfig,
    inventory: CatalogInventory,
    plan: SyncPlan,
    as_json: bool,
) -> int:
    checks = run_doctor(config, inventory, plan)
    if as_json:
        _print_json({"checks": [_json_safe(asdict(check)) for check in checks]})
    else:
        for check in checks:
            target = f" [{check.target}]" if check.target else ""
            print(f"{check.level.value.upper():7} {check.code}{target}: {check.message}")
    return 1 if any(check.level is CheckLevel.ERROR for check in checks) else 0


def _command_render(config: BridgeConfig, inventory: CatalogInventory, as_json: bool) -> int:
    rendered = render_marketplace(config, inventory)
    payload = {
        "root": str(rendered.root),
        "build_root": str(rendered.build_root),
        "digest": rendered.digest,
        "codex_plugins": rendered.codex_plugins,
        "claude_plugins": rendered.claude_plugins,
    }
    if as_json:
        _print_json(payload)
    else:
        print(f"rendered marketplace: {rendered.root}")
        print(f"digest: {rendered.digest}")
    return 0


def _command_apply(
    config: BridgeConfig,
    inventory: CatalogInventory,
    plan: SyncPlan,
    confirmed: bool,
    as_json: bool,
) -> int:
    orphaned_targets = find_orphaned_target_states(config)
    if orphaned_targets:
        raise ApplyError("ownership state has no enabled target; restore and reconcile: " + ", ".join(orphaned_targets))
    if plan.has_conflicts:
        _print_plan(plan)
        raise ApplyError("resolve conflicts before applying")
    if plan.has_changes and not _confirm("Apply this plan?", confirmed):
        print("cancelled", file=sys.stderr)
        return 2
    result = apply_plan(config, inventory, plan)
    payload = {
        "applied": len(result.applied),
        "backups": [str(path) for path in result.backups],
        "marketplace": str(result.marketplace.root) if result.marketplace else None,
        "registration_commands": [_command_payload(command) for command in plan.commands],
    }
    if as_json:
        _print_json(payload)
    else:
        print(f"applied {payload['applied']} actions")
        if result.marketplace:
            print(f"marketplace: {result.marketplace.root}")
        for backup in result.backups:
            print(f"backup: {backup}")
        if plan.commands:
            print("plugin registration remains explicit; review and run:")
            for command in plan.commands:
                print(f"  [{command.target}] {_format_command(command)}")
    return 0


def _command_register(
    config: BridgeConfig,
    inventory: CatalogInventory,
    plan: SyncPlan,
    selected_targets: tuple[str, ...],
    confirmed: bool,
) -> int:
    orphaned_targets = find_orphaned_target_states(config)
    if orphaned_targets:
        raise ConfigError(
            "ownership state has no enabled target; restore and reconcile: " + ", ".join(orphaned_targets)
        )
    known_targets = {target.name: target for target in config.targets if target.enabled}
    local_platform = current_platform()
    target_names = (
        set(selected_targets)
        if selected_targets
        else {name for name, target in known_targets.items() if target.platform is local_platform}
    )
    if not target_names:
        raise ConfigError(f"no enabled targets match the current {local_platform.value} platform")
    unknown = target_names - set(known_targets)
    if unknown:
        raise ConfigError(f"unknown or disabled targets: {', '.join(sorted(unknown))}")

    for name in sorted(target_names):
        target = known_targets[name]
        if target.platform is not local_platform:
            raise ConfigError(f"target {name!r} uses {target.platform.value}; run registration from that platform")

    commands = tuple(command for command in plan.commands if command.target in target_names)
    if not commands:
        print("no plugin registration commands are required")
        return 0
    print("commands to execute:")
    for command in commands:
        print(f"  [{command.target}] {_format_command(command)}")
    if not _confirm("Execute these product CLI commands?", confirmed):
        print("cancelled", file=sys.stderr)
        return 2

    fresh_inventory = discover_catalog(config)
    fresh_plan = build_plan(config, fresh_inventory)
    if fresh_plan != plan:
        raise ConfigError("catalog, generated state, or destinations changed; review a fresh plan")
    render_marketplace(config, fresh_inventory)

    commands_by_target: dict[str, tuple[CommandHint, ...]] = {}
    for name in sorted(target_names):
        target = known_targets[name]
        target_commands = tuple(command for command in commands if command.target == name)
        commands_by_target[name] = target_commands
        environment = os.environ.copy()
        if target_commands:
            environment.update(dict(target_commands[0].environment))
            _preflight_registration_ownership(config, target, target_commands, environment)

    for name in sorted(target_names):
        target = known_targets[name]
        target_commands = commands_by_target[name]
        for command in target_commands:
            command_environment = os.environ.copy()
            command_environment.update(dict(command.environment))
            _run_registration_command(command, command_environment)
        write_registered_plugins(config, target, desired_plugin_names(target, fresh_inventory))
    return 0


def _preflight_registration_ownership(
    config: BridgeConfig,
    target: TargetConfig,
    commands: tuple[CommandHint, ...],
    environment: dict[str, str],
) -> None:
    """Refuse registration when the bridge-named marketplace has another owner."""

    registration = read_registration_state(config, target)
    if not commands:
        return

    actual_source = _registered_marketplace_source(target, environment)
    if actual_source is None:
        # A retry may start after the old marketplace was already removed.
        return
    allowed_sources = {registration_marketplace_source(config, target)}
    if registration.marketplace_source is not None:
        allowed_sources.add(registration.marketplace_source)
    if actual_source not in allowed_sources:
        raise ConfigError(
            f"target {target.name!r}: refusing registration because "
            "marketplace 'agent-config-bridge' is registered from an unowned source"
        )


def _registered_marketplace_source(
    target: TargetConfig,
    environment: dict[str, str],
) -> str | None:
    """Return the physical source for the bridge-named product marketplace."""

    if target.product is Product.CODEX:
        argv = ("codex", "plugin", "marketplace", "list", "--json")
    else:
        argv = ("claude", "plugin", "marketplace", "list", "--json")
    try:
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, TypeError) as exc:
        raise ConfigError(
            f"target {target.name!r}: could not verify marketplace ownership before registration"
        ) from exc

    raw_source = (
        _codex_marketplace_source(payload, target)
        if target.product is Product.CODEX
        else _claude_marketplace_source(payload, target)
    )
    if raw_source is None:
        return None
    source = Path(raw_source)
    if not source.is_absolute():
        raise ConfigError(
            f"target {target.name!r}: product returned a non-absolute marketplace source; refusing registration"
        )
    return path_comparison_key(source, windows=target.platform is Platform.WINDOWS)


def _codex_marketplace_source(payload: object, target: TargetConfig) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("marketplaces"), list):
        raise ConfigError(f"target {target.name!r}: unexpected Codex marketplace-list JSON")
    entries = payload["marketplaces"]
    if not all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in entries):
        raise ConfigError(f"target {target.name!r}: unexpected Codex marketplace-list JSON")
    matches = [entry for entry in entries if entry["name"] == "agent-config-bridge"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ConfigError(f"target {target.name!r}: duplicate Codex bridge marketplaces")
    entry = matches[0]
    root = entry.get("root")
    marketplace_source = entry.get("marketplaceSource")
    source: object = marketplace_source.get("source") if isinstance(marketplace_source, dict) else None
    if (
        not isinstance(root, str)
        or not isinstance(marketplace_source, dict)
        or marketplace_source.get("sourceType") != "local"
        or not isinstance(source, str)
    ):
        raise ConfigError(f"target {target.name!r}: unexpected Codex bridge marketplace source")
    windows = target.platform is Platform.WINDOWS
    if path_comparison_key(Path(root), windows=windows) != path_comparison_key(Path(source), windows=windows):
        raise ConfigError(f"target {target.name!r}: inconsistent Codex bridge marketplace source")
    return source


def _claude_marketplace_source(payload: object, target: TargetConfig) -> str | None:
    if not isinstance(payload, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in payload
    ):
        raise ConfigError(f"target {target.name!r}: unexpected Claude marketplace-list JSON")
    matches = [entry for entry in payload if entry["name"] == "agent-config-bridge"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ConfigError(f"target {target.name!r}: duplicate Claude bridge marketplaces")
    entry = matches[0]
    source_path = entry.get("path")
    install_location = entry.get("installLocation")
    if entry.get("source") != "directory" or not isinstance(source_path, str) or not isinstance(install_location, str):
        raise ConfigError(f"target {target.name!r}: unexpected Claude bridge marketplace source")
    windows = target.platform is Platform.WINDOWS
    if path_comparison_key(Path(source_path), windows=windows) != path_comparison_key(
        Path(install_location), windows=windows
    ):
        raise ConfigError(f"target {target.name!r}: inconsistent Claude bridge marketplace source")
    return source_path


def _run_registration_command(command: CommandHint, environment: dict[str, str]) -> None:
    try:
        subprocess.run(command.argv, check=True, env=environment)
    except subprocess.CalledProcessError:
        if _removal_is_already_satisfied(command, environment):
            return
        raise


def _removal_is_already_satisfied(command: CommandHint, environment: dict[str, str]) -> bool:
    argv = command.argv
    probe_argv: tuple[str, ...]
    removal_kind: str
    if argv[:3] == ("claude", "plugin", "uninstall") and len(argv) >= 4:
        probe_argv = ("claude", "plugin", "list", "--json")
        expected_name = argv[3]
        removal_kind = "plugin"

    elif argv[:4] == ("claude", "plugin", "marketplace", "remove") and len(argv) == 5:
        probe_argv = ("claude", "plugin", "marketplace", "list", "--json")
        expected_name = argv[4]
        removal_kind = "marketplace"

    elif argv[:4] == ("codex", "plugin", "marketplace", "remove") and len(argv) == 5:
        probe_argv = ("codex", "plugin", "marketplace", "list", "--json")
        expected_name = argv[4]
        removal_kind = "codex-marketplace"

    else:
        return False

    try:
        result = subprocess.run(
            probe_argv,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        return False
    if removal_kind == "codex-marketplace":
        if not isinstance(payload, dict) or not isinstance(payload.get("marketplaces"), list):
            return False
        if not all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in payload["marketplaces"]):
            return False
        return not any(
            isinstance(entry, dict) and entry.get("name") == expected_name for entry in payload["marketplaces"]
        )
    if not isinstance(payload, list):
        return False
    if removal_kind == "plugin":
        if not all(
            isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("scope"), str)
            for entry in payload
        ):
            return False
        return not any(
            isinstance(entry, dict) and entry.get("id") == expected_name and entry.get("scope") == "user"
            for entry in payload
        )
    if not all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in payload):
        return False
    return not any(isinstance(entry, dict) and entry.get("name") == expected_name for entry in payload)


def _confirm(prompt: str, confirmed: bool) -> bool:
    if confirmed:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _print_plan(plan: SyncPlan) -> None:
    for action in plan.actions:
        print(
            f"{action.disposition.value.upper():8} {action.operation.value:6} "
            f"{action.target}:{action.name} -> {action.destination} ({action.detail})"
        )
    for warning in plan.warnings:
        print(f"WARNING  {warning}")
    if plan.reviews:
        print("security review items:")
        for review in plan.reviews:
            print(f"  REVIEW  {review}")
    if plan.commands:
        print("after apply, plugin registration commands are available:")
        for command in plan.commands:
            print(f"  [{command.target}] {_format_command(command)}")


def _format_command(command: CommandHint) -> str:
    if command.platform.value == "windows":
        environment = "; ".join(f"$env:{key} = {_powershell_quote(value)}" for key, value in command.environment)
        argv = " ".join(_powershell_quote(value) for value in command.argv)
        return f"{environment}; & {argv}" if environment else f"& {argv}"
    environment = " ".join(f"{key}={shlex.quote(value)}" for key, value in command.environment)
    argv = shlex.join(command.argv)
    return f"{environment} {argv}".strip()


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _plan_payload(plan: SyncPlan) -> dict[str, Any]:
    return {
        "actions": [_json_safe(asdict(action)) for action in plan.actions],
        "commands": [_command_payload(command) for command in plan.commands],
        "reviews": list(plan.reviews),
        "warnings": list(plan.warnings),
        "has_changes": plan.has_changes,
        "has_conflicts": plan.has_conflicts,
    }


def _command_payload(command: CommandHint) -> dict[str, Any]:
    return {
        "target": command.target,
        "environment": dict(command.environment),
        "argv": list(command.argv),
        "reason": command.reason,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_json_safe(payload), indent=2, sort_keys=True))
