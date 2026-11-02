"""Conflict-aware, owned-leaf projection for product settings files.

The settings engine deliberately manages individual leaves instead of replacing
an entire vendor settings document.  It accepts product-specific fragments from
the canonical catalog, plans changes against the current destination and a
digest-only ownership record, and applies a reviewed plan atomically.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias, cast

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Comment, InlineTable, Null, Table, Whitespace

from agent_config_bridge.models import Product

__all__ = [
    "CODEX_MCP_SERVERS_IN_SETTINGS",
    "OwnedSettingLeaf",
    "OwnedTableRule",
    "SettingChange",
    "SettingDisposition",
    "SettingIssue",
    "SettingLeafSpec",
    "SettingsApplyResult",
    "SettingsError",
    "SettingsFragment",
    "SettingsPatchPlan",
    "apply_settings_patch",
    "build_settings_patch",
    "discover_settings_fragments",
    "merge_settings_fragments",
    "plan_settings_patch",
    "read_settings_document",
    "safe_key",
    "settings_patch_digest",
    "setting_value_digest",
]

SettingScalar: TypeAlias = str | int | float | bool | None
SettingValue: TypeAlias = SettingScalar | list["SettingValue"] | dict[str, "SettingValue"]
SettingPath: TypeAlias = tuple[str, ...]

_ARTIFACT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_PRODUCT_FRAGMENT = {
    Product.CODEX: ("codex", "config.toml"),
    Product.CLAUDE_CODE: ("claude-code", "settings.json"),
}
_MISSING = object()
CODEX_MCP_SERVERS_IN_SETTINGS = (
    "Codex settings fragment must not declare mcp_servers; declare MCP servers under catalog/mcp"
)
"""Shared validation message: MCP servers have their own component and ownership (ADR-0009)."""


class SettingsError(RuntimeError):
    """Raised when settings sources, ownership, or destinations are unsafe."""


class SettingDisposition(StrEnum):
    """The result of comparing one desired or formerly owned setting leaf."""

    CREATE = "create"
    UPDATE = "update"
    REMOVE = "remove"
    NOOP = "noop"
    CONFLICT = "conflict"


class SettingIssue(StrEnum):
    """Why a setting change is a conflict."""

    UNOWNED = "unowned"
    """An existing or blocking value is not Bridge-owned."""
    MISSING = "missing"
    """A Bridge-owned value is absent or now blocked by a non-table ancestor."""
    DRIFT = "drift"
    """A Bridge-owned value changed after installation."""
    LEFTOVER = "leftover"
    """Removing owned leaves would leave an unremovable remainder of an owned table."""


@dataclass(frozen=True, slots=True)
class OwnedTableRule:
    """Remove a whole table together with its last owned leaves.

    When a plan removes every remaining owned leaf below ``path`` and no
    desired leaf stays there, the table is deleted as a unit if its other
    children are only ``removable_children`` or ``removable_when_empty``
    children holding an empty array or table. Any other unowned child makes
    the removal a :attr:`SettingIssue.LEFTOVER` conflict. Codex rejects an
    ``mcp_servers.<name>`` table without a transport, so its tool approvals are
    removed with the server instead of left behind (ADR-0009).
    """

    path: SettingPath
    removable_children: tuple[str, ...] = ()
    removable_when_empty: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingLeafSpec:
    """One desired setting leaf parsed from a canonical product fragment."""

    source_id: str
    path: SettingPath
    value: SettingValue
    digest: str


@dataclass(frozen=True, slots=True)
class OwnedSettingLeaf:
    """Digest-only ownership state for one previously installed setting leaf.

    ``created_parents`` records only container paths created to hold this leaf;
    it contains no setting values.  Cleanup may prune those containers when
    they are still mappings and have become empty.
    """

    source_id: str
    path: SettingPath
    digest: str
    created_parents: tuple[SettingPath, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingsFragment:
    """A validated product-specific settings fragment from one catalog bundle."""

    name: str
    product: Product
    path: Path
    leaves: tuple[SettingLeafSpec, ...]


@dataclass(frozen=True, slots=True)
class SettingChange:
    """One planned setting leaf reconciliation."""

    disposition: SettingDisposition
    path: SettingPath
    source_id: str
    detail: str
    issue: SettingIssue | None = None
    keys: tuple[str, ...] = ()
    """Raw key names behind a :attr:`SettingIssue.LEFTOVER` conflict; escape them with :func:`safe_key`."""


@dataclass(frozen=True, slots=True)
class SettingsPatchPlan:
    """A stale-checkable settings plan that does not contain prior values.

    ``owned_tables`` are the table rules the plan was computed with, and
    ``removed_tables`` the tables that applying it deletes as a unit.
    ``claimed_tables`` are the tables desired leaves may create only while no
    unowned table exists at that path.
    """

    product: Product
    destination: Path
    desired: tuple[SettingLeafSpec, ...]
    previous: tuple[OwnedSettingLeaf, ...]
    changes: tuple[SettingChange, ...]
    resulting_ownership: tuple[OwnedSettingLeaf, ...]
    destination_existed: bool
    destination_digest: str | None
    owned_tables: tuple[OwnedTableRule, ...] = ()
    removed_tables: tuple[SettingPath, ...] = ()
    claimed_tables: tuple[SettingPath, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        """Return whether applying the plan would touch unsafe state."""

        return any(change.disposition is SettingDisposition.CONFLICT for change in self.changes)

    @property
    def has_changes(self) -> bool:
        """Return whether the destination document requires a write."""

        return any(
            change.disposition in {SettingDisposition.CREATE, SettingDisposition.UPDATE, SettingDisposition.REMOVE}
            for change in self.changes
        )


@dataclass(frozen=True, slots=True)
class SettingsApplyResult:
    """The digest-only ownership result of applying a settings plan."""

    changed: bool
    ownership: tuple[OwnedSettingLeaf, ...]


def discover_settings_fragments(catalog_root: Path) -> tuple[SettingsFragment, ...]:
    """Discover and parse strict product-specific settings bundles.

    The accepted layout is ``settings/<bundle>/codex/config.toml`` and/or
    ``settings/<bundle>/claude-code/settings.json``.  Unknown visible entries,
    symlinks, reparse points, special files, empty fragments, and non-portable
    bundle names are rejected.

    Args:
        catalog_root: Root of the canonical catalog, not its ``settings`` child.

    Returns:
        Deterministically ordered validated fragments.

    Raises:
        SettingsError: If the settings catalog is malformed or unsafe.
    """

    settings_root = catalog_root / "settings"
    if not os.path.lexists(settings_root):
        return ()
    _require_real_directory(settings_root, "settings catalog group")

    fragments: list[SettingsFragment] = []
    portable_names: dict[str, str] = {}
    for bundle_path in sorted(settings_root.iterdir(), key=lambda candidate: candidate.name):
        if bundle_path.name.startswith("."):
            continue
        _validate_bundle_name(bundle_path.name, bundle_path)
        portable_name = bundle_path.name.rstrip(" .").casefold()
        if previous_name := portable_names.get(portable_name):
            raise SettingsError(
                f"settings bundle names collide on case-insensitive filesystems: "
                f"{previous_name!r} and {bundle_path.name!r}"
            )
        portable_names[portable_name] = bundle_path.name
        _require_real_directory(bundle_path, "settings bundle")

        recognized_directories = {directory for directory, _ in _PRODUCT_FRAGMENT.values()}
        visible_children = tuple(child for child in bundle_path.iterdir() if not child.name.startswith("."))
        unknown = sorted(child.name for child in visible_children if child.name not in recognized_directories)
        if unknown:
            raise SettingsError(
                f"settings bundle contains unsupported entries; only product-specific fragments are allowed: "
                f"{bundle_path}: {', '.join(unknown)}"
            )

        bundle_fragments = 0
        for product in Product:
            directory_name, filename = _PRODUCT_FRAGMENT[product]
            product_root = bundle_path / directory_name
            if not os.path.lexists(product_root):
                continue
            _require_real_directory(product_root, f"{product.value} settings fragment directory")
            visible_product_children = tuple(
                child for child in product_root.iterdir() if not child.name.startswith(".")
            )
            unknown_product_children = sorted(
                child.name for child in visible_product_children if child.name != filename
            )
            if unknown_product_children:
                raise SettingsError(
                    f"{product.value} settings fragment contains unsupported entries: "
                    f"{product_root}: {', '.join(unknown_product_children)}"
                )
            fragment_path = product_root / filename
            if not os.path.lexists(fragment_path):
                raise SettingsError(f"{product.value} settings fragment is missing {filename}: {product_root}")
            document = _read_fragment_document(product, fragment_path)
            if product is Product.CODEX and "mcp_servers" in document:
                raise SettingsError(f"{CODEX_MCP_SERVERS_IN_SETTINGS}: {fragment_path}")
            leaves = _flatten_document(document, source_prefix=f"settings/{bundle_path.name}/{product.value}")
            if not leaves:
                raise SettingsError(f"settings fragment has no setting leaves: {fragment_path}")
            for leaf in leaves:
                _validate_leaf_spec(leaf)
            fragments.append(
                SettingsFragment(
                    name=bundle_path.name,
                    product=product,
                    path=fragment_path,
                    leaves=leaves,
                )
            )
            bundle_fragments += 1
        if bundle_fragments == 0:
            raise SettingsError(f"settings bundle has no product-specific fragments: {bundle_path}")

    return tuple(fragments)


def merge_settings_fragments(
    product: Product,
    fragments: Sequence[SettingsFragment],
) -> tuple[SettingLeafSpec, ...]:
    """Merge one product's fragments, rejecting duplicate or ancestor paths."""

    selected = sorted(
        (fragment for fragment in fragments if fragment.product is product),
        key=lambda fragment: (fragment.name, str(fragment.path)),
    )
    leaves: list[SettingLeafSpec] = []
    by_path: dict[SettingPath, SettingLeafSpec] = {}
    for fragment in selected:
        if fragment.product is not product:
            continue
        for leaf in fragment.leaves:
            _validate_leaf_spec(leaf)
            collision = _colliding_path(leaf.path, by_path)
            if collision is not None:
                previous = by_path[collision]
                raise SettingsError(
                    f"settings fragments define colliding paths "
                    f"{_format_path(previous.path)} ({previous.source_id}) and "
                    f"{_format_path(leaf.path)} ({leaf.source_id})"
                )
            by_path[leaf.path] = leaf
            leaves.append(leaf)
    return tuple(sorted(leaves, key=lambda leaf: leaf.path))


