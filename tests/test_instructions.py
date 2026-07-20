"""Instructions component tests: catalog, governance gating, state, plan/apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_config_bridge.applier import apply_plan
from agent_config_bridge.catalog import CatalogError, discover_catalog
from agent_config_bridge.governance import GovernanceError, resolve_inventory
from agent_config_bridge.instructions import instruction_digest, instruction_files, managed_instruction_directories
from agent_config_bridge.models import Component, LinkMode, Product
from agent_config_bridge.planner import Disposition, build_plan
from agent_config_bridge.provenance import ROOT_MARKER_FILENAME, read_instruction_dir_marker
from agent_config_bridge.state import (
    BridgeStateError,
    InstructionStateEntry,
    instruction_state_path,
    read_instruction_state,
    write_instruction_state,
)
from tests.conftest import make_catalog, make_config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bundle(catalog: Path, name: str, files: dict[str, str]) -> None:
    for relative, content in files.items():
        _write(catalog / "instructions" / name / relative, content)


def _claude_config(tmp_path: Path, catalog: Path, *, mode: LinkMode = LinkMode.SYMLINK):
    return make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        mode=mode,
        components=frozenset({Component.INSTRUCTIONS}),
    )


def _require_file_symlink_support(root: Path) -> None:
    target = root / ".agentbridge-file-symlink-probe-target"
    link = root / ".agentbridge-file-symlink-probe"
    target.write_text("probe\n", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")
    finally:
        if link.is_symlink():
            link.unlink()
        target.unlink()


def test_instruction_bundle_discovers_and_enumerates(tmp_path: Path) -> None:
    """A valid bundle is discovered and its files enumerate sorted per product."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(
        catalog,
        "global-policy",
        {
            "claude-code/CLAUDE.md": "# Global\n",
            "claude-code/rules/git-workflow.md": "# Git\n",
            "claude-code/commands/review.md": "# Review\n",
            "codex/AGENTS.md": "# Agents\n",
            "codex/agents/reviewer.md": "# Reviewer\n",
        },
    )
    config = make_config(tmp_path, catalog)

    inventory = discover_catalog(config)

    assert [bundle.name for bundle in inventory.instructions] == ["global-policy"]
    claude_files = instruction_files(inventory.instructions[0], Product.CLAUDE_CODE)
    assert [file.relpath for file in claude_files] == [
        "CLAUDE.md",
        "commands/review.md",
        "rules/git-workflow.md",
    ]
    codex_files = instruction_files(inventory.instructions[0], Product.CODEX)
    assert [file.relpath for file in codex_files] == ["AGENTS.md", "agents/reviewer.md"]


def test_instruction_bundle_rejects_unknown_overlay_root(tmp_path: Path) -> None:
    """There is deliberately no common/ cross-product overlay."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "bad", {"common/CLAUDE.md": "# X\n"})
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="unsupported roots 'common'"):
        discover_catalog(config)


@pytest.mark.parametrize(
    ("relative", "match"),
    [
        ("claude-code/settings.json", "not allowed for claude-code"),
        ("claude-code/skills/x.md", "outside the allowed claude-code directories"),
        ("codex/rules/x.md", "outside the allowed codex directories"),
        ("codex/CLAUDE.md", "not allowed for codex"),
    ],
)
def test_instruction_bundle_rejects_disallowed_destination(tmp_path: Path, relative: str, match: str) -> None:
    """The per-product allowlist keeps instructions from writing arbitrary files."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "bad", {relative: "# X\n"})
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match=match):
        discover_catalog(config)


