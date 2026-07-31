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
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from agent_config_bridge.applier import ApplyError, apply_plan, apply_skill_plan, validate_skill_only_plan
from agent_config_bridge.catalog import CatalogError, CatalogInventory, discover_catalog
from agent_config_bridge.config import ConfigError, load_config
from agent_config_bridge.doctor import CheckLevel, run_doctor
from agent_config_bridge.filesystem import FilesystemError
from agent_config_bridge.governance import (
    GovernanceError,
    GovernanceFinding,
    GovernanceSeverity,
    build_registry_payload,
    registry_path,
    resolve_inventory,
    run_governance,
    serialize_registry,
)
from agent_config_bridge.instruction_profiles import (
    InstructionProfileError,
    InstructionProfileReport,
    check_instruction_profiles,
    generate_instruction_profiles,
)
from agent_config_bridge.marketplace_registry import (
    MarketplaceRegistryError,
    probe_marketplace_source,
    run_utf8_json_command,
)
from agent_config_bridge.models import BridgeConfig, Component, Product, TargetConfig
from agent_config_bridge.planner import CommandHint, SyncPlan, build_plan
from agent_config_bridge.platforms import UnsupportedPlatformError, current_platform, scope_product_home_environment
from agent_config_bridge.renderer import RenderError, render_marketplace
from agent_config_bridge.retention import (
    RetentionAction,
    RetentionError,
    RetentionPlan,
    apply_retention_plan,
    build_retention_plan,
)
from agent_config_bridge.schedule_runner import ScheduleRunnerError, run_due_schedules, run_named_schedule
from agent_config_bridge.schedule_store import ScheduleStoreError
from agent_config_bridge.scheduler_backends import ScheduleBackendError
from agent_config_bridge.scheduler_registration import (
    SchedulerRegistrationError,
    SchedulerRegistrationPlan,
    apply_scheduler_registrations,
    build_scheduler_registration,
    resolve_vendor_executable,
    validate_vendor_executable,
)
from agent_config_bridge.schedules import ScheduleError, ScheduleExecutionError
from agent_config_bridge.settings import SettingsError
from agent_config_bridge.skill_migration import (
    MigrationError,
    MigrationSource,
    apply_skill_migration,
    build_skill_migration_plan,
    migration_report_json,
    write_migration_reports,
)
from agent_config_bridge.state import (
    BridgeStateError,
    desired_plugin_names,
    find_orphaned_target_states,
    read_registration_state,
    read_scheduler_state,
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
        if args.command == "migrate-skills":
            return _command_migrate_skills(args)
        if args.command == "schedule":
            config = load_config(Path(args.config))
            return _command_schedule(config, args)
        if args.command == "state":
            config = load_config(Path(args.config))
            return _command_state(config, args)
        if args.command == "instructions":
            config = load_config(Path(args.config))
            return _command_instructions(config, args.instructions_command, args.json)
        config, inventory = _load_context(Path(args.config))
        if args.command == "registry":
            return _command_registry(inventory, args.registry_command, args.json)
        if args.command == "validate":
            return _command_validate(inventory, args.json)
        if args.command == "render":
            return _command_render(config, inventory, args.json)
        if args.command == "doctor":
            # Doctor must stay usable when required-mode governance errors
            # block planning; the governance.mode check reports the cause.
            try:
                plan = build_plan(config, inventory)
            except GovernanceError:
                plan = SyncPlan(actions=(), commands=(), reviews=(), warnings=())
            return _command_doctor(config, inventory, plan, args.json)
        plan = build_plan(config, inventory)
        if args.command == "plan":
            return _command_plan(plan, args.json)
        if args.command == "apply":
            return _command_apply(config, inventory, plan, args.yes, args.json)
        if args.command == "sync-skills":
            return _command_sync_skills(config, inventory, plan, args.yes, args.json)
        if args.command == "register":
            return _command_register(config, inventory, plan, tuple(args.target), args.yes)
        parser.error(f"unknown command: {args.command}")
    except (
        ApplyError,
        CatalogError,
        ConfigError,
        FilesystemError,
        GovernanceError,
        InstructionProfileError,
        RenderError,
        BridgeStateError,
        ScheduleBackendError,
        SchedulerRegistrationError,
        ScheduleRunnerError,
        ScheduleStoreError,
        ScheduleError,
        ScheduleExecutionError,
        SettingsError,
        MigrationError,
        RetentionError,
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
        description="Share canonical agent settings and automations across Codex and Claude Code.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version('agent-config-bridge')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a starter config and empty catalog")
    init_parser.add_argument("--config", default=_DEFAULT_CONFIG)
    init_parser.add_argument("--force", action="store_true", help="replace only the generated config file")

    migration_parser = subparsers.add_parser(
        "migrate-skills",
        help="import existing user Skill roots into one canonical catalog",
    )
    migration_parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="source root in priority order; repeat for every discovery root",
    )
    migration_parser.add_argument("--catalog", required=True, type=Path, help="canonical catalog directory")
    migration_parser.add_argument(
        "--conflicts",
        required=True,
        type=Path,
        help="private directory that retains divergent variants",
    )
    migration_parser.add_argument(
        "--report",
        required=True,
        type=Path,
        help="HADS .md report outside every source, catalog, and conflict store",
    )
    migration_parser.add_argument(
        "--repair-legacy-frontmatter",
        action="store_true",
        help="add minimal name/description frontmatter only to migrated copies that have none",
    )
    migration_parser.add_argument("--json", action="store_true", help="emit the content-free migration report")
    migration_parser.add_argument("-y", "--yes", action="store_true", help="confirm catalog and report writes")

    schedule_parser = subparsers.add_parser("schedule", help="run a rendered host-managed schedule")
    schedule_subparsers = schedule_parser.add_subparsers(dest="schedule_command", required=True)
    tick_parser = schedule_subparsers.add_parser("tick", help="run schedules due in the current minute")
    tick_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
    tick_parser.add_argument("--target", required=True)
    tick_parser.add_argument("--vendor-executable", required=True, type=Path, help=argparse.SUPPRESS)
    run_parser = schedule_subparsers.add_parser("run", help="run one rendered schedule immediately")
    run_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
    run_parser.add_argument("--target", required=True)
    run_parser.add_argument("--name", required=True)

    state_parser = subparsers.add_parser("state", help="inspect or prune Bridge-generated operational state")
    state_subparsers = state_parser.add_subparsers(dest="state_command", required=True)
    prune_parser = state_subparsers.add_parser(
        "prune",
        help=("plan bounded generated-state retention; --yes validates no-change plans and fails closed for actions"),
    )
    prune_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
    prune_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    prune_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="validate a reviewed no-change plan; action-bearing plans fail closed",
    )

    instructions_parser = subparsers.add_parser(
        "instructions",
        help="generate or drift-check declared Codex instruction profiles",
    )
    instructions_subparsers = instructions_parser.add_subparsers(
        dest="instructions_command",
        required=True,
    )
    for instructions_command, help_text in (
        ("generate", "atomically generate declared developer-instruction-only Codex profiles"),
        ("check", "strictly byte-compare declared Codex profiles without writing"),
    ):
        instructions_command_parser = instructions_subparsers.add_parser(
            instructions_command,
            help=help_text,
        )
        instructions_command_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
        instructions_command_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    for command, help_text in (
        ("validate", "validate config and catalog structure"),
        ("plan", "show a read-only synchronization plan"),
        ("doctor", "diagnose target discovery and configuration"),
        ("render", "build the immutable dual plugin marketplace"),
        (
            "apply",
            "reconcile Skills, Instructions, Settings, Plugin/Hook marketplace builds, and Schedule snapshots",
        ),
        ("sync-skills", "apply only reviewed standalone Skill changes"),
        (
            "register",
            "reconcile product Plugin/Hook registrations and host scheduler heartbeats",
        ),
    ):
        command_parser = subparsers.add_parser(command, help=help_text, description=help_text)
        command_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
        if command in {"validate", "plan", "doctor", "render", "apply", "sync-skills"}:
            command_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        if command in {"apply", "sync-skills", "register"}:
            command_parser.add_argument("-y", "--yes", action="store_true", help="confirm state-changing actions")
        if command == "register":
            command_parser.add_argument(
                "--target",
                action="append",
                default=[],
                help="register one configured target; repeat to select multiple",
            )

    registry_parser = subparsers.add_parser(
        "registry",
        help="generate or drift-check the governed capability registry",
    )
    registry_subparsers = registry_parser.add_subparsers(dest="registry_command", required=True)
    for registry_command, help_text in (
        ("generate", "regenerate catalog/registry.json from governance manifests"),
        ("check", "verify manifests and byte-compare the committed registry snapshot"),
    ):
        registry_command_parser = registry_subparsers.add_parser(registry_command, help=help_text)
        registry_command_parser.add_argument("-c", "--config", default=_DEFAULT_CONFIG)
        registry_command_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _load_context(config_path: Path) -> tuple[BridgeConfig, CatalogInventory]:
    config = load_config(config_path)
    inventory = discover_catalog(config)
    return config, inventory