def build_settings_patch(
    product: Product,
    fragments: Sequence[SettingsFragment],
) -> tuple[SettingLeafSpec, ...]:
    """Compatibility name for building a merged desired leaf set."""

    return merge_settings_fragments(product, fragments)


def plan_settings_patch(
    product: Product,
    destination: Path,
    desired: Sequence[SettingLeafSpec],
    previous: Sequence[OwnedSettingLeaf] = (),
    *,
    owned_tables: Sequence[OwnedTableRule] = (),
    claimed_tables: Sequence[SettingPath] = (),
) -> SettingsPatchPlan:
    """Plan owned-leaf changes against the current product settings document.

    New leaves may claim only absent paths.  Existing unowned paths are always
    conflicts, even when their values happen to equal the desired value.  A
    previously owned current value must match its recorded digest before it can
    be updated or removed.  ``owned_tables`` lists tables that must disappear
    as a unit with their last owned leaves; see :class:`OwnedTableRule`.

    ``claimed_tables`` lists tables that desired leaves may only create, never
    merge into: when no previously owned leaf lies below a claimed table and
    the table already exists with any child, the table is a
    :attr:`SettingIssue.UNOWNED` conflict and its desired leaves are not
    planned. An MCP server table that already holds credentials, approvals, or
    another transport must be adopted explicitly instead (ADR-0009).
    """

    _validate_destination_name(product, destination)
    desired_leaves = _validate_desired_leaves(desired)
    previous_leaves = _validate_owned_leaves(previous)
    raw, document = _read_destination(product, destination)
    existed = raw is not None
    snapshot_digest = _bytes_digest(raw) if raw is not None else None
    current: Mapping[str, object] = document

    desired_by_path = {leaf.path: leaf for leaf in desired_leaves}
    previous_by_path = {leaf.path: leaf for leaf in previous_leaves}
    inherited_created_parents = {parent for leaf in previous_leaves for parent in leaf.created_parents}
    changes: list[SettingChange] = []
    resulting_ownership: list[OwnedSettingLeaf] = []

    claims = tuple(sorted(set(claimed_tables)))
    blocked_claims = _plan_claimed_tables(current, desired_leaves, previous_leaves, claims, changes)

    for leaf in desired_leaves:
        if any(_is_strict_prefix(table, leaf.path) for table in blocked_claims):
            continue
        owned = previous_by_path.get(leaf.path)
        current_value, blocked_at = _lookup_leaf(current, leaf.path)
        if owned is not None:
            if current_value is _MISSING:
                detail = (
                    f"owned path is blocked by a non-table ancestor {_format_path(blocked_at)}"
                    if blocked_at is not None
                    else "previously owned setting is missing"
                )
                changes.append(_change(SettingDisposition.CONFLICT, leaf, detail, SettingIssue.MISSING))
                continue
            if isinstance(current_value, Mapping):
                changes.append(
                    _change(
                        SettingDisposition.CONFLICT,
                        leaf,
                        "owned leaf was replaced by a settings table",
                        SettingIssue.DRIFT,
                    )
                )
                continue
            current_digest = setting_value_digest(cast(SettingValue, current_value))
            if current_digest != owned.digest:
                changes.append(
                    _change(
                        SettingDisposition.CONFLICT,
                        leaf,
                        "current value no longer matches ownership digest",
                        SettingIssue.DRIFT,
                    )
                )
                continue
            disposition = SettingDisposition.NOOP if current_digest == leaf.digest else SettingDisposition.UPDATE
            detail = "owned setting is current" if disposition is SettingDisposition.NOOP else "update owned setting"
            changes.append(_change(disposition, leaf, detail))
            resulting_ownership.append(
                OwnedSettingLeaf(
                    source_id=leaf.source_id,
                    path=leaf.path,
                    digest=leaf.digest,
                    created_parents=owned.created_parents,
                )
            )
            continue

        if current_value is not _MISSING:
            changes.append(
                _change(SettingDisposition.CONFLICT, leaf, "existing setting is not bridge-owned", SettingIssue.UNOWNED)
            )
            continue
        if blocked_at is not None:
            changes.append(
                _change(
                    SettingDisposition.CONFLICT,
                    leaf,
                    f"desired path is blocked by existing non-table ancestor {_format_path(blocked_at)}",
                    SettingIssue.UNOWNED,
                )
            )
            continue

        created_parents = _created_parent_paths(current, leaf.path)
        inherited = tuple(
            parent for parent in sorted(inherited_created_parents) if _is_strict_prefix(parent, leaf.path)
        )
        created_parents = tuple(sorted(set(created_parents) | set(inherited)))
        changes.append(_change(SettingDisposition.CREATE, leaf, "create absent setting"))
        resulting_ownership.append(
            OwnedSettingLeaf(
                source_id=leaf.source_id,
                path=leaf.path,
                digest=leaf.digest,
                created_parents=created_parents,
            )
        )

    for owned in previous_leaves:
        if owned.path in desired_by_path:
            continue
        current_value, blocked_at = _lookup_leaf(current, owned.path)
        if current_value is _MISSING:
            detail = (
                f"formerly owned path is blocked by non-table ancestor {_format_path(blocked_at)}; "
                "no removal is required"
                if blocked_at is not None
                else "formerly owned setting is already absent"
            )
            changes.append(
                SettingChange(
                    disposition=SettingDisposition.NOOP,
                    path=owned.path,
                    source_id=owned.source_id,
                    detail=detail,
                )
            )
            continue
        if isinstance(current_value, Mapping):
            changes.append(
                SettingChange(
                    disposition=SettingDisposition.CONFLICT,
                    path=owned.path,
                    source_id=owned.source_id,
                    detail="formerly owned leaf was replaced by a settings table",
                    issue=SettingIssue.DRIFT,
                )
            )
            continue
        if setting_value_digest(cast(SettingValue, current_value)) != owned.digest:
            changes.append(
                SettingChange(
                    disposition=SettingDisposition.CONFLICT,
                    path=owned.path,
                    source_id=owned.source_id,
                    detail="formerly owned setting changed after installation",
                    issue=SettingIssue.DRIFT,
                )
            )
            continue
        changes.append(
            SettingChange(
                disposition=SettingDisposition.REMOVE,
                path=owned.path,
                source_id=owned.source_id,
                detail="remove deselected owned setting",
            )
        )

    rules = tuple(owned_tables)
    removed_tables = _plan_owned_table_removals(current, desired_leaves, previous_leaves, rules, changes)
    return SettingsPatchPlan(
        product=product,
        destination=destination,
        desired=desired_leaves,
        previous=previous_leaves,
        changes=tuple(sorted(changes, key=lambda change: (change.path, change.disposition.value))),
        resulting_ownership=tuple(sorted(resulting_ownership, key=lambda leaf: leaf.path)),
        destination_existed=existed,
        destination_digest=snapshot_digest,
        owned_tables=rules,
        removed_tables=removed_tables,
        claimed_tables=claims,
    )


