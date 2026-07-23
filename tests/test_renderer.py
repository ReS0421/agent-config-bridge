"""Tests for immutable dual-marketplace rendering."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.models import Component, Product
from agent_config_bridge.renderer import (
    RenderError,
    _copy_overlay,
    _rendered_tree_digest,
    _update_tree_digest,
    published_marketplace_digest,
    render_marketplace,
    validate_marketplace_build,
)
from tests.conftest import make_catalog, make_config, require_directory_symlink_support


def test_render_marketplace_builds_both_product_catalogs(tmp_path: Path) -> None:
    """A canonical plugin renders both manifests and both marketplaces."""

    catalog = make_catalog(
        tmp_path / "catalog",
        plugins=("shared-plugin",),
        hooks=("audit-event",),
    )
    components = frozenset({Component.PLUGINS, Component.HOOKS})
    config = make_config(tmp_path, catalog, components=components)
    claude_target = replace(
        config.targets[0],
        name="claude",
        product=Product.CLAUDE_CODE,
        config_home=tmp_path / "home/.claude",
    )
    config = replace(config, targets=(config.targets[0], claude_target))

    rendered = render_marketplace(config, discover_catalog(config))

    assert rendered.root == config.state_dir / "marketplace"
    assert rendered.build_root.parent == config.state_dir / "builds"
    codex_plugin = rendered.root / "plugins/codex/shared-plugin"
    claude_plugin = rendered.root / "plugins/claude-code/shared-plugin"
    assert (codex_plugin / ".codex-plugin/plugin.json").is_file()
    assert (claude_plugin / ".claude-plugin/plugin.json").is_file()
    assert (codex_plugin / "skills/inside/SKILL.md").is_file()
    assert (claude_plugin / "skills/inside/SKILL.md").is_file()
    assert (rendered.root / ".agents/plugins/marketplace.json").is_file()
    assert (rendered.root / ".claude-plugin/marketplace.json").is_file()

    codex_hook_plugin = rendered.root / "plugins/codex/agent-config-bridge-hooks"
    claude_hook_plugin = rendered.root / "plugins/claude-code/agent-config-bridge-hooks"
    codex_hooks = json.loads((codex_hook_plugin / "hooks/hooks.json").read_text(encoding="utf-8"))
    claude_hooks = json.loads((claude_hook_plugin / "hooks/hooks.json").read_text(encoding="utf-8"))
    assert codex_hooks == claude_hooks
    assert (codex_hook_plugin / "scripts/audit-event/allow.py").is_file()
    assert (claude_hook_plugin / "scripts/audit-event/allow.py").is_file()


def test_render_marketplace_respects_per_product_component_selection(tmp_path: Path) -> None:
    """A target that selects only Hooks must not expose canonical Plugins."""

    catalog = make_catalog(
        tmp_path / "catalog",
        plugins=("shared-plugin",),
        hooks=("audit-event",),
    )
    components = frozenset({Component.PLUGINS, Component.HOOKS})
    config = make_config(tmp_path, catalog, components=components)
    claude_target = replace(
        config.targets[0],
        name="claude",
        product=Product.CLAUDE_CODE,
        config_home=tmp_path / "home/.claude",
        components=frozenset({Component.HOOKS}),
    )
    config = replace(config, targets=(config.targets[0], claude_target))

    rendered = render_marketplace(config, discover_catalog(config))

    assert rendered.codex_plugins == ("shared-plugin", "agent-config-bridge-hooks")
    assert rendered.claude_plugins == ("agent-config-bridge-hooks",)
    assert not (rendered.root / "plugins/claude-code/shared-plugin").exists()


def test_render_marketplace_reuses_content_addressed_build(tmp_path: Path) -> None:
    """Unchanged sources resolve to the same immutable build directory."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    inventory = discover_catalog(config)

    first = render_marketplace(config, inventory)
    second = render_marketplace(config, inventory)

    assert first == second


