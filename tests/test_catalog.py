"""Tests for canonical catalog discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_config_bridge.catalog import CatalogError, discover_catalog
from agent_config_bridge.filesystem import MANAGED_MARKER
from tests.conftest import make_catalog, make_config


def test_discover_catalog_returns_sorted_valid_artifacts(tmp_path: Path) -> None:
    """All artifact groups are validated and returned in name order."""

    catalog = make_catalog(
        tmp_path / "catalog",
        skills=("zeta", "alpha"),
        plugins=("shared-plugin",),
        hooks=("audit-event",),
    )

    inventory = discover_catalog(make_config(tmp_path, catalog))

    assert [artifact.name for artifact in inventory.skills] == ["alpha", "zeta"]
    assert [artifact.name for artifact in inventory.plugins] == ["shared-plugin"]
    assert [artifact.name for artifact in inventory.hooks] == ["audit-event"]
    assert inventory.hook_version == "0.1.0"


def test_discover_catalog_rejects_missing_skill_entrypoint(tmp_path: Path) -> None:
    """A Skill directory without exact-case SKILL.md is invalid."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    invalid = catalog / "skills" / "broken"
    invalid.mkdir()
    (invalid / "skill.md").write_text("wrong case", encoding="utf-8")

    with pytest.raises(CatalogError, match="SKILL.md"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("# No frontmatter\n", "must start"),
        ("---\nname: hello\n", "no closing"),
        ("---\nname: other\ndescription: Test.\n---\n", "does not match"),
        ("---\nname: hello\n---\n", "description"),
    ],
)
def test_discover_catalog_validates_skill_frontmatter(tmp_path: Path, contents: str, message: str) -> None:
    """Portable Skills require the common name and description frontmatter."""

    catalog = make_catalog(tmp_path / "catalog")
    (catalog / "skills/hello/SKILL.md").write_text(contents, encoding="utf-8")

    with pytest.raises(CatalogError, match=message):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize(
    "description",
    [
        "description:\n  A folded continuation without an explicit scalar marker.\n  Use it for migration.",
        "description: >\n  A folded block scalar.\n  Use it for migration.",
        "description: |\n  A literal block scalar.\n  Use it for migration.",
    ],
)
def test_discover_catalog_accepts_multiline_skill_description(tmp_path: Path, description: str) -> None:
    """Installed Skills commonly wrap long YAML descriptions across lines."""

    catalog = make_catalog(tmp_path / "catalog")
    (catalog / "skills/hello/SKILL.md").write_text(
        f"---\nname: hello\n{description}\n---\n\nRun the workflow.\n",
        encoding="utf-8",
    )

    assert discover_catalog(make_config(tmp_path, catalog)).skills[0].name == "hello"


