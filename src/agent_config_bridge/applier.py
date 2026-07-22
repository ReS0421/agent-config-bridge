"""Apply previously inspected synchronization plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_config_bridge.catalog import CatalogInventory, discover_catalog
from agent_config_bridge.filesystem import (
    apply_copy,
    apply_link,
    apply_remove,
    tree_digest,
)
from agent_config_bridge.governance import ResolvedInventory, resolve_inventory
from agent_config_bridge.instructions import (
    apply_instruction_copy,
    apply_instruction_link,
    apply_instruction_remove,
    instruction_digest,
    instruction_files,
    instruction_source_id,
)
from agent_config_bridge.models import BridgeConfig, Component, LinkMode, Platform, TargetConfig
from agent_config_bridge.planner import Action, Disposition, Operation, SyncPlan, build_plan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.provenance import write_instruction_dir_markers, write_skill_root_marker
from agent_config_bridge.renderer import RenderedMarketplace, render_marketplace
from agent_config_bridge.schedule_store import RenderedScheduleSet, remove_schedule_set, render_schedule_set
from agent_config_bridge.schedules import discover_schedules
from agent_config_bridge.settings import (
    OwnedSettingLeaf,
    apply_settings_patch,
    build_settings_patch,
    discover_settings_fragments,
    plan_settings_patch,
    settings_patch_digest,
)
from agent_config_bridge.state import (
    BridgeStateError,
    InstructionStateEntry,
    SettingsState,
    SettingStateEntry,
    SkillStateEntry,
    effective_link_mode,
    find_orphaned_target_states,
    read_instruction_state,
    read_settings_state,
    read_skill_state,
    write_instruction_state,
    write_settings_state,
    write_skill_state,
    write_skill_state_entries,
)

__all__ = ["ApplyError", "ApplyResult", "apply_plan", "apply_skill_plan", "validate_skill_only_plan"]


class ApplyError(RuntimeError):
    """Raised when a plan is unsafe or cannot be applied."""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Summary of applied actions, retained backups, and non-fatal warnings."""

    applied: tuple[Action, ...]
    backups: tuple[Path, ...]
    marketplace: RenderedMarketplace | None
    schedules: tuple[RenderedScheduleSet, ...]
    warnings: tuple[str, ...] = ()


def validate_skill_only_plan(plan: SyncPlan) -> tuple[Action, ...]:
    """Return changing Skill actions after enforcing the skill-only safety gate."""

    if plan.has_conflicts:
        conflicts = ", ".join(
            f"{action.target}:{action.name}" for action in plan.actions if action.disposition is Disposition.CONFLICT
        )
        raise ApplyError(f"refusing skill-only sync because the full plan has conflicts: {conflicts}")
    non_skill_changes = tuple(
        action
        for action in plan.actions
        if action.component is not Component.SKILLS
        and action.disposition in {Disposition.CREATE, Disposition.UPDATE, Disposition.REMOVE}
    )
    if non_skill_changes:
        pending = ", ".join(f"{action.component.value}:{action.target}:{action.name}" for action in non_skill_changes)
        raise ApplyError(f"refusing skill-only sync while non-skill changes are pending: {pending}")
    return tuple(
        action
        for action in plan.actions
        if action.component is Component.SKILLS
        and action.disposition in {Disposition.CREATE, Disposition.UPDATE, Disposition.REMOVE}
    )


