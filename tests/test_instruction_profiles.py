"""Codex developer-instruction profile projection and lifecycle tests."""

from __future__ import annotations

import errno
import os
import shutil
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_config_bridge import instruction_profiles as profile_module
from agent_config_bridge import instructions as instruction_module
from agent_config_bridge.applier import apply_plan
from agent_config_bridge.catalog import CatalogError, discover_catalog
from agent_config_bridge.filesystem import FilesystemError
from agent_config_bridge.instruction_profiles import (
    InstructionProfileError,
    check_instruction_profiles,
    generate_instruction_profiles,
)
from agent_config_bridge.models import Component, LinkMode
from agent_config_bridge.planner import Disposition, build_plan
from agent_config_bridge.state import read_instruction_state
from tests.conftest import make_catalog, make_config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _profile_bundle(
    catalog: Path,
    *,
    bundle_name: str = "team-policy",
    profile_name: str = "team-lead",
    source_text: str = "# Team Lead\n",
) -> tuple[Path, Path, Path]:
    bundle = catalog / "instructions" / bundle_name
    source = bundle / "codex" / "model-instructions" / "team-lead.md"
    output = bundle / "codex" / f"{profile_name}.config.toml"
    _write(source, source_text)
    _write(
        bundle / "projections.toml",
        (
            "schema_version = 1\n\n"
            "[[codex_profiles]]\n"
            f'name = "{profile_name}"\n'
            'source = "codex/model-instructions/team-lead.md"\n'
        ),
    )
    return bundle, source, output


def _runtime_hook_state_suffix(trusted_hash: str = "a" * 64) -> bytes:
    return (
        f'\n[hooks.state]\n\n[hooks.state."hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "sha256:{trusted_hash}"\n'
    ).encode()