def test_retention_helpers_validate_published_and_exact_build_identity(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(
        tmp_path,
        catalog,
        components=frozenset({Component.PLUGINS}),
    )
    rendered = render_marketplace(config, discover_catalog(config))

    assert published_marketplace_digest(config) == rendered.digest
    assert validate_marketplace_build(config, rendered.build_root, rendered.digest) == replace(
        rendered, root=rendered.build_root
    )

    with pytest.raises(RenderError, match="path does not match"):
        validate_marketplace_build(
            config,
            rendered.build_root,
            "0" * 20,
        )


def test_render_marketplace_changes_digest_after_source_change(tmp_path: Path) -> None:
    """A source edit creates a new build rather than mutating the previous one."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    first = render_marketplace(config, discover_catalog(config))
    skill = catalog / "plugins/shared-plugin/common/skills/inside/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8")
    for manifest in (
        catalog / "plugins/shared-plugin/codex/.codex-plugin/plugin.json",
        catalog / "plugins/shared-plugin/claude-code/.claude-plugin/plugin.json",
    ):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["version"] = "0.1.1"
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    second = render_marketplace(config, discover_catalog(config))

    assert first.digest != second.digest
    assert first.build_root != second.build_root
    assert first.root == second.root
    assert first.root.is_dir()
    assert second.root.is_dir()


def test_render_marketplace_requires_plugin_version_bump(tmp_path: Path) -> None:
    """A changed package cannot silently reuse a cached product version."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    render_marketplace(config, discover_catalog(config))
    skill = catalog / "plugins/shared-plugin/common/skills/inside/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8")

    with pytest.raises(RenderError, match="does not increase"):
        render_marketplace(config, discover_catalog(config))


def test_render_marketplace_rejects_corrupted_published_snapshot(tmp_path: Path) -> None:
    """A stable marketplace is rehashed before it is reused or replaced."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    rendered = render_marketplace(config, discover_catalog(config))
    marketplace = rendered.root / ".agents/plugins/marketplace.json"
    marketplace.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RenderError, match="content digest"):
        render_marketplace(config, discover_catalog(config))


def test_hook_version_changes_render_digest_and_generated_manifest(tmp_path: Path) -> None:
    """The synthetic Hook plugin version participates in immutable identity."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.HOOKS}))
    first = render_marketplace(config, discover_catalog(config))
    (catalog / "hooks/.version").write_text("0.1.1\n", encoding="utf-8")

    second = render_marketplace(config, discover_catalog(config))

    manifest = second.root / "plugins/codex/agent-config-bridge-hooks/.codex-plugin/plugin.json"
    assert first.digest != second.digest
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == "0.1.1"


def test_render_rejects_hook_without_selected_product_representation(tmp_path: Path) -> None:
    """A product-only Hook is never silently rendered as an empty plugin."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    common_hook = catalog / "hooks/audit-event/common/hooks.json"
    codex_hook = catalog / "hooks/audit-event/codex/hooks.json"
    codex_hook.parent.mkdir()
    common_hook.replace(codex_hook)
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        components=frozenset({Component.HOOKS}),
    )

    with pytest.raises(RenderError, match="no common or claude-code representation"):
        render_marketplace(config, discover_catalog(config))


def test_render_marketplace_rejects_conflicting_overlays(tmp_path: Path) -> None:
    """Product overlays may not silently replace shared output files."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    shared = catalog / "plugins/shared-plugin/common/conflict.txt"
    product = catalog / "plugins/shared-plugin/codex/conflict.txt"
    shared.write_text("shared", encoding="utf-8")
    product.write_text("codex", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))

    with pytest.raises(RenderError, match="conflicting content"):
        render_marketplace(config, discover_catalog(config))


def test_render_marketplace_allows_identical_file_in_both_overlays(tmp_path: Path) -> None:
    """An exact output path may merge only when the file bytes also match."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    shared = catalog / "plugins/shared-plugin/common/shared.txt"
    product = catalog / "plugins/shared-plugin/codex/shared.txt"
    shared.write_text("same\n", encoding="utf-8")
    product.write_text("same\n", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))

    rendered = render_marketplace(config, discover_catalog(config))

    assert (rendered.root / "plugins/codex/shared-plugin/shared.txt").read_text(encoding="utf-8") == "same\n"


def test_render_marketplace_rejects_cross_overlay_windows_file_alias(tmp_path: Path) -> None:
    """Case-distinct source paths cannot collapse into one Windows output."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    shared = catalog / "plugins/shared-plugin/common/assets"
    product = catalog / "plugins/shared-plugin/codex/assets"
    shared.mkdir()
    product.mkdir()
    (shared / "Readme.md").write_text("shared\n", encoding="utf-8")
    (product / "README.md").write_text("product\n", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))

    with pytest.raises(RenderError, match="collide on Windows"):
        render_marketplace(config, discover_catalog(config))


