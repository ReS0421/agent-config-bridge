"""Tests for conflict-aware product settings projection."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import tomlkit

from agent_config_bridge import settings as settings_module
from agent_config_bridge.models import Product
from agent_config_bridge.settings import (
    OwnedSettingLeaf,
    OwnedTableRule,
    SettingDisposition,
    SettingIssue,
    SettingLeafSpec,
    SettingsError,
    apply_settings_patch,
    build_settings_patch,
    discover_settings_fragments,
    merge_settings_fragments,
    plan_settings_patch,
    setting_value_digest,
)


def _write_fragment(
    catalog: Path,
    bundle: str,
    product: Product,
    content: str,
) -> Path:
    relative = Path("codex/config.toml") if product is Product.CODEX else Path("claude-code/settings.json")
    path = catalog / "settings" / bundle / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _leaf(source: str, path: tuple[str, ...], value: object) -> SettingLeafSpec:
    return SettingLeafSpec(
        source_id=f"settings/{source}",
        path=path,
        value=value,  # type: ignore[arg-type]
        digest=setting_value_digest(value),  # type: ignore[arg-type]
    )


def _owned(leaf: SettingLeafSpec, *, parents: tuple[tuple[str, ...], ...] = ()) -> OwnedSettingLeaf:
    return OwnedSettingLeaf(
        source_id=leaf.source_id,
        path=leaf.path,
        digest=leaf.digest,
        created_parents=parents,
    )


def test_discover_parses_product_specific_fragments_and_escaped_source_ids(tmp_path: Path) -> None:
    """Discovery parses both native formats and emits deterministic leaf specs."""

    catalog = tmp_path / "catalog"
    _write_fragment(catalog, "preferences", Product.CODEX, 'model = "gpt-test"\n[features]\nhooks = true\n')
    _write_fragment(
        catalog,
        "preferences",
        Product.CLAUDE_CODE,
        '{"model":"claude-test","permissions":{"allow":["Read(/a~b)"]},"a/b":true}\n',
    )

    fragments = discover_settings_fragments(catalog)

    assert [(fragment.name, fragment.product) for fragment in fragments] == [
        ("preferences", Product.CODEX),
        ("preferences", Product.CLAUDE_CODE),
    ]
    claude = fragments[1]
    assert [leaf.path for leaf in claude.leaves] == [("a/b",), ("model",), ("permissions", "allow")]
    assert claude.leaves[0].source_id.endswith("/a~1b")


def test_discover_missing_settings_group_is_empty(tmp_path: Path) -> None:
    """A catalog need not contain settings when the component is unused."""

    assert discover_settings_fragments(tmp_path / "catalog") == ()


@pytest.mark.parametrize("name", ["Common", "has space", "con", "trailing-"])
def test_discover_rejects_nonportable_bundle_names(tmp_path: Path, name: str) -> None:
    """Bundle identities are stable on both Linux and Windows filesystems."""

    catalog = tmp_path / "catalog"
    _write_fragment(catalog, name, Product.CODEX, "model = 'x'\n")

    with pytest.raises(SettingsError, match="bundle name"):
        discover_settings_fragments(catalog)


def test_discover_rejects_common_and_unknown_product_entries(tmp_path: Path) -> None:
    """Settings are native product overlays, not a misleading common schema."""

    catalog = tmp_path / "catalog"
    _write_fragment(catalog, "preferences", Product.CODEX, "model = 'x'\n")
    common = catalog / "settings/preferences/common"
    common.mkdir()
    (common / "settings.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SettingsError, match="product-specific"):
        discover_settings_fragments(catalog)


@pytest.mark.parametrize(
    ("product", "content", "message"),
    [
        (Product.CODEX, "[broken\n", "invalid codex"),
        (Product.CLAUDE_CODE, '{"model":1,"model":2}', "duplicate JSON"),
        (Product.CLAUDE_CODE, "[]", "top-level"),
        (Product.CLAUDE_CODE, '{"value": NaN}', "non-finite"),
    ],
)
def test_discover_rejects_malformed_documents(
    tmp_path: Path,
    product: Product,
    content: str,
    message: str,
) -> None:
    """Native settings syntax and deterministic values are validated eagerly."""

    catalog = tmp_path / "catalog"
    _write_fragment(catalog, "preferences", product, content)

    with pytest.raises(SettingsError, match=message):
        discover_settings_fragments(catalog)


def test_discover_rejects_empty_fragment_and_extra_product_file(tmp_path: Path) -> None:
    """A fragment must have leaves and exactly one native settings file."""

    catalog = tmp_path / "catalog"
    path = _write_fragment(catalog, "empty", Product.CLAUDE_CODE, "{}\n")

    with pytest.raises(SettingsError, match="no setting leaves"):
        discover_settings_fragments(catalog)

    path.write_text('{"model":"x"}\n', encoding="utf-8")
    (path.parent / "other.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="unsupported entries"):
        discover_settings_fragments(catalog)


def test_discover_rejects_symlinked_fragment(tmp_path: Path) -> None:
    """A catalog symlink cannot redirect settings discovery outside its bundle."""

    catalog = tmp_path / "catalog"
    outside = tmp_path / "outside.toml"
    outside.write_text("model = 'x'\n", encoding="utf-8")
    fragment = catalog / "settings/preferences/codex/config.toml"
    fragment.parent.mkdir(parents=True)
    try:
        fragment.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(SettingsError, match="real regular file"):
        discover_settings_fragments(catalog)


def test_merge_filters_product_and_rejects_same_leaf_collision(tmp_path: Path) -> None:
    """Every matching bundle contributes, but no two bundles own one path."""

    catalog = tmp_path / "catalog"
    _write_fragment(catalog, "a", Product.CODEX, "model = 'one'\n")
    _write_fragment(catalog, "b", Product.CODEX, "model = 'two'\n")
    _write_fragment(catalog, "b", Product.CLAUDE_CODE, '{"theme":"dark"}\n')
    fragments = discover_settings_fragments(catalog)

    assert [leaf.path for leaf in merge_settings_fragments(Product.CLAUDE_CODE, fragments)] == [("theme",)]
    assert build_settings_patch(Product.CLAUDE_CODE, fragments) == merge_settings_fragments(
        Product.CLAUDE_CODE, fragments
    )
    with pytest.raises(SettingsError, match="colliding paths"):
        merge_settings_fragments(Product.CODEX, fragments)


def test_merge_rejects_ancestor_leaf_collision() -> None:
    """An atomic leaf and a descendant table cannot both be projected."""

    shallow = _leaf("a/codex/a", ("permissions",), ["Read"])
    deep = _leaf("b/codex/b", ("permissions", "allow"), ["Edit"])
    from agent_config_bridge.settings import SettingsFragment

    fragments = (
        SettingsFragment("a", Product.CODEX, Path("/a"), (shallow,)),
        SettingsFragment("b", Product.CODEX, Path("/b"), (deep,)),
    )

    with pytest.raises(SettingsError, match="colliding paths"):
        merge_settings_fragments(Product.CODEX, fragments)


def test_value_digest_is_type_stable_and_mapping_order_independent() -> None:
    """Ownership cannot confuse booleans, integers, floats, or map order."""

    assert setting_value_digest(True) != setting_value_digest(1)
    assert setting_value_digest(1) != setting_value_digest(1.0)
    assert setting_value_digest({"a": 1, "b": 2}) == setting_value_digest({"b": 2, "a": 1})


def test_plan_creates_absent_nested_leaf_and_records_only_created_parents(tmp_path: Path) -> None:
    """Planning records structural ownership without retaining setting values."""

    destination = tmp_path / "home/.claude/settings.json"
    desired = (_leaf("preferences/claude-code/model", ("permissions", "allow"), ["Read"]),)

    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)

    assert plan.changes[0].disposition is SettingDisposition.CREATE
    assert plan.resulting_ownership == (_owned(desired[0], parents=(("permissions",),)),)
    ownership_payload = asdict(plan.resulting_ownership[0])
    assert "value" not in ownership_payload
    assert ownership_payload["digest"] == desired[0].digest


def test_plan_does_not_claim_existing_unmanaged_value_even_when_equal(tmp_path: Path) -> None:
    """Matching content is not ownership proof."""

    destination = tmp_path / "settings.json"
    destination.write_text('{"model":"same"}\n', encoding="utf-8")
    desired = (_leaf("preferences/claude-code/model", ("model",), "same"),)

    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)

    assert plan.has_conflicts
    assert plan.changes[0].disposition is SettingDisposition.CONFLICT
    assert "not bridge-owned" in plan.changes[0].detail


def test_plan_noop_update_and_drift_use_owned_digest(tmp_path: Path) -> None:
    """Only the exact previously installed value can be retained or updated."""

    destination = tmp_path / "settings.json"
    destination.write_text('{"model":"old"}\n', encoding="utf-8")
    old = _leaf("preferences/claude-code/model", ("model",), "old")
    same_plan = plan_settings_patch(Product.CLAUDE_CODE, destination, (old,), (_owned(old),))
    new = _leaf("preferences/claude-code/model", ("model",), "new")
    update_plan = plan_settings_patch(Product.CLAUDE_CODE, destination, (new,), (_owned(old),))
    drifted_owner = replace(_owned(old), digest=setting_value_digest("another"))
    drift_plan = plan_settings_patch(Product.CLAUDE_CODE, destination, (new,), (drifted_owner,))

    assert same_plan.changes[0].disposition is SettingDisposition.NOOP
    assert update_plan.changes[0].disposition is SettingDisposition.UPDATE
    assert drift_plan.changes[0].disposition is SettingDisposition.CONFLICT


def test_plan_cleanup_removes_matching_owned_value_and_forgets_missing_value(tmp_path: Path) -> None:
    """Cleanup removes matching content and safely clears already-absent ownership."""

    destination = tmp_path / "settings.json"
    old = _leaf("preferences/claude-code/model", ("model",), "old")
    destination.write_text('{"model":"old"}\n', encoding="utf-8")
    removal = plan_settings_patch(Product.CLAUDE_CODE, destination, (), (_owned(old),))
    destination.write_text("{}\n", encoding="utf-8")
    absent = plan_settings_patch(Product.CLAUDE_CODE, destination, (), (_owned(old),))

    assert removal.changes[0].disposition is SettingDisposition.REMOVE
    assert absent.changes[0].disposition is SettingDisposition.NOOP
    assert absent.resulting_ownership == ()


def test_plan_rejects_wrong_destination_name_relative_path_and_forged_inputs(tmp_path: Path) -> None:
    """The parser choice and ownership digests cannot be redirected or forged."""

    desired = _leaf("preferences/codex/model", ("model",), "x")
    with pytest.raises(SettingsError, match="named 'config.toml'"):
        plan_settings_patch(Product.CODEX, tmp_path / "settings.json", (desired,))
    with pytest.raises(SettingsError, match="absolute"):
        plan_settings_patch(Product.CODEX, Path("config.toml"), (desired,))
    forged = replace(desired, digest="0" * 64)
    with pytest.raises(SettingsError, match="does not match"):
        plan_settings_patch(Product.CODEX, tmp_path / "config.toml", (forged,))
    duplicate_owner = (_owned(desired), _owned(desired))
    with pytest.raises(SettingsError, match="owned settings contain colliding"):
        plan_settings_patch(Product.CODEX, tmp_path / "config.toml", (), duplicate_owner)


def test_apply_json_preserves_unrelated_keys_and_returns_digest_only_state(tmp_path: Path) -> None:
    """JSON projection updates only the planned owned leaf."""

    destination = tmp_path / "home/.claude/settings.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"theme":"dark"}\n', encoding="utf-8")
    desired = (_leaf("preferences/claude-code/permissions", ("permissions", "allow"), ["Read"]),)
    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)

    result = apply_settings_patch(plan)
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert result.changed
    assert payload == {"theme": "dark", "permissions": {"allow": ["Read"]}}
    assert result.ownership == plan.resulting_ownership
    assert not list(destination.parent.glob("*.bak"))
    assert not list(destination.parent.glob("*.tmp"))


def test_apply_codex_toml_preserves_comments_and_unrelated_tables(tmp_path: Path) -> None:
    """tomlkit retains Codex comments while adding a managed leaf."""

    destination = tmp_path / "home/.codex/config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(
        "# keep this user comment\nmodel = 'personal'\n\n[tui]\nanimations = true # keep inline\n",
        encoding="utf-8",
    )
    desired = (_leaf("preferences/codex/hooks", ("features", "hooks"), True),)

    result = apply_settings_patch(plan_settings_patch(Product.CODEX, destination, desired))
    rendered = destination.read_text(encoding="utf-8")
    parsed = tomlkit.parse(rendered)

    assert result.changed
    assert "# keep this user comment" in rendered
    assert "# keep inline" in rendered
    assert parsed["model"] == "personal"
    assert parsed["tui"]["animations"] is True
    assert parsed["features"]["hooks"] is True


def test_apply_update_then_cleanup_prunes_only_bridge_created_parent(tmp_path: Path) -> None:
    """Deselection prunes recorded containers but preserves unrelated mappings."""

    destination = tmp_path / "settings.json"
    destination.write_text('{"unrelated":{}}\n', encoding="utf-8")
    first = _leaf("preferences/claude-code/permissions", ("permissions", "allow"), ["Read"])
    first_result = apply_settings_patch(plan_settings_patch(Product.CLAUDE_CODE, destination, (first,)))
    second = _leaf("preferences/claude-code/permissions", ("permissions", "allow"), ["Read", "Edit"])
    second_result = apply_settings_patch(
        plan_settings_patch(Product.CLAUDE_CODE, destination, (second,), first_result.ownership)
    )
    cleanup = apply_settings_patch(plan_settings_patch(Product.CLAUDE_CODE, destination, (), second_result.ownership))

    assert cleanup.ownership == ()
    assert json.loads(destination.read_text(encoding="utf-8")) == {"unrelated": {}}


def test_cleanup_preserves_preexisting_empty_parent(tmp_path: Path) -> None:
    """A container that predates ownership is not pruned after its leaf is removed."""

    destination = tmp_path / "settings.json"
    destination.write_text('{"permissions":{}}\n', encoding="utf-8")
    leaf = _leaf("preferences/claude-code/permissions", ("permissions", "allow"), ["Read"])
    installed = apply_settings_patch(plan_settings_patch(Product.CLAUDE_CODE, destination, (leaf,)))

    assert installed.ownership[0].created_parents == ()
    apply_settings_patch(plan_settings_patch(Product.CLAUDE_CODE, destination, (), installed.ownership))
    assert json.loads(destination.read_text(encoding="utf-8")) == {"permissions": {}}


def test_codex_cleanup_preserves_comment_added_to_created_parent(tmp_path: Path) -> None:
    """Semantic cleanup never discards TOML comment-only user content."""

    destination = tmp_path / "config.toml"
    leaf = _leaf("preferences/codex/hooks", ("features", "hooks"), True)
    installed = apply_settings_patch(plan_settings_patch(Product.CODEX, destination, (leaf,)))
    rendered = destination.read_text(encoding="utf-8").replace("[features]\n", "[features]\n# user note\n")
    destination.write_text(rendered, encoding="utf-8")

    apply_settings_patch(plan_settings_patch(Product.CODEX, destination, (), installed.ownership))

    assert "[features]" in destination.read_text(encoding="utf-8")
    assert "# user note" in destination.read_text(encoding="utf-8")


def test_apply_refuses_conflict_and_stale_destination(tmp_path: Path) -> None:
    """No writes occur for a conflicting or stale reviewed plan."""

    destination = tmp_path / "settings.json"
    destination.write_text('{"model":"user"}\n', encoding="utf-8")
    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)
    conflict = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)
    with pytest.raises(SettingsError, match="with conflicts"):
        apply_settings_patch(conflict)
    assert json.loads(destination.read_text(encoding="utf-8")) == {"model": "user"}

    absent_destination = tmp_path / "other/settings.json"
    fresh = plan_settings_patch(Product.CLAUDE_CODE, absent_destination, desired)
    absent_destination.parent.mkdir(parents=True)
    absent_destination.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="changed after planning"):
        apply_settings_patch(fresh)


def test_apply_rejects_forged_plan_changes(tmp_path: Path) -> None:
    """Callers cannot bypass planning by modifying a frozen plan via replace."""

    destination = tmp_path / "settings.json"
    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)
    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)
    forged = replace(plan, changes=())

    with pytest.raises(SettingsError, match="plan or destination changed"):
        apply_settings_patch(forged)


def test_apply_rejects_symlink_and_directory_destinations(tmp_path: Path) -> None:
    """Settings writes never follow links or replace special path types."""

    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)
    real = tmp_path / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    linked = tmp_path / "settings.json"
    try:
        linked.symlink_to(real)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    with pytest.raises(SettingsError, match="real regular file"):
        plan_settings_patch(Product.CLAUDE_CODE, linked, desired)

    linked.unlink()
    linked.mkdir()
    with pytest.raises(SettingsError, match="real regular file"):
        plan_settings_patch(Product.CLAUDE_CODE, linked, desired)


def test_plan_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    """A linked config directory cannot redirect atomic output."""

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)

    with pytest.raises(SettingsError, match="real directory"):
        plan_settings_patch(Product.CLAUDE_CODE, linked_parent / "settings.json", desired)


def test_atomic_replace_failure_removes_temporary_and_keeps_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final replacement leaves no secret-bearing temporary file."""

    destination = tmp_path / "settings.json"
    original = '{"unrelatedSecret":"do-not-copy"}\n'
    destination.write_text(original, encoding="utf-8")
    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)
    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, desired)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("agent_config_bridge.settings.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        apply_settings_patch(plan)

    assert destination.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".*.agentbridge.*.tmp"))