def _command_registry(inventory: CatalogInventory, registry_command: str, as_json: bool) -> int:
    report = run_governance(inventory)
    payload = build_registry_payload(report.manifests, inventory)
    blob = serialize_registry(payload)
    findings = list(report.findings)
    snapshot = registry_path(inventory)

    wrote = False
    if registry_command == "check":
        committed = snapshot.read_bytes() if snapshot.is_file() and not snapshot.is_symlink() else b""
        if committed != blob:
            findings.append(
                GovernanceFinding(
                    "GOV050",
                    GovernanceSeverity.ERROR,
                    detail=f"registry snapshot drift: {snapshot} does not match the regenerated payload",
                )
            )
    elif not report.has_error:
        if snapshot.is_symlink():
            raise GovernanceError(f"refusing to write registry through a symlink: {snapshot}")
        temporary = snapshot.with_name(f".{snapshot.name}.tmp")
        try:
            temporary.write_bytes(blob)
            os.replace(temporary, snapshot)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise GovernanceError(f"could not write registry snapshot: {snapshot}: {exc}") from exc
        wrote = True

    errors = [finding for finding in findings if finding.severity is GovernanceSeverity.ERROR]
    warnings = [finding for finding in findings if finding.severity is GovernanceSeverity.WARNING]
    if as_json:
        _print_json(
            {
                "mode": report.mode.value,
                "manifests": len(report.manifests),
                "capabilities": len(payload["capabilities"]),
                "registry": str(snapshot),
                "written": wrote,
                "findings": [asdict(finding) for finding in findings],
            }
        )
    else:
        print(
            f"mode={report.mode.value}  manifests={len(report.manifests)}  "
            f"capabilities={len(payload['capabilities'])}  errors={len(errors)}  warnings={len(warnings)}"
        )
        for finding in sorted(errors, key=lambda item: (item.code, item.artifact_ref or "", item.capability_id or "")):
            print(f"  ERROR   {finding.code} {finding.capability_id or finding.artifact_ref or '-'}: {finding.detail}")
        coverage = [finding for finding in warnings if finding.code == "GOV030"]
        for finding in sorted(
            (finding for finding in warnings if finding.code != "GOV030"),
            key=lambda item: (item.code, item.artifact_ref or ""),
        ):
            print(f"  WARN    {finding.code} {finding.artifact_ref or finding.capability_id or '-'}: {finding.detail}")
        if coverage:
            print(
                f"  WARN    GOV030 x{len(coverage)}: artifacts without a governance manifest ({report.mode.value} mode)"
            )
        if registry_command == "check":
            drifted = any(finding.code == "GOV050" for finding in errors)
            print(f"registry check: {'DRIFT' if drifted else 'committed snapshot matches'} ({snapshot})")
        elif wrote:
            print(f"wrote registry: {snapshot} ({len(blob)} bytes)")
        else:
            print("registry not written: resolve manifest errors first")
    return 1 if errors else 0