def _assert_posix_private_mode(path: Path) -> None:
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_generate_then_check_round_trips_arbitrary_prompt_and_detects_drift_read_only(
    tmp_path: Path,
) -> None:
    """The generated TOML is deterministic and check never repairs drift."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    prompt = (
        "# 역할\r\n"
        '경로는 C:\\Users\\ReS 이고 "따옴표"와 """삼중 따옴표"""를 유지한다.\r'
        "apostrophe ''' and emoji 😀\r\n"
    )
    _bundle, _source, output = _profile_bundle(catalog, source_text=prompt)

    generated = generate_instruction_profiles(catalog)

    assert generated.valid is True
    assert generated.changed == 1
    assert [(profile.name, profile.status) for profile in generated.profiles] == [("team-lead", "created")]
    first_bytes = output.read_bytes()
    with output.open("rb") as stream:
        parsed = tomllib.load(stream)
    assert set(parsed) == {"developer_instructions"}
    assert parsed["developer_instructions"] == prompt.replace("\r\n", "\n").replace("\r", "\n")
    assert first_bytes.endswith(b"\n")
    assert b"source-sha256: " in first_bytes

    repeated = generate_instruction_profiles(catalog)
    assert repeated.changed == 0
    assert repeated.profiles[0].status == "current"
    assert output.read_bytes() == first_bytes

    output.write_bytes(first_bytes.replace(b"\n", b"\r\n"))
    crlf_bytes = output.read_bytes()
    checked = check_instruction_profiles(catalog)
    assert checked.valid is False
    assert checked.profiles[0].status == "stale"
    assert output.read_bytes() == crlf_bytes

    output.write_bytes(first_bytes + b"# local drift\n")
    drifted_bytes = output.read_bytes()
    checked = check_instruction_profiles(catalog)

    assert checked.valid is False
    assert checked.changed == 0
    assert checked.profiles[0].status == "stale"
    assert output.read_bytes() == drifted_bytes


@pytest.mark.parametrize("state", ["missing", "stale"])
def test_discover_rejects_missing_or_stale_declared_profile_output(tmp_path: Path, state: str) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    if state == "missing":
        output.unlink()
    else:
        output.write_text('developer_instructions = "different"\n', encoding="utf-8")
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match=state):
        discover_catalog(config)


@pytest.mark.parametrize(
    ("descriptor", "match"),
    [
        (
            'schema_version = 2\n[[codex_profiles]]\nname = "team-lead"\n'
            'source = "codex/model-instructions/team-lead.md"\n',
            "schema_version",
        ),
        (
            'schema_version = 1\nextra = true\n[[codex_profiles]]\nname = "team-lead"\n'
            'source = "codex/model-instructions/team-lead.md"\n',
            "exactly",
        ),
        (
            'schema_version = 1\n[[codex_profiles]]\nname = "team-lead"\nsource = "../outside.md"\n',
            "direct codex/model-instructions",
        ),
        (
            'schema_version = 1\n[[codex_profiles]]\nname = "team-lead"\n'
            'source = "codex/model-instructions/nested/team-lead.md"\n',
            "direct codex/model-instructions",
        ),
        (
            'schema_version = 1\n[[codex_profiles]]\nname = "team-lead"\nsource = "codex/model-instructions/bad?.md"\n',
            "not portable to Windows",
        ),
        (
            'schema_version = 1\n[[codex_profiles]]\nname = "TeamLead"\n'
            'source = "codex/model-instructions/team-lead.md"\n',
            "lowercase kebab-case",
        ),
        (
            'schema_version = 1\n[[codex_profiles]]\nname = "team-lead"\n'
            'source = "codex/model-instructions/team-lead.md"\nextra = true\n',
            "exact keys",
        ),
    ],
)
def test_descriptor_schema_and_paths_are_strict(tmp_path: Path, descriptor: str, match: str) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle, _source, _output = _profile_bundle(catalog)
    (bundle / "projections.toml").write_text(descriptor, encoding="utf-8")

    with pytest.raises(InstructionProfileError, match=match):
        generate_instruction_profiles(catalog)


def test_descriptor_rejects_duplicate_and_casefold_colliding_names(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle, _source, _output = _profile_bundle(catalog)
    (bundle / "projections.toml").write_text(
        (
            "schema_version = 1\n"
            "[[codex_profiles]]\n"
            'name = "team-lead"\n'
            'source = "codex/model-instructions/team-lead.md"\n'
            "[[codex_profiles]]\n"
            'name = "TEAM-LEAD"\n'
            'source = "codex/model-instructions/team-lead.md"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(InstructionProfileError, match="case-insensitive"):
        generate_instruction_profiles(catalog)


def test_descriptor_source_must_be_real_regular_file(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle, source, _output = _profile_bundle(catalog)
    real_source = bundle / "real.md"
    source.replace(real_source)
    try:
        source.symlink_to(real_source)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(InstructionProfileError, match="real regular file"):
        generate_instruction_profiles(catalog)


def test_generate_refuses_symlink_output_destination(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    foreign = tmp_path / "foreign.toml"
    foreign.write_text("keep\n", encoding="utf-8")
    try:
        output.symlink_to(foreign)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(InstructionProfileError, match="symlink"):
        generate_instruction_profiles(catalog)
    assert foreign.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    ("profile_text", "match"),
    [
        ('developer_instructions = "# Team Lead\\n"\nmodel = "dangerous"\n', "only developer_instructions"),
        ('developer_instructions = ""\n', "must not be blank"),
        ("developer_instructions = 7\n", "must be a string"),
    ],
)
def test_profile_rejects_additional_keys_and_invalid_prompt_values(
    tmp_path: Path,
    profile_text: str,
    match: str,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    output.write_text(profile_text, encoding="utf-8")
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match=match):
        discover_catalog(config)


@pytest.mark.parametrize("filename", ["config.toml", "undeclared.config.toml"])
def test_undeclared_or_base_codex_config_is_never_an_instruction_profile(
    tmp_path: Path,
    filename: str,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle, _source, _output = _profile_bundle(catalog)
    (bundle / "codex" / filename).write_text('developer_instructions = "x"\n', encoding="utf-8")
    config = make_config(tmp_path, catalog)

    with pytest.raises(CatalogError, match="never allowed|undeclared"):
        discover_catalog(config)


def test_generated_root_profile_uses_existing_copy_state_backup_and_unmanaged_conflict(
    tmp_path: Path,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    target = config.targets[0]

    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    profile_action = next(action for action in plan.actions if action.name == "team-lead.config.toml")
    assert profile_action.disposition is Disposition.CREATE
    result = apply_plan(config, inventory, plan)
    installed = target.config_home / "team-lead.config.toml"
    assert installed.read_bytes() == output.read_bytes()
    assert any(entry.relpath == "team-lead.config.toml" for entry in read_instruction_state(config, target))
    assert not result.backups

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    profile_action = next(action for action in plan.actions if action.name == "team-lead.config.toml")
    assert profile_action.disposition is Disposition.UPDATE
    result = apply_plan(config, inventory, plan)
    assert installed.read_bytes() == output.read_bytes()
    assert any("team-lead.config.toml" in backup.parts for backup in result.backups)

    other = tmp_path / "other"
    other_catalog = make_catalog(other / "catalog", skills=())
    _other_bundle, _other_source, other_output = _profile_bundle(other_catalog)
    generate_instruction_profiles(other_catalog)
    other_config = make_config(
        other,
        other_catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    unmanaged = other_config.targets[0].config_home / "team-lead.config.toml"
    unmanaged.parent.mkdir(parents=True)
    shutil.copyfile(other_output, unmanaged)

    conflict = build_plan(other_config, discover_catalog(other_config))
    profile_action = next(action for action in conflict.actions if action.name == "team-lead.config.toml")
    assert profile_action.disposition is Disposition.CONFLICT
    assert "no matching target ownership state" in profile_action.detail
    assert os.path.samefile(unmanaged, unmanaged)


def test_generated_profile_copy_preserves_valid_runtime_hook_state_across_lifecycle(
    tmp_path: Path,
) -> None:
    """Provider-owned trust stays opaque across no-op, update, backup, and removal."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    target = config.targets[0]
    inventory = discover_catalog(config)

    create = build_plan(config, inventory)
    create_action = next(action for action in create.actions if action.name == "team-lead.config.toml")
    assert create_action.disposition is Disposition.CREATE
    apply_plan(config, inventory, create)
    installed = target.config_home / "team-lead.config.toml"
    assert installed.read_bytes() == output.read_bytes()
    _assert_posix_private_mode(installed)
    assert set(tomllib.loads(output.read_text(encoding="utf-8"))) == {"developer_instructions"}

    suffix = _runtime_hook_state_suffix()
    installed.write_bytes(installed.read_bytes() + suffix)
    with_runtime_state = installed.read_bytes()
    noop = build_plan(config, discover_catalog(config))
    noop_action = next(action for action in noop.actions if action.name == "team-lead.config.toml")
    assert noop_action.disposition is Disposition.NOOP
    apply_plan(config, discover_catalog(config), noop)
    assert installed.read_bytes() == with_runtime_state

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    update = build_plan(config, inventory)
    update_action = next(action for action in update.actions if action.name == "team-lead.config.toml")
    assert update_action.disposition is Disposition.UPDATE
    updated = apply_plan(config, inventory, update)
    assert installed.read_bytes() == output.read_bytes() + suffix
    _assert_posix_private_mode(installed)
    profile_backup = next(backup for backup in updated.backups if "team-lead.config.toml" in backup.parts)
    assert profile_backup.read_bytes() == with_runtime_state

    deselected_target = replace(target, components=frozenset())
    deselected = replace(config, components=frozenset(), targets=(deselected_target,))
    inventory = discover_catalog(deselected)
    removal = build_plan(deselected, inventory)
    removal_action = next(action for action in removal.actions if action.name == "team-lead.config.toml")
    assert removal_action.disposition is Disposition.REMOVE
    removed = apply_plan(deselected, inventory, removal)
    assert not installed.exists()
    profile_backup = next(backup for backup in removed.backups if "team-lead.config.toml" in backup.parts)
    assert profile_backup.read_bytes() == output.read_bytes() + suffix


