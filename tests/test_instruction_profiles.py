"""Codex developer-instruction profile projection and lifecycle tests."""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

import pytest

from agent_config_bridge import instruction_profiles as profile_module
from agent_config_bridge.applier import apply_plan
from agent_config_bridge.catalog import CatalogError, discover_catalog
from agent_config_bridge.instruction_profiles import (
    InstructionProfileError,
    check_instruction_profiles,
    generate_instruction_profiles,
)
from agent_config_bridge.models import Component, LinkMode
from agent_config_bridge.planner import Disposition, build_plan
from agent_config_bridge.state import read_instruction_state
from tests.conftest import make_catalog, make_config


def _write(path: Path, text: str, *, newline: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline=newline)


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