def _plan_claimed_tables(
    current: Mapping[str, object],
    desired: Sequence[SettingLeafSpec],
    previous: Sequence[OwnedSettingLeaf],
    claims: Sequence[SettingPath],
    changes: list[SettingChange],
) -> frozenset[SettingPath]:
    """Return claimed tables that already exist unowned, appending their conflicts in place."""

    blocked: set[SettingPath] = set()
    for table_path in claims:
        _validate_setting_path(table_path)
        if any(_is_strict_prefix(table_path, leaf.path) for leaf in previous):
            continue
        claimants = [leaf for leaf in desired if _is_strict_prefix(table_path, leaf.path)]
        existing, _ = _lookup_leaf(current, table_path)
        if not claimants or not isinstance(existing, Mapping) or not existing:
            continue
        blocked.add(table_path)
        changes.append(
            SettingChange(
                disposition=SettingDisposition.CONFLICT,
                path=table_path,
                source_id=claimants[0].source_id,
                detail="existing table is not bridge-owned; desired leaves are not merged into it",
                issue=SettingIssue.UNOWNED,
            )
        )
    return frozenset(blocked)


def _plan_owned_table_removals(
    current: Mapping[str, object],
    desired: Sequence[SettingLeafSpec],
    previous: Sequence[OwnedSettingLeaf],
    rules: Sequence[OwnedTableRule],
    changes: list[SettingChange],
) -> tuple[SettingPath, ...]:
    """Apply :class:`OwnedTableRule` removals, appending their changes in place."""

    removed: list[SettingPath] = []
    changes_by_path = {change.path: change for change in changes}
    for rule in rules:
        _validate_setting_path(rule.path)
        owned = [leaf for leaf in previous if _is_strict_prefix(rule.path, leaf.path)]
        if not owned or any(_is_strict_prefix(rule.path, leaf.path) for leaf in desired):
            continue
        owned_changes = [changes_by_path[leaf.path] for leaf in owned if leaf.path in changes_by_path]
        if any(change.disposition is SettingDisposition.CONFLICT for change in owned_changes):
            continue
        removed_paths = {change.path for change in owned_changes if change.disposition is SettingDisposition.REMOVE}
        if not removed_paths:
            continue
        table, _ = _lookup_leaf(current, rule.path)
        if not isinstance(table, Mapping):
            continue
        removed_keys = {path[len(rule.path)] for path in removed_paths if len(path) == len(rule.path) + 1}
        remainder = sorted(key for key in table if key not in removed_keys)
        blocking = [
            key
            for key in remainder
            if key not in rule.removable_children and not (key in rule.removable_when_empty and table[key] in ([], {}))
        ]
        source_id = owned[0].source_id
        if blocking:
            changes.append(
                SettingChange(
                    disposition=SettingDisposition.CONFLICT,
                    path=rule.path,
                    source_id=source_id,
                    detail=(
                        "removing the owned leaves would leave unowned keys in the table: "
                        + ", ".join(safe_key(key) for key in blocking)
                    ),
                    issue=SettingIssue.LEFTOVER,
                    keys=tuple(blocking),
                )
            )
            continue
        removed.append(rule.path)
        changes.extend(
            SettingChange(
                disposition=SettingDisposition.REMOVE,
                path=(*rule.path, key),
                source_id=source_id,
                detail="remove unowned child together with its removed table",
            )
            for key in remainder
        )
    return tuple(sorted(removed))