def test_render_marketplace_rejects_cross_overlay_windows_directory_alias(tmp_path: Path) -> None:
    """Case-distinct directory aliases cannot produce a host-dependent merge."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    shared = catalog / "plugins/shared-plugin/common/Assets"
    product = catalog / "plugins/shared-plugin/codex/assets"
    shared.mkdir()
    product.mkdir()
    (shared / "shared.txt").write_text("shared\n", encoding="utf-8")
    (product / "product.txt").write_text("product\n", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))

    with pytest.raises(RenderError, match="collide on Windows"):
        render_marketplace(config, discover_catalog(config))


def test_render_marketplace_rejects_cross_overlay_file_ancestor(tmp_path: Path) -> None:
    """A portable output cannot be both a file and an ancestor directory."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    (catalog / "plugins/shared-plugin/common/assets").write_text("file\n", encoding="utf-8")
    product = catalog / "plugins/shared-plugin/codex/assets"
    product.mkdir()
    (product / "icon.txt").write_text("child\n", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))

    with pytest.raises(RenderError, match="conflicting file and directory output"):
        render_marketplace(config, discover_catalog(config))


def test_render_hook_scripts_reject_cross_overlay_windows_alias(tmp_path: Path) -> None:
    """Hook script overlays use the same portable output collision rules."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    product_scripts = catalog / "hooks/audit-event/codex/scripts"
    product_scripts.mkdir(parents=True)
    (product_scripts / "ALLOW.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    config = make_config(tmp_path, catalog, components=frozenset({Component.HOOKS}))

    with pytest.raises(RenderError, match="collide on Windows"):
        render_marketplace(config, discover_catalog(config))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_update_tree_digest_rejects_fifo(tmp_path: Path) -> None:
    """Typed inventories cannot make digesting silently omit a special node."""

    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "input.pipe")

    with pytest.raises(RenderError, match="unsupported filesystem node"):
        _update_tree_digest(hashlib.sha256(), source)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_copy_overlay_rejects_fifo(tmp_path: Path) -> None:
    """Typed inventories cannot make copying silently omit a special node."""

    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "input.pipe")

    with pytest.raises(RenderError, match="unsupported filesystem node"):
        _copy_overlay(source, tmp_path / "destination", source)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_rendered_tree_digest_rejects_fifo(tmp_path: Path) -> None:
    """Generated-tree validation must reject special nodes explicitly."""

    root = tmp_path / "rendered"
    root.mkdir()
    os.mkfifo(root / "input.pipe")

    with pytest.raises(RenderError, match="unsupported filesystem node"):
        _rendered_tree_digest(root)


def test_render_rejects_symlinked_builds_directory_escape(tmp_path: Path) -> None:
    """Generated build paths cannot redirect writes outside state_dir."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    outside = tmp_path / "outside"
    outside.mkdir()
    config.state_dir.mkdir()
    (config.state_dir / "builds").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RenderError, match="builds directory.*(?:escapes|symlink)"):
        render_marketplace(config, discover_catalog(config))

    assert tuple(outside.iterdir()) == ()


def test_render_rejects_symlinked_published_marketplace(tmp_path: Path) -> None:
    """A forged stable marketplace link is never followed or replaced implicitly."""

    require_directory_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    config = make_config(tmp_path, catalog, components=frozenset({Component.PLUGINS}))
    outside = tmp_path / "outside-marketplace"
    outside.mkdir()
    config.state_dir.mkdir()
    (config.state_dir / "marketplace").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RenderError, match="published marketplace.*(?:escapes|symlink)"):
        render_marketplace(config, discover_catalog(config))

    assert (config.state_dir / "marketplace").is_symlink()