def test_apply_noop_revalidates_snapshot_without_rewriting(tmp_path: Path) -> None:
    """A no-op returns ownership but still rejects post-plan drift."""

    destination = tmp_path / "settings.json"
    leaf = _leaf("preferences/claude-code/model", ("model",), "same")
    destination.write_text('{"model":"same"}\n', encoding="utf-8")
    plan = plan_settings_patch(Product.CLAUDE_CODE, destination, (leaf,), (_owned(leaf),))
    original_stat = destination.stat()

    result = apply_settings_patch(plan)

    assert not result.changed
    assert result.ownership == (_owned(leaf),)
    assert destination.stat().st_mtime_ns == original_stat.st_mtime_ns


def test_apply_creates_private_settings_file(tmp_path: Path) -> None:
    """A newly created vendor settings file is user-readable only on POSIX."""

    destination = tmp_path / "home/.claude/settings.json"
    desired = (_leaf("preferences/claude-code/model", ("model",), "bridge"),)

    apply_settings_patch(plan_settings_patch(Product.CLAUDE_CODE, destination, desired))

    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


# --- Owned tables, claimed tables, and content-free errors (ADR-0009) ------------------------------------------

_SERVER_RULE = OwnedTableRule(
    path=("mcp_servers", "x"),
    removable_children=("tools",),
    removable_when_empty=("args", "env_vars"),
)


