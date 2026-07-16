"""Build read-only synchronization plans for configured product targets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from agent_config_bridge.catalog import Artifact, CatalogInventory
from agent_config_bridge.filesystem import read_managed_marker, tree_digest
from agent_config_bridge.governance import ResolvedInventory, resolve_inventory
from agent_config_bridge.models import BridgeConfig, Component, LinkMode, Platform, Product, Surface, TargetConfig
from agent_config_bridge.path_safety import path_comparison_key, paths_overlap, read_symlink_target
from agent_config_bridge.platforms import (
    current_platform,
    product_home_environment,
    product_home_environment_unsets,
)
from agent_config_bridge.renderer import (
    marketplace_build_path,
    marketplace_is_current,
    marketplace_publish_path,
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
    SettingsPatchPlan,
    build_settings_patch,
    discover_settings_fragments,
    plan_settings_patch,
    settings_patch_digest,
)
from agent_config_bridge.state import (
    BridgeStateError,
    SettingsState,
    SkillStateEntry,
    desired_plugin_names,
    effective_link_mode,
    find_orphaned_target_states,
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
]


class Operation(StrEnum):
    """A filesystem or rendering operation."""

    LINK = "link"
    COPY = "copy"
    RENDER = "render"
    PATCH = "patch"
    REMOVE = "remove"


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
    """A product command required to register or install rendered plugins."""

    target: str
    platform: Platform
    environment: tuple[tuple[str, str], ...]
    argv: tuple[str, ...]
    reason: str
    environment_unsets: tuple[str, ...] = ()


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

    for target_name in find_orphaned_target_states(config):
        warnings.append(
            f"{target_name}: ownership state has no enabled target; restore the same target identity and "
            "reconcile with components=[] before deleting or changing it"
        )

    if any(
        Component.PLUGINS in target.components or Component.HOOKS in target.components for target in enabled_targets
    ):
        build_path = marketplace_build_path(config, inventory)
        publish_path = marketplace_publish_path(config)
        if not os.path.lexists(publish_path):
            disposition = Disposition.CREATE
        else:
            disposition = Disposition.NOOP if marketplace_is_current(config, inventory) else Disposition.UPDATE
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
            reviews.extend(_hook_reviews(target, inventory))
        if Component.PLUGINS in target.components:
            reviews.extend(_plugin_reviews(target, inventory))

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

    commands = _registration_hints(config, inventory, host_platform)
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


def _settings_action(
    target: TargetConfig,
    inventory: CatalogInventory,
    plan: SettingsPatchPlan,
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
        component=Component.SETTINGS,
        target=target.name,
        name="product-settings",
        source=inventory.root / "settings",
        destination=plan.destination,
        detail=f"merge owned setting leaves ({details})",
        source_id=f"settings/{target.name}",
        source_digest=settings_patch_digest(plan.desired),
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
    return _plan_copy(skill, destination, target, previous)


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
                source_digest=tree_digest(skill.path),
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
        source_digest=tree_digest(skill.path),
    )


def _plan_copy(
    skill: Artifact,
    destination: Path,
    target: TargetConfig,
    previous: SkillStateEntry | None,
) -> Action:
    source_digest = tree_digest(skill.path)
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
    if destination.is_symlink() or not destination.is_dir():
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="copy destination exists with an unsupported type",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )

    if previous is None or previous.mode is not LinkMode.COPY:
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="existing copy destination has no matching target ownership state",
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
    current_digest = tree_digest(destination)
    if installed_digest != current_digest:
        return _conflict_action(
            operation=Operation.COPY,
            skill=skill,
            destination=destination,
            target=target,
            detail="managed copy was modified after installation",
            source_id=_skill_source_id(skill),
            source_digest=source_digest,
        )
    if source_digest == current_digest:
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
        detail="update unchanged managed copy and retain backup",
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


def _hook_reviews(target: TargetConfig, inventory: CatalogInventory) -> list[str]:
    reviews: list[str] = []
    for artifact in inventory.hooks:
        for overlay in ("common", target.product.value):
            path = artifact.path / overlay / "hooks.json"
            if path.is_file():
                reviews.extend(_hook_document_reviews(target.name, artifact.name, overlay, path))
    return reviews


def _plugin_reviews(target: TargetConfig, inventory: CatalogInventory) -> list[str]:
    reviews: list[str] = []
    for artifact in inventory.plugins:
        for overlay in ("common", target.product.value):
            root = artifact.path / overlay
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("hooks.json")):
                reviews.extend(_hook_document_reviews(target.name, artifact.name, overlay, path))
            for path in sorted(root.rglob(".mcp.json")):
                reviews.extend(_sensitive_json_reviews(target.name, artifact.name, "MCP", path))
            manifest_directory = ".codex-plugin" if target.product is Product.CODEX else ".claude-plugin"
            manifest = root / manifest_directory / "plugin.json"
            if manifest.is_file():
                reviews.extend(_sensitive_json_reviews(target.name, artifact.name, "manifest", manifest))
    return reviews


def _hook_document_reviews(target: str, artifact: str, overlay: str, path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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
                executable = handler.get("command") or handler.get("url") or handler.get("prompt") or "<inline>"
                reviews.append(
                    f"{target}: hook {artifact}/{overlay} event={event} matcher={matcher!r} "
                    f"type={handler_type!r} handler={executable!r}"
                )
    return reviews


def _sensitive_json_reviews(target: str, artifact: str, kind: str, path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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
    if not isinstance(installed_digest, str) or tree_digest(destination) != installed_digest:
        return replace(
            base,
            disposition=Disposition.CONFLICT,
            detail="previously managed Skill copy was modified after installation",
        )
    return replace(base, source_digest=installed_digest)


def _registration_hints(
    config: BridgeConfig,
    inventory: CatalogInventory,
    host_platform: Platform,
) -> tuple[CommandHint, ...]:
    hints: list[CommandHint] = []
    build_root = marketplace_publish_path(config)
    for target in config.targets:
        if not target.enabled or target.platform is not host_platform:
            continue
        selected_names = desired_plugin_names(target, inventory)
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