def apply_settings_patch(plan: SettingsPatchPlan) -> SettingsApplyResult:
    """Atomically apply a conflict-free settings plan.

    The destination byte snapshot is rechecked before mutation.  Existing
    bytes are held in memory only; no backup containing unrelated vendor
    settings is persisted.  A same-directory temporary regular file is replaced
    atomically and removed if staging fails.
    """

    if plan.has_conflicts:
        conflicts = ", ".join(
            _format_path(change.path) for change in plan.changes if change.disposition is SettingDisposition.CONFLICT
        )
        raise SettingsError(f"refusing to apply settings plan with conflicts: {conflicts}")

    _validate_destination_name(plan.product, plan.destination)
    fresh_plan = plan_settings_patch(
        plan.product,
        plan.destination,
        plan.desired,
        plan.previous,
        owned_tables=plan.owned_tables,
        claimed_tables=plan.claimed_tables,
    )
    if fresh_plan != plan:
        raise SettingsError("settings plan or destination changed after planning; review a fresh plan")
    desired = fresh_plan.desired
    previous = fresh_plan.previous
    raw, _ = _read_destination(plan.product, plan.destination)
    existed = raw is not None
    digest = _bytes_digest(raw) if raw is not None else None
    if not plan.has_changes:
        return SettingsApplyResult(changed=False, ownership=plan.resulting_ownership)

    mutable_document = _parse_mutable_destination(plan.product, raw)
    desired_by_path = {leaf.path: leaf for leaf in desired}
    previous_by_path = {leaf.path: leaf for leaf in previous}

    removal_paths = [
        change.path
        for change in plan.changes
        if change.disposition is SettingDisposition.REMOVE and change.path in previous_by_path
    ]
    for path in sorted(removal_paths, key=lambda candidate: (-len(candidate), candidate)):
        _delete_path(mutable_document, path)
    trailing_comments: list[tuple[list[tuple[Any, Any]], str]] = []
    for table_path in sorted(plan.removed_tables, key=lambda candidate: (-len(candidate), candidate)):
        if plan.product is Product.CODEX:
            trailing_comments.extend(_delete_toml_table_keeping_comments(mutable_document, table_path))
        else:
            _delete_path(mutable_document, table_path)

    parent_paths = {parent for path in removal_paths for parent in previous_by_path[path].created_parents}
    for parent_path in sorted(parent_paths, key=lambda candidate: (-len(candidate), candidate)):
        _prune_empty_owned_parent(mutable_document, parent_path, plan.product)

    for change in plan.changes:
        if change.disposition not in {SettingDisposition.CREATE, SettingDisposition.UPDATE}:
            continue
        leaf = desired_by_path[change.path]
        _set_path(mutable_document, leaf.path, copy.deepcopy(leaf.value), plan.product)
    # Comments that ended the last table of a container go back after any
    # table added to that container, so they stay above what follows it.
    for body, text in trailing_comments:
        body.append((None, Whitespace(text)))

    output = _serialize_document(plan.product, mutable_document)
    _atomic_replace(plan.destination, output, expected_existed=existed, expected_digest=digest)
    return SettingsApplyResult(changed=True, ownership=plan.resulting_ownership)