def _command_instructions(
    config: BridgeConfig,
    instructions_command: str,
    as_json: bool,
) -> int:
    if instructions_command == "generate":
        report = generate_instruction_profiles(config.catalog)
    elif instructions_command == "check":
        report = check_instruction_profiles(config.catalog)
    else:  # pragma: no cover - argparse constrains this value
        raise InstructionProfileError(f"unknown instructions command: {instructions_command}")

    payload = _instruction_profile_payload(report, instructions_command)
    if as_json:
        _print_json(payload)
    else:
        state = "CURRENT" if report.valid else "DRIFT"
        print(
            f"instructions {instructions_command}: {state}  profiles={len(report.profiles)}  "
            f"changed={report.changed}  catalog={report.catalog}"
        )
        for profile in report.profiles:
            print(f"  {profile.status.upper():7} {profile.bundle}:{profile.destination} <- {profile.source}")
    return 0 if report.valid else 1


def _instruction_profile_payload(
    report: InstructionProfileReport,
    command: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "catalog": str(report.catalog),
        "profiles": [asdict(profile) for profile in report.profiles],
        "changed": report.changed,
        "valid": report.valid,
    }


def _command_migrate_skills(args: argparse.Namespace) -> int:
    sources = tuple(_parse_migration_source(value) for value in args.source)
    plan = build_skill_migration_plan(
        sources,
        catalog=args.catalog,
        conflicts=args.conflicts,
        report=args.report,
        repair_legacy_frontmatter=args.repair_legacy_frontmatter,
    )
    payload = migration_report_json(plan)
    if not args.json:
        summary = payload["summary"]
        assert isinstance(summary, dict)
        print(
            "migration plan: "
            f"{summary['skill_names']} names, {summary['create']} create, "
            f"{summary['unchanged']} unchanged, {summary['conflict']} conflict, "
            f"{summary['blocked']} blocked"
        )
        for decision in plan.decisions:
            if decision.disposition.value in {"conflict", "blocked"}:
                print(f"{decision.disposition.value.upper():8} {decision.name}: {decision.detail}")

    pending = any(decision.disposition.value in {"create", "conflict"} for decision in plan.decisions)
    if not pending:
        reports: tuple[Path, Path] | None = None
        if args.yes:
            reports = write_migration_reports(plan)
            if not args.json:
                print(f"wrote migration reports: {reports[0]}, {reports[1]}")
        return _finish_migration_command(
            payload,
            json_output=args.json,
            applied=False,
            reports=reports,
            exit_code=1 if plan.has_blocked else 0,
        )
    if not args.yes:
        if args.json:
            return _finish_migration_command(
                payload,
                json_output=True,
                applied=False,
                reports=None,
                exit_code=1,
            )
        print("migration not applied; rerun with --yes after reviewing the plan")
        return 1

    apply_skill_migration(plan)
    reports = write_migration_reports(plan)
    if not args.json:
        print(f"migrated canonical Skills to {plan.catalog / 'skills'}")
        if plan.has_conflicts:
            print(f"retained divergent variants below {plan.conflicts}")
        print(f"wrote migration reports: {reports[0]}, {reports[1]}")
    return _finish_migration_command(
        payload,
        json_output=args.json,
        applied=True,
        reports=reports,
        exit_code=1 if plan.has_conflicts or plan.has_blocked else 0,
    )


