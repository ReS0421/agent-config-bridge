"""Explicit hand-over of MCP server ownership: ``mcp adopt`` and ``mcp release`` (ADR-0009).

Both commands change only Bridge ownership records in ``mcp.json``; neither
writes Codex ``config.toml`` or the Claude user config. Adoption records
ownership of unowned live entries that exactly equal their catalog
declaration. Release is its inverse: it forgets ownership so that a missing or
drifted entry stops blocking plans and can be re-created, adopted, or removed
by hand.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from agent_config_bridge.catalog import CatalogInventory
from agent_config_bridge.governance import resolve_inventory
from agent_config_bridge.mcp import (
    McpAdoptionEntry,
    McpAdoptionStatus,
    McpError,
    claude_adoption_entry,
    codex_adoption_entry,
    discover_mcp_servers,
    read_claude_mcp_servers,
    render_claude_spec,
    render_codex_leaves,
)
from agent_config_bridge.models import BridgeConfig, Component, Product, TargetConfig
from agent_config_bridge.planner import desired_mcp_servers
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.settings import read_settings_document
from agent_config_bridge.state import (
    McpState,
    SettingsState,
    SettingStateEntry,
    find_orphaned_target_states,
    read_mcp_state,
    read_settings_state,
    write_mcp_state,
)

__all__ = [
    "McpReleaseEntry",
    "McpReleaseStatus",
    "adoption_entries",
    "record_adoption",
    "record_release",
    "release_entries",
    "require_no_orphaned_state",
    "select_mcp_targets",
]


class McpReleaseStatus(StrEnum):
    """The outcome of one ownership release."""

    RELEASABLE = "releasable"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class McpReleaseEntry:
    """One owned MCP server whose Bridge ownership can be forgotten.

    ``detail`` names owned keys only, never values.
    """

    target: str
    product: Product
    name: str
    status: McpReleaseStatus
    detail: str


def require_no_orphaned_state(config: BridgeConfig) -> None:
    """Fail closed when ownership state exists for a target that is no longer enabled."""

    orphaned_targets = find_orphaned_target_states(config)
    if orphaned_targets:
        raise McpError("ownership state has no enabled target; restore and reconcile: " + ", ".join(orphaned_targets))


def select_mcp_targets(
    config: BridgeConfig,
    selected: Iterable[str],
    *,
    command: str,
    include_owned: bool,
) -> tuple[TargetConfig, ...]:
    """Select enabled targets on this platform for ``mcp adopt`` or ``mcp release``.

    Without explicit names, every enabled same-platform target that selects
    ``mcp`` is used; with ``include_owned`` a target that still holds MCP
    ownership after deselecting ``mcp`` is used as well. Explicit targets on
    another platform, or without ``mcp`` (and without ownership when
    ``include_owned``), are errors.
    """

    known = {target.name: target for target in config.targets if target.enabled}
    host_platform = current_platform()
    requested = sorted(set(selected))

    requirement = "select the mcp component" + (" or hold MCP ownership" if include_owned else "")

    def eligible(target: TargetConfig) -> bool:
        if Component.MCP in target.components:
            return True
        return include_owned and not read_mcp_state(config, target).is_empty

    if not requested:
        targets = tuple(
            target for _, target in sorted(known.items()) if target.platform is host_platform and eligible(target)
        )
        if not targets:
            raise McpError(f"no enabled {host_platform.value} targets {requirement}")
        return targets
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise McpError(f"unknown or disabled targets: {', '.join(unknown)}")
    for name in requested:
        target = known[name]
        if target.platform is not host_platform:
            raise McpError(f"target {name!r} uses {target.platform.value}; run {command} from that platform")
        if not eligible(target):
            raise McpError(f"target {name!r} does not {requirement}")
    return tuple(known[name] for name in requested)


def adoption_entries(
    config: BridgeConfig,
    inventory: CatalogInventory,
    targets: tuple[TargetConfig, ...],
    names: frozenset[str],
) -> tuple[McpAdoptionEntry, ...]:
    """Compare live entries with rendered declarations without writing anything."""

    resolved = resolve_inventory(inventory)
    servers = discover_mcp_servers(inventory.root)
    entries: list[McpAdoptionEntry] = []
    desired_names: set[str] = set()
    for target in targets:
        desired = desired_mcp_servers(target, resolved, servers)
        desired_names.update(server.name for server in desired)
        selected = tuple(server for server in desired if not names or server.name in names)
        if not selected:
            continue
        state = read_mcp_state(config, target)
        if target.product is Product.CODEX:
            owned_paths = frozenset(entry.path for entry in state.leaves.entries)
            settings_paths = frozenset(entry.path for entry in read_settings_state(config, target).entries)
            document = read_settings_document(Product.CODEX, target.config_home / "config.toml")
            entries.extend(
                codex_adoption_entry(
                    target.name,
                    server.name,
                    render_codex_leaves(server, target),
                    document,
                    owned_paths=owned_paths,
                    foreign_paths=settings_paths,
                )
                for server in selected
            )
        else:
            owned = dict(state.claude_servers)
            live = read_claude_mcp_servers(target)
            entries.extend(
                claude_adoption_entry(
                    target.name,
                    server.name,
                    render_claude_spec(server, target),
                    live.get(server.name),
                    owned.get(server.name),
                )
                for server in selected
            )
    unknown = names - desired_names
    if unknown:
        raise McpError(f"MCP servers are not delivered to the selected targets: {', '.join(sorted(unknown))}")
    return tuple(entries)


def record_adoption(
    config: BridgeConfig,
    targets: tuple[TargetConfig, ...],
    adoptable: tuple[McpAdoptionEntry, ...],
) -> None:
    """Record ownership for reviewed adoptable entries; product files are untouched."""

    for target in targets:
        target_entries = tuple(
            entry for entry in adoptable if entry.target == target.name and entry.status is McpAdoptionStatus.ADOPTABLE
        )
        if not target_entries:
            continue
        state = read_mcp_state(config, target)
        if target.product is Product.CODEX:
            leaves = state.leaves
            entries = (
                *leaves.entries,
                *(
                    SettingStateEntry(source_id=leaf.source_id, path=leaf.path, value_digest=leaf.digest)
                    for entry in target_entries
                    for leaf in entry.leaves
                ),
            )
            # The adopted server table counts as Bridge-held, like a table the
            # Bridge created, so that removing the server removes the table.
            containers = set(leaves.created_containers) | {("mcp_servers", entry.name) for entry in target_entries}
            write_mcp_state(
                config,
                target,
                McpState(
                    leaves=SettingsState(
                        file_created=leaves.file_created,
                        created_containers=tuple(sorted(containers, key=lambda path: (len(path), path))),
                        entries=tuple(sorted(entries, key=lambda entry: entry.path)),
                    ),
                    claude_servers=state.claude_servers,
                ),
            )
        else:
            ownership = dict(state.claude_servers)
            for entry in target_entries:
                if entry.digest is None:
                    raise McpError(f"adoption entry has no spec digest: {target.name}:{entry.name}")
                ownership[entry.name] = entry.digest
            write_mcp_state(
                config,
                target,
                McpState(leaves=state.leaves, claude_servers=tuple(sorted(ownership.items()))),
            )


def release_entries(
    config: BridgeConfig,
    targets: tuple[TargetConfig, ...],
    names: frozenset[str],
) -> tuple[McpReleaseEntry, ...]:
    """List owned MCP servers whose ownership can be forgotten, without writing anything.

    Only Bridge ownership records are read; servers need not still be in the
    catalog, so deselected servers can be released too.
    """

    entries: list[McpReleaseEntry] = []
    owned_names: set[str] = set()
    for target in targets:
        state = read_mcp_state(config, target)
        if target.product is Product.CODEX:
            keys_by_name: dict[str, list[str]] = {}
            for entry in state.leaves.entries:
                keys_by_name.setdefault(entry.path[1], []).append(entry.path[2])
            owned_names.update(keys_by_name)
            entries.extend(
                McpReleaseEntry(
                    target.name,
                    Product.CODEX,
                    name,
                    McpReleaseStatus.RELEASABLE,
                    f"forget owned leaves: {', '.join(sorted(keys))}",
                )
                for name, keys in sorted(keys_by_name.items())
                if not names or name in names
            )
        else:
            owned_names.update(name for name, _ in state.claude_servers)
            entries.extend(
                McpReleaseEntry(
                    target.name,
                    Product.CLAUDE_CODE,
                    name,
                    McpReleaseStatus.RELEASABLE,
                    "forget the recorded user-scope registration",
                )
                for name, _ in state.claude_servers
                if not names or name in names
            )
    unknown = names - owned_names
    if unknown:
        raise McpError(f"MCP servers are not owned by the selected targets: {', '.join(sorted(unknown))}")
    return tuple(entries)


def record_release(
    config: BridgeConfig,
    targets: tuple[TargetConfig, ...],
    releasable: tuple[McpReleaseEntry, ...],
) -> None:
    """Forget ownership of reviewed entries; product files are untouched."""

    for target in targets:
        released = {entry.name for entry in releasable if entry.target == target.name}
        if not released:
            continue
        state = read_mcp_state(config, target)
        kept_entries = tuple(entry for entry in state.leaves.entries if entry.path[1] not in released)
        kept_containers = tuple(
            container
            for container in state.leaves.created_containers
            if any(entry.path[: len(container)] == container for entry in kept_entries)
        )
        write_mcp_state(
            config,
            target,
            McpState(
                leaves=SettingsState(
                    file_created=state.leaves.file_created and bool(kept_entries),
                    created_containers=kept_containers,
                    entries=kept_entries,
                ),
                claude_servers=tuple(item for item in state.claude_servers if item[0] not in released),
            ),
        )
