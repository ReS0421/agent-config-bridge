"""Render a dual Codex and Claude Code plugin marketplace from canonical sources."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_config_bridge.catalog import Artifact, CatalogInventory, discover_catalog
from agent_config_bridge.governance import GovernanceReport, ResolvedInventory, resolve_inventory
from agent_config_bridge.models import BridgeConfig, Component, Product
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "RenderError",
    "RenderedMarketplace",
    "marketplace_build_path",
    "marketplace_digest",
    "marketplace_is_current",
    "marketplace_publish_path",
    "published_marketplace_digest",
    "render_marketplace",
    "validate_marketplace_build",
]

_MARKETPLACE_NAME = "agent-config-bridge"
_HOOK_PLUGIN_NAME = "agent-config-bridge-hooks"


class RenderError(RuntimeError):
    """Raised when target-specific artifacts cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class RenderedMarketplace:
    """An immutable rendered marketplace build."""

    root: Path
    build_root: Path
    digest: str
    codex_plugins: tuple[str, ...]
    claude_plugins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PortableOutputEntry:
    raw_parts: tuple[str, ...]
    is_directory: bool
    source: Path


class _PortableOutputRegistry:
    """Track one package's outputs under Windows path-comparison rules."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], _PortableOutputEntry] = {}

    def register(self, relative: Path, *, is_directory: bool, source: Path) -> None:
        """Reject aliases and file/ancestor conflicts before materialization."""

        raw_parts = relative.parts
        portable_parts = tuple(part.rstrip(" .").casefold() for part in raw_parts)
        existing = self._entries.get(portable_parts)
        if existing is not None:
            if existing.raw_parts != raw_parts:
                self._raise_alias(relative, source, existing)
            if existing.is_directory != is_directory:
                raise RenderError(
                    f"product overlays define conflicting file and directory output for {relative}: "
                    f"{existing.source} and {source}"
                )
            return

        for length in range(1, len(portable_parts)):
            ancestor = self._entries.get(portable_parts[:length])
            if ancestor is None:
                continue
            if ancestor.raw_parts != raw_parts[:length]:
                self._raise_alias(relative, source, ancestor)
            if not ancestor.is_directory:
                raise RenderError(
                    f"product overlays define file output {Path(*ancestor.raw_parts)} as an ancestor of {relative}: "
                    f"{ancestor.source} and {source}"
                )

        for candidate_parts, candidate in self._entries.items():
            if len(candidate_parts) <= len(portable_parts) or candidate_parts[: len(portable_parts)] != portable_parts:
                continue
            if candidate.raw_parts[: len(raw_parts)] != raw_parts:
                self._raise_alias(relative, source, candidate)
            if not is_directory:
                raise RenderError(
                    f"product overlays define file output {relative} as an ancestor of "
                    f"{Path(*candidate.raw_parts)}: {source} and {candidate.source}"
                )

        self._entries[portable_parts] = _PortableOutputEntry(
            raw_parts=raw_parts,
            is_directory=is_directory,
            source=source,
        )

    @staticmethod
    def _raise_alias(relative: Path, source: Path, existing: _PortableOutputEntry) -> None:
        raise RenderError(
            f"product overlays define paths that collide on Windows: "
            f"{Path(*existing.raw_parts)} from {existing.source} and {relative} from {source}"
        )


def render_marketplace(
    config: BridgeConfig,
    inventory: CatalogInventory,
    *,
    resolved: ResolvedInventory | None = None,
) -> RenderedMarketplace:
    """Render selected plugin and hook artifacts into an immutable dual marketplace.

    The build is content-addressed. Existing builds are reused, while a new build is
    assembled in a temporary directory and atomically renamed into place.

    Args:
        config: Loaded bridge configuration.
        inventory: Validated catalog inventory.
        resolved: Governance-gated inventory to reuse; resolved internally when
            omitted. Callers in one operation should thread a single resolved
            object so render and ownership writes reflect the same read.

    Returns:
        Metadata for the rendered marketplace.

    Raises:
        RenderError: If overlays conflict or a source escapes its artifact root.
    """

    if resolved is None:
        resolved = resolve_inventory(inventory)
    digest = marketplace_digest(config, inventory, resolved=resolved)
    builds_dir = config.state_dir / "builds"
    build_root = builds_dir / digest
    _validate_state_directory(config, builds_dir, "marketplace builds directory")
    _validate_state_directory(config, build_root, "immutable marketplace build")

    if build_root.is_dir():
        existing = _read_rendered_marketplace(build_root, digest)
        codex_plugins = existing.codex_plugins
        claude_plugins = existing.claude_plugins
    else:
        builds_dir.mkdir(parents=True, exist_ok=True)
        temporary_root = builds_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
        temporary_root.mkdir(parents=False)
        try:
            plugins_by_product: dict[Product, tuple[str, ...]] = {}
            for product in sorted(_selected_products(config), key=lambda item: item.value):
                plugin_names: list[str] = []
                product_root = temporary_root / "plugins" / product.value
                if _product_has_component(config, product, Component.PLUGINS):
                    for plugin in inventory.plugins:
                        _render_plugin(plugin, product, product_root / plugin.name)
                        plugin_names.append(plugin.name)
                gated_hooks = resolved.hooks_for_product(config, product)
                if _product_has_component(config, product, Component.HOOKS) and gated_hooks:
                    _render_hook_plugin(
                        gated_hooks,
                        product,
                        product_root / _HOOK_PLUGIN_NAME,
                        inventory.hook_version,
                        resolved.report,
                    )
                    plugin_names.append(_HOOK_PLUGIN_NAME)
                plugins_by_product[product] = tuple(plugin_names)

            codex_plugins = plugins_by_product.get(Product.CODEX, ())
            claude_plugins = plugins_by_product.get(Product.CLAUDE_CODE, ())
            _write_marketplaces(temporary_root, codex_plugins, claude_plugins)
            output_digest = _rendered_tree_digest(temporary_root)
            _write_json(
                temporary_root / "bridge-build.json",
                {
                    "schema_version": 3,
                    "digest": digest,
                    "output_digest": output_digest,
                    "codex_plugins": list(codex_plugins),
                    "claude_plugins": list(claude_plugins),
                },
            )
            os.replace(temporary_root, build_root)
        except Exception:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise

    fresh_inventory = discover_catalog(config)
    if marketplace_digest(config, fresh_inventory, resolved=resolve_inventory(fresh_inventory)) != digest:
        raise RenderError("catalog changed while the marketplace was being rendered; retry")
    published_root = _publish_marketplace(config, build_root, digest)

    return RenderedMarketplace(
        root=published_root,
        build_root=build_root,
        digest=digest,
        codex_plugins=codex_plugins,
        claude_plugins=claude_plugins,
    )


def marketplace_digest(
    config: BridgeConfig,
    inventory: CatalogInventory,
    *,
    resolved: ResolvedInventory | None = None,
) -> str:
    """Return the deterministic digest for the selected marketplace inputs."""

    if resolved is None:
        resolved = resolve_inventory(inventory)
    return _build_digest(config, inventory, resolved)


def marketplace_build_path(
    config: BridgeConfig,
    inventory: CatalogInventory,
    *,
    resolved: ResolvedInventory | None = None,
) -> Path:
    """Return the immutable build path without creating it."""

    return config.state_dir / "builds" / marketplace_digest(config, inventory, resolved=resolved)


def marketplace_publish_path(config: BridgeConfig) -> Path:
    """Return the stable local marketplace path registered with product CLIs."""

    return config.state_dir / "marketplace"


def marketplace_is_current(
    config: BridgeConfig,
    inventory: CatalogInventory,
    *,
    resolved: ResolvedInventory | None = None,
) -> bool:
    """Return whether the validated stable marketplace matches current inputs."""

    published_root = marketplace_publish_path(config)
    if not os.path.lexists(published_root):
        return False
    _validate_state_directory(config, published_root, "published marketplace")
    marker_digest = _marker_digest(published_root)
    _read_rendered_marketplace(published_root, marker_digest)
    return marker_digest == marketplace_digest(config, inventory, resolved=resolved)


def published_marketplace_digest(config: BridgeConfig) -> str | None:
    """Return the integrity-checked digest of the stable published marketplace."""

    published_root = marketplace_publish_path(config)
    if not os.path.lexists(published_root):
        return None
    _validate_state_directory(config, published_root, "published marketplace")
    digest = _marker_digest(published_root)
    _read_rendered_marketplace(published_root, digest)
    return digest


def validate_marketplace_build(
    config: BridgeConfig,
    build_root: Path,
    digest: str,
) -> RenderedMarketplace:
    """Validate one exact content-addressed build without publishing it."""

    expected = config.state_dir / "builds" / digest
    if build_root != expected:
        raise RenderError(f"immutable marketplace build path does not match its digest: {build_root}")
    _validate_state_directory(config, build_root, "immutable marketplace build")
    return _read_rendered_marketplace(build_root, digest)


def _selected_products(config: BridgeConfig) -> frozenset[Product]:
    return frozenset(
        target.product
        for target in config.targets
        if target.enabled and (Component.PLUGINS in target.components or Component.HOOKS in target.components)
    )


def _product_has_component(config: BridgeConfig, product: Product, component: Component) -> bool:
    return any(
        target.enabled and target.product is product and component in target.components for target in config.targets
    )


def _build_digest(config: BridgeConfig, inventory: CatalogInventory, resolved: ResolvedInventory) -> str:
    digest = hashlib.sha256()
    digest.update(b"agent-config-bridge-render-v7\0")
    for product in sorted(_selected_products(config), key=lambda item: item.value):
        digest.update(product.value.encode())
        digest.update(b"\0")
        for component, artifacts in (
            (Component.PLUGINS, inventory.plugins),
            (Component.HOOKS, resolved.hooks_for_product(config, product)),
        ):
            if not _product_has_component(config, product, component):
                continue
            digest.update(component.value.encode())
            digest.update(b"\0")
            # Fold the hook version only when this product actually renders a
            # hook plugin (its gated set is non-empty); otherwise bumping the
            # version for a hook scoped to another product would force a
            # byte-identical rebuild here.
            if component is Component.HOOKS and artifacts:
                digest.update((inventory.hook_version or "").encode())
                digest.update(b"\0")
            for artifact in artifacts:
                digest.update(artifact.name.encode())
                digest.update(b"\0")
                _update_tree_digest(digest, artifact.path)
                if component is Component.HOOKS:
                    for attribution_path in _hook_attribution_paths(artifact, resolved.report):
                        digest.update(b"attribution\0")
                        digest.update(attribution_path.encode())
                        digest.update(b"\0")
    return digest.hexdigest()[:20]


def _update_tree_digest(digest: Any, root: Path) -> None:
    root_mode = _source_mode(root)
    if not stat.S_ISDIR(root_mode):
        raise RenderError(f"source tree root must be a real directory: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        mode = _source_mode(path)
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        if stat.S_ISLNK(mode):
            _resolve_regular_file(path, root)
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif stat.S_ISREG(mode):
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif stat.S_ISDIR(mode):
            digest.update(b"D")
        else:
            raise RenderError(f"source tree contains an unsupported filesystem node: {path}")


def _render_plugin(artifact: Artifact, product: Product, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    portable_outputs = _PortableOutputRegistry()
    for overlay_name in ("common", product.value):
        overlay = artifact.path / overlay_name
        if overlay.is_dir():
            _copy_overlay(overlay, destination, artifact.path, portable_outputs=portable_outputs)

    manifest_directory = ".codex-plugin" if product is Product.CODEX else ".claude-plugin"
    manifest = destination / manifest_directory / "plugin.json"
    if not manifest.is_file():
        raise RenderError(
            f"rendered {product.value} plugin {artifact.name!r} is missing {manifest_directory}/plugin.json"
        )


def _render_hook_plugin(
    hooks: tuple[Artifact, ...],
    product: Product,
    destination: Path,
    hook_version: str | None,
    governance_report: GovernanceReport,
) -> None:
    manifest_directory = ".codex-plugin" if product is Product.CODEX else ".claude-plugin"
    (destination / manifest_directory).mkdir(parents=True)
    (destination / "hooks").mkdir(parents=True)
    (destination / "scripts").mkdir(parents=True)

    documents: list[dict[str, Any]] = []
    for artifact in hooks:
        common = _optional_json(artifact.path / "common" / "hooks.json")
        product_document = _optional_json(artifact.path / product.value / "hooks.json")
        if common is None and product_document is None:
            raise RenderError(
                f"hook {artifact.name!r} has no common or {product.value} representation for a selected target"
            )
        if common is not None:
            documents.append(common)
        if product_document is not None:
            documents.append(product_document)

        scripts_destination = destination / "scripts" / artifact.name
        scripts_destination.mkdir()
        portable_outputs = _PortableOutputRegistry()
        for overlay_name in ("common", product.value):
            scripts = artifact.path / overlay_name / "scripts"
            if scripts.is_dir():
                _copy_overlay(scripts, scripts_destination, artifact.path, portable_outputs=portable_outputs)
        _copy_hook_attribution_files(artifact, governance_report, destination)

    _write_json(destination / "hooks" / "hooks.json", _merge_hook_documents(documents))
    if hook_version is None:
        raise RenderError("hook catalog has no validated version")
    manifest = _codex_hook_manifest(hook_version) if product is Product.CODEX else _claude_hook_manifest(hook_version)
    _write_json(destination / manifest_directory / "plugin.json", manifest)


def _copy_hook_attribution_files(
    artifact: Artifact,
    report: GovernanceReport,
    destination: Path,
) -> None:
    """Ship safely declared Hook attribution files with the rendered plugin.

    Governance remains the source of truth. Invalid declarations are already
    diagnostics and are ignored here in audit mode; required mode rejects them
    before rendering. Only real, contained regular files are copied.
    """

    for declared_path in _hook_attribution_paths(artifact, report):
        relative = Path(declared_path)
        if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
            continue
        source = artifact.path / relative
        try:
            mode = _source_mode(source)
        except RenderError:
            continue
        if not stat.S_ISREG(mode):
            continue
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(artifact.path.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        target = destination / "licenses" / artifact.name / relative
        _copy_file_checked(source, target)


def _hook_attribution_paths(
    artifact: Artifact,
    report: GovernanceReport,
) -> tuple[str, ...]:
    """Return the normalized declared attribution inputs for one Hook."""

    artifact_ref = f"hooks/{artifact.name}"
    declared: set[str] = set()
    for manifest in report.manifests:
        for governed_artifact in manifest.data.get("artifacts", []):
            if not isinstance(governed_artifact, dict) or governed_artifact.get("ref") != artifact_ref:
                continue
            provenance = governed_artifact.get("provenance")
            if not isinstance(provenance, dict):
                continue
            attribution_files = provenance.get("attribution_files")
            if not isinstance(attribution_files, list):
                continue
            declared.update(value for value in attribution_files if isinstance(value, str) and value.strip())
    return tuple(sorted(declared))


def _codex_hook_manifest(version: str) -> dict[str, Any]:
    return {
        "name": _HOOK_PLUGIN_NAME,
        "version": version,
        "description": "Lifecycle hooks rendered safely by Agent Config Bridge.",
        "author": {
            "name": "Agent Config Bridge contributors",
            "url": "https://github.com/ReS0421/agent-config-bridge",
        },
        "homepage": "https://github.com/ReS0421/agent-config-bridge",
        "repository": "https://github.com/ReS0421/agent-config-bridge",
        "license": "MIT",
        "keywords": ["hooks", "shared-config"],
        "interface": {
            "displayName": "Agent Config Bridge Hooks",
            "shortDescription": "Run lifecycle hooks from a canonical catalog.",
            "longDescription": "Installs the Codex lifecycle hooks selected and rendered by Agent Config Bridge.",
            "developerName": "Agent Config Bridge contributors",
            "category": "Developer Tools",
            "capabilities": ["Automation"],
            "defaultPrompt": ["Show the installed Agent Config Bridge hooks."],
            "brandColor": "#2563EB",
        },
    }


def _claude_hook_manifest(version: str) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/claude-code-plugin-manifest.json",
        "name": _HOOK_PLUGIN_NAME,
        "displayName": "Agent Config Bridge Hooks",
        "version": version,
        "description": "Lifecycle hooks rendered safely by Agent Config Bridge.",
        "author": {
            "name": "Agent Config Bridge contributors",
            "url": "https://github.com/ReS0421/agent-config-bridge",
        },
        "homepage": "https://github.com/ReS0421/agent-config-bridge",
        "repository": "https://github.com/ReS0421/agent-config-bridge",
        "license": "MIT",
        "keywords": ["hooks", "shared-config"],
    }


def _copy_overlay(
    source: Path,
    destination: Path,
    artifact_root: Path,
    *,
    portable_outputs: _PortableOutputRegistry | None = None,
) -> None:
    source_mode = _source_mode(source)
    if not stat.S_ISDIR(source_mode):
        raise RenderError(f"source overlay root must be a real directory: {source}")
    if portable_outputs is None:
        portable_outputs = _PortableOutputRegistry()
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        mode = _source_mode(path)
        relative = path.relative_to(source)
        target = destination / relative
        if stat.S_ISLNK(mode):
            resolved = _resolve_regular_file(path, artifact_root)
            portable_outputs.register(relative, is_directory=False, source=path)
            _copy_file_checked(resolved, target)
        elif stat.S_ISDIR(mode):
            portable_outputs.register(relative, is_directory=True, source=path)
            target.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            portable_outputs.register(relative, is_directory=False, source=path)
            _copy_file_checked(path, target)
        else:
            raise RenderError(f"source overlay contains an unsupported filesystem node: {path}")


def _copy_file_checked(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            return
        raise RenderError(f"product overlays define conflicting content for {destination}")
    shutil.copy2(source, destination)


def _ensure_within(candidate: Path, root: Path, source: Path) -> None:
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RenderError(f"source symlink escapes artifact root: {source} -> {candidate}") from exc


def _source_mode(path: Path) -> int:
    try:
        if is_directory_reparse_point(path):
            raise RenderError(f"source directory reparse points are not supported: {path}")
        return path.lstat().st_mode
    except OSError as exc:
        raise RenderError(f"cannot inspect source path {path}: {exc}") from exc


def _resolve_regular_file(path: Path, artifact_root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RenderError(f"source symlink is broken or unresolvable: {path}: {exc}") from exc
    _ensure_within(resolved, artifact_root, path)
    try:
        target_mode = resolved.stat().st_mode
    except OSError as exc:
        raise RenderError(f"cannot inspect source symlink target {path} -> {resolved}: {exc}") from exc
    if not stat.S_ISREG(target_mode):
        raise RenderError(f"source symlink target must be a contained regular file: {path} -> {resolved}")
    return resolved


def _optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"invalid hook document {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), dict):
        raise RenderError(f"hook document must contain a hooks object: {path}")
    return payload


def _merge_hook_documents(documents: Iterable[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, list[Any]] = {}
    for document in documents:
        hooks = document["hooks"]
        for event, groups in hooks.items():
            if not isinstance(event, str) or not isinstance(groups, list):
                raise RenderError("hook events must map to arrays of matcher groups")
            merged.setdefault(event, []).extend(groups)
    return {"hooks": merged}


def _write_marketplaces(root: Path, codex_plugins: tuple[str, ...], claude_plugins: tuple[str, ...]) -> None:
    codex_entries = [_codex_marketplace_entry(root, name) for name in codex_plugins]
    claude_entries = [_claude_marketplace_entry(root, name) for name in claude_plugins]
    _write_json(
        root / ".agents" / "plugins" / "marketplace.json",
        {
            "name": _MARKETPLACE_NAME,
            "interface": {"displayName": "Agent Config Bridge"},
            "plugins": codex_entries,
        },
    )
    _write_json(
        root / ".claude-plugin" / "marketplace.json",
        {
            "name": _MARKETPLACE_NAME,
            "description": "Plugins and hooks rendered by Agent Config Bridge.",
            "owner": {"name": "Agent Config Bridge"},
            "plugins": claude_entries,
        },
    )


def _codex_marketplace_entry(root: Path, name: str) -> dict[str, Any]:
    relative_plugin = Path("plugins") / Product.CODEX.value / name
    metadata = _plugin_metadata(root / relative_plugin / ".codex-plugin" / "plugin.json")
    return {
        "name": name,
        "source": {"source": "local", "path": f"./{relative_plugin.as_posix()}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
        "description": metadata.get("description", "Rendered plugin"),
    }


def _claude_marketplace_entry(root: Path, name: str) -> dict[str, Any]:
    relative_plugin = Path("plugins") / Product.CLAUDE_CODE.value / name
    metadata = _plugin_metadata(root / relative_plugin / ".claude-plugin" / "plugin.json")
    entry: dict[str, Any] = {
        "name": name,
        "source": f"./{relative_plugin.as_posix()}",
        "description": metadata.get("description", "Rendered plugin"),
    }
    for field in ("version", "author", "homepage", "repository", "license", "keywords"):
        if field in metadata:
            entry[field] = metadata[field]
    return entry


def _plugin_metadata(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"invalid rendered plugin manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RenderError(f"rendered plugin manifest must be an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish_marketplace(config: BridgeConfig, build_root: Path, digest: str) -> Path:
    destination = marketplace_publish_path(config)
    _validate_state_directory(config, destination, "published marketplace")
    if os.path.lexists(destination):
        existing_digest = _marker_digest(destination)
        _read_rendered_marketplace(destination, existing_digest)
        if existing_digest == digest:
            return destination
        _validate_package_version_changes(destination, build_root)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    displaced = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.old")
    try:
        shutil.copytree(build_root, temporary)
        _read_rendered_marketplace(temporary, digest)
        if os.path.lexists(destination):
            os.replace(destination, displaced)
        os.replace(temporary, destination)
    except Exception:
        if os.path.lexists(displaced) and not os.path.lexists(destination):
            os.replace(displaced, destination)
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    shutil.rmtree(displaced, ignore_errors=True)
    return destination


def _validate_state_directory(config: BridgeConfig, path: Path, label: str) -> None:
    state_root = config.state_dir.resolve(strict=False)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RenderError(f"{label} escapes configured state_dir: {path}") from exc
    if not os.path.lexists(path):
        return
    try:
        redirected = path.is_symlink() or is_directory_reparse_point(path)
    except OSError as exc:
        raise RenderError(f"cannot inspect {label}: {path}: {exc}") from exc
    if redirected:
        raise RenderError(f"{label} must not be a symlink or directory junction: {path}")
    if not path.is_dir():
        raise RenderError(f"{label} is not a directory: {path}")


def _validate_package_version_changes(previous_root: Path, next_root: Path) -> None:
    for product, manifest_directory in (
        (Product.CODEX, ".codex-plugin"),
        (Product.CLAUDE_CODE, ".claude-plugin"),
    ):
        previous_product = previous_root / "plugins" / product.value
        next_product = next_root / "plugins" / product.value
        if not previous_product.is_dir() or not next_product.is_dir():
            continue
        previous_names = {path.name for path in previous_product.iterdir() if path.is_dir()}
        next_names = {path.name for path in next_product.iterdir() if path.is_dir()}
        for name in sorted(previous_names & next_names):
            previous_plugin = previous_product / name
            next_plugin = next_product / name
            if _rendered_tree_digest(previous_plugin) == _rendered_tree_digest(next_plugin):
                continue
            previous_version = _manifest_version(previous_plugin / manifest_directory / "plugin.json")
            next_version = _manifest_version(next_plugin / manifest_directory / "plugin.json")
            if not _semver_is_newer(next_version, previous_version):
                raise RenderError(
                    f"rendered {product.value} plugin {name!r} changed, but version {next_version} "
                    f"does not increase {previous_version}; update both canonical manifests or hooks/.version. "
                    "A governance-only change (quarantining or re-scoping a hook that leaves other hooks in the "
                    "plugin) still changes the plugin content, so it also requires a version bump to publish."
                )


def _manifest_version(path: Path) -> str:
    payload = _plugin_metadata(path)
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise RenderError(f"rendered plugin manifest has no version: {path}")
    return version


def _semver_is_newer(candidate: str, previous: str) -> bool:
    candidate_core, candidate_prerelease = _semver_parts(candidate)
    previous_core, previous_prerelease = _semver_parts(previous)
    if candidate_core != previous_core:
        return candidate_core > previous_core
    if candidate_prerelease is None:
        return previous_prerelease is not None
    if previous_prerelease is None:
        return False
    return _prerelease_is_newer(candidate_prerelease, previous_prerelease)


def _semver_parts(version: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    without_build = version.split("+", maxsplit=1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = (int(value) for value in core.split("."))
    return (major, minor, patch), tuple(prerelease.split(".")) if separator else None


def _prerelease_is_newer(candidate: tuple[str, ...], previous: tuple[str, ...]) -> bool:
    for candidate_part, previous_part in zip(candidate, previous, strict=False):
        if candidate_part == previous_part:
            continue
        candidate_numeric = candidate_part.isdigit()
        previous_numeric = previous_part.isdigit()
        if candidate_numeric and previous_numeric:
            return int(candidate_part) > int(previous_part)
        if candidate_numeric != previous_numeric:
            return not candidate_numeric
        return candidate_part > previous_part
    return len(candidate) > len(previous)


def _marker_digest(root: Path) -> str:
    marker = root / "bridge-build.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        digest = payload["digest"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RenderError(f"existing marketplace is not managed or is invalid: {root}") from exc
    if not isinstance(digest, str):
        raise RenderError(f"existing marketplace has an invalid digest: {root}")
    return digest


def _rendered_tree_digest(root: Path) -> str:
    if is_directory_reparse_point(root):
        raise RenderError(f"rendered marketplaces must not be directory reparse points: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == "bridge-build.json":
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_symlink() or is_directory_reparse_point(path):
            raise RenderError(f"rendered marketplaces must not contain links or junctions: {path}")
        if path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
        else:
            raise RenderError(f"rendered marketplaces contain an unsupported filesystem node: {path}")
    return digest.hexdigest()


def _read_rendered_marketplace(root: Path, digest: str) -> RenderedMarketplace:
    marker = root / "bridge-build.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload["schema_version"] != 3 or payload["digest"] != digest:
            raise ValueError("build marker version or digest does not match")
        output_digest = payload["output_digest"]
        codex_plugins = _plugin_name_list(payload["codex_plugins"])
        claude_plugins = _plugin_name_list(payload["claude_plugins"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RenderError(f"invalid existing rendered marketplace: {root}") from exc
    except ValueError as exc:
        raise RenderError(f"invalid existing rendered marketplace: {root}: {exc}") from exc
    if not isinstance(output_digest, str) or _rendered_tree_digest(root) != output_digest:
        raise RenderError(f"rendered marketplace content digest does not match its marker: {root}")
    _validate_rendered_entries(root, Product.CODEX, codex_plugins)
    _validate_rendered_entries(root, Product.CLAUDE_CODE, claude_plugins)
    return RenderedMarketplace(
        root=root,
        build_root=root,
        digest=digest,
        codex_plugins=codex_plugins,
        claude_plugins=claude_plugins,
    )


def _plugin_name_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise ValueError("plugin names must be non-empty strings")
    names = tuple(value)
    if len(names) != len(set(names)):
        raise ValueError("plugin names must be unique")
    return names


def _validate_rendered_entries(root: Path, product: Product, expected_names: tuple[str, ...]) -> None:
    if product is Product.CODEX:
        marketplace = root / ".agents" / "plugins" / "marketplace.json"
        manifest_directory = ".codex-plugin"
    else:
        marketplace = root / ".claude-plugin" / "marketplace.json"
        manifest_directory = ".claude-plugin"
    try:
        payload = json.loads(marketplace.read_text(encoding="utf-8"))
        entries = payload["plugins"]
        names = tuple(entry["name"] for entry in entries)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RenderError(f"invalid rendered marketplace manifest: {marketplace}") from exc
    if names != expected_names:
        raise RenderError(f"rendered marketplace plugin list does not match its marker: {marketplace}")
    for name in names:
        manifest = root / "plugins" / product.value / name / manifest_directory / "plugin.json"
        if not manifest.is_file():
            raise RenderError(f"rendered marketplace plugin is missing its manifest: {manifest}")