def apply_skill_plan(config: BridgeConfig, inventory: CatalogInventory, plan: SyncPlan) -> ApplyResult:
    """Apply only Skill actions from a reviewed full synchronization plan.

    The complete plan is rebuilt and compared before any mutation. Conflicts in
    any component and pending changes outside Skills fail closed.
    """

    orphaned_targets = find_orphaned_target_states(config)
    if orphaned_targets:
        raise ApplyError(
            "refusing to sync Skills while ownership state has no enabled target: " + ", ".join(orphaned_targets)
        )

    fresh_inventory = discover_catalog(config)
    resolved = resolve_inventory(fresh_inventory)
    fresh_plan = build_plan(config, fresh_inventory, resolved=resolved)
    if fresh_plan != plan:
        raise ApplyError("catalog or destination state changed after planning; review a fresh plan")
    skill_actions = validate_skill_only_plan(fresh_plan)

    targets_by_name = {target.name: target for target in config.targets if target.enabled}
    skill_ownership = {
        name: {entry.name: entry for entry in read_skill_state(config, target)}
        for name, target in targets_by_name.items()
    }
    host_platform = current_platform()
    applied: list[Action] = []
    backups: list[Path] = []
    for action in skill_actions:
        target = targets_by_name.get(action.target)
        if target is None:
            raise ApplyError(f"Skill action has no enabled target: {action.target}:{action.name}")
        windows_path_semantics = host_platform is Platform.WINDOWS or target.platform is Platform.WINDOWS
        backup: Path | None = None
        previous_entries: dict[str, SkillStateEntry] | None = None
        if action.disposition is Disposition.CREATE:
            if action.source_id is None or action.source_digest is None:
                raise ApplyError(f"create action is missing ownership data: {action.target}:{action.name}")
            mode = LinkMode.SYMLINK if action.operation is Operation.LINK else LinkMode.COPY
            previous_entries = dict(skill_ownership[action.target])
            next_entries = dict(skill_ownership[action.target])
            next_entries[action.name] = SkillStateEntry(
                name=action.name,
                mode=mode,
                source_id=action.source_id,
                link_target=str(action.source.resolve()) if mode is LinkMode.SYMLINK else None,
            )
            try:
                write_skill_state_entries(config, target, tuple(next_entries.values()))
            except BridgeStateError as state_exc:
                raise ApplyError(
                    f"Skill ownership checkpoint failed before create; no filesystem mutation occurred: "
                    f"{action.target}:{action.name}: {state_exc}"
                ) from state_exc
            skill_ownership[action.target] = next_entries
        try:
            if action.operation is Operation.LINK:
                if action.source_digest is None or tree_digest(action.source) != action.source_digest:
                    raise ApplyError(f"link source changed after planning: {action.target}:{action.name}")
                apply_link(action.source, action.destination)
            elif action.operation is Operation.COPY:
                if action.source_id is None or action.source_digest is None:
                    raise ApplyError(f"copy action is missing source identity: {action.target}:{action.name}")
                backup = apply_copy(
                    action.source,
                    action.destination,
                    source_id=action.source_id,
                    source_digest=action.source_digest,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    update=action.disposition is Disposition.UPDATE,
                    previous_mode=action.link_mode or LinkMode.COPY,
                    expected_link_target=action.source if action.link_mode is LinkMode.SYMLINK else None,
                    windows_path_semantics=windows_path_semantics,
                )
            elif action.operation is Operation.REMOVE:
                if action.link_mode is None or action.source_id is None:
                    raise ApplyError(f"remove action is missing ownership data: {action.target}:{action.name}")
                backup = apply_remove(
                    action.destination,
                    mode=action.link_mode,
                    source_id=action.source_id,
                    expected_link_target=action.source if action.link_mode is LinkMode.SYMLINK else None,
                    installed_digest=action.source_digest,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    windows_path_semantics=windows_path_semantics,
                )
            else:
                raise ApplyError(f"unsupported skill-only operation: {action.operation.value}")
        except BaseException as apply_exc:
            if previous_entries is not None:
                try:
                    write_skill_state_entries(config, target, tuple(previous_entries.values()))
                except BridgeStateError as rollback_exc:
                    raise ApplyError(
                        f"Skill create failed and its durable ownership checkpoint could not be rolled back: "
                        f"{action.target}:{action.name}: {rollback_exc}"
                    ) from apply_exc
                skill_ownership[action.target] = previous_entries
            raise
        if backup is not None:
            backups.append(backup)
        applied.append(action)

    warnings: list[str] = []
    for target in config.targets:
        if not target.enabled:
            continue
        desired_skills = resolved.skills_for_target(target)
        write_skill_state(config, target, desired_skills)
        try:
            write_skill_root_marker(config, target, desired_skills)
        except BridgeStateError as exc:
            warnings.append(f"{target.name}: provenance marker was not updated: {exc}")

    return ApplyResult(
        applied=tuple(applied),
        backups=tuple(backups),
        marketplace=None,
        schedules=(),
        warnings=tuple(warnings),
    )


