"""Build read-only synchronization plans for configured product targets."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from agent_config_bridge.catalog import Artifact, CatalogInventory
from agent_config_bridge.filesystem import managed_copy_digest_matches, read_managed_marker, tree_digest
from agent_config_bridge.governance import ResolvedInventory, resolve_inventory
from agent_config_bridge.instructions import (
    InstructionFile,
    codex_profile_allows_runtime_hook_state,
    inspect_instruction_copy,
    instruction_digest,
    instruction_files,
    instruction_source_id,
)
from agent_config_bridge.mcp import (
    ClaudeMcpPlan,
    McpError,
    McpServer,
    claude_user_config_path,
    codex_owned_table_rules,
    codex_unowned_server_keys,
    discover_mcp_servers,
    mcp_leaves_digest,
    read_claude_mcp_servers,
    reconcile_claude_mcp,
    release_hint,
    render_claude_spec,
    render_codex_leaves,
    server_review,
)
from agent_config_bridge.models import BridgeConfig, Component, LinkMode, Platform, Product, Surface, TargetConfig
from agent_config_bridge.path_safety import path_comparison_key, paths_overlap, read_symlink_target
from agent_config_bridge.platforms import (
    current_platform,
    product_home_environment,
    product_home_environment_unsets,
)
from agent_config_bridge.renderer import (
    MarketplaceSourceSnapshot,
    capture_marketplace_sources,
    marketplace_publish_path,
    published_marketplace_digest,
)
from agent_config_bridge.schedule_store import (
    read_schedule_set,
    schedule_publish_path,
    schedule_set_digest,
    schedule_set_is_current,
)
from agent_config_bridge.schedules import discover_schedules
from agent_config_bridge.settings import (
    OwnedSettingLeaf,
    SettingDisposition,
    SettingIssue,
    SettingsPatchPlan,
    build_settings_patch,
    discover_settings_fragments,
    plan_settings_patch,
    read_settings_document,
    safe_key,
    settings_patch_digest,
)
from agent_config_bridge.state import (
    BridgeStateError,
    InstructionStateEntry,
    SettingsState,
    SkillStateEntry,
    desired_plugin_names,
    effective_link_mode,
    find_orphaned_target_states,
    read_claude_mcp_ownership,
    read_instruction_state,
    read_mcp_leaf_state,
    read_registration_state,
    read_settings_state,
    read_skill_state,
    registration_marketplace_source,
    skill_root_for_target,
)

__all__ = [
    "Action",
    "CommandHint",
    "Disposition",
    "Operation",
    "SyncPlan",
    "build_plan",
    "codex_mcp_removed_approvals",
    "codex_mcp_unowned_keys",
    "desired_mcp_servers",
    "plan_claude_mcp",
    "plan_codex_mcp",
]


class Operation(StrEnum):
    """A filesystem or rendering operation."""

    LINK = "link"
    COPY = "copy"
    RENDER = "render"
    PATCH = "patch"
    REMOVE = "remove"
    REGISTER = "register"


class Disposition(StrEnum):
    """The result of inspecting an intended operation."""

    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    REMOVE = "remove"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Action:
    """One deterministic plan action."""

    operation: Operation
    disposition: Disposition
    component: Component
    target: str
    name: str
    source: Path
    destination: Path
    detail: str
    source_id: str | None = None
    source_digest: str | None = None
    link_mode: LinkMode | None = None


@dataclass(frozen=True, slots=True)
class CommandHint:
    """A product command required to register plugins or Claude MCP servers.

    ``component`` is ``plugins`` for Plugin/Hook marketplace registration and
    ``mcp`` for Claude Code user-scope MCP server registration.
    """

    target: str
    platform: Platform
    environment: tuple[tuple[str, str], ...]
    argv: tuple[str, ...]
    reason: str
    environment_unsets: tuple[str, ...] = ()
    component: Component = Component.PLUGINS


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """A read-only synchronization plan."""

    actions: tuple[Action, ...]
    commands: tuple[CommandHint, ...]
    reviews: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def has_conflicts(self) -> bool:
        """Return whether any action would overwrite unmanaged or modified content."""

        return any(action.disposition is Disposition.CONFLICT for action in self.actions)

    @property
    def has_changes(self) -> bool:
        """Return whether the plan contains safe create or update actions."""

        return any(
            action.disposition in {Disposition.CREATE, Disposition.UPDATE, Disposition.REMOVE}
            for action in self.actions
        )


def build_plan(
    config: BridgeConfig,
    inventory: CatalogInventory,
    *,
    resolved: ResolvedInventory | None = None,
) -> SyncPlan:
    """Build a synchronization plan without writing to disk.

    ``resolved`` lets apply reuse one governance read for plan verification
    and ownership writes instead of re-reading governance files per call.
    """

    actions: list[Action] = []
    reviews: list[str] = []
    warnings: list[str] = []
    enabled_targets = tuple(target for target in config.targets if target.enabled)
    if resolved is None:
        resolved = resolve_inventory(inventory)
    host_platform = current_platform()
    previous_skills_by_target = {target.name: read_skill_state(config, target) for target in enabled_targets}
    _validate_skill_root_reservations(enabled_targets, previous_skills_by_target)
    schedule_catalog = discover_schedules(config)
    settings_fragments = discover_settings_fragments(inventory.root)
    mcp_catalog = discover_mcp_servers(inventory.root)
    marketplace_snapshot: MarketplaceSourceSnapshot | None = None

    for target_name in find_orphaned_target_states(config):
        warnings.append(
            f"{target_name}: ownership state has no enabled target; restore the same target identity and "
            "reconcile with components=[] before deleting or changing it"
        )

    if any(
        Component.PLUGINS in target.components or Component.HOOKS in target.components for target in enabled_targets
    ):
        marketplace_snapshot = capture_marketplace_sources(config, inventory, resolved=resolved)
        build_path = config.state_dir / "builds" / marketplace_snapshot.digest
        publish_path = marketplace_publish_path(config)
        if not os.path.lexists(publish_path):
            disposition = Disposition.CREATE
        else:
            disposition = (
                Disposition.NOOP
                if published_marketplace_digest(config) == marketplace_snapshot.digest
                else Disposition.UPDATE
            )
        rendered_component = (
            Component.PLUGINS
            if any(Component.PLUGINS in target.components for target in enabled_targets)
            else Component.HOOKS
        )
        actions.append(
            Action(
                operation=Operation.RENDER,
                disposition=disposition,
                component=rendered_component,
                target="marketplace",
                name=build_path.name,
                source=inventory.root,
                destination=publish_path,
                detail="publish a stable dual Codex and Claude Code marketplace snapshot",
                source_id="marketplace",
                source_digest=build_path.name,
            )
        )

    mcp_commands: list[CommandHint] = []
    for target in enabled_targets:
        windows_path_semantics = host_platform is Platform.WINDOWS or target.platform is Platform.WINDOWS
        selected_skill_names: set[str] = set()
        previous_skills = previous_skills_by_target[target.name]
        previous_skills_by_name = {entry.name: entry for entry in previous_skills}
        if Component.SKILLS in target.components:
            destination_root = _skill_destination(target)
            mode = effective_link_mode(config.link_mode, target.platform)
            for skill in resolved.skills_for_target(target):
                selected_skill_names.add(skill.name)
                actions.append(
                    _plan_skill(
                        skill,
                        destination_root / skill.name,
                        target,
                        mode,
                        previous_skills_by_name.get(skill.name),
                        windows_path_semantics=windows_path_semantics,
                    )
                )

        for previous_skill in previous_skills:
            if previous_skill.name not in selected_skill_names:
                actions.append(
                    _plan_skill_removal(
                        previous_skill,
                        _skill_destination(target),
                        target,
                        inventory,
                        windows_path_semantics=windows_path_semantics,
                    )
                )

        previous_instructions = read_instruction_state(config, target)
        selected_instruction_relpaths: set[str] = set()
        if Component.INSTRUCTIONS in target.components:
            previous_instructions_by_relpath = {entry.relpath: entry for entry in previous_instructions}
            mode = effective_link_mode(config.link_mode, target.platform)
            for bundle in resolved.instructions_for_target(target):
                for instruction in instruction_files(bundle, target.product):
                    selected_instruction_relpaths.add(instruction.relpath)
                    actions.append(
                        _plan_instruction_file(
                            instruction,
                            _instruction_destination(target, instruction.relpath),
                            target,
                            mode,
                            previous_instructions_by_relpath.get(instruction.relpath),
                            windows_path_semantics=windows_path_semantics,
                        )
                    )
        for previous_instruction in previous_instructions:
            if previous_instruction.relpath not in selected_instruction_relpaths:
                actions.append(
                    _plan_instruction_removal(
                        previous_instruction,
                        target,
                        inventory,
                        windows_path_semantics=windows_path_semantics,
                    )
                )

        settings_state = read_settings_state(config, target)
        settings_destination = _settings_destination(target)
        desired_settings = (
            build_settings_patch(target.product, settings_fragments) if Component.SETTINGS in target.components else ()
        )
        previous_settings = _owned_settings(settings_state)
        settings_plan = plan_settings_patch(
            target.product,
            settings_destination,
            desired_settings,
            previous_settings,
        )
        if settings_plan.changes:
            actions.append(_settings_action(target, inventory, settings_plan))
        if Component.SETTINGS in target.components:
            for fragment in settings_fragments:
                if fragment.product is target.product:
                    reviews.append(
                        f"{target.name}: settings fragment {fragment.path} manages "
                        f"{len(fragment.leaves)} explicit leaf values in {settings_destination}"
                    )

        target_mcp_servers = desired_mcp_servers(target, resolved, mcp_catalog)
        if target.product is Product.CODEX:
            codex_mcp_plan = plan_codex_mcp(config, target, target_mcp_servers)
            if codex_mcp_plan.changes:
                actions.append(_codex_mcp_action(target, inventory, codex_mcp_plan))
            warnings.extend(
                f"{target.name}: owned Codex MCP server {name} also carries keys the Bridge does not manage: "
                f"{', '.join(safe_key(key) for key in keys)}"
                for name, keys in codex_mcp_unowned_keys(codex_mcp_plan).items()
            )
            warnings.extend(
                f"{target.name}: removing mcp_servers.{name} also deletes its tools approvals ({count})"
                for name, count in codex_mcp_removed_approvals(codex_mcp_plan).items()
            )
        elif target.platform is host_platform:
            claude_mcp_plan = plan_claude_mcp(config, target, target_mcp_servers)
            if claude_mcp_plan is not None:
                actions.append(_claude_mcp_action(target, inventory, claude_mcp_plan))
                mcp_commands.extend(_claude_mcp_hints(target, claude_mcp_plan))
        elif target_mcp_servers or read_claude_mcp_ownership(config, target):
            warnings.append(
                f"{target.name}: Claude Code MCP registration is omitted on this {host_platform.value} host; "
                f"run plan/register from the configured {target.platform.value} platform"
            )
        reviews.extend(server_review(server, target) for server in target_mcp_servers)

        published_schedules = schedule_publish_path(config, target)
        schedules_selected = Component.SCHEDULES in target.components and bool(schedule_catalog.schedules)
        if schedules_selected:
            schedule_digest = schedule_set_digest(schedule_catalog, target)
            if not os.path.lexists(published_schedules):
                schedule_disposition = Disposition.CREATE
            else:
                schedule_disposition = (
                    Disposition.NOOP
                    if schedule_set_is_current(config, schedule_catalog, target)
                    else Disposition.UPDATE
                )
            actions.append(
                Action(
                    operation=Operation.RENDER,
                    disposition=schedule_disposition,
                    component=Component.SCHEDULES,
                    target=target.name,
                    name="host-schedules",
                    source=inventory.root / "schedules",
                    destination=published_schedules,
                    detail="publish an immutable target-native schedule snapshot",
                    source_id=f"schedules/{target.name}",
                    source_digest=schedule_digest,
                )
            )
        elif os.path.lexists(published_schedules):
            current_schedules = read_schedule_set(config, target)
            actions.append(
                Action(
                    operation=Operation.REMOVE,
                    disposition=Disposition.REMOVE,
                    component=Component.SCHEDULES,
                    target=target.name,
                    name="host-schedules",
                    source=current_schedules.build_file if current_schedules is not None else inventory.root,
                    destination=published_schedules,
                    detail="remove the deselected published schedule pointer",
                    source_id=f"schedules/{target.name}",
                    source_digest=current_schedules.digest if current_schedules is not None else None,
                )
            )

        if Component.SCHEDULES in target.components:
            warnings.append(
                f"{target.name}: schedules are host-managed CLI runs and do not appear in product-native "
                "Desktop scheduler views; run register on the target host to reconcile the minute heartbeat"
            )

        if Component.HOOKS in target.components:
            # Review the product-wide union actually delivered to this target,
            # not just its own gated set — a same-product sibling target can
            # pull additional hooks into the shared plugin.
            if marketplace_snapshot is None:
                raise AssertionError("Hook planning requires a marketplace source snapshot")
            reviews.extend(
                _hook_reviews(
                    target,
                    resolved.hooks_for_product(config, target.product),
                    marketplace_snapshot,
                )
            )
        if Component.PLUGINS in target.components:
            if marketplace_snapshot is None:
                raise AssertionError("Plugin planning requires a marketplace source snapshot")
            reviews.extend(_plugin_reviews(target, inventory, marketplace_snapshot))

        if (
            target.product is Product.CLAUDE_CODE
            and Surface.DESKTOP in target.surfaces
            and (Component.PLUGINS in target.components or Component.HOOKS in target.components)
        ):
            warnings.append(
                f"{target.name}: Claude Code Desktop loads plugins only in Local or SSH sessions, "
                "not Remote (cloud) or WSL sessions"
            )

        if target.platform is not host_platform and (
            Component.PLUGINS in target.components
            or Component.HOOKS in target.components
            or read_registration_state(config, target).plugins
        ):
            warnings.append(
                f"{target.name}: plugin registration commands are omitted on this {host_platform.value} host; "
                f"run plan/register from the configured {target.platform.value} platform with native paths"
            )

    commands = (*_registration_hints(config, inventory, resolved, host_platform), *mcp_commands)
    return SyncPlan(
        actions=tuple(actions),
        commands=commands,
        reviews=tuple(reviews),
        warnings=tuple(warnings),
    )


def _skill_destination(target: TargetConfig) -> Path:
    return skill_root_for_target(target)


def _settings_destination(target: TargetConfig) -> Path:
    filename = "config.toml" if target.product is Product.CODEX else "settings.json"
    return target.config_home / filename


def _owned_settings(state: SettingsState) -> tuple[OwnedSettingLeaf, ...]:
    return tuple(
        OwnedSettingLeaf(
            source_id=entry.source_id,
            path=entry.path,
            digest=entry.value_digest,
            created_parents=tuple(
                parent
                for parent in state.created_containers
                if len(parent) < len(entry.path) and entry.path[: len(parent)] == parent
            ),
        )
        for entry in state.entries
    )


def desired_mcp_servers(
    target: TargetConfig,
    resolved: ResolvedInventory,
    servers: Sequence[McpServer],
) -> tuple[McpServer, ...]:
    """Return the governance-gated MCP servers one target should receive."""

    if Component.MCP not in target.components:
        return ()
    by_name = {server.name: server for server in servers}
    selected: list[McpServer] = []
    for artifact in resolved.mcp_for_target(target):
        server = by_name.get(artifact.name)
        if server is None:
            raise McpError(f"MCP catalog changed during planning: {artifact.name!r} is no longer declared")
        selected.append(server)
    return tuple(selected)


def plan_codex_mcp(
    config: BridgeConfig,
    target: TargetConfig,
    servers: Sequence[McpServer],
) -> SettingsPatchPlan:
    """Plan owned Codex ``mcp_servers`` leaves against the target ``config.toml``.

    MCP leaves use their own ownership record, separate from Settings bundles,
    with the same comment-preserving, digest-owned engine.
    """

    if target.product is not Product.CODEX:
        raise McpError(f"target {target.name!r} is not a Codex target")
    desired = tuple(leaf for server in servers for leaf in render_codex_leaves(server, target))
    previous = _owned_settings(read_mcp_leaf_state(config, target))
    return plan_settings_patch(
        target.product,
        _settings_destination(target),
        desired,
        previous,
        owned_tables=codex_owned_table_rules(leaf.path for leaf in previous),
        # A desired server may create its table but never merge into an
        # existing unowned one; that table must go through `mcp adopt`.
        claimed_tables=tuple(("mcp_servers", server.name) for server in servers),
    )


def codex_mcp_unowned_keys(plan: SettingsPatchPlan) -> dict[str, tuple[str, ...]]:
    """Return unexpected unowned keys in owned Codex server tables, by server name.

    These keys are not conflicts; the Bridge reports them so the owner can
    review settings the catalog does not declare.
    """

    if not plan.previous:
        return {}
    document = read_settings_document(plan.product, plan.destination)
    return codex_unowned_server_keys(document, (leaf.path for leaf in plan.previous))


def codex_mcp_removed_approvals(plan: SettingsPatchPlan) -> dict[str, int]:
    """Count the ``tools`` approvals deleted with each removed Codex server table.

    Only server names and counts are returned, never approval values.
    """

    if not plan.removed_tables:
        return {}
    document = read_settings_document(plan.product, plan.destination)
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    counts: dict[str, int] = {}
    for path in plan.removed_tables:
        table = servers.get(path[-1]) if len(path) == 2 and path[0] == "mcp_servers" else None
        tools = table.get("tools") if isinstance(table, dict) else None
        if isinstance(tools, dict) and tools:
            counts[path[-1]] = len(tools)
    return counts


def plan_claude_mcp(
    config: BridgeConfig,
    target: TargetConfig,
    servers: Sequence[McpServer],
) -> ClaudeMcpPlan | None:
    """Plan Claude Code user-scope MCP registrations without writing anything.

    The Claude user config is read only when servers are desired or owned; an
    unreadable or invalid file becomes a whole-target conflict. Returns
    ``None`` when the target has neither desired nor owned MCP servers.
    """

    if target.product is not Product.CLAUDE_CODE:
        raise McpError(f"target {target.name!r} is not a Claude Code target")
    owned = dict(read_claude_mcp_ownership(config, target))
    if not servers and not owned:
        return None
    desired = {server.name: render_claude_spec(server, target) for server in servers}
    config_path = claude_user_config_path(target)
    try:
        live = read_claude_mcp_servers(target)
    except McpError as exc:
        return reconcile_claude_mcp(config_path, desired, owned, None, target=target.name, error=str(exc))
    return reconcile_claude_mcp(config_path, desired, owned, live, target=target.name)


def _claude_mcp_action(
    target: TargetConfig,
    inventory: CatalogInventory,
    plan: ClaudeMcpPlan,
) -> Action:
    """Summarize Claude MCP registration; ``register`` executes its commands.

    The action is ``conflict`` or ``noop``: pending product-CLI changes appear
    as command hints, like Plugin registrations, so they never make ``apply``
    or ``sync-skills`` report filesystem changes.
    """

    if plan.error is not None:
        detail = f"cannot read the Claude user config: {plan.error}"
    else:
        counts = {
            disposition: sum(change.disposition is disposition for change in plan.changes)
            for disposition in SettingDisposition
        }
        details = ", ".join(f"{kind.value}={counts[kind]}" for kind in SettingDisposition if counts[kind])
        conflicts = "; ".join(
            f"{change.name}: {change.detail}"
            for change in plan.changes
            if change.disposition is SettingDisposition.CONFLICT
        )
        detail = f"register user-scope MCP servers through the product CLI ({details or 'no servers'})"
        if conflicts:
            detail = f"{detail}; {conflicts}"
        elif plan.has_commands or plan.needs_state_write:
            detail = f"{detail}; run register"
    return Action(
        operation=Operation.REGISTER,
        disposition=Disposition.CONFLICT if plan.has_conflicts else Disposition.NOOP,
        component=Component.MCP,
        target=target.name,
        name="mcp-servers",
        source=inventory.root / "mcp",
        destination=plan.config_path,
        detail=detail,
        source_id=f"mcp/{target.name}",
        source_digest=plan.desired_digest,
    )


def _claude_mcp_hints(target: TargetConfig, plan: ClaudeMcpPlan) -> list[CommandHint]:
    executable = str(target.executable) if target.executable is not None else "claude"
    environment = product_home_environment(target)
    environment_unsets = product_home_environment_unsets(target)
    hints: list[CommandHint] = []

    def hint(argv: tuple[str, ...], reason: str) -> CommandHint:
        return CommandHint(
            target=target.name,
            platform=target.platform,
            environment=environment,
            argv=(executable, *argv),
            reason=reason,
            environment_unsets=environment_unsets,
            component=Component.MCP,
        )

    for change in plan.changes:
        if change.disposition in {SettingDisposition.UPDATE, SettingDisposition.REMOVE}:
            reason = (
                f"remove owned Claude Code MCP server {change.name} before re-registering it"
                if change.disposition is SettingDisposition.UPDATE
                else f"remove deselected bridge-owned Claude Code MCP server {change.name}"
            )
            hints.append(hint(("mcp", "remove", "--scope", "user", change.name), reason))
        if change.disposition in {SettingDisposition.CREATE, SettingDisposition.UPDATE}:
            if change.spec_json is None:
                raise McpError(f"Claude MCP change has no rendered spec: {change.name}")
            hints.append(
                hint(
                    ("mcp", "add-json", "--scope", "user", change.name, change.spec_json),
                    f"register Claude Code user-scope MCP server {change.name}",
                )
            )
    return hints


def _settings_action(
    target: TargetConfig,
    inventory: CatalogInventory,
    plan: SettingsPatchPlan,
) -> Action:
    return _leaf_patch_action(
        target,
        plan,
        component=Component.SETTINGS,
        name="product-settings",
        source=inventory.root / "settings",
        detail="merge owned setting leaves",
        source_id=f"settings/{target.name}",
        source_digest=settings_patch_digest(plan.desired),
    )


def _codex_mcp_action(
    target: TargetConfig,
    inventory: CatalogInventory,
    plan: SettingsPatchPlan,
) -> Action:
    issues: dict[str, set[str]] = {}
    for change in plan.changes:
        if change.disposition is SettingDisposition.CONFLICT and len(change.path) >= 2:
            issues.setdefault(change.path[1], set()).add((change.issue or SettingIssue.DRIFT).value)
    note = "; ".join(
        f"{name}: {', '.join(sorted(kinds))}"
        + (
            f"; {release_hint(target.name, name)}"
            if kinds & {SettingIssue.DRIFT.value, SettingIssue.MISSING.value}
            else ""
        )
        for name, kinds in sorted(issues.items())
    )
    return _leaf_patch_action(
        target,
        plan,
        component=Component.MCP,
        name="mcp-servers",
        source=inventory.root / "mcp",
        detail="merge owned MCP server leaves",
        source_id=f"mcp/{target.name}",
        source_digest=mcp_leaves_digest(plan.desired),
        note=note,
    )


def _leaf_patch_action(
    target: TargetConfig,
    plan: SettingsPatchPlan,
    *,
    component: Component,
    name: str,
    source: Path,
    detail: str,
    source_id: str,
    source_digest: str,
    note: str = "",
) -> Action:
    counts = {
        disposition: sum(change.disposition is disposition for change in plan.changes)
        for disposition in SettingDisposition
    }
    if plan.has_conflicts:
        disposition = Disposition.CONFLICT
    elif not plan.has_changes:
        disposition = Disposition.NOOP
    elif counts[SettingDisposition.REMOVE] and not plan.desired:
        disposition = Disposition.REMOVE
    elif plan.destination_existed:
        disposition = Disposition.UPDATE
    else:
        disposition = Disposition.CREATE

    details = ", ".join(f"{kind.value}={counts[kind]}" for kind in SettingDisposition if counts[kind])
    return Action(
        operation=Operation.PATCH,
        disposition=disposition,
        component=component,
        target=target.name,
        name=name,
        source=source,
        destination=plan.destination,
        detail=f"{detail} ({details}){f'; {note}' if note else ''}",
        source_id=source_id,
        source_digest=source_digest,
    )


def _validate_skill_root_reservations(
    targets: tuple[TargetConfig, ...],
    previous_skills_by_target: dict[str, tuple[SkillStateEntry, ...]],
) -> None:
    """Reject overlapping discovery roots, product homes, or retained claims.

    A target with recorded Skill ownership keeps its physical destination
    reserved while ``components=[]`` is used to reconcile that state. This
    prevents a new target from observing the old files as a no-op immediately
    before the old target removes them. A target that neither selects Skills
    nor has prior Skill ownership is a passive consumer and may share another
    target's discovery root. At most one target may hold a current or retained
    write claim for an overlapping set of roots.
    """

    reservations: list[tuple[TargetConfig, Path, tuple[SkillStateEntry, ...]]] = []
    for target in targets:
        previous_skills = previous_skills_by_target[target.name]
        destination = _skill_destination(target)
        for reserved_target, reserved_destination, reserved_skills in reservations:
            if not _paths_overlap_for_targets(
                destination,
                target,
                reserved_destination,
                reserved_target,
            ):
                continue

            reserved_claims_root = Component.SKILLS in reserved_target.components or bool(reserved_skills)
            target_claims_root = Component.SKILLS in target.components or bool(previous_skills)
            if not (reserved_claims_root and target_claims_root):
                continue

            if reserved_skills or previous_skills:
                owner = reserved_target if reserved_skills else target
                owner_destination = reserved_destination if reserved_skills else destination
                claimant = target if reserved_skills else reserved_target
                raise BridgeStateError(
                    f"skill root {owner_destination} remains reserved by target {owner.name!r} with non-empty "
                    f"ownership state; reconcile {owner.name!r} alone with components=[] before target "
                    f"{claimant.name!r} can claim it"
                )
            raise BridgeStateError(
                f"enabled targets {reserved_target.name!r} and {target.name!r} have overlapping physical "
                f"Skill discovery roots: {reserved_destination} <-> {destination}"
            )

        reservations.append((target, destination, previous_skills))

    for home_target in targets:
        for skill_target, destination, previous_skills in reservations:
            if home_target.name == skill_target.name and home_target.product is Product.CLAUDE_CODE:
                continue
            if not _paths_overlap_for_targets(
                home_target.config_home,
                home_target,
                destination,
                skill_target,
            ):
                continue
            if previous_skills:
                raise BridgeStateError(
                    f"skill root {destination} remains reserved by target {skill_target.name!r} with "
                    f"non-empty ownership state and overlaps target {home_target.name!r} config_home; "
                    f"reconcile {skill_target.name!r} before changing target paths"
                )
            raise BridgeStateError(
                f"target {home_target.name!r} config_home overlaps target {skill_target.name!r} "
                f"Skill discovery root: {home_target.config_home} <-> {destination}"
            )


def _paths_overlap_for_targets(
    left: Path,
    left_target: TargetConfig,
    right: Path,
    right_target: TargetConfig,
) -> bool:
    windows = Platform.WINDOWS in {left_target.platform, right_target.platform}
    try:
        return paths_overlap(left, right, windows=windows)
    except (OSError, RuntimeError) as error:
        raise BridgeStateError(
            f"could not physically resolve paths for targets {left_target.name!r} and "
            f"{right_target.name!r} during overlap validation"
        ) from error


def _plan_skill(
    skill: Artifact,
    destination: Path,
    target: TargetConfig,
    mode: LinkMode,
    previous: SkillStateEntry | None,
    *,
    windows_path_semantics: bool,
) -> Action:
    if mode is LinkMode.SYMLINK:
        return _plan_link(
            skill,
            destination,
            target,
            previous,
            windows_path_semantics=windows_path_semantics,
        )
    if any(path.is_symlink() for path in skill.path.rglob("*")):
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="managed copy mode does not support symlinks inside a Skill",
            source_id=_skill_source_id(skill),
        )
    return _plan_copy(
        skill,
        destination,
        target,
        previous,
        windows_path_semantics=windows_path_semantics,
    )


def _plan_link(
    skill: Artifact,
    destination: Path,
    target: TargetConfig,
    previous: SkillStateEntry | None,
    *,
    windows_path_semantics: bool,
) -> Action:
    if destination.is_symlink():
        if previous is None or previous.mode is not LinkMode.SYMLINK:
            return _conflict_action(
                operation=Operation.LINK,
                skill=skill,
                destination=destination,
                target=target,
                detail="existing symlink has no matching target ownership state",
            )
        try:
            actual_target = destination.resolve(strict=True)
            same_target = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                skill.path.resolve(strict=True),
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            actual_target = None
            same_target = False
        recorded_target = Path(previous.link_target) if previous.link_target is not None else None
        recorded_target_matches = bool(
            actual_target is not None
            and recorded_target is not None
            and path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            )
            == path_comparison_key(
                recorded_target,
                windows=windows_path_semantics,
            )
        )
        if same_target and recorded_target_matches:
            return Action(
                operation=Operation.LINK,
                disposition=Disposition.NOOP,
                component=Component.SKILLS,
                target=target.name,
                name=skill.name,
                source=skill.path,
                destination=destination,
                detail="symlink already points to canonical skill",
                source_id=_skill_source_id(skill),
                source_digest=tree_digest(skill.path, ignore_bytecode_caches=True),
            )
        detail = (
            "managed symlink no longer matches its recorded target"
            if same_target
            else "destination is a different or dangling symlink"
        )
        return _conflict_action(
            operation=Operation.LINK,
            skill=skill,
            destination=destination,
            target=target,
            detail=detail,
        )
    if os.path.lexists(destination):
        return _conflict_action(
            operation=Operation.LINK,
            skill=skill,
            destination=destination,
            target=target,
            detail="destination already exists and is not managed as this symlink",
        )
    return Action(
        operation=Operation.LINK,
        disposition=Disposition.CREATE,
        component=Component.SKILLS,
        target=target.name,
        name=skill.name,
        source=skill.path,
        destination=destination,
        detail="create canonical skill symlink",
        source_id=_skill_source_id(skill),
        source_digest=tree_digest(skill.path, ignore_bytecode_caches=True),
    )


def _plan_copy(
    skill: Artifact,
    destination: Path,
    target: TargetConfig,
    previous: SkillStateEntry | None,
    *,
    windows_path_semantics: bool,
) -> Action:
    source_digest = tree_digest(skill.path, ignore_bytecode_caches=True)
    if not os.path.lexists(destination):
        return Action(
            operation=Operation.COPY,
            disposition=Disposition.CREATE,
            component=Component.SKILLS,
            target=target.name,
            name=skill.name,
            source=skill.path,
            destination=destination,
            detail="create managed skill copy",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    if destination.is_symlink():
        if (
            previous is None
            or previous.mode is not LinkMode.SYMLINK
            or previous.source_id != _skill_source_id(skill)
            or previous.link_target is None
        ):
            return _conflict_action(
                operation=Operation.COPY,
                skill=skill,
                destination=destination,
                target=target,
                detail="existing symlink has no matching Bridge ownership state",
                source_id=_skill_source_id(skill),
                source_digest=source_digest,
            )
        actual_target: Path | None
        source_target: Path | None
        recorded_target: Path | None
        try:
            actual_target = read_symlink_target(destination)
            source_target = skill.path.resolve(strict=True)
            recorded_target = Path(previous.link_target)
        except (OSError, RuntimeError):
            actual_target = None
            source_target = None
            recorded_target = None
        targets_match = bool(
            actual_target is not None
            and source_target is not None
            and recorded_target is not None
            and path_comparison_key(actual_target, windows=windows_path_semantics)
            == path_comparison_key(source_target, windows=windows_path_semantics)
            == path_comparison_key(recorded_target, windows=windows_path_semantics)
        )
        if not targets_match:
            return _conflict_action(
                operation=Operation.COPY,
                skill=skill,
                destination=destination,
                target=target,
                detail="managed symlink no longer matches its recorded canonical source",
                source_id=_skill_source_id(skill),
                source_digest=source_digest,
            )
        return Action(
            operation=Operation.COPY,
            disposition=Disposition.UPDATE,
            component=Component.SKILLS,
            target=target.name,
            name=skill.name,
            source=skill.path,
            destination=destination,
            detail="replace unchanged managed symlink with a managed copy and retain backup",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
            link_mode=LinkMode.SYMLINK,
        )
    if not destination.is_dir():
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="copy destination exists with an unsupported type",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )

    if previous is None or previous.mode not in {LinkMode.COPY, LinkMode.SYMLINK}:
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="existing copy destination has no matching target ownership state",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    if previous.source_id != _skill_source_id(skill):
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="existing copy destination has mismatched target ownership state",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )

    marker = read_managed_marker(destination)
    if marker is None:
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="destination has no valid bridge ownership marker",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    if marker.get("source_id") != _skill_source_id(skill):
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="managed copy belongs to a different canonical source",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )

    installed_digest = marker.get("installed_digest")
    if not isinstance(installed_digest, str) or not managed_copy_digest_matches(destination, installed_digest):
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="managed copy was modified after installation",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    current_digest = tree_digest(destination, ignore_bytecode_caches=True)
    if previous.mode is LinkMode.SYMLINK:
        if source_digest != current_digest:
            return _conflict_action(
                operation=Operation.COPY,
                skill=skill,
                destination=destination,
                target=target,
                detail="partially migrated managed copy does not match the canonical source",
                source_id=_skill_source_id(skill),
                source_digest=source_digest,
            )
        return Action(
            operation=Operation.COPY,
            disposition=Disposition.UPDATE,
            component=Component.SKILLS,
            target=target.name,
            name=skill.name,
            source=skill.path,
            destination=destination,
            detail="recover ownership state for an already installed managed copy",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
            link_mode=LinkMode.COPY,
        )
    if source_digest == current_digest and installed_digest == current_digest:
        return Action(
            operation=Operation.COPY,
            disposition=Disposition.NOOP,
            component=Component.SKILLS,
            target=target.name,
            name=skill.name,
            source=skill.path,
            destination=destination,
            detail="managed copy matches canonical skill",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    return Action(
        operation=Operation.COPY,
        disposition=Disposition.UPDATE,
        component=Component.SKILLS,
        target=target.name,
        name=skill.name,
        source=skill.path,
        destination=destination,
        detail=(
            "re-record managed copy digest without bytecode caches and retain backup"
            if source_digest == current_digest
            else "update unchanged managed copy and retain backup"
        ),
        source_id=_skill_source_id(skill),
        source_digest=source_digest,
    )


def _conflict_action(
    *,
    operation: Operation,
    skill: Artifact,
    destination: Path,
    target: TargetConfig,
    detail: str,
    source_id: str | None = None,
    source_digest: str | None = None,
) -> Action:
    return Action(
        operation=operation,
        disposition=Disposition.CONFLICT,
        component=Component.SKILLS,
        target=target.name,
        name=skill.name,
        source=skill.path,
        destination=destination,
        detail=detail,
        source_id=source_id,
        source_digest=source_digest,
    )


def _skill_source_id(skill: Artifact) -> str:
    return f"skills/{skill.name}"


def _instruction_destination(target: TargetConfig, relpath: str) -> Path:
    destination = target.config_home.joinpath(*relpath.split("/"))
    # Defense in depth: relpath validation already forbids traversal and
    # drive-letter components, so a join that re-anchors outside config_home
    # can only mean corrupted input and must never reach apply.
    if not destination.is_relative_to(target.config_home):
        raise BridgeStateError(f"instruction destination escapes target config_home: {relpath!r} -> {destination}")
    return destination


def _plan_instruction_file(
    instruction: InstructionFile,
    destination: Path,
    target: TargetConfig,
    mode: LinkMode,
    previous: InstructionStateEntry | None,
    *,
    windows_path_semantics: bool,
) -> Action:
    source_id = instruction_source_id(instruction.bundle)
    source_digest = instruction_digest(instruction.source)
    if mode is LinkMode.SYMLINK:
        return _plan_instruction_link(
            instruction,
            destination,
            target,
            previous,
            source_id=source_id,
            source_digest=source_digest,
            windows_path_semantics=windows_path_semantics,
        )
    return _plan_instruction_copy(
        instruction,
        destination,
        target,
        previous,
        source_id=source_id,
        source_digest=source_digest,
    )


def _plan_instruction_link(
    instruction: InstructionFile,
    destination: Path,
    target: TargetConfig,
    previous: InstructionStateEntry | None,
    *,
    source_id: str,
    source_digest: str,
    windows_path_semantics: bool,
) -> Action:
    base = Action(
        operation=Operation.LINK,
        disposition=Disposition.CREATE,
        component=Component.INSTRUCTIONS,
        target=target.name,
        name=instruction.relpath,
        source=instruction.source,
        destination=destination,
        detail="create canonical instruction file symlink",
        source_id=source_id,
        source_digest=source_digest,
    )
    if destination.is_symlink():
        if previous is None or previous.mode is not LinkMode.SYMLINK:
            return replace(
                base,
                disposition=Disposition.CONFLICT,
                detail="existing symlink has no matching target ownership state",
            )
        try:
            actual_target = destination.resolve(strict=True)
            same_target = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                instruction.source.resolve(strict=True),
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            actual_target = None
            same_target = False
        recorded_target = Path(previous.link_target) if previous.link_target is not None else None
        recorded_target_matches = bool(
            actual_target is not None
            and recorded_target is not None
            and path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            )
            == path_comparison_key(
                recorded_target,
                windows=windows_path_semantics,
            )
        )
        if same_target and recorded_target_matches:
            return replace(
                base,
                disposition=Disposition.NOOP,
                detail="symlink already points to canonical instruction file",
            )
        detail = (
            "managed symlink no longer matches its recorded target"
            if same_target
            else "destination is a different or dangling symlink"
        )
        return replace(base, disposition=Disposition.CONFLICT, detail=detail)
    if os.path.lexists(destination):
        # Per ADR-5 an existing unmanaged destination file is a conflict even
        # when its content matches the catalog source; it is never adopted.
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="destination already exists and is not managed as this symlink",
        )
    return base


def _plan_instruction_copy(
    instruction: InstructionFile,
    destination: Path,
    target: TargetConfig,
    previous: InstructionStateEntry | None,
    *,
    source_id: str,
    source_digest: str,
) -> Action:
    base = Action(
        operation=Operation.COPY,
        disposition=Disposition.CREATE,
        component=Component.INSTRUCTIONS,
        target=target.name,
        name=instruction.relpath,
        source=instruction.source,
        destination=destination,
        detail="create managed instruction file copy",
        source_id=source_id,
        source_digest=source_digest,
    )
    if not os.path.lexists(destination):
        return base
    if destination.is_symlink() or not destination.is_file():
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="copy destination exists with an unsupported type",
        )
    if previous is None or previous.mode is not LinkMode.COPY or previous.installed_digest is None:
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="existing copy destination has no matching target ownership state",
        )
    allows_runtime_state = codex_profile_allows_runtime_hook_state(
        target.product,
        instruction.relpath,
    )
    inspection = inspect_instruction_copy(
        destination,
        installed_digest=previous.installed_digest,
        allow_runtime_hook_state=allows_runtime_state,
    )
    if not inspection.managed_matches:
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="managed instruction copy was modified after installation",
        )
    permissions_are_private = (
        not allows_runtime_state or os.name == "nt" or stat.S_IMODE(destination.stat().st_mode) == 0o600
    )
    if source_digest == previous.installed_digest and permissions_are_private:
        return replace(
            base,
            disposition=Disposition.NOOP,
            detail="managed copy matches canonical instruction file",
        )
    return replace(
        base,
        disposition=Disposition.UPDATE,
        detail=(
            "repair managed Codex profile permissions and retain backup"
            if source_digest == previous.installed_digest
            else "update unchanged managed instruction copy and retain backup"
        ),
    )


def _plan_instruction_removal(
    previous: InstructionStateEntry,
    target: TargetConfig,
    inventory: CatalogInventory,
    *,
    windows_path_semantics: bool,
) -> Action:
    destination = _instruction_destination(target, previous.relpath)
    # Unlike a Skill, whose source is its whole directory, an instruction
    # removal names one file: point at the per-file catalog path.
    source = (
        Path(previous.link_target)
        if previous.link_target is not None
        else (inventory.root / previous.source_id / target.product.value).joinpath(*previous.relpath.split("/"))
    )
    base = Action(
        operation=Operation.REMOVE,
        disposition=Disposition.REMOVE,
        component=Component.INSTRUCTIONS,
        target=target.name,
        name=previous.relpath,
        source=source,
        destination=destination,
        detail="remove a deselected bridge-managed instruction file",
        source_id=previous.source_id,
        source_digest=previous.installed_digest,
        link_mode=previous.mode,
    )
    if not os.path.lexists(destination):
        return replace(
            base,
            disposition=Disposition.NOOP,
            detail="previously managed instruction file is already absent",
        )

    if previous.mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or previous.link_target is None:
            return replace(
                base,
                disposition=Disposition.CONFLICT,
                detail="previously managed instruction link was replaced by other content",
            )
        try:
            actual_target = read_symlink_target(destination)
            target_matches = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                Path(previous.link_target),
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            target_matches = False
        if not target_matches:
            return replace(
                base,
                disposition=Disposition.CONFLICT,
                detail="previously managed instruction link now points elsewhere",
            )
        return base

    if destination.is_symlink() or not destination.is_file():
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed instruction copy was replaced by another path type",
        )
    if (
        previous.installed_digest is None
        or not inspect_instruction_copy(
            destination,
            installed_digest=previous.installed_digest,
            allow_runtime_hook_state=codex_profile_allows_runtime_hook_state(target.product, previous.relpath),
        ).managed_matches
    ):
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed instruction copy was modified after installation",
        )
    return base


def _hook_reviews(
    target: TargetConfig,
    hooks: tuple[Artifact, ...],
    source_snapshot: MarketplaceSourceSnapshot,
) -> list[str]:
    reviews: list[str] = []
    for artifact in hooks:
        for overlay in ("common", target.product.value):
            relative = Path(overlay) / "hooks.json"
            source_bytes = source_snapshot.file_bytes(Component.HOOKS, artifact.name, relative)
            if source_bytes is not None:
                reviews.extend(
                    _hook_document_reviews(
                        target.name,
                        artifact.name,
                        overlay,
                        artifact.path / relative,
                        source_bytes=source_bytes,
                    )
                )
    return reviews


def _plugin_reviews(
    target: TargetConfig,
    inventory: CatalogInventory,
    source_snapshot: MarketplaceSourceSnapshot,
) -> list[str]:
    reviews: list[str] = []
    for artifact in inventory.plugins:
        files = source_snapshot.files(Component.PLUGINS, artifact.name)
        for overlay in ("common", target.product.value):
            overlay_files = tuple(
                (relative, content) for relative, content in files if relative.parts and relative.parts[0] == overlay
            )
            for relative, source_bytes in overlay_files:
                if relative.name == "hooks.json":
                    reviews.extend(
                        _hook_document_reviews(
                            target.name,
                            artifact.name,
                            overlay,
                            artifact.path / relative,
                            source_bytes=source_bytes,
                        )
                    )
            for relative, source_bytes in overlay_files:
                if relative.name == ".mcp.json":
                    reviews.extend(
                        _sensitive_json_reviews(
                            target.name,
                            artifact.name,
                            "MCP",
                            artifact.path / relative,
                            source_bytes=source_bytes,
                        )
                    )
            manifest_directory = ".codex-plugin" if target.product is Product.CODEX else ".claude-plugin"
            manifest_relative = Path(overlay) / manifest_directory / "plugin.json"
            manifest_bytes = source_snapshot.file_bytes(Component.PLUGINS, artifact.name, manifest_relative)
            if manifest_bytes is not None:
                reviews.extend(
                    _sensitive_json_reviews(
                        target.name,
                        artifact.name,
                        "manifest",
                        artifact.path / manifest_relative,
                        source_bytes=manifest_bytes,
                    )
                )
    return reviews


def _hook_document_reviews(
    target: str,
    artifact: str,
    overlay: str,
    path: Path,
    *,
    source_bytes: bytes | None = None,
) -> list[str]:
    try:
        payload = json.loads(path.read_bytes() if source_bytes is None else source_bytes)
    except (OSError, json.JSONDecodeError):
        return [f"{target}: inspect unreadable hook document {path}"]
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        return [f"{target}: inspect malformed hook document {path}"]

    reviews: list[str] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            reviews.append(f"{target}: inspect malformed hook event {artifact}/{overlay}:{event}")
            continue
        for group in groups:
            if not isinstance(group, dict):
                reviews.append(f"{target}: inspect malformed hook matcher {artifact}/{overlay}:{event}")
                continue
            matcher = group.get("matcher", "*")
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                reviews.append(f"{target}: inspect malformed hook handlers {artifact}/{overlay}:{event}")
                continue
            for handler in handlers:
                if not isinstance(handler, dict):
                    continue
                handler_type = handler.get("type", "unknown")
                review_values = tuple(
                    (field, handler[field])
                    for field in ("command", "commandWindows", "url", "prompt")
                    if field in handler
                ) or (("handler", "<inline>"),)
                for field, value in review_values:
                    reviews.append(
                        f"{target}: hook {artifact}/{overlay} event={event} matcher={matcher!r} "
                        f"type={handler_type!r} {field}={value!r}"
                    )
    return reviews


def _sensitive_json_reviews(
    target: str,
    artifact: str,
    kind: str,
    path: Path,
    *,
    source_bytes: bytes | None = None,
) -> list[str]:
    try:
        payload = json.loads(path.read_bytes() if source_bytes is None else source_bytes)
    except (OSError, json.JSONDecodeError):
        return [f"{target}: inspect invalid {kind} JSON for plugin {artifact}: {path}"]

    if kind == "manifest" and isinstance(payload, dict):
        payload = payload.get("mcpServers", {})

    values: list[tuple[str, str]] = []

    def visit(value: object, location: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{location}.{key}"
                if key in {"command", "url"} and isinstance(item, str):
                    values.append((child, item))
                visit(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{location}[{index}]")

    visit(payload, "$")
    return [f"{target}: plugin {artifact} {kind} {location}={value!r}" for location, value in values]


def _plan_skill_removal(
    previous: SkillStateEntry,
    destination_root: Path,
    target: TargetConfig,
    inventory: CatalogInventory,
    *,
    windows_path_semantics: bool,
) -> Action:
    destination = destination_root / previous.name
    source = (
        Path(previous.link_target) if previous.link_target is not None else inventory.root / "skills" / previous.name
    )
    base = Action(
        operation=Operation.REMOVE,
        disposition=Disposition.REMOVE,
        component=Component.SKILLS,
        target=target.name,
        name=previous.name,
        source=source,
        destination=destination,
        detail="remove a deselected bridge-managed Skill",
        source_id=previous.source_id,
        link_mode=previous.mode,
    )
    if not os.path.lexists(destination):
        return replace(
            base,
            disposition=Disposition.NOOP,
            detail="previously managed Skill is already absent",
        )

    if previous.mode is LinkMode.SYMLINK:
        if not destination.is_symlink() or previous.link_target is None:
            return replace(
                base,
                disposition=Disposition.CONFLICT,
                detail="previously managed Skill link was replaced by other content",
            )
        try:
            actual_target = read_symlink_target(destination)
            expected_target = Path(previous.link_target)
            target_matches = path_comparison_key(
                actual_target,
                windows=windows_path_semantics,
            ) == path_comparison_key(
                expected_target,
                windows=windows_path_semantics,
            )
        except (OSError, RuntimeError):
            target_matches = False
        if not target_matches:
            return replace(
                base,
                disposition=Disposition.CONFLICT,
                detail="previously managed Skill link now points elsewhere",
            )
        return base

    if destination.is_symlink() or not destination.is_dir():
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed Skill copy was replaced by another path type",
        )
    marker = read_managed_marker(destination)
    if marker is None or marker.get("source_id") != previous.source_id:
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed Skill copy has no matching ownership marker",
        )
    installed_digest = marker.get("installed_digest")
    if not isinstance(installed_digest, str) or not managed_copy_digest_matches(destination, installed_digest):
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed Skill copy was modified after installation",
        )
    return replace(base, source_digest=installed_digest)


def _registration_hints(
    config: BridgeConfig,
    inventory: CatalogInventory,
    resolved: ResolvedInventory,
    host_platform: Platform,
) -> tuple[CommandHint, ...]:
    hints: list[CommandHint] = []
    build_root = marketplace_publish_path(config)
    for target in config.targets:
        if not target.enabled or target.platform is not host_platform:
            continue
        selected_names = desired_plugin_names(target, inventory, resolved.hooks_for_target(target))
        registration = read_registration_state(config, target)
        previous_names = registration.plugins
        current_marketplace_source = registration_marketplace_source(config, target)
        source_changed = bool(
            previous_names
            and registration.marketplace_source is not None
            and registration.marketplace_source != current_marketplace_source
        )
        removed_names = (
            previous_names if source_changed else tuple(name for name in previous_names if name not in selected_names)
        )
        if not selected_names and not removed_names:
            continue

        executable = (
            str(target.executable)
            if target.executable is not None
            else ("codex" if target.product is Product.CODEX else "claude")
        )
        environment = product_home_environment(target)
        environment_unsets = product_home_environment_unsets(target)

        if target.product is Product.CODEX:
            for name in removed_names:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "remove", f"{name}@agent-config-bridge"),
                        reason=(
                            f"remove bridge-managed Codex plugin {name} before marketplace relocation"
                            if source_changed
                            else f"remove deselected bridge-managed Codex plugin {name}"
                        ),
                        environment_unsets=environment_unsets,
                    )
                )
            if source_changed:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "marketplace", "remove", "agent-config-bridge"),
                        reason="remove the relocated Codex marketplace before registering its new source",
                        environment_unsets=environment_unsets,
                    )
                )
            if selected_names:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "marketplace", "add", str(build_root)),
                        reason="register or refresh the stable Codex marketplace",
                        environment_unsets=environment_unsets,
                    )
                )
            for name in selected_names:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "add", f"{name}@agent-config-bridge"),
                        reason=f"install or refresh rendered Codex plugin {name}",
                        environment_unsets=environment_unsets,
                    )
                )
            if previous_names and not selected_names and not source_changed:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "marketplace", "remove", "agent-config-bridge"),
                        reason="remove the unused Codex marketplace",
                        environment_unsets=environment_unsets,
                    )
                )
        else:
            for name in removed_names:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(
                            executable,
                            "plugin",
                            "uninstall",
                            f"{name}@agent-config-bridge",
                            "--scope",
                            "user",
                            "--keep-data",
                        ),
                        reason=(
                            f"remove bridge-managed Claude Code plugin {name} before marketplace relocation"
                            if source_changed
                            else f"remove deselected bridge-managed Claude Code plugin {name}"
                        ),
                        environment_unsets=environment_unsets,
                    )
                )
            if source_changed:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "marketplace", "remove", "agent-config-bridge"),
                        reason="remove the relocated Claude Code marketplace before registering its new source",
                        environment_unsets=environment_unsets,
                    )
                )
            if selected_names:
                hints.extend(
                    (
                        CommandHint(
                            target=target.name,
                            platform=target.platform,
                            environment=environment,
                            argv=(executable, "plugin", "marketplace", "add", str(build_root)),
                            reason="register the stable Claude Code marketplace",
                            environment_unsets=environment_unsets,
                        ),
                        CommandHint(
                            target=target.name,
                            platform=target.platform,
                            environment=environment,
                            argv=(executable, "plugin", "marketplace", "update", "agent-config-bridge"),
                            reason="refresh the Claude Code marketplace cache",
                            environment_unsets=environment_unsets,
                        ),
                    )
                )
            for name in selected_names:
                hints.extend(
                    (
                        CommandHint(
                            target=target.name,
                            platform=target.platform,
                            environment=environment,
                            argv=(
                                executable,
                                "plugin",
                                "install",
                                f"{name}@agent-config-bridge",
                                "--scope",
                                "user",
                            ),
                            reason=f"install rendered Claude Code plugin {name} when absent",
                            environment_unsets=environment_unsets,
                        ),
                        CommandHint(
                            target=target.name,
                            platform=target.platform,
                            environment=environment,
                            argv=(
                                executable,
                                "plugin",
                                "update",
                                f"{name}@agent-config-bridge",
                                "--scope",
                                "user",
                            ),
                            reason=f"refresh rendered Claude Code plugin {name}",
                            environment_unsets=environment_unsets,
                        ),
                    )
                )
            if previous_names and not selected_names and not source_changed:
                hints.append(
                    CommandHint(
                        target=target.name,
                        platform=target.platform,
                        environment=environment,
                        argv=(executable, "plugin", "marketplace", "remove", "agent-config-bridge"),
                        reason="remove the unused Claude Code marketplace",
                        environment_unsets=environment_unsets,
                    )
                )
    return tuple(hints)