def test_instruction_bundle_rejects_bom_non_utf8_and_blank(tmp_path: Path) -> None:
    """Instruction sources must be non-empty UTF-8 without BOM."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle = catalog / "instructions" / "bad"
    target = bundle / "codex" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    config = make_config(tmp_path, catalog)

    target.write_bytes(b"\xef\xbb\xbf# Agents\n")
    with pytest.raises(CatalogError, match="UTF-8 without BOM"):
        discover_catalog(config)

    target.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(CatalogError, match="valid UTF-8"):
        discover_catalog(config)

    target.write_bytes(b"  \n\n")
    with pytest.raises(CatalogError, match="must not be empty"):
        discover_catalog(config)


def test_instruction_bundle_rejects_reserved_marker_name(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "bad", {"claude-code/rules/AGENTBRIDGE-MANAGED.json": "{}\n"})
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="reserved for bridge provenance markers"):
        discover_catalog(config)


def test_instruction_bundle_requires_a_product_overlay_with_files(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle = catalog / "instructions" / "empty"
    bundle.mkdir(parents=True)
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="must contain a codex/ or claude-code/ product overlay"):
        discover_catalog(config)

    (bundle / "claude-code").mkdir()
    with pytest.raises(CatalogError, match="contains no files"):
        discover_catalog(config)


def test_cross_bundle_destination_collision_is_rejected(tmp_path: Path) -> None:
    """Two bundles shipping one relpath for one product violate single ownership."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "first", {"codex/AGENTS.md": "# A\n"})
    _write_bundle(catalog, "second", {"codex/AGENTS.md": "# B\n"})
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="owned by both 'first' and 'second'"):
        discover_catalog(config)


def test_cross_bundle_collision_is_detected_case_insensitively(tmp_path: Path) -> None:
    """Relpaths differing only by case collide on case-insensitive filesystems."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "first", {"claude-code/rules/Foo.md": "# A\n"})
    _write_bundle(catalog, "second", {"claude-code/rules/foo.md": "# B\n"})
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="owned by both 'first' and 'second'"):
        discover_catalog(config)


def test_same_relpath_for_different_products_is_allowed(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "first", {"codex/agents/helper.md": "# A\n"})
    _write_bundle(catalog, "second", {"claude-code/agents/helper.md": "# B\n"})
    config = make_config(tmp_path, catalog)

    inventory = discover_catalog(config)

    assert [bundle.name for bundle in inventory.instructions] == ["first", "second"]


def _instruction_manifest(identifier: str, *, product: str = "claude-code", lifecycle: str = "active") -> str:
    return (
        f'id = "{identifier}"\n'
        'capability_kind = "instruction"\n'
        'delivery = "standalone"\n'
        f'lifecycle = "{lifecycle}"\n'
        'failure_policy = "advisory"\n'
        'owner = "test"\n'
        'last_reviewed = "2026-07-20"\n'
        "[[targets]]\n"
        f'product = "{product}"\n'
        'platform = "linux"\n'
        'surfaces = ["cli", "desktop"]\n'
        "[[artifacts]]\n"
        f'ref = "instructions/{identifier}"\n'
        "[artifacts.provenance]\n"
        'origin = "local-original"\n'
    )


def test_required_mode_covers_and_gates_instruction_bundles(tmp_path: Path) -> None:
    """An ungoverned bundle is a GOV030 error; manifests gate per target."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write_bundle(catalog, "matching", {"claude-code/rules/git.md": "# Git\n"})
    _write_bundle(catalog, "other-product", {"claude-code/rules/other.md": "# Other\n"})
    config = _claude_config(tmp_path, catalog)

    with pytest.raises(GovernanceError, match="GOV030"):
        resolve_inventory(discover_catalog(config))

    _write(catalog / "governance" / "matching.toml", _instruction_manifest("matching"))
    _write(
        catalog / "governance" / "other-product.toml",
        _instruction_manifest("other-product", product="codex"),
    )
    resolved = resolve_inventory(discover_catalog(config))

    names = [bundle.name for bundle in resolved.instructions_for_target(config.targets[0])]
    assert names == ["matching"]


def test_audit_mode_never_gates_instruction_bundles(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "ungoverned", {"claude-code/rules/x.md": "# X\n"})
    config = _claude_config(tmp_path, catalog)

    resolved = resolve_inventory(discover_catalog(config))

    assert [bundle.name for bundle in resolved.instructions_for_target(config.targets[0])] == ["ungoverned"]