def test_discover_catalog_rejects_manifest_name_mismatch(tmp_path: Path) -> None:
    """Plugin namespaces cannot silently diverge across manifests."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    manifest = catalog / "plugins/shared-plugin/codex/.codex-plugin/plugin.json"
    manifest.write_text(json.dumps({"name": "other"}), encoding="utf-8")

    with pytest.raises(CatalogError, match="does not match"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_hook_without_hooks_object(tmp_path: Path) -> None:
    """Raw hook JSON must have the shared top-level hooks shape."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    hook = catalog / "hooks/audit-event/common/hooks.json"
    hook.write_text("{}", encoding="utf-8")

    with pytest.raises(CatalogError, match="hooks object"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize(
    "hooks",
    [
        {"SessionStart": {}},
        {"SessionStart": [{}]},
        {"SessionStart": [{"hooks": [{"type": "command"}]}]},
        {"SessionStart": [{"matcher": 42, "hooks": []}]},
        {"SessionStart": [{"hooks": [{"type": "command", "command": "ok", "timeout": 0}]}]},
    ],
)
def test_discover_catalog_rejects_malformed_hook_groups(tmp_path: Path, hooks: object) -> None:
    """Hook matcher and handler structure is checked before rendering."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    path = catalog / "hooks/audit-event/common/hooks.json"
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    with pytest.raises(CatalogError, match="hook"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_allows_no_hook_version_when_there_are_no_hooks(tmp_path: Path) -> None:
    """An empty hook catalog does not need synthetic plugin version metadata."""

    catalog = make_catalog(tmp_path / "catalog", hooks=())

    assert discover_catalog(make_config(tmp_path, catalog)).hook_version is None


def test_discover_catalog_requires_hook_version_when_hooks_exist(tmp_path: Path) -> None:
    """Hook bundles require a version for their generated plugin."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    (catalog / "hooks/.version").unlink()

    with pytest.raises(CatalogError, match="missing required strict-SemVer"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize("version", ["1.0", "01.0.0", "1.0.0-01", "v1.0.0"])
def test_discover_catalog_rejects_non_strict_hook_semver(tmp_path: Path, version: str) -> None:
    """Hook versions follow SemVer 2.0.0, including leading-zero rules."""

    catalog = make_catalog(tmp_path / "catalog", hooks=("audit-event",))
    (catalog / "hooks/.version").write_text(version, encoding="utf-8")

    with pytest.raises(CatalogError, match="strict SemVer"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_plugin_version_mismatch(tmp_path: Path) -> None:
    """The two rendered variants of a plugin must share one release version."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    manifest = catalog / "plugins/shared-plugin/claude-code/.claude-plugin/plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "0.2.0"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogError, match="versions do not match"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize("version", ["1", "1.0", "01.0.0", "1.0.0-alpha.01"])
def test_discover_catalog_rejects_non_strict_plugin_semver(tmp_path: Path, version: str) -> None:
    """Plugin manifest versions must use strict SemVer 2.0.0 syntax."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    manifest = catalog / "plugins/shared-plugin/codex/.codex-plugin/plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = version
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogError, match="strict SemVer"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_product_manifest_under_common(tmp_path: Path) -> None:
    """Product manifests belong only to their target-specific overlays."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    manifest = catalog / "plugins/shared-plugin/common/.codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(CatalogError, match="must not be placed under common"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.parametrize(
    ("overlay", "namespace", "message"),
    [
        ("common", ".codex-plugin", "Codex metadata namespace.*common"),
        ("common", ".claude-plugin", "Claude Code metadata namespace.*common"),
        ("codex", ".claude-plugin", "Claude Code metadata namespace.*codex"),
        ("claude-code", ".codex-plugin", "Codex metadata namespace.*claude-code"),
        ("common", ".CODEX-PLUGIN", "Codex metadata namespace.*common"),
        ("common", ".CLAUDE-PLUGIN", "Claude Code metadata namespace.*common"),
        ("codex", ".CLAUDE-PLUGIN", "Claude Code metadata namespace.*codex"),
        ("claude-code", ".CODEX-PLUGIN", "Codex metadata namespace.*claude-code"),
    ],
)
def test_discover_catalog_rejects_foreign_root_metadata_namespace(
    tmp_path: Path,
    overlay: str,
    namespace: str,
    message: str,
) -> None:
    """Only the selected product overlay may contribute its metadata root."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("shared-plugin",))
    (catalog / "plugins" / "shared-plugin" / overlay / namespace).mkdir()

    with pytest.raises(CatalogError, match=message):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_reserved_hook_plugin_name(tmp_path: Path) -> None:
    """Canonical plugins cannot shadow the generated hook plugin."""

    catalog = make_catalog(tmp_path / "catalog", plugins=("agent-config-bridge-hooks",))

    with pytest.raises(CatalogError, match="reserved for generated hooks"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_catalog_group_symlink_escape(tmp_path: Path) -> None:
    """A catalog group cannot redirect discovery outside the catalog root."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    outside = tmp_path / "outside"
    outside.mkdir()
    (catalog / "skills").rmdir()
    _symlink_or_skip(catalog / "skills", outside, target_is_directory=True)

    with pytest.raises(CatalogError, match="catalog group.*symlink or junction"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_artifact_root_symlink_escape(tmp_path: Path) -> None:
    """An artifact directory cannot be an alias to an external tree."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("external", encoding="utf-8")
    _symlink_or_skip(catalog / "skills/external", outside, target_is_directory=True)

    with pytest.raises(CatalogError, match="artifact root.*symlink or junction"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_artifact_root_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows junction cannot serve as an artifact root alias."""

    catalog = make_catalog(tmp_path / "catalog")
    artifact_root = catalog / "skills/hello"
    monkeypatch.setattr(
        "agent_config_bridge.catalog.is_directory_reparse_point",
        lambda path: path == artifact_root,
    )

    with pytest.raises(CatalogError, match="artifact root.*symlink or junction"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_nested_symlink_escape(tmp_path: Path) -> None:
    """Files inside an artifact cannot link to external content."""

    catalog = make_catalog(tmp_path / "catalog")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(catalog / "skills/hello/secret.txt", outside)

    with pytest.raises(CatalogError, match="artifact symlink escapes"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_broken_artifact_symlink(tmp_path: Path) -> None:
    """Broken links are invalid rather than silently omitted from rendering."""

    catalog = make_catalog(tmp_path / "catalog")
    _symlink_or_skip(catalog / "skills/hello/broken.txt", tmp_path / "missing.txt")

    with pytest.raises(CatalogError, match="missing, broken, or unresolvable"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_allows_contained_artifact_symlink(tmp_path: Path) -> None:
    """A valid link whose target stays inside the artifact remains portable input."""

    catalog = make_catalog(tmp_path / "catalog")
    _symlink_or_skip(catalog / "skills/hello/reference.md", Path("SKILL.md"))

    inventory = discover_catalog(make_config(tmp_path, catalog))

    assert [artifact.name for artifact in inventory.skills] == ["hello"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_discover_catalog_rejects_fifo(tmp_path: Path) -> None:
    """Special filesystem nodes are never accepted as catalog payloads."""

    catalog = make_catalog(tmp_path / "catalog")
    os.mkfifo(catalog / "skills/hello/input.pipe")

    with pytest.raises(CatalogError, match="unsupported filesystem node"):
        discover_catalog(make_config(tmp_path, catalog))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_discover_catalog_rejects_symlink_to_fifo(tmp_path: Path) -> None:
    """A contained symlink is valid only when its final target is regular."""

    catalog = make_catalog(tmp_path / "catalog")
    targets = catalog / "skills/hello/targets"
    targets.mkdir()
    os.mkfifo(targets / "input.pipe")
    _symlink_or_skip(catalog / "skills/hello/input-link", Path("targets/input.pipe"))

    with pytest.raises(CatalogError, match="symlink target must be a contained regular file"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_directory_symlink_cycle(tmp_path: Path) -> None:
    """Contained directory links cannot recurse forever during rendering."""

    catalog = make_catalog(tmp_path / "catalog")
    _symlink_or_skip(catalog / "skills/hello/loop", Path("."), target_is_directory=True)

    with pytest.raises(CatalogError, match="directory symlinks are not supported"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_windows_directory_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows junction is rejected even when Path.is_symlink would be false."""

    catalog = make_catalog(tmp_path / "catalog")
    junction = catalog / "skills/hello/junction"
    junction.mkdir()
    monkeypatch.setattr(
        "agent_config_bridge.catalog.is_directory_reparse_point",
        lambda path: path == junction,
    )

    with pytest.raises(CatalogError, match="junctions are not supported"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_nested_windows_reserved_name(tmp_path: Path) -> None:
    """Every nested component must be materializable on Windows."""

    catalog = make_catalog(tmp_path / "catalog")
    reserved = catalog / "skills/hello/con.txt"
    try:
        reserved.write_text("reserved", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"host filesystem rejects Windows device names directly: {exc}")

    with pytest.raises(CatalogError, match="reserved on Windows"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_reserved_managed_copy_marker(tmp_path: Path) -> None:
    """Canonical Skill content cannot be overwritten by bridge ownership metadata."""

    catalog = make_catalog(tmp_path / "catalog")
    (catalog / "skills/hello" / MANAGED_MARKER).write_text("authored content\n", encoding="utf-8")

    with pytest.raises(CatalogError, match="reserved for managed-copy ownership metadata"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_allows_nested_managed_marker_name(tmp_path: Path) -> None:
    """The managed marker name is reserved only at a standalone Skill root."""

    catalog = make_catalog(tmp_path / "catalog")
    nested = catalog / "skills/hello/examples"
    nested.mkdir()
    (nested / MANAGED_MARKER).write_text("example payload\n", encoding="utf-8")

    assert discover_catalog(make_config(tmp_path, catalog)).skills[0].name == "hello"


def test_discover_catalog_rejects_nested_case_collision(tmp_path: Path) -> None:
    """Nested overlay paths cannot collapse onto one Windows destination."""

    catalog = make_catalog(tmp_path / "catalog")
    parent = catalog / "skills/hello/assets"
    parent.mkdir()
    (parent / "Readme.md").write_text("first", encoding="utf-8")
    (parent / "README.md").write_text("second", encoding="utf-8")
    if len(tuple(parent.iterdir())) != 2:
        pytest.skip("host filesystem is already case-insensitive")

    with pytest.raises(CatalogError, match="collide on case-insensitive"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_windows_reserved_artifact_name(tmp_path: Path) -> None:
    """Device names such as CON cannot be materialized reliably on Windows."""

    if os.name == "nt":
        pytest.skip("Windows rejects device-name directories before catalog validation")
    catalog = make_catalog(tmp_path / "catalog", skills=("con",))

    with pytest.raises(CatalogError, match="reserved on Windows"):
        discover_catalog(make_config(tmp_path, catalog))


def test_discover_catalog_rejects_case_insensitive_name_collision(tmp_path: Path) -> None:
    """Linux catalogs cannot contain two names that collapse on Windows."""

    if os.name == "nt":
        pytest.skip("Windows collapses case-insensitive names before catalog validation")
    catalog = make_catalog(tmp_path / "catalog", skills=("hello",))
    duplicate = catalog / "skills/HELLO"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text("duplicate", encoding="utf-8")

    with pytest.raises(CatalogError, match="collide on case-insensitive"):
        discover_catalog(make_config(tmp_path, catalog))


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable in this test environment: {exc}")