def _server_leaf(key: str, value: object, name: str = "x") -> SettingLeafSpec:
    return SettingLeafSpec(
        source_id=f"mcp/{name}",
        path=("mcp_servers", name, key),
        value=value,  # type: ignore[arg-type]
        digest=setting_value_digest(value),  # type: ignore[arg-type]
    )


def _plan_server_removal(destination: Path, *owned: SettingLeafSpec) -> object:
    return plan_settings_patch(
        Product.CODEX,
        destination,
        (),
        tuple(_owned(leaf) for leaf in owned),
        owned_tables=(_SERVER_RULE,),
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        pytest.param(
            'model = "o3"\n\n[mcp_servers.x]\ncommand = "a"\n\n# NOTE about y\n[mcp_servers.y]\ncommand = "b"\n',
            'model = "o3"\n\n# NOTE about y\n[mcp_servers.y]\ncommand = "b"\n',
            id="implicit-super-table-middle",
        ),
        pytest.param(
            '[mcp_servers]\n# servers below\n\n[mcp_servers.x]\ncommand = "a"\n\n'
            '[mcp_servers.x.tools.t]\napproval_mode = "approve"\n\n# NOTE about y\n[mcp_servers.y]\ncommand = "b"\n',
            '[mcp_servers]\n# servers below\n\n# NOTE about y\n[mcp_servers.y]\ncommand = "b"\n',
            id="explicit-super-table-with-nested-tools",
        ),
        pytest.param(
            '[mcp_servers.y]\ncommand = "b"\n\n[mcp_servers.x]\ncommand = "a"\n\n# NOTE at the end of the file\n',
            '[mcp_servers.y]\ncommand = "b"\n\n# NOTE at the end of the file\n',
            id="last-table-in-file",
        ),
        pytest.param(
            '[mcp_servers.x]\ncommand = "a"\n\n# NOTE about features\n[features]\nfoo = true\n',
            "# NOTE about features\n[features]\nfoo = true\n",
            id="next-table-outside-the-super-table",
        ),
    ],
)
def test_owned_table_removal_keeps_the_comment_block_of_what_follows(
    tmp_path: Path,
    original: str,
    expected: str,
) -> None:
    """tomlkit stores the next table's comments in the removed table; they must survive byte-for-byte."""

    destination = tmp_path / "config.toml"
    destination.write_text(original, encoding="utf-8")
    plan = _plan_server_removal(destination, _server_leaf("command", "a"))

    assert plan.removed_tables == (("mcp_servers", "x"),)  # type: ignore[attr-defined]
    apply_settings_patch(plan)  # type: ignore[arg-type]

    assert destination.read_text(encoding="utf-8") == expected