def read_settings_document(product: Product, destination: Path) -> dict[str, SettingValue]:
    """Read and validate one product settings document without changing it.

    An absent destination is an empty document. The same strict parsing and
    real-file checks used before an owned-leaf patch apply here.
    """

    _validate_destination_name(product, destination)
    _, document = _read_destination(product, destination)
    return document


def setting_value_digest(value: SettingValue) -> str:
    """Return a type-stable SHA-256 digest for one setting value."""

    normalized = _validate_setting_value(value, "setting value")
    digest = hashlib.sha256()
    _update_value_digest(digest, normalized)
    return digest.hexdigest()


def settings_patch_digest(desired: Sequence[SettingLeafSpec]) -> str:
    """Return the deterministic identity of one validated desired leaf set."""

    digest = hashlib.sha256()
    for leaf in _validate_desired_leaves(desired):
        digest.update(leaf.source_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(leaf.digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_fragment_document(product: Product, path: Path) -> dict[str, SettingValue]:
    raw = _read_real_regular_file(path, f"{product.value} settings fragment")
    return _parse_plain_document(product, raw, path, name_keys=True)


def _parse_plain_document(
    product: Product,
    raw: bytes,
    path: Path,
    *,
    name_keys: bool = False,
) -> dict[str, SettingValue]:
    """Parse one settings document strictly.

    Errors carry the file path and a line/column position only, never parser
    text that could quote content. ``name_keys`` adds escaped key paths to
    value errors; it is used only for reviewed catalog fragments, not for
    product-owned destinations.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError(f"settings document is not UTF-8: {path}") from exc
    try:
        if product is Product.CODEX:
            parsed: object = tomlkit.parse(text).unwrap()
        else:
            parsed = json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
    except (TOMLKitError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"invalid {product.value} settings document: {path}{_parse_error_detail(exc)}") from None
    if not isinstance(parsed, dict):
        raise SettingsError(f"{product.value} settings document must contain a top-level table/object: {path}")
    return cast(
        dict[str, SettingValue],
        _validate_setting_value(parsed, str(path), require_mapping=True, name_keys=name_keys),
    )


def _parse_error_detail(exc: Exception) -> str:
    """Describe a parse failure by position only, never by quoted content."""

    if isinstance(exc, _ContentFreeJsonError):
        return f": {exc}"
    line = getattr(exc, "lineno", getattr(exc, "line", None))
    column = getattr(exc, "colno", getattr(exc, "col", None))
    if type(line) is int and type(column) is int:
        return f" (at line {line}, column {column})"
    return ""


def _parse_mutable_destination(product: Product, raw: bytes | None) -> MutableMapping[str, Any]:
    if raw is None:
        return tomlkit.document() if product is Product.CODEX else {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError("settings destination is not UTF-8") from exc
    try:
        if product is Product.CODEX:
            return cast(MutableMapping[str, Any], tomlkit.parse(text))
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TOMLKitError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"settings destination became invalid during apply{_parse_error_detail(exc)}") from None
    if not isinstance(parsed, dict):
        raise SettingsError("settings destination must contain a top-level table/object")
    return cast(MutableMapping[str, Any], parsed)


def _read_destination(product: Product, destination: Path) -> tuple[bytes | None, dict[str, SettingValue]]:
    _validate_absolute_normalized_path(destination)
    _inspect_existing_directory_chain(destination.parent)
    if not os.path.lexists(destination):
        return None, {}
    raw = _read_real_regular_file(destination, f"{product.value} settings destination")
    return raw, _parse_plain_document(product, raw, destination)


def _flatten_document(
    document: Mapping[str, SettingValue],
    *,
    source_prefix: str,
) -> tuple[SettingLeafSpec, ...]:
    leaves: list[SettingLeafSpec] = []

    def visit(value: SettingValue, path: SettingPath) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                visit(value[key], (*path, key))
            return
        pointer = _json_pointer(path)
        leaves.append(
            SettingLeafSpec(
                source_id=f"{source_prefix}{pointer}",
                path=path,
                value=value,
                digest=setting_value_digest(value),
            )
        )

    for root_key in sorted(document):
        visit(document[root_key], (root_key,))
    return tuple(leaves)


def _validate_desired_leaves(desired: Sequence[SettingLeafSpec]) -> tuple[SettingLeafSpec, ...]:
    by_path: dict[SettingPath, SettingLeafSpec] = {}
    for leaf in desired:
        _validate_leaf_spec(leaf)
        collision = _colliding_path(leaf.path, by_path)
        if collision is not None:
            raise SettingsError(
                f"desired settings contain colliding paths: {_format_path(collision)} and {_format_path(leaf.path)}"
            )
        by_path[leaf.path] = leaf
    return tuple(sorted(by_path.values(), key=lambda leaf: leaf.path))


def _validate_owned_leaves(previous: Sequence[OwnedSettingLeaf]) -> tuple[OwnedSettingLeaf, ...]:
    by_path: dict[SettingPath, OwnedSettingLeaf] = {}
    for leaf in previous:
        _validate_source_id(leaf.source_id)
        _validate_setting_path(leaf.path)
        _validate_digest(leaf.digest, "owned setting digest")
        collision = _colliding_path(leaf.path, by_path)
        if collision is not None:
            raise SettingsError(
                f"owned settings contain colliding paths: {_format_path(collision)} and {_format_path(leaf.path)}"
            )
        normalized_parents: set[SettingPath] = set()
        for parent in leaf.created_parents:
            _validate_setting_path(parent)
            if not _is_strict_prefix(parent, leaf.path):
                raise SettingsError(
                    f"owned created-parent path {_format_path(parent)} is not an ancestor of {_format_path(leaf.path)}"
                )
            if parent in normalized_parents:
                raise SettingsError(f"duplicate created-parent path: {_format_path(parent)}")
            normalized_parents.add(parent)
        by_path[leaf.path] = leaf
    return tuple(sorted(by_path.values(), key=lambda leaf: leaf.path))


def _validate_leaf_spec(leaf: SettingLeafSpec) -> None:
    _validate_source_id(leaf.source_id)
    _validate_setting_path(leaf.path)
    _validate_digest(leaf.digest, "desired setting digest")
    if isinstance(leaf.value, dict):
        raise SettingsError(f"desired setting leaf cannot itself be a table: {_format_path(leaf.path)}")
    if setting_value_digest(leaf.value) != leaf.digest:
        raise SettingsError(f"desired setting digest does not match its value: {_format_path(leaf.path)}")


def _validate_setting_path(path: SettingPath) -> None:
    if not isinstance(path, tuple) or not path or not all(isinstance(part, str) and part for part in path):
        raise SettingsError("setting paths must be non-empty tuples of non-empty strings")


def _validate_source_id(source_id: str) -> None:
    if not isinstance(source_id, str) or not source_id.startswith(("settings/", "mcp/")) or "\x00" in source_id:
        raise SettingsError(f"invalid settings source identity: {source_id!r}")


def _validate_digest(digest: str, description: str) -> None:
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise SettingsError(f"invalid {description}")


def _validate_setting_value(
    value: object,
    context: str,
    *,
    require_mapping: bool = False,
    name_keys: bool = True,
) -> SettingValue:
    if value is None or isinstance(value, str) or type(value) in {bool, int}:
        if require_mapping:
            raise SettingsError(f"{context} must be a table/object")
        return cast(SettingScalar, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise SettingsError(f"{context} contains a non-finite number")
        if require_mapping:
            raise SettingsError(f"{context} must be a table/object")
        return value
    if isinstance(value, list):
        if require_mapping:
            raise SettingsError(f"{context} must be a table/object")
        return [
            _validate_setting_value(item, f"{context}[]" if name_keys else context, name_keys=name_keys)
            for item in value
        ]
    if isinstance(value, Mapping):
        result: dict[str, SettingValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise SettingsError(f"{context} contains an empty or non-string key")
            result[key] = _validate_setting_value(
                item,
                f"{context}.{safe_key(key)}" if name_keys else context,
                name_keys=name_keys,
            )
        return result
    raise SettingsError(f"{context} contains unsupported value type {type(value).__name__}")


def _update_value_digest(digest: Any, value: SettingValue) -> None:
    if value is None:
        digest.update(b"N")
    elif type(value) is bool:
        digest.update(b"B1" if value else b"B0")
    elif type(value) is int:
        digest.update(b"I")
        digest.update(str(value).encode("ascii"))
        digest.update(b";")
    elif type(value) is float:
        digest.update(b"F")
        digest.update(value.hex().encode("ascii"))
        digest.update(b";")
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        digest.update(b"S")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
    elif isinstance(value, list):
        digest.update(b"[")
        for item in value:
            _update_value_digest(digest, item)
        digest.update(b"]")
    elif isinstance(value, dict):
        digest.update(b"{")
        for key in sorted(value):
            _update_value_digest(digest, key)
            _update_value_digest(digest, value[key])
        digest.update(b"}")
    else:  # pragma: no cover - guarded by validation
        raise SettingsError(f"unsupported setting value type: {type(value).__name__}")


def _lookup_leaf(document: Mapping[str, object], path: SettingPath) -> tuple[object, SettingPath | None]:
    current: object = document
    for index, part in enumerate(path):
        if not isinstance(current, Mapping):
            return _MISSING, path[:index]
        if part not in current:
            return _MISSING, None
        current = current[part]
    return current, None


def _created_parent_paths(document: Mapping[str, object], path: SettingPath) -> tuple[SettingPath, ...]:
    current: object = document
    created: list[SettingPath] = []
    missing = False
    for index, part in enumerate(path[:-1], start=1):
        parent_path = path[:index]
        if missing:
            created.append(parent_path)
            continue
        if not isinstance(current, Mapping) or part not in current:
            missing = True
            created.append(parent_path)
            continue
        current = current[part]
    return tuple(created)


def _set_path(
    document: MutableMapping[str, Any],
    path: SettingPath,
    value: SettingValue,
    product: Product,
) -> None:
    current: MutableMapping[str, Any] = document
    for part in path[:-1]:
        child = current.get(part, _MISSING)
        if child is _MISSING:
            child = tomlkit.table() if product is Product.CODEX else {}
            current[part] = child
        if not isinstance(child, MutableMapping):
            raise SettingsError(f"settings destination changed at non-table ancestor: {_format_path(path)}")
        current = cast(MutableMapping[str, Any], child)
    current[path[-1]] = value


def _delete_path(document: MutableMapping[str, Any], path: SettingPath) -> None:
    current: MutableMapping[str, Any] = document
    for part in path[:-1]:
        child = current.get(part, _MISSING)
        if not isinstance(child, MutableMapping):
            raise SettingsError(f"owned setting disappeared during apply: {_format_path(path)}")
        current = cast(MutableMapping[str, Any], child)
    if path[-1] not in current:
        raise SettingsError(f"owned setting disappeared during apply: {_format_path(path)}")
    del current[path[-1]]


def _delete_toml_table_keeping_comments(
    document: MutableMapping[str, Any],
    path: SettingPath,
) -> list[tuple[list[tuple[Any, Any]], str]]:
    """Delete one TOML table while keeping the comment block that ends it.

    tomlkit stores the comment lines above the next table header as trailing
    items of the preceding table, so deleting a table as a unit would also
    delete a note written for whatever follows it. Comment lines at the end of
    each removed table body (including its last nested table) are therefore
    put back verbatim: at the table's former position when a sibling follows
    it, so they stay above that sibling. When the table was the last item of
    its container, the comments are returned as ``(body, text)`` for the
    caller to append after any table it adds, so they stay above whatever
    follows the container, or at the end of the file. Comments between the
    table's own keys are removed with it.
    """

    # Each owner is the document or a real (possibly out-of-order) table
    # holding a part of the path; tables split across the file have several.
    owners: list[Any] = [document]
    for part in path[:-1]:
        owners = [
            item
            for owner in owners
            for key, item in _toml_body(owner)
            if key is not None and key.key == part and isinstance(item, (Table, InlineTable))
        ]
    removed = False
    deferred: list[tuple[list[tuple[Any, Any]], str]] = []
    for owner in owners:
        body = _toml_body(owner)
        slots = [
            (index, _trailing_toml_comments(item))
            for index, (key, item) in enumerate(body)
            if key is not None and key.key == path[-1]
        ]
        if not slots:
            continue
        owner.remove(path[-1])
        for index, text in slots:
            if not text:
                continue
            if all(isinstance(item, (Null, Whitespace)) for _, item in body[index + 1 :]):
                deferred.append((body, text))
            else:
                # The removed slot holds a Null placeholder; a non-fixed
                # Whitespace item renders verbatim and never adds a header.
                body[index] = (None, Whitespace(text))
        removed = True
    if not removed:
        raise SettingsError(f"owned setting disappeared during apply: {_format_path(path)}")
    return deferred


def _toml_body(owner: Any) -> list[tuple[Any, Any]]:
    container = owner.value if isinstance(owner, (Table, InlineTable)) else owner
    return cast(list[tuple[Any, Any]], container.body)


def _trailing_toml_comments(item: object) -> str:
    """Return the trailing comment block of a table body, from its first comment line on."""

    if isinstance(item, AoT):
        tables = item.body
        return _trailing_toml_comments(tables[-1]) if tables else ""
    if not isinstance(item, Table):
        return ""
    parts: list[str] = []
    for key, child in reversed([entry for entry in item.value.body if not isinstance(entry[1], Null)]):
        if key is None and isinstance(child, (Comment, Whitespace)):
            parts.append(child.as_string())
            continue
        if key is not None and not key.is_dotted() and isinstance(child, (Table, AoT)):
            parts.append(_trailing_toml_comments(child))
        break
    text = "".join(reversed(parts))
    lines = text.splitlines(keepends=True)
    while lines and not lines[0].lstrip().startswith("#"):
        lines.pop(0)
    return "".join(lines)


def safe_key(key: str) -> str:
    """Return a key name with control and other non-printable characters escaped.

    Key names read from product files reach terminals in plans, warnings, and
    doctor output; escaping keeps an embedded escape sequence inert.
    """

    if key.isprintable():
        return key
    return "".join(
        character
        if character.isprintable()
        else (
            f"\\x{ord(character):02x}"
            if ord(character) < 0x100
            else f"\\u{ord(character):04x}"
            if ord(character) < 0x10000
            else f"\\U{ord(character):08x}"
        )
        for character in key
    )


def _prune_empty_owned_parent(
    document: MutableMapping[str, Any],
    path: SettingPath,
    product: Product,
) -> None:
    current: MutableMapping[str, Any] = document
    for part in path[:-1]:
        child = current.get(part, _MISSING)
        if not isinstance(child, MutableMapping):
            return
        current = cast(MutableMapping[str, Any], child)
    child = current.get(path[-1], _MISSING)
    has_toml_comments = product is Product.CODEX and isinstance(child, Mapping) and bool(tomlkit.dumps(child).strip())
    if isinstance(child, Mapping) and not child and not has_toml_comments:
        del current[path[-1]]


def _serialize_document(product: Product, document: MutableMapping[str, Any]) -> bytes:
    if product is Product.CODEX:
        return tomlkit.dumps(document).encode("utf-8")
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_replace(
    destination: Path,
    output: bytes,
    *,
    expected_existed: bool,
    expected_digest: str | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _inspect_existing_directory_chain(destination.parent, require_complete=True)
    current_raw = _read_real_regular_file(destination, "settings destination") if os.path.lexists(destination) else None
    if (current_raw is not None) != expected_existed or (
        _bytes_digest(current_raw) if current_raw is not None else None
    ) != expected_digest:
        raise SettingsError("settings destination changed while staging the update")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.agentbridge.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        if current_raw is not None:
            mode = stat.S_IMODE(destination.stat(follow_symlinks=False).st_mode)
            os.chmod(temporary, mode)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        _require_real_regular_file(temporary, "staged settings file")

        latest_raw = (
            _read_real_regular_file(destination, "settings destination") if os.path.lexists(destination) else None
        )
        if (latest_raw is not None) != expected_existed or (
            _bytes_digest(latest_raw) if latest_raw is not None else None
        ) != expected_digest:
            raise SettingsError("settings destination changed before atomic replacement")
        os.replace(temporary, destination)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _read_real_regular_file(path: Path, description: str) -> bytes:
    _require_real_regular_file(path, description)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SettingsError(f"could not read {description}: {path}: {exc}") from exc


def _require_real_regular_file(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise SettingsError(f"could not inspect {description}: {path}: {exc}") from exc
    attributes = getattr(status, "st_file_attributes", 0)
    if not stat.S_ISREG(status.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise SettingsError(f"{description} must be a real regular file: {path}")


def _require_real_directory(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise SettingsError(f"could not inspect {description}: {path}: {exc}") from exc
    attributes = getattr(status, "st_file_attributes", 0)
    if not stat.S_ISDIR(status.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise SettingsError(f"{description} must be a real directory: {path}")


def _inspect_existing_directory_chain(path: Path, *, require_complete: bool = False) -> None:
    current = path
    missing: list[Path] = []
    while True:
        if os.path.lexists(current):
            _require_real_directory(current, "settings destination ancestor")
        else:
            missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if require_complete and missing:
        raise SettingsError(f"settings destination parent was not created safely: {missing[0]}")


def _validate_absolute_normalized_path(path: Path) -> None:
    if not path.is_absolute():
        raise SettingsError(f"settings destination must be absolute: {path}")
    normalized = Path(os.path.abspath(path))
    if normalized != path:
        raise SettingsError(f"settings destination must not contain relative path segments: {path}")


def _validate_destination_name(product: Product, destination: Path) -> None:
    _validate_absolute_normalized_path(destination)
    expected = _PRODUCT_FRAGMENT[product][1]
    if destination.name != expected:
        raise SettingsError(f"{product.value} settings destination must be named {expected!r}: {destination}")


def _validate_bundle_name(name: str, path: Path) -> None:
    portable_name = name.rstrip(" .").casefold()
    if portable_name in _WINDOWS_DEVICE_NAMES:
        raise SettingsError(f"settings bundle name is reserved on Windows: {path}")
    if _ARTIFACT_NAME.fullmatch(name) is None:
        raise SettingsError(f"settings bundle name must be portable lowercase kebab-case: {path}")


def _colliding_path(path: SettingPath, existing: Mapping[SettingPath, object]) -> SettingPath | None:
    for previous in existing:
        if previous == path or _is_strict_prefix(previous, path) or _is_strict_prefix(path, previous):
            return previous
    return None


def _is_strict_prefix(prefix: SettingPath, path: SettingPath) -> bool:
    return len(prefix) < len(path) and path[: len(prefix)] == prefix


def _change(
    disposition: SettingDisposition,
    leaf: SettingLeafSpec,
    detail: str,
    issue: SettingIssue | None = None,
) -> SettingChange:
    return SettingChange(
        disposition=disposition,
        path=leaf.path,
        source_id=leaf.source_id,
        detail=detail,
        issue=issue,
    )


def _json_pointer(path: SettingPath) -> str:
    return "".join(f"/{part.replace('~', '~0').replace('/', '~1')}" for part in path)


def _format_path(path: SettingPath | None) -> str:
    if path is None:
        return "<root>"
    return _json_pointer(path) or "/"


def _bytes_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class _ContentFreeJsonError(ValueError):
    """A strict-JSON rejection whose message quotes no document content."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _ContentFreeJsonError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _ContentFreeJsonError("non-finite JSON number is not supported")