def apply_plan(config: BridgeConfig, inventory: CatalogInventory, plan: SyncPlan) -> ApplyResult:
    """Apply safe create/update actions from a read-only plan.

    Args:
        config: Loaded bridge configuration.
        inventory: Catalog inventory used to build the plan.
        plan: The plan to apply.

    Returns:
        Applied action, backup, and rendered marketplace metadata.

    Raises:
        ApplyError: If the plan contains any conflict or an action is malformed.
    """

    orphaned_targets = find_orphaned_target_states(config)
    if orphaned_targets:
        raise ApplyError(
            "refusing to apply while ownership state has no enabled target: " + ", ".join(orphaned_targets)
        )

    fresh_inventory = discover_catalog(config)
    # One governance read serves both plan verification and the ownership
    # writes below; independent re-reads would open a window where governance
    # edits during apply desync the recorded state from the applied actions.
    resolved = resolve_inventory(fresh_inventory)
    fresh_plan = build_plan(config, fresh_inventory, resolved=resolved)
    if fresh_plan != plan:
        raise ApplyError("catalog or destination state changed after planning; review a fresh plan")
    inventory = fresh_inventory

    if plan.has_conflicts:
        conflicts = ", ".join(
            f"{action.target}:{action.name}" for action in plan.actions if action.disposition is Disposition.CONFLICT
        )
        raise ApplyError(f"refusing to apply a plan with conflicts: {conflicts}")

    applied: list[Action] = []
    backups: list[Path] = []
    marketplace: RenderedMarketplace | None = None
    schedules: list[RenderedScheduleSet] = []
    targets_by_name = {target.name: target for target in config.targets if target.enabled}
    host_platform = current_platform()
    # Read once before any removal so update digests and stale-marker cleanup
    # reflect the pre-apply ownership, not a partially applied state.
    previous_instructions_by_target = {
        name: read_instruction_state(config, target) for name, target in targets_by_name.items()
    }
    schedule_catalog = (
        discover_schedules(config)
        if any(
            action.operation is Operation.RENDER and action.component is Component.SCHEDULES for action in plan.actions
        )
        else None
    )
    for action in plan.actions:
        if action.disposition is Disposition.NOOP:
            if action.operation is Operation.PATCH and action.component is Component.SETTINGS:
                target = targets_by_name.get(action.target)
                if target is None:
                    raise ApplyError(f"settings action has no enabled target: {action.target}")
                if action.source_digest is None:
                    raise ApplyError(f"settings action has no source digest: {action.target}")
                _apply_target_settings(config, inventory, target, action.source_digest)
            elif action.operation is Operation.RENDER:
                if action.component is Component.SCHEDULES:
                    target = targets_by_name.get(action.target)
                    if target is None:
                        raise ApplyError(f"schedule action has no enabled target: {action.target}")
                    if schedule_catalog is None or action.source_digest is None:
                        raise ApplyError(f"schedule action is missing reviewed source data: {action.target}")
                    schedules.append(
                        render_schedule_set(
                            config,
                            schedule_catalog,
                            target,
                            expected_digest=action.source_digest,
                        )
                    )
                else:
                    marketplace = render_marketplace(config, inventory, resolved=resolved)
            continue
        if action.disposition not in {Disposition.CREATE, Disposition.UPDATE, Disposition.REMOVE}:
            continue

        if action.operation is Operation.RENDER:
            if action.component is Component.SCHEDULES:
                target = targets_by_name.get(action.target)
                if target is None:
                    raise ApplyError(f"schedule action has no enabled target: {action.target}")
                if schedule_catalog is None or action.source_digest is None:
                    raise ApplyError(f"schedule action is missing reviewed source data: {action.target}")
                schedules.append(
                    render_schedule_set(
                        config,
                        schedule_catalog,
                        target,
                        expected_digest=action.source_digest,
                    )
                )
            else:
                marketplace = render_marketplace(config, inventory, resolved=resolved)
        elif action.operation is Operation.PATCH:
            if action.component is not Component.SETTINGS:
                raise ApplyError(f"unsupported patch component: {action.component.value}")
            target = targets_by_name.get(action.target)
            if target is None:
                raise ApplyError(f"settings action has no enabled target: {action.target}")
            if action.source_digest is None:
                raise ApplyError(f"settings action has no source digest: {action.target}")
            _apply_target_settings(config, inventory, target, action.source_digest)
        elif action.operation is Operation.LINK:
            if action.component is Component.INSTRUCTIONS:
                if action.source_digest is None or instruction_digest(action.source) != action.source_digest:
                    raise ApplyError(f"link source changed after planning: {action.target}:{action.name}")
                apply_instruction_link(action.source, action.destination)
            else:
                if action.source_digest is None or tree_digest(action.source) != action.source_digest:
                    raise ApplyError(f"link source changed after planning: {action.target}:{action.name}")
                apply_link(action.source, action.destination)
        elif action.operation is Operation.COPY:
            if action.source_id is None or action.source_digest is None:
                raise ApplyError(f"copy action is missing source identity: {action.target}:{action.name}")
            if action.component is Component.INSTRUCTIONS:
                previous_entry = next(
                    (
                        entry
                        for entry in previous_instructions_by_target.get(action.target, ())
                        if entry.relpath == action.name
                    ),
                    None,
                )
                backup = apply_instruction_copy(
                    action.source,
                    action.destination,
                    source_digest=action.source_digest,
                    installed_digest=previous_entry.installed_digest if previous_entry is not None else None,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    relpath=action.name,
                    update=action.disposition is Disposition.UPDATE,
                )
            else:
                backup = apply_copy(
                    action.source,
                    action.destination,
                    source_id=action.source_id,
                    source_digest=action.source_digest,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    update=action.disposition is Disposition.UPDATE,
                    previous_mode=action.link_mode or LinkMode.COPY,
                    expected_link_target=action.source if action.link_mode is LinkMode.SYMLINK else None,
                    windows_path_semantics=(
                        host_platform is Platform.WINDOWS or targets_by_name[action.target].platform is Platform.WINDOWS
                    ),
                )
            if backup is not None:
                backups.append(backup)
        elif action.operation is Operation.REMOVE:
            if action.component is Component.SCHEDULES:
                target = targets_by_name.get(action.target)
                if target is None:
                    raise ApplyError(f"schedule action has no enabled target: {action.target}")
                remove_schedule_set(config, target)
                applied.append(action)
                continue
            if action.link_mode is None or action.source_id is None:
                raise ApplyError(f"remove action is missing ownership data: {action.target}:{action.name}")
            target = targets_by_name.get(action.target)
            if target is None:
                raise ApplyError(f"remove action has no enabled target: {action.target}:{action.name}")
            windows_path_semantics = host_platform is Platform.WINDOWS or target.platform is Platform.WINDOWS
            if action.component is Component.INSTRUCTIONS:
                backup = apply_instruction_remove(
                    action.destination,
                    mode=action.link_mode,
                    expected_link_target=action.source if action.link_mode is LinkMode.SYMLINK else None,
                    installed_digest=action.source_digest,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    relpath=action.name,
                    windows_path_semantics=windows_path_semantics,
                )
            else:
                backup = apply_remove(
                    action.destination,
                    mode=action.link_mode,
                    source_id=action.source_id,
                    expected_link_target=action.source if action.link_mode.value == "symlink" else None,
                    installed_digest=action.source_digest,
                    state_dir=config.state_dir,
                    target_name=action.target,
                    windows_path_semantics=windows_path_semantics,
                )
            if backup is not None:
                backups.append(backup)
        else:  # pragma: no cover - guarded by the Operation enum
            raise ApplyError(f"unsupported plan operation: {action.operation}")
        applied.append(action)

    warnings: list[str] = []
    for target in config.targets:
        if target.enabled:
            desired_skills = resolved.skills_for_target(target)
            write_skill_state(config, target, desired_skills)
            desired_instructions = _desired_instruction_entries(config, target, resolved)
            write_instruction_state(config, target, desired_instructions)
            # The marker is legibility metadata, not sync state: a squatted or
            # unwritable marker path must not abort an apply whose real actions
            # and ownership records already succeeded.
            try:
                write_skill_root_marker(config, target, desired_skills)
            except BridgeStateError as exc:
                warnings.append(f"{target.name}: provenance marker was not updated: {exc}")
            try:
                write_instruction_dir_markers(
                    config,
                    target,
                    tuple(entry.relpath for entry in desired_instructions),
                    tuple(entry.relpath for entry in previous_instructions_by_target.get(target.name, ())),
                )
            except BridgeStateError as exc:
                warnings.append(f"{target.name}: provenance marker was not updated: {exc}")

    return ApplyResult(
        applied=tuple(applied),
        backups=tuple(backups),
        marketplace=marketplace,
        schedules=tuple(schedules),
        warnings=tuple(warnings),
    )