def _finish_migration_command(
    payload: dict[str, object],
    *,
    json_output: bool,
    applied: bool,
    reports: tuple[Path, Path] | None,
    exit_code: int,
) -> int:
    payload["execution"] = {
        "applied": applied,
        "reports_written": reports is not None,
        "markdown_report": str(reports[0]) if reports else None,
        "json_report": str(reports[1]) if reports else None,
    }
    if json_output:
        _print_json(payload)
    return exit_code


def _parse_migration_source(value: str) -> MigrationSource:
    label, separator, raw_path = value.partition("=")
    if not separator or not label or not raw_path:
        raise MigrationError(f"migration source must use LABEL=PATH: {value!r}")
    return MigrationSource(label=label, root=Path(raw_path).expanduser().absolute())


def _command_schedule(config: BridgeConfig, args: argparse.Namespace) -> int:
    targets = {target.name: target for target in config.targets if target.enabled}
    target = targets.get(args.target)
    if target is None:
        raise ConfigError(f"unknown or disabled target: {args.target!r}")
    host_platform = current_platform()
    if target.platform is not host_platform:
        raise ConfigError(
            f"target {target.name!r} uses {target.platform.value}; "
            f"run its schedule from that platform, not {host_platform.value}"
        )
    if Component.SCHEDULES not in target.components:
        print(f"schedule target {target.name!r} is deselected; nothing to run")
        return 0

    if args.schedule_command == "tick":
        vendor_executable = validate_vendor_executable(target, args.vendor_executable)
        result = run_due_schedules(config, target, vendor_executable=vendor_executable)
    elif args.schedule_command == "run":
        result = run_named_schedule(
            config,
            target,
            args.name,
            vendor_executable=resolve_vendor_executable(target),
        )
    else:  # pragma: no cover - argparse restricts the nested command
        raise ConfigError(f"unknown schedule command: {args.schedule_command}")

    if result.skipped_reason:
        print(f"schedule tick skipped for {target.name}: {result.skipped_reason}")
        return 0
    failed = False
    for run in result.runs:
        if run.skipped_reason:
            print(f"schedule skipped: {target.name}/{run.name}: {run.skipped_reason}")
        elif run.succeeded:
            print(f"schedule completed: {target.name}/{run.name}")
        else:
            failed = True
            print(f"schedule failed: {target.name}/{run.name}: {run.error}", file=sys.stderr)
    return 1 if failed else 0