def test_instruction_state_round_trip_and_removal(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    config = _claude_config(tmp_path, catalog)
    target = config.targets[0]
    entries = (
        InstructionStateEntry(
            relpath="rules/git.md",
            mode=LinkMode.SYMLINK,
            source_id="instructions/global-policy",
            link_target=str(catalog / "instructions" / "global-policy" / "claude-code" / "rules" / "git.md"),
            installed_digest=None,
        ),
        InstructionStateEntry(
            relpath="CLAUDE.md",
            mode=LinkMode.COPY,
            source_id="instructions/global-policy",
            link_target=None,
            installed_digest="a" * 64,
        ),
    )

    write_instruction_state(config, target, entries)
    assert read_instruction_state(config, target) == entries

    write_instruction_state(config, target, ())
    assert not instruction_state_path(config, target).exists()
    assert read_instruction_state(config, target) == ()


@pytest.mark.parametrize(
    "entry",
    [
        {"relpath": "../evil.md", "mode": "symlink", "source_id": "instructions/b", "link_target": "/x"},
        {"relpath": "rules//x.md", "mode": "symlink", "source_id": "instructions/b", "link_target": "/x"},
        # A drive-letter component would re-anchor PureWindowsPath.joinpath
        # outside config_home; the validator must reject it on every host.
        {"relpath": "D:evil.md", "mode": "copy", "source_id": "instructions/b", "installed_digest": "a" * 64},
        {"relpath": "rules/con.md", "mode": "symlink", "source_id": "instructions/b", "link_target": "/x"},
        {"relpath": "rules/x.", "mode": "symlink", "source_id": "instructions/b", "link_target": "/x"},
        {"relpath": "rules/x.md", "mode": "auto", "source_id": "instructions/b", "link_target": "/x"},
        {"relpath": "rules/x.md", "mode": "symlink", "source_id": "skills/b", "link_target": "/x"},
        {"relpath": "rules/x.md", "mode": "symlink", "source_id": "instructions/b", "link_target": None},
        {"relpath": "rules/x.md", "mode": "copy", "source_id": "instructions/b", "installed_digest": "short"},
        {
            "relpath": "rules/x.md",
            "mode": "symlink",
            "source_id": "instructions/b",
            "link_target": "/x",
            "installed_digest": "a" * 64,
        },
    ],
)
def test_instruction_state_rejects_tampered_entries(tmp_path: Path, entry: dict) -> None:
    import json

    catalog = make_catalog(tmp_path / "catalog", skills=())
    config = _claude_config(tmp_path, catalog)
    target = config.targets[0]
    write_instruction_state(
        config,
        target,
        (
            InstructionStateEntry(
                relpath="rules/keep.md",
                mode=LinkMode.SYMLINK,
                source_id="instructions/b",
                link_target="/x",
                installed_digest=None,
            ),
        ),
    )
    path = instruction_state_path(config, target)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["instructions"] = [entry]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BridgeStateError, match="invalid instruction entry"):
        read_instruction_state(config, target)


def test_apply_links_instructions_records_state_and_markers(tmp_path: Path) -> None:
    """Symlink-mode apply projects per-file links, ownership, and dir markers."""

    _require_file_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(
        catalog,
        "global-policy",
        {"claude-code/CLAUDE.md": "# Global\n", "claude-code/rules/git.md": "# Git\n"},
    )
    config = _claude_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)

    plan = build_plan(config, inventory)
    assert {action.disposition for action in plan.actions} == {Disposition.CREATE}
    apply_plan(config, inventory, plan)

    config_home = tmp_path / "home" / ".claude"
    assert (config_home / "CLAUDE.md").is_symlink()
    assert (config_home / "rules" / "git.md").is_symlink()
    assert (config_home / "rules" / "git.md").read_text(encoding="utf-8") == "# Git\n"

    entries = read_instruction_state(config, target)
    assert sorted(entry.relpath for entry in entries) == ["CLAUDE.md", "rules/git.md"]

    marker = read_instruction_dir_marker(target, "rules")
    assert marker is not None and marker["component"] == "instructions" and marker["file_count"] == 1
    # Root-level single files carry no marker: the config home is not bridge-owned.
    assert not (config_home / ROOT_MARKER_FILENAME).exists()

    replan = build_plan(config, discover_catalog(config))
    assert {action.disposition for action in replan.actions} == {Disposition.NOOP}


@pytest.mark.parametrize("mode", [LinkMode.SYMLINK, LinkMode.COPY])
def test_existing_unmanaged_file_is_a_conflict_even_when_identical(tmp_path: Path, mode: LinkMode) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "global-policy", {"claude-code/rules/git.md": "# Git\n"})
    config = _claude_config(tmp_path, catalog, mode=mode)
    _write(tmp_path / "home" / ".claude" / "rules" / "git.md", "# Git\n")

    plan = build_plan(config, discover_catalog(config))

    assert plan.has_conflicts
    conflict = next(action for action in plan.actions if action.disposition is Disposition.CONFLICT)
    assert conflict.name == "rules/git.md"