def test_owned_table_removal_keeps_trailing_comments_above_a_table_added_in_the_same_apply(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(
        '[mcp_servers.x]\ncommand = "a"\n\n# NOTE about features\n[features]\nfoo = true\n', encoding="utf-8"
    )
    plan = plan_settings_patch(
        Product.CODEX,
        destination,
        (_server_leaf("command", "z", name="z"),),
        (_owned(_server_leaf("command", "a")),),
        owned_tables=(_SERVER_RULE,),
    )

    apply_settings_patch(plan)

    text = destination.read_text(encoding="utf-8")
    assert "# NOTE about features\n[features]\n" in text
    assert tomlkit.parse(text).unwrap() == {"mcp_servers": {"z": {"command": "z"}}, "features": {"foo": True}}


def test_owned_table_rule_skips_removal_when_an_owned_leaf_drifted(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text('[mcp_servers.x]\ncommand = "edited"\n[mcp_servers.x.tools.t]\nk = 1\n', encoding="utf-8")

    plan = _plan_server_removal(destination, _server_leaf("command", "a"))

    assert plan.removed_tables == ()  # type: ignore[attr-defined]
    assert [(change.disposition, change.issue) for change in plan.changes] == [  # type: ignore[attr-defined]
        (SettingDisposition.CONFLICT, SettingIssue.DRIFT)
    ]


def test_owned_table_rule_removes_the_table_when_another_owned_leaf_is_already_absent(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text('[mcp_servers.x]\ncommand = "a"\n[mcp_servers.x.tools.t]\nk = 1\n', encoding="utf-8")

    plan = _plan_server_removal(destination, _server_leaf("command", "a"), _server_leaf("args", ["-y"]))

    assert plan.removed_tables == (("mcp_servers", "x"),)  # type: ignore[attr-defined]
    result = apply_settings_patch(plan)  # type: ignore[arg-type]
    assert result.ownership == ()
    assert tomlkit.parse(destination.read_text(encoding="utf-8")).unwrap() == {}


def test_owned_table_rule_leaves_a_table_whose_owned_leaves_are_all_absent(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    original = "[mcp_servers.x.tools.t]\nk = 1\n"
    destination.write_text(original, encoding="utf-8")

    plan = _plan_server_removal(destination, _server_leaf("command", "a"))

    assert plan.removed_tables == ()  # type: ignore[attr-defined]
    assert not plan.has_changes  # type: ignore[attr-defined]
    apply_settings_patch(plan)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original


def test_apply_deletes_unowned_children_only_through_their_removed_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(
        '[mcp_servers.x]\ncommand = "a"\nargs = []\n[mcp_servers.x.tools.t]\nk = 1\n', encoding="utf-8"
    )
    plan = _plan_server_removal(destination, _server_leaf("command", "a"))
    removals = {
        change.path: change.detail
        for change in plan.changes  # type: ignore[attr-defined]
        if change.disposition is SettingDisposition.REMOVE
    }
    assert set(removals) == {
        ("mcp_servers", "x", "command"),
        ("mcp_servers", "x", "args"),
        ("mcp_servers", "x", "tools"),
    }
    deleted: list[tuple[str, ...]] = []
    original_delete = settings_module._delete_path

    def recording_delete(document: object, path: tuple[str, ...]) -> None:
        deleted.append(path)
        original_delete(document, path)  # type: ignore[arg-type]

    monkeypatch.setattr(settings_module, "_delete_path", recording_delete)

    apply_settings_patch(plan)  # type: ignore[arg-type]

    # Only the owned leaf is deleted by path; unowned children go with the table.
    assert deleted == [("mcp_servers", "x", "command")]
    assert tomlkit.parse(destination.read_text(encoding="utf-8")).unwrap() == {}


def test_leftover_conflict_carries_raw_keys_and_an_escaped_detail(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text('[mcp_servers.x]\ncommand = "a"\n"\\u001b[2J" = 1\n', encoding="utf-8")

    plan = _plan_server_removal(destination, _server_leaf("command", "a"))

    leftover = [change for change in plan.changes if change.issue is SettingIssue.LEFTOVER]  # type: ignore[attr-defined]
    assert [change.keys for change in leftover] == [("\x1b[2J",)]
    assert "\\x1b[2J" in leftover[0].detail
    assert "\x1b" not in leftover[0].detail


@pytest.mark.parametrize(
    "existing",
    [
        '[mcp_servers.x]\nenv = { TOKEN = "SECRET" }\n',
        '[mcp_servers.x]\nbearer_token = "SECRET"\n',
        '[mcp_servers.x]\nurl = "https://example.com/mcp"\n',
        '[mcp_servers.x.tools.t]\napproval_mode = "approve"\n',
    ],
)
def test_claimed_table_that_already_exists_is_an_unowned_conflict(tmp_path: Path, existing: str) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text(existing, encoding="utf-8")
    desired = (_server_leaf("command", "npx"), _server_leaf("args", ["-y"]))

    plan = plan_settings_patch(Product.CODEX, destination, desired, claimed_tables=(("mcp_servers", "x"),))

    assert [(change.path, change.issue) for change in plan.changes] == [(("mcp_servers", "x"), SettingIssue.UNOWNED)]
    assert plan.resulting_ownership == ()
    assert "SECRET" not in plan.changes[0].detail
    with pytest.raises(SettingsError, match="conflicts"):
        apply_settings_patch(plan)
    assert destination.read_text(encoding="utf-8") == existing


def test_claimed_table_may_be_created_absent_or_empty_and_kept_once_owned(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text("[mcp_servers.x]\n", encoding="utf-8")
    leaf = _server_leaf("command", "npx")
    claims = (("mcp_servers", "x"),)

    created = plan_settings_patch(Product.CODEX, destination, (leaf,), claimed_tables=claims)
    assert [change.disposition for change in created.changes] == [SettingDisposition.CREATE]
    apply_settings_patch(created)

    with destination.open("a", encoding="utf-8") as stream:
        stream.write('[mcp_servers.x.tools.t]\napproval_mode = "approve"\n')
    owned = plan_settings_patch(Product.CODEX, destination, (leaf,), (_owned(leaf),), claimed_tables=claims)
    assert [change.disposition for change in owned.changes] == [SettingDisposition.NOOP]


def test_codex_destination_parse_errors_report_only_a_position(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text('model = "o3"\ntoken = "ghp_SECRETVALUE\n', encoding="utf-8")

    with pytest.raises(SettingsError, match=r"\(at line 2, column \d+\)") as raised:
        plan_settings_patch(Product.CODEX, destination, ())

    assert "SECRET" not in str(raised.value)


def test_destination_value_errors_do_not_name_product_keys(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text("[profiles.SECRETNAME]\nlimit = nan\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="non-finite") as raised:
        plan_settings_patch(Product.CODEX, destination, ())

    assert "SECRETNAME" not in str(raised.value)


def test_fragment_value_errors_name_escaped_keys(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    _write_fragment(catalog, "preferences", Product.CODEX, '[profiles."x\\u001by"]\nlimit = nan\n')

    with pytest.raises(SettingsError, match="non-finite") as raised:
        discover_settings_fragments(catalog)

    assert "profiles.x\\x1by.limit" in str(raised.value)
    assert "\x1b" not in str(raised.value)


def test_json_destination_duplicate_keys_are_not_echoed(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_text('{"SECRETKEY": 1, "SECRETKEY": 2}', encoding="utf-8")

    with pytest.raises(SettingsError, match="duplicate JSON object key") as raised:
        plan_settings_patch(Product.CLAUDE_CODE, destination, ())

    assert "SECRETKEY" not in str(raised.value)