def _command_state(config: BridgeConfig, args: argparse.Namespace) -> int:
    if args.state_command != "prune":  # pragma: no cover - argparse restricts the nested command
        raise ConfigError(f"unknown state command: {args.state_command}")

    plan = build_retention_plan(config)
    if plan.has_blockers:
        payload = _retention_payload(plan, applied=False)
        if args.json:
            _print_json(payload)
        else:
            _print_retention_plan(plan)
        return 1

    if args.yes:
        result = apply_retention_plan(config, plan)
        payload = _retention_payload(
            result.final_plan,
            applied=True,
            reviewed_plan=plan,
            deleted=result.deleted,
            reclaimed_bytes=result.reclaimed_bytes,
        )
        if args.json:
            _print_json(payload)
        else:
            print("retention validation completed: the reviewed no-change plan remains current")
        return 0

    payload = _retention_payload(plan, applied=False)
    if args.json:
        _print_json(payload)
    else:
        _print_retention_plan(plan)
    return 0


def _retention_payload(
    plan: RetentionPlan,
    *,
    applied: bool,
    reviewed_plan: RetentionPlan | None = None,
    deleted: Sequence[RetentionAction] = (),
    reclaimed_bytes: int | None = None,
) -> dict[str, Any]:
    source_plan = reviewed_plan or plan
    return {
        "schema_version": 1,
        "applied": applied,
        "safe": not source_plan.has_blockers,
        "has_changes": source_plan.has_changes,
        "has_blockers": source_plan.has_blockers,
        "limits": asdict(source_plan.limits),
        "marketplace_builds": {
            "count": source_plan.build_count,
            "bytes": source_plan.build_bytes,
        },
        "skill_backups": {
            "group_count": source_plan.skill_backup_group_count,
            "snapshot_count": source_plan.skill_backup_snapshot_count,
            "bytes": source_plan.skill_backup_bytes,
        },
        "reclaimable_bytes": source_plan.reclaimable_bytes,
        "actions": [asdict(action) for action in source_plan.actions],
        "blockers": [asdict(blocker) for blocker in source_plan.blockers],
        "excluded_instruction_roots": list(source_plan.excluded_instruction_roots),
        "deleted": [asdict(item) for item in deleted],
        "reclaimed_bytes": reclaimed_bytes if reclaimed_bytes is not None else 0,
        "converged": not plan.has_changes and not plan.has_blockers,
    }


def _print_retention_plan(plan: RetentionPlan) -> None:
    print(
        "retention plan: "
        f"{plan.build_count} marketplace builds, "
        f"{plan.skill_backup_snapshot_count} Skill backups, "
        f"{len(plan.actions)} deletion candidates, {len(plan.blockers)} blockers"
    )
    for blocker in plan.blockers:
        print(f"  BLOCKED {blocker.path}: {blocker.reason}")
    for action in plan.actions:
        print(f"  CANDIDATE  {action.category}: {action.path}")
    if plan.has_changes:
        print("review only; automated deletion is disabled, and --yes fails closed when the plan contains actions")