def test_deselecting_a_bundle_removes_managed_files_state_and_marker(tmp_path: Path) -> None:
    import shutil

    _require_file_symlink_support(tmp_path)
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "global-policy", {"claude-code/rules/git.md": "# Git\n"})
    config = _claude_config(tmp_path, catalog)
    target = config.targets[0]
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    assert (tmp_path / "home" / ".claude" / "rules" / "git.md").is_symlink()

    shutil.rmtree(catalog / "instructions" / "global-policy")
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    removals = [action for action in plan.actions if action.disposition is Disposition.REMOVE]
    assert [action.name for action in removals] == ["rules/git.md"]

    apply_plan(config, inventory, plan)
    assert not (tmp_path / "home" / ".claude" / "rules" / "git.md").exists()
    assert read_instruction_state(config, target) == ()
    assert read_instruction_dir_marker(target, "rules") is None


def test_copy_mode_deselection_removes_file_and_names_per_file_source(tmp_path: Path) -> None:
    """Copy-mode removal retains a backup and reports the per-file source path."""

    import shutil

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "global-policy", {"claude-code/rules/git.md": "# Git\n"})
    config = _claude_config(tmp_path, catalog, mode=LinkMode.COPY)
    target = config.targets[0]
    destination = tmp_path / "home" / ".claude" / "rules" / "git.md"
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    assert destination.is_file()

    shutil.rmtree(catalog / "instructions" / "global-policy")
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    removal = next(action for action in plan.actions if action.disposition is Disposition.REMOVE)
    assert removal.source == catalog / "instructions" / "global-policy" / "claude-code" / "rules" / "git.md"

    result = apply_plan(config, inventory, plan)
    assert not destination.exists()
    assert read_instruction_state(config, target) == ()
    assert len(result.backups) == 1
    assert result.backups[0].read_text(encoding="utf-8") == "# Git\n"


def test_copy_mode_creates_updates_and_detects_drift(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "global-policy", {"claude-code/rules/git.md": "# Git v1\n"})
    config = _claude_config(tmp_path, catalog, mode=LinkMode.COPY)
    target = config.targets[0]
    destination = tmp_path / "home" / ".claude" / "rules" / "git.md"
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    assert destination.is_file() and not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "# Git v1\n"
    entries = read_instruction_state(config, target)
    assert entries[0].installed_digest == instruction_digest(destination)

    destination.write_text("# hand-edited\n", encoding="utf-8")
    drift_plan = build_plan(config, discover_catalog(config))
    conflict = next(action for action in drift_plan.actions if action.disposition is Disposition.CONFLICT)
    assert conflict.detail == "managed instruction copy was modified after installation"

    destination.write_text("# Git v1\n", encoding="utf-8")
    _write(catalog / "instructions" / "global-policy" / "claude-code" / "rules" / "git.md", "# Git v2\n")
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    assert [action.disposition for action in plan.actions] == [Disposition.UPDATE]

    result = apply_plan(config, inventory, plan)
    assert destination.read_text(encoding="utf-8") == "# Git v2\n"
    assert len(result.backups) == 1
    assert result.backups[0].read_text(encoding="utf-8") == "# Git v1\n"
    assert result.backups[0].is_relative_to(config.state_dir / "backups" / target.name / "instructions")


def test_copy_mode_newline_only_difference_is_not_drift(tmp_path: Path) -> None:
    """Content identity normalizes CRLF/CR to LF like Schedule prompts."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _write_bundle(catalog, "global-policy", {"claude-code/rules/git.md": "# Git\nline\n"})
    config = _claude_config(tmp_path, catalog, mode=LinkMode.COPY)
    destination = tmp_path / "home" / ".claude" / "rules" / "git.md"
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))

    destination.write_bytes(b"# Git\r\nline\r\n")
    plan = build_plan(config, discover_catalog(config))

    assert [action.disposition for action in plan.actions] == [Disposition.NOOP]


def test_managed_instruction_directories_skip_root_files() -> None:
    assert managed_instruction_directories(("CLAUDE.md", "rules/a.md", "rules/b/c.md", "commands/x.md")) == (
        "commands",
        "rules",
    )
    assert managed_instruction_directories(("AGENTS.md",)) == ()