def test_profile_update_rejects_managed_inspection_aba_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign A -> managed B -> foreign A read cannot install B's suffix."""

    old_source = tmp_path / "old.config.toml"
    new_source = tmp_path / "new.config.toml"
    destination = tmp_path / "profile.config.toml"
    old_source.write_bytes(b'developer_instructions = "old"\n')
    new_source.write_bytes(b'developer_instructions = "new"\n')
    runtime_suffix = _runtime_hook_state_suffix()
    managed_b = old_source.read_bytes() + runtime_suffix
    foreign_a = b'foreign = "same bytes before and after observation"\n'
    destination.write_bytes(foreign_a)
    original_snapshot = instruction_module._read_exact_regular_file_snapshot
    injected = False

    def snapshot_with_aba(path: Path) -> instruction_module._ExactRegularFileSnapshot:
        nonlocal injected
        if path == destination and not injected:
            destination.write_bytes(managed_b)
            try:
                return original_snapshot(path)
            finally:
                destination.write_bytes(foreign_a)
                injected = True
        return original_snapshot(path)

    monkeypatch.setattr(
        instruction_module,
        "_read_exact_regular_file_snapshot",
        snapshot_with_aba,
    )

    with pytest.raises(FilesystemError, match="changed after planning"):
        instruction_module.apply_instruction_copy(
            new_source,
            destination,
            source_digest=instruction_module.instruction_digest(new_source),
            installed_digest=instruction_module.instruction_digest(old_source),
            state_dir=tmp_path / "state",
            target_name="local",
            relpath="profile.config.toml",
            update=True,
            allow_runtime_hook_state=True,
        )

    assert injected is True
    assert destination.read_bytes() == foreign_a
    assert not tuple((tmp_path / "state").rglob("*"))
    assert not tuple(tmp_path.glob(".profile.config.toml.agentbridge.*.old"))