def _command_init(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    if config_path.exists() and not args.force:
        raise ConfigError(f"config already exists (use --force to replace it): {config_path}")

    catalog = config_path.parent / "catalog"
    for group in ("skills", "plugins", "hooks", "settings", "schedules", "instructions"):
        (catalog / group).mkdir(parents=True, exist_ok=True)
    template = """schema_version = 1

[bridge]
catalog = "./catalog"
state_dir = "./.agentbridge"
link_mode = "auto"
components = ["skills", "plugins", "hooks", "settings", "schedules", "instructions"]

[bridge.retention]
marketplace_builds = 20
skill_backups = 3

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
        "settings": len(inventory.settings),
        "schedules": len(inventory.schedules),
        "instructions": len(inventory.instructions),
        "valid": True,
    }
    if as_json:
        _print_json(payload)
    else:
        print(
            f"valid: {payload['skills']} skills, {payload['plugins']} plugins, "
            f"{payload['hooks']} hook bundles, {payload['settings']} settings bundles, "
            f"{payload['schedules']} schedules, and {payload['instructions']} instruction bundles "
            f"in {payload['catalog']}"
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
        "schedule_snapshots": [str(rendered.published_file) for rendered in result.schedules],
        "registration_commands": [_command_payload(command) for command in plan.commands],
        "warnings": list(result.warnings),
    }
    if as_json:
        _print_json(payload)
    else:
        print(f"applied {payload['applied']} actions")
        if result.marketplace:
            print(f"marketplace: {result.marketplace.root}")
        for rendered in result.schedules:
            print(f"schedule snapshot [{rendered.target}]: {rendered.published_file}")
        for backup in result.backups:
            print(f"backup: {backup}")
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if plan.commands:
            print("plugin and schedule registration remains explicit; review and run:")
            for command in plan.commands:
                print(f"  [{command.target}] {_format_command(command)}")
    return 0


def _command_sync_skills(
    config: BridgeConfig,
    inventory: CatalogInventory,
    plan: SyncPlan,
    confirmed: bool,
    as_json: bool,
) -> int:
    """Apply only Skill changes after validating the complete reviewed plan."""

    skill_actions = validate_skill_only_plan(plan)
    if not skill_actions:
        result = apply_skill_plan(config, inventory, plan)
        warnings = [*plan.warnings, *result.warnings]
        payload = {
            "scope": "skills",
            "skill_only": True,
            "applied": 0,
            "backups": [],
            "warnings": warnings,
            "no_op": True,
        }
        if as_json:
            _print_json(payload)
        else:
            print("skill-only sync: no Skill changes")
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
        return 0
    if not _confirm("Apply this skill-only plan?", confirmed):
        print("cancelled", file=sys.stderr)
        return 2

    result = apply_skill_plan(config, inventory, plan)
    warnings = [*plan.warnings, *result.warnings]
    payload = {
        "scope": "skills",
        "skill_only": True,
        "applied": len(result.applied),
        "backups": [str(path) for path in result.backups],
        "warnings": warnings,
        "no_op": False,
    }
    if as_json:
        _print_json(payload)
    else:
        print(f"skill-only sync: applied {payload['applied']} Skill actions")
        for backup in result.backups:
            print(f"backup: {backup}")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
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
    for name in sorted(target_names):
        target = known_targets[name]
        target_commands = tuple(command for command in commands if command.target == name)
        _validate_registration_executable(target, target_commands)
    scheduler_registrations = _build_scheduler_registrations(
        config,
        inventory,
        tuple(known_targets[name] for name in sorted(target_names)),
    )
    if commands:
        print("product commands to execute:")
        for command in commands:
            print(f"  [{command.target}] {_format_command(command)}")
    if scheduler_registrations:
        print("host scheduler reconciliation:")
        for registration in scheduler_registrations:
            print(
                f"  [{registration.target.name}] {registration.plan.disposition.value.upper()} "
                f"{registration.plan.backend.value}: {registration.plan.detail}"
            )
            print(f"    agentbridge: {registration.spec.agentbridge_executable}")
            print(f"    product CLI: {registration.spec.vendor_executable}")
            print(f"    config: {registration.spec.config_path}")
    conflicts = tuple(registration for registration in scheduler_registrations if registration.has_conflict)
    if conflicts:
        raise ConfigError(
            "resolve host scheduler conflicts before registering: "
            + ", ".join(registration.target.name for registration in conflicts)
        )
    has_scheduler_changes = any(registration.has_changes for registration in scheduler_registrations)
    if not commands and not has_scheduler_changes:
        print("no plugin or host scheduler registration changes are required")
        return 0
    if not _confirm("Execute these product and host scheduler changes?", confirmed):
        print("cancelled", file=sys.stderr)
        return 2

    fresh_inventory = discover_catalog(config)
    # One governance read for the whole registration flow: the staleness
    # check, the render, and the ownership write must all reflect the same
    # resolved state, or a governance edit landing mid-register desyncs the
    # recorded plugin set from what was verified (the apply_plan TOCTOU class).
    fresh_resolved = resolve_inventory(fresh_inventory)
    fresh_plan = build_plan(config, fresh_inventory, resolved=fresh_resolved)
    if fresh_plan != plan:
        raise ConfigError("catalog, generated state, or destinations changed; review a fresh plan")
    fresh_scheduler_registrations = _build_scheduler_registrations(
        config,
        fresh_inventory,
        tuple(known_targets[name] for name in sorted(target_names)),
        reviewed=scheduler_registrations,
    )
    if _scheduler_review_keys(fresh_scheduler_registrations) != _scheduler_review_keys(scheduler_registrations):
        raise ConfigError("host scheduler state changed; review a fresh registration plan")
    if commands:
        marketplace_action = next(
            (action for action in fresh_plan.actions if action.target == "marketplace"),
            None,
        )
        if marketplace_action is not None:
            if marketplace_action.source_digest is None:
                raise ConfigError("registration plan is missing its reviewed marketplace source digest")
            render_marketplace(
                config,
                fresh_inventory,
                resolved=fresh_resolved,
                expected_digest=marketplace_action.source_digest,
            )

    commands_by_target: dict[str, tuple[CommandHint, ...]] = {}
    for name in sorted(target_names):
        target = known_targets[name]
        target_commands = tuple(command for command in commands if command.target == name)
        commands_by_target[name] = target_commands
        environment = os.environ.copy()
        scope_product_home_environment(environment, target)
        if target_commands:
            _scope_command_environment(environment, target_commands[0])
            _preflight_registration_ownership(config, target, target_commands, environment)

    for name in sorted(target_names):
        target = known_targets[name]
        target_commands = commands_by_target[name]
        for command in target_commands:
            command_environment = os.environ.copy()
            scope_product_home_environment(command_environment, target)
            _scope_command_environment(command_environment, command)
            _run_registration_command(command, command_environment, target.product)
        write_registered_plugins(
            config, target, desired_plugin_names(target, fresh_inventory, fresh_resolved.hooks_for_target(target))
        )
    apply_scheduler_registrations(config, fresh_inventory, fresh_scheduler_registrations)
    return 0


def _build_scheduler_registrations(
    config: BridgeConfig,
    inventory: CatalogInventory,
    targets: tuple[TargetConfig, ...],
    *,
    reviewed: tuple[SchedulerRegistrationPlan, ...] = (),
) -> tuple[SchedulerRegistrationPlan, ...]:
    reviewed_by_target = {item.target.name: item for item in reviewed}
    registrations: list[SchedulerRegistrationPlan] = []
    for target in targets:
        state = read_scheduler_state(config, target)
        if Component.SCHEDULES not in target.components and state is None:
            continue
        previous = reviewed_by_target.get(target.name)
        registrations.append(
            build_scheduler_registration(
                config,
                inventory,
                target,
                executable=previous.spec.agentbridge_executable if previous is not None else None,
                vendor_executable=previous.spec.vendor_executable if previous is not None else None,
            )
        )
    return tuple(registrations)


def _scheduler_review_keys(registrations: tuple[SchedulerRegistrationPlan, ...]) -> tuple[object, ...]:
    return tuple(
        (
            registration.target.name,
            registration.spec,
            registration.plan,
            registration.desired,
            registration.previous_state,
        )
        for registration in registrations
    )


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

    executable = _registration_command_executable(target, commands)
    actual_source = _registered_marketplace_source(target, environment, executable)
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
    executable: str,
) -> str | None:
    """Return the physical source for the bridge-named product marketplace."""

    try:
        return probe_marketplace_source(target, executable, environment)
    except MarketplaceRegistryError as exc:
        raise ConfigError(
            f"target {target.name!r}: could not verify marketplace ownership before registration: {exc}"
        ) from exc


def _run_registration_command(
    command: CommandHint,
    environment: dict[str, str],
    product: Product,
) -> None:
    try:
        subprocess.run(command.argv, check=True, env=environment)
    except subprocess.CalledProcessError:
        if _removal_is_already_satisfied(command, environment, product):
            return
        raise


def _removal_is_already_satisfied(
    command: CommandHint,
    environment: dict[str, str],
    product: Product,
) -> bool:
    argv = command.argv
    executable = argv[0]
    arguments = argv[1:]
    probe_argv: tuple[str, ...]
    removal_kind: str
    if product is Product.CLAUDE_CODE and arguments[:2] == ("plugin", "uninstall") and len(arguments) >= 3:
        probe_argv = (executable, "plugin", "list", "--json")
        expected_name = arguments[2]
        removal_kind = "plugin"

    elif arguments[:3] == ("plugin", "marketplace", "remove") and len(arguments) == 4:
        probe_argv = (executable, "plugin", "marketplace", "list", "--json")
        expected_name = arguments[3]
        removal_kind = "codex-marketplace" if product is Product.CODEX else "marketplace"

    else:
        return False

    try:
        payload = run_utf8_json_command(
            probe_argv,
            environment,
        )
    except MarketplaceRegistryError:
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


def _registration_command_executable(target: TargetConfig, commands: tuple[CommandHint, ...]) -> str:
    """Return the single executable reviewed for one target's commands."""

    executables = {command.argv[0] for command in commands}
    if len(executables) != 1:
        raise ConfigError(f"target {target.name!r}: registration commands do not use one product executable")
    return next(iter(executables))


def _validate_registration_executable(target: TargetConfig, commands: tuple[CommandHint, ...]) -> None:
    """Validate an explicit CLI override and the command plan that consumes it."""

    if not commands:
        return
    planned = _registration_command_executable(target, commands)
    default = "codex" if target.product is Product.CODEX else "claude"
    expected = str(validate_vendor_executable(target, target.executable)) if target.executable is not None else default
    if planned != expected:
        raise ConfigError(f"target {target.name!r}: registration command executable changed after planning")


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
        unsets = "; ".join(f"Remove-Item Env:{key} -ErrorAction SilentlyContinue" for key in command.environment_unsets)
        assignments = "; ".join(f"$env:{key} = {_powershell_quote(value)}" for key, value in command.environment)
        environment = "; ".join(part for part in (unsets, assignments) if part)
        argv = " ".join(_powershell_quote(value) for value in command.argv)
        return f"{environment}; & {argv}" if environment else f"& {argv}"
    if command.environment_unsets:
        environment_argv = ["env"]
        for key in command.environment_unsets:
            environment_argv.extend(("-u", key))
        environment_argv.extend(f"{key}={value}" for key, value in command.environment)
        return shlex.join((*environment_argv, *command.argv))
    environment = " ".join(f"{key}={shlex.quote(value)}" for key, value in command.environment)
    argv = shlex.join(command.argv)
    return f"{environment} {argv}".strip()


def _scope_command_environment(environment: dict[str, str], command: CommandHint) -> None:
    """Apply one planned environment delta to an internal subprocess scope."""

    for key in command.environment_unsets:
        environment.pop(key, None)
    environment.update(dict(command.environment))


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _plan_payload(plan: SyncPlan) -> dict[str, Any]:
    return {
        "schema_version": 1,
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
        "environment_unsets": list(command.environment_unsets),
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
