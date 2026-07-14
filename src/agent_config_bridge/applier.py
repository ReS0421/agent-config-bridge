"""Apply previously inspected synchronization plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_config_bridge.catalog import CatalogInventory, discover_catalog
from agent_config_bridge.filesystem import apply_copy, apply_link, apply_remove, tree_digest
from agent_config_bridge.models import BridgeConfig
from agent_config_bridge.planner import Action, Disposition, Operation, SyncPlan, build_plan
from agent_config_bridge.renderer import RenderedMarketplace, render_marketplace
from agent_config_bridge.state import find_orphaned_target_states, write_skill_state

__all__ = ["ApplyError", "ApplyResult", "apply_plan"]


class ApplyError(RuntimeError):
    """Raised when a plan is unsafe or cannot be applied."""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Summary of applied actions and retained backups."""

    applied: tuple[Action, ...]
    backups: tuple[Path, ...]
    marketplace: RenderedMarketplace | None


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
    fresh_plan = build_plan(config, fresh_inventory)
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
    for action in plan.actions:
        if action.disposition is Disposition.NOOP:
            if action.operation is Operation.RENDER:
                marketplace = render_marketplace(config, inventory)
            continue
        if action.disposition not in {Disposition.CREATE, Disposition.UPDATE, Disposition.REMOVE}:
            continue

        if action.operation is Operation.RENDER:
            marketplace = render_marketplace(config, inventory)
        elif action.operation is Operation.LINK:
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
            )
            if backup is not None:
                backups.append(backup)
        elif action.operation is Operation.REMOVE:
            if action.link_mode is None or action.source_id is None:
                raise ApplyError(f"remove action is missing ownership data: {action.target}:{action.name}")
            backup = apply_remove(
                action.destination,
                mode=action.link_mode,
                source_id=action.source_id,
                expected_link_target=action.source if action.link_mode.value == "symlink" else None,
                installed_digest=action.source_digest,
                state_dir=config.state_dir,
                target_name=action.target,
            )
            if backup is not None:
                backups.append(backup)
        else:  # pragma: no cover - guarded by the Operation enum
            raise ApplyError(f"unsupported plan operation: {action.operation}")
        applied.append(action)

    for target in config.targets:
        if target.enabled:
            write_skill_state(config, target, inventory)

    return ApplyResult(applied=tuple(applied), backups=tuple(backups), marketplace=marketplace)