def test_profile_removal_rejects_managed_inspection_aba_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal cannot parse managed B but displace and retain foreign A."""

    old_source = tmp_path / "old.config.toml"
    destination = tmp_path / "profile.config.toml"
    old_source.write_bytes(b'developer_instructions = "old"\n')
    managed_b = old_source.read_bytes() + _runtime_hook_state_suffix()
    foreign_a = b'foreign = "same bytes before and after observation"\n'
    destination.write_bytes(foreign_a)
    original_snapshot = instruction_module._read_exact_regular_file_snapshot
    injected = False

    def snapshot_with_aba(path: Path) -> instruction_module._ExactRegularFileSnapshot:
        nonlocal injected
        if path == destination and not injected:
            destination.write_bytes(managed_b)
            try:
                return original_snapshot(path)
            finally:
                destination.write_bytes(foreign_a)
                injected = True
        return original_snapshot(path)

    monkeypatch.setattr(
        instruction_module,
        "_read_exact_regular_file_snapshot",
        snapshot_with_aba,
    )

    with pytest.raises(FilesystemError, match="changed after planning"):
        instruction_module.apply_instruction_remove(
            destination,
            mode=LinkMode.COPY,
            expected_link_target=None,
            installed_digest=instruction_module.instruction_digest(old_source),
            state_dir=tmp_path / "state",
            target_name="local",
            relpath="profile.config.toml",
            windows_path_semantics=False,
            allow_runtime_hook_state=True,
        )

    assert injected is True
    assert destination.read_bytes() == foreign_a
    assert not tuple((tmp_path / "state").rglob("*"))
    assert not tuple(tmp_path.glob(".profile.config.toml.agentbridge.*.old"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode repair has no Windows ACL equivalent")
def test_profile_apply_repairs_legacy_permissions_for_copy_and_backup(tmp_path: Path) -> None:
    """A legacy managed profile and its retained backup are tightened to 0600."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    suffix = _runtime_hook_state_suffix()
    installed.write_bytes(installed.read_bytes() + suffix)
    installed.chmod(0o644)

    repair = build_plan(config, discover_catalog(config))
    action = next(item for item in repair.actions if item.name == "team-lead.config.toml")
    assert action.disposition is Disposition.UPDATE
    assert "permissions" in action.detail
    repaired = apply_plan(config, discover_catalog(config), repair)

    assert installed.read_bytes() == output.read_bytes() + suffix
    _assert_posix_private_mode(installed)
    backup = next(path for path in repaired.backups if "team-lead.config.toml" in path.parts)
    assert backup.read_bytes() == output.read_bytes() + suffix
    _assert_posix_private_mode(backup)


def test_profile_update_restores_concurrent_hook_trust_change_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trust append racing the swap is never silently replaced by stale state."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    original_suffix = _runtime_hook_state_suffix("a" * 64)
    latest_suffix = _runtime_hook_state_suffix("b" * 64)
    installed.write_bytes(installed.read_bytes() + original_suffix)

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    update = build_plan(config, inventory)
    original_replace = instruction_module.os.replace
    chmod = Mock(wraps=instruction_module._chmod_regular_file_identity)
    injected = False

    def replace_with_trust_race(source_path: Path, destination_path: Path) -> None:
        nonlocal injected
        if not injected and Path(source_path) == installed and str(destination_path).endswith(".old"):
            managed = installed.read_bytes()[: -len(original_suffix)]
            installed.write_bytes(managed + latest_suffix)
            injected = True
        original_replace(source_path, destination_path)

    monkeypatch.setattr(instruction_module.os, "replace", replace_with_trust_race)
    monkeypatch.setattr(instruction_module, "_chmod_regular_file_identity", chmod)

    with pytest.raises(FilesystemError, match="changed during update validation"):
        apply_plan(config, inventory, update)

    assert injected is True
    chmod.assert_not_called()
    assert installed.read_bytes().endswith(latest_suffix)
    assert not installed.read_bytes().endswith(original_suffix)
    _assert_posix_private_mode(installed)