def _desired_instruction_entries(
    config: BridgeConfig,
    target: TargetConfig,
    resolved: ResolvedInventory,
) -> tuple[InstructionStateEntry, ...]:
    """Build the governance-gated instruction ownership entries for one target."""

    if Component.INSTRUCTIONS not in target.components:
        return ()
    mode = effective_link_mode(config.link_mode, target.platform)
    entries: list[InstructionStateEntry] = []
    for bundle in resolved.instructions_for_target(target):
        for instruction in instruction_files(bundle, target.product):
            entries.append(
                InstructionStateEntry(
                    relpath=instruction.relpath,
                    mode=mode,
                    source_id=instruction_source_id(instruction.bundle),
                    link_target=str(instruction.source.resolve()) if mode is LinkMode.SYMLINK else None,
                    installed_digest=instruction_digest(instruction.source) if mode is LinkMode.COPY else None,
                )
            )
    return tuple(entries)


def _apply_target_settings(
    config: BridgeConfig,
    inventory: CatalogInventory,
    target: TargetConfig,
    expected_source_digest: str,
) -> None:
    previous_state = read_settings_state(config, target)
    previous = tuple(
        OwnedSettingLeaf(
            source_id=entry.source_id,
            path=entry.path,
            digest=entry.value_digest,
            created_parents=tuple(
                parent
                for parent in previous_state.created_containers
                if len(parent) < len(entry.path) and entry.path[: len(parent)] == parent
            ),
        )
        for entry in previous_state.entries
    )
    fragments = discover_settings_fragments(inventory.root)
    desired = build_settings_patch(target.product, fragments) if Component.SETTINGS in target.components else ()
    if settings_patch_digest(desired) != expected_source_digest:
        raise ApplyError(f"settings sources changed after planning: {target.name}")
    filename = "config.toml" if target.product.value == "codex" else "settings.json"
    settings_plan = plan_settings_patch(target.product, target.config_home / filename, desired, previous)
    result = apply_settings_patch(settings_plan)

    containers = tuple(
        sorted(
            {parent for leaf in result.ownership for parent in leaf.created_parents},
            key=lambda path: (len(path), path),
        )
    )
    entries = tuple(
        SettingStateEntry(source_id=leaf.source_id, path=leaf.path, value_digest=leaf.digest)
        for leaf in result.ownership
    )
    next_state = SettingsState(
        file_created=bool(entries) and (previous_state.file_created or not settings_plan.destination_existed),
        created_containers=containers,
        entries=entries,
    )
    if next_state != previous_state:
        write_settings_state(config, target, next_state)