@pytest.mark.skipif(os.name == "nt", reason="Windows file sharing prevents replacing an open destination")
def test_profile_update_preserves_open_fd_change_after_replacement_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late old-inode write is retained without deleting the active replacement."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    original_suffix = _runtime_hook_state_suffix("a" * 64)
    latest_suffix = _runtime_hook_state_suffix("b" * 64)
    installed.write_bytes(installed.read_bytes() + original_suffix)
    latest_bytes = installed.read_bytes()[: -len(original_suffix)] + latest_suffix

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    update = build_plan(config, inventory)
    original_install = instruction_module._install_staged_copy_without_replacement
    injected = False

    with installed.open("r+b", buffering=0) as displaced_stream:

        def install_then_write_old_inode(temporary: Path, destination: Path) -> tuple[int, int]:
            nonlocal injected
            installed_identity = original_install(temporary, destination)
            if not injected and destination == installed:
                displaced_stream.seek(-len(original_suffix), os.SEEK_END)
                displaced_stream.write(latest_suffix)
                os.fsync(displaced_stream.fileno())
                injected = True
            return installed_identity

        monkeypatch.setattr(
            instruction_module,
            "_install_staged_copy_without_replacement",
            install_then_write_old_inode,
        )

        with pytest.raises(FilesystemError, match="changed after replacement install"):
            apply_plan(config, inventory, update)

    assert injected is True
    assert installed.read_bytes() == output.read_bytes() + original_suffix
    _assert_posix_private_mode(installed)
    profile_backup = next(
        backup
        for backup in (config.state_dir / "backups").rglob("*")
        if backup.is_file() and "team-lead.config.toml" in backup.parts
    )
    assert profile_backup.read_bytes() == latest_bytes
    assert any(
        candidate.read_bytes() == latest_bytes
        for candidate in installed.parent.glob(".team-lead.config.toml.agentbridge.*.old")
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows file sharing prevents replacing an open destination")
def test_profile_update_cross_filesystem_backup_detects_open_fd_change_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXDEV copy keeps the local old inode until a post-copy stability check."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    original_suffix = _runtime_hook_state_suffix("a" * 64)
    latest_suffix = _runtime_hook_state_suffix("b" * 64)
    installed.write_bytes(installed.read_bytes() + original_suffix)
    latest_bytes = installed.read_bytes()[: -len(original_suffix)] + latest_suffix

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    update = build_plan(config, inventory)
    original_link = instruction_module.os.link
    original_copy = instruction_module._copy_regular_file_exclusive
    injected = False

    with installed.open("r+b", buffering=0) as displaced_stream:

        def cross_filesystem_link(
            source_path: Path,
            destination_path: Path,
            *,
            follow_symlinks: bool = True,
        ) -> None:
            if config.state_dir in Path(destination_path).parents:
                raise OSError(errno.EXDEV, "injected cross-filesystem link")
            original_link(source_path, destination_path, follow_symlinks=follow_symlinks)

        def copy_then_write_old_inode(
            source_path: Path,
            destination_path: Path,
        ) -> tuple[str, tuple[int, int], tuple[int, int]]:
            nonlocal injected
            result = original_copy(source_path, destination_path)
            if (
                not injected
                and config.state_dir in Path(destination_path).parents
                and Path(source_path).name.startswith(".team-lead.config.toml.agentbridge.")
            ):
                displaced_stream.seek(-len(original_suffix), os.SEEK_END)
                displaced_stream.write(latest_suffix)
                os.fsync(displaced_stream.fileno())
                injected = True
            return result

        monkeypatch.setattr(instruction_module.os, "link", cross_filesystem_link)
        monkeypatch.setattr(instruction_module, "_copy_regular_file_exclusive", copy_then_write_old_inode)

        with pytest.raises(FilesystemError, match="changed while retaining backup"):
            apply_plan(config, inventory, update)

    assert injected is True
    assert installed.read_bytes() == latest_bytes
    _assert_posix_private_mode(installed)


def test_profile_update_exclusive_fallback_never_overwrites_recreated_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent creator wins between unsupported-link and O_EXCL fallback."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    latest_suffix = _runtime_hook_state_suffix("b" * 64)
    concurrent_bytes = installed.read_bytes() + latest_suffix

    source.write_text("# Team Lead v2\n", encoding="utf-8")
    generate_instruction_profiles(catalog)
    inventory = discover_catalog(config)
    update = build_plan(config, inventory)
    original_link = instruction_module.os.link
    injected = False

    def link_with_recreated_destination(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal injected
        if not injected and Path(destination_path) == installed:
            installed.write_bytes(concurrent_bytes)
            injected = True
            raise OSError(errno.EOPNOTSUPP, "injected hard-link unsupported")
        original_link(source_path, destination_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(instruction_module.os, "link", link_with_recreated_destination)

    with pytest.raises(FilesystemError, match="concurrently created destination"):
        apply_plan(config, inventory, update)

    assert injected is True
    assert installed.read_bytes() == concurrent_bytes
    assert tuple(installed.parent.glob(".team-lead.config.toml.agentbridge.*.old"))


def test_profile_update_recovery_never_unlinks_an_occupied_active_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery has no check-then-unlink window for an exact active replacement."""

    destination = tmp_path / "profile.config.toml"
    candidate = tmp_path / ".profile.config.toml.agentbridge.old"
    raced = tmp_path / "concurrent.config.toml"
    destination.write_bytes(b"staged replacement\n")
    candidate.write_bytes(b"old managed bytes\n")
    raced.write_bytes(b"concurrent active bytes\n")
    original_unlink = Path.unlink
    active_unlink_attempted = False

    def unlink_with_concurrent_replacement(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal active_unlink_attempted
        if path == destination:
            active_unlink_attempted = True
            os.replace(raced, destination)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_with_concurrent_replacement)

    instruction_module._recover_instruction_update(
        destination=destination,
        candidates=(candidate,),
    )

    assert active_unlink_attempted is False
    assert destination.read_bytes() == b"staged replacement\n"
    assert candidate.read_bytes() == b"old managed bytes\n"
    assert raced.read_bytes() == b"concurrent active bytes\n"


def test_instruction_copy_install_uses_exclusive_fallback_without_hard_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COPY delivery remains available on filesystems without hard-link support."""

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"managed profile bytes\n")
    source.chmod(0o600)

    def unsupported_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del source_path, destination_path, follow_symlinks
        raise OSError(errno.EOPNOTSUPP, "injected hard-link unsupported")

    monkeypatch.setattr(instruction_module.os, "link", unsupported_link)

    installed_identity = instruction_module._install_staged_copy_without_replacement(source, destination)

    assert destination.read_bytes() == b"managed profile bytes\n"
    assert instruction_module._regular_file_identity(destination) == installed_identity
    _assert_posix_private_mode(destination)
    assert not source.exists()


def test_exclusive_copy_source_read_rejects_raced_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback binds its lstat identity to a no-follow opened descriptor."""

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    foreign = tmp_path / "foreign"
    raced_link = tmp_path / "raced-link"
    source.write_bytes(b"intended managed bytes\n")
    foreign.write_bytes(b"foreign bytes must not be copied\n")
    try:
        raced_link.symlink_to(foreign)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    original_open = instruction_module.os.open
    injected = False

    def open_with_symlink_race(path: Path, flags: int, mode: int = 0o777) -> int:
        nonlocal injected
        if Path(path) == source and not injected:
            os.replace(raced_link, source)
            injected = True
        return original_open(path, flags, mode)

    monkeypatch.setattr(instruction_module.os, "open", open_with_symlink_race)

    with pytest.raises(FilesystemError, match="open instruction file|changed while opening"):
        instruction_module._copy_regular_file_exclusive(source, destination)

    assert injected is True
    assert source.is_symlink()
    assert foreign.read_bytes() == b"foreign bytes must not be copied\n"
    assert not destination.exists()


def test_profile_removal_restores_concurrent_hook_trust_change_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deselection never backs up and removes bytes that changed after validation."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    original_suffix = _runtime_hook_state_suffix("a" * 64)
    latest_suffix = _runtime_hook_state_suffix("b" * 64)
    installed.write_bytes(installed.read_bytes() + original_suffix)
    target = config.targets[0]
    deselected = replace(
        config,
        components=frozenset(),
        targets=(replace(target, components=frozenset()),),
    )
    inventory = discover_catalog(deselected)
    removal = build_plan(deselected, inventory)
    original_replace = instruction_module.os.replace
    chmod = Mock(wraps=instruction_module._chmod_regular_file_identity)
    injected = False

    def replace_with_trust_race(source_path: Path, destination_path: Path) -> None:
        nonlocal injected
        if not injected and Path(source_path) == installed and str(destination_path).endswith(".old"):
            managed = installed.read_bytes()[: -len(original_suffix)]
            installed.write_bytes(managed + latest_suffix)
            injected = True
        original_replace(source_path, destination_path)

    monkeypatch.setattr(instruction_module.os, "replace", replace_with_trust_race)
    monkeypatch.setattr(instruction_module, "_chmod_regular_file_identity", chmod)

    with pytest.raises(FilesystemError, match="changed during removal validation"):
        apply_plan(deselected, inventory, removal)

    assert injected is True
    chmod.assert_not_called()
    assert installed.read_bytes().endswith(latest_suffix)
    _assert_posix_private_mode(installed)


def test_profile_removal_restores_from_backup_when_digest_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXDEV recovery exclusively copies a retained backup back to an absent path."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    target = config.targets[0]
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = target.config_home / "team-lead.config.toml"
    suffix = _runtime_hook_state_suffix()
    installed.write_bytes(installed.read_bytes() + suffix)
    original_bytes = installed.read_bytes()
    original_digest = instruction_module._exact_file_digest
    original_link = instruction_module.os.link
    original_remove = instruction_module._remove_displaced_copy
    displaced_removed = False
    failure_injected = False

    def cross_filesystem_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        source = Path(source_path)
        destination = Path(destination_path)
        if config.state_dir in source.parents or config.state_dir in destination.parents:
            raise OSError(errno.EXDEV, "injected cross-filesystem link")
        original_link(source, destination, follow_symlinks=follow_symlinks)

    def remove_then_arm_failure(
        displaced: Path,
        *,
        expected_digest: str,
        expected_identity: tuple[int, int],
    ) -> None:
        nonlocal displaced_removed
        original_remove(
            displaced,
            expected_digest=expected_digest,
            expected_identity=expected_identity,
        )
        displaced_removed = True

    def fail_backup_digest(path: Path) -> str:
        nonlocal failure_injected
        if displaced_removed and not failure_injected and config.state_dir in path.parents:
            failure_injected = True
            raise FilesystemError("injected backup digest failure")
        return original_digest(path)

    monkeypatch.setattr(instruction_module.os, "link", cross_filesystem_link)
    monkeypatch.setattr(instruction_module, "_remove_displaced_copy", remove_then_arm_failure)
    monkeypatch.setattr(instruction_module, "_exact_file_digest", fail_backup_digest)

    with pytest.raises(FilesystemError, match="injected backup digest failure"):
        instruction_module.apply_instruction_remove(
            installed,
            mode=LinkMode.COPY,
            expected_link_target=None,
            installed_digest=instruction_module.instruction_digest(output),
            state_dir=config.state_dir,
            target_name=target.name,
            relpath="team-lead.config.toml",
            windows_path_semantics=False,
            allow_runtime_hook_state=True,
        )

    assert installed.read_bytes() == original_bytes
    assert failure_injected is True


@pytest.mark.parametrize(
    "suffix",
    [
        b'\nmodel = "unsafe"\n',
        b"\n[hooks]\nenabled = true\n",
        b'\n[hooks.state]\n[hooks.state."entry"]\ntrusted_hash = "sha256:ABC"\n',
        b'\n[hooks.state]\n[hooks.state."entry"]\ntrusted_hash = "sha256:' + b"a" * 64 + b'"\nextra = true\n',
        b'\n[hooks.state]\n[hooks.state.""]\ntrusted_hash = "sha256:' + b"a" * 64 + b'"\n',
        b'\n [hooks.state]\n[hooks.state."entry"]\ntrusted_hash = "sha256:' + b"a" * 64 + b'"\n',
        b'\n[hooks.state] # comment\n[hooks.state."entry"]\ntrusted_hash = "sha256:' + b"a" * 64 + b'"\n',
    ],
)
def test_generated_profile_copy_conflicts_on_unsafe_runtime_suffix(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    """Only the closed provider Hook trust shape is excluded from drift."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed = config.targets[0].config_home / "team-lead.config.toml"
    installed.write_bytes(installed.read_bytes() + suffix)

    plan = build_plan(config, discover_catalog(config))

    profile_action = next(action for action in plan.actions if action.name == "team-lead.config.toml")
    assert profile_action.disposition is Disposition.CONFLICT
    assert profile_action.detail == "managed instruction copy was modified after installation"


def test_runtime_hook_state_exception_does_not_apply_to_other_instruction_files(
    tmp_path: Path,
) -> None:
    """A product suffix never broadens drift tolerance for canonical Markdown."""

    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, _output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    installed_markdown = config.targets[0].config_home / "model-instructions/team-lead.md"
    installed_markdown.write_bytes(installed_markdown.read_bytes() + _runtime_hook_state_suffix())

    plan = build_plan(config, discover_catalog(config))

    action = next(action for action in plan.actions if action.name == "model-instructions/team-lead.md")
    assert action.disposition is Disposition.CONFLICT


def test_apply_rechecks_profile_projection_and_stops_on_post_plan_drift(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, _source, output = _profile_bundle(catalog)
    generate_instruction_profiles(catalog)
    config = make_config(
        tmp_path,
        catalog,
        mode=LinkMode.COPY,
        components=frozenset({Component.INSTRUCTIONS}),
    )
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    output.write_bytes(output.read_bytes() + b"# post-plan drift\n")

    with pytest.raises(CatalogError, match="stale"):
        apply_plan(config, inventory, plan)
    assert not (config.targets[0].config_home / "team-lead.config.toml").exists()


@pytest.mark.parametrize("operation", ["generate", "check"])
def test_profile_operation_rejects_source_change_after_scan_without_writing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _bundle, source, output = _profile_bundle(catalog)
    if operation == "check":
        generate_instruction_profiles(catalog)
    original_output = output.read_bytes() if output.exists() else None
    original_scan = profile_module._scan_catalog

    def scan_then_change(root: Path) -> tuple[Path, tuple[profile_module.CodexProfileProjection, ...]]:
        result = original_scan(root)
        source.write_text("# changed after scan\n", encoding="utf-8")
        return result

    monkeypatch.setattr(profile_module, "_scan_catalog", scan_then_change)

    command = generate_instruction_profiles if operation == "generate" else check_instruction_profiles
    expected_operation = "generation" if operation == "generate" else "check"
    with pytest.raises(InstructionProfileError, match=f"changed during {expected_operation}"):
        command(catalog)
    assert (output.read_bytes() if output.exists() else None) == original_output


@pytest.mark.parametrize("operation", ["generate", "check"])
@pytest.mark.parametrize("reparse_location", ["bundle", "output-parent"])
def test_profile_operation_rejects_directory_reparse_points_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    reparse_location: str,
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    bundle, source, output = _profile_bundle(catalog, source_text="# PRIVATE SOURCE\n")
    if operation == "check":
        generate_instruction_profiles(catalog)
    source_before = source.read_bytes()
    output_before = output.read_bytes() if output.exists() else None
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_sentinel = foreign / "sentinel.txt"
    foreign_sentinel.write_text("FOREIGN PRIVATE CONTENT\n", encoding="utf-8")
    foreign_before = foreign_sentinel.read_bytes()
    reparse_path = bundle if reparse_location == "bundle" else output.parent

    monkeypatch.setattr(
        profile_module,
        "is_directory_reparse_point",
        lambda candidate: candidate == reparse_path,
    )

    command = generate_instruction_profiles if operation == "generate" else check_instruction_profiles
    with pytest.raises(InstructionProfileError, match="junction/reparse") as raised:
        command(catalog)
    assert "PRIVATE SOURCE" not in str(raised.value)
    assert "FOREIGN PRIVATE CONTENT" not in str(raised.value)
    assert source.read_bytes() == source_before
    assert (output.read_bytes() if output.exists() else None) == output_before
    assert foreign_sentinel.read_bytes() == foreign_before
