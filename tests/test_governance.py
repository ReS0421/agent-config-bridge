"""Governance core tests, including parity with the catalog tools/ prototype.

The fixture cases and expected GovernanceFinding codes mirror the catalog
repo's ``docs/agent-harness/tools/test_governance_core.py`` (ADR-2 §3 parity
condition); do not weaken them without updating that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_config_bridge import cli
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.governance import (
    GovernanceError,
    GovernanceMode,
    GovernanceSeverity,
    build_registry_payload,
    read_governance_mode,
    registry_path,
    run_governance,
    serialize_registry,
)
from tests.conftest import make_catalog, make_config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest(
    identifier: str,
    *,
    lifecycle: str = "proposed",
    kind: str = "skill",
    delivery: str = "standalone",
    ref: str | None = "skills/good",
    provenance: bool = False,
) -> str:
    lines = [
        f'id = "{identifier}"',
        f'capability_kind = "{kind}"',
        f'delivery = "{delivery}"',
        f'lifecycle = "{lifecycle}"',
        'failure_policy = "advisory"',
        'owner = "test"',
        'last_reviewed = "2026-07-16"',
    ]
    if ref is not None:
        lines += ["[[artifacts]]", f'ref = "{ref}"']
        if provenance:
            lines += ["[artifacts.provenance]", 'origin = "local-original"']
    return "\n".join(lines) + "\n"


def _parity_catalog(tmp_path: Path) -> tuple[Path, Path]:
    """Recreate the prototype's fixture catalog with the Bridge builders."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good", "weird"))
    _write(
        catalog / "skills" / "weird" / "SKILL.md",
        "---\nname: weird\ndescription: d\nform: skill\n---\n# weird\n",
    )
    governance = catalog / "governance"
    _write(governance / "malformed.toml", "id = \nnot toml")
    _write(governance / "dup1.toml", _manifest("dup"))
    _write(governance / "dup2.toml", _manifest("dup"))
    _write(governance / "illegal.toml", _manifest("illegal", delivery="plugin"))
    _write(governance / "hostjob.toml", _manifest("hj", kind="host-job", lifecycle="active", provenance=True))
    _write(governance / "dangling.toml", _manifest("dang", ref="skills/missing"))
    _write(governance / "noprov.toml", _manifest("np", lifecycle="active"))
    return catalog, governance


def test_parity_fixture_codes_match_the_prototype(tmp_path: Path) -> None:
    """The in-Bridge core reproduces the prototype's diagnostic codes."""

    catalog, _ = _parity_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    report = run_governance(inventory)
    codes = {finding.code for finding in report.findings}

    assert {"GOV001", "GOV003", "GOV021", "GOV022", "GOV012", "GOV023", "GOV030", "GOV043"} <= codes
    assert report.has_error
    coverage = [finding for finding in report.findings if finding.code == "GOV030"]
    assert all(finding.severity is GovernanceSeverity.WARNING for finding in coverage)


def test_registry_serialization_is_byte_deterministic(tmp_path: Path) -> None:
    """Two builds over the same catalog produce identical bytes."""

    catalog, _ = _parity_catalog(tmp_path)
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    report = run_governance(inventory)

    first = serialize_registry(build_registry_payload(report.manifests, inventory))
    second = serialize_registry(build_registry_payload(report.manifests, inventory))

    assert first == second
    assert first.endswith(b"\n")


def test_tombstone_may_have_no_artifacts_but_others_may_not(tmp_path: Path) -> None:
    """GOV010 fires for artifact-less manifests unless lifecycle=removed."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "empty.toml", _manifest("empty", ref=None))
    _write(catalog / "governance" / "tomb.toml", _manifest("tomb", lifecycle="removed", ref=None))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    gov010 = [finding for finding in report.findings if finding.code == "GOV010"]
    assert [finding.capability_id for finding in gov010] == ["empty"]


def test_policy_file_sets_mode_and_is_never_a_manifest(tmp_path: Path) -> None:
    """policy.toml is reserved: it configures the mode and is skipped by load."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    governance = catalog / "governance"
    _write(governance / "policy.toml", 'schema_version = 1\nmode = "audit"\n')
    _write(governance / "good.toml", _manifest("good", lifecycle="active", provenance=True))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert report.mode is GovernanceMode.AUDIT
    assert [manifest.id for manifest in report.manifests] == ["good"]
    assert not report.has_error


def test_unenforceable_committed_mode_refuses_to_pass(tmp_path: Path) -> None:
    """A committed required/public-export mode must not silently run as audit."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    config = make_config(tmp_path, catalog)

    with pytest.raises(GovernanceError):
        run_governance(discover_catalog(config))


def test_invalid_policy_is_an_error_not_a_default(tmp_path: Path) -> None:
    """Malformed or unknown policy content raises instead of degrading."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    policy = catalog / "governance" / "policy.toml"
    _write(policy, "mode = not toml")
    with pytest.raises(GovernanceError):
        read_governance_mode(catalog / "governance")

    _write(policy, 'schema_version = 1\nmode = "strict"\n')
    with pytest.raises(GovernanceError):
        read_governance_mode(catalog / "governance")

    _write(policy, 'schema_version = 2\nmode = "audit"\n')
    with pytest.raises(GovernanceError):
        read_governance_mode(catalog / "governance")


def test_missing_governance_directory_yields_empty_audit_report(tmp_path: Path) -> None:
    """A catalog without governance/ runs audit with coverage warnings only."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert report.mode is GovernanceMode.AUDIT
    assert not report.manifests
    assert not report.has_error
    assert {finding.code for finding in report.findings} == {"GOV030"}


def _write_config(tmp_path: Path, catalog: Path) -> Path:
    config_path = tmp_path / "agentbridge.toml"
    _write(
        config_path,
        f"""schema_version = 1

[bridge]
catalog = '{catalog}'
state_dir = '{tmp_path / "state"}'
link_mode = "symlink"
components = ["skills"]

[[targets]]
name = "target"
product = "codex"
platform = "linux"
user_home = '{tmp_path / "home"}'
components = ["skills"]
surfaces = ["cli"]
enabled = true
""",
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return config_path


def test_cli_generate_then_check_round_trip_and_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """generate writes the snapshot, check passes, catalog change drifts."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "good.toml", _manifest("good", lifecycle="active", provenance=True))
    config_path = _write_config(tmp_path, catalog)

    assert cli.main(["registry", "generate", "--config", str(config_path)]) == 0
    snapshot = catalog / "registry.json"
    assert snapshot.is_file()
    assert b'"schema_version": 1' in snapshot.read_bytes()

    assert cli.main(["registry", "check", "--config", str(config_path)]) == 0
    assert "committed snapshot matches" in capsys.readouterr().out

    before_drift = snapshot.read_bytes()
    _write(catalog / "skills" / "good" / "extra.md", "changed\n")
    assert cli.main(["registry", "check", "--config", str(config_path)]) == 1
    output = capsys.readouterr().out
    assert "GOV050" in output
    assert "DRIFT" in output
    assert snapshot.read_bytes() == before_drift


def test_cli_check_never_writes_and_generate_refuses_on_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """check leaves no file behind; generate does not persist a bad registry."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "dangling.toml", _manifest("dang", ref="skills/missing"))
    config_path = _write_config(tmp_path, catalog)
    snapshot = catalog / "registry.json"

    assert cli.main(["registry", "check", "--config", str(config_path)]) == 1
    assert not snapshot.exists()

    assert cli.main(["registry", "generate", "--config", str(config_path)]) == 1
    assert not snapshot.exists()
    assert "registry not written" in capsys.readouterr().out


def test_registry_path_is_inside_the_catalog(tmp_path: Path) -> None:
    """The committed snapshot lives at catalog/registry.json."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    assert registry_path(inventory) == inventory.root / "registry.json"


def test_every_load_and_axis_code_has_direct_coverage(tmp_path: Path) -> None:
    """GOV002 (missing id), GOV011 (unknown component), GOV020 (illegal axis)."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    governance = catalog / "governance"
    _write(governance / "anonymous.toml", 'title = "no id here"\n')
    _write(governance / "unknown-ref.toml", _manifest("uref", ref="widgets/thing"))
    _write(governance / "bad-axis.toml", _manifest("axis", kind="banana"))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    by_code = {finding.code: finding for finding in report.findings}

    assert by_code["GOV002"].capability_id == "anonymous"
    assert by_code["GOV011"].artifact_ref == "widgets/thing"
    assert "capability_kind='banana'" in by_code["GOV020"].detail


def test_skill_frontmatter_codes_for_broken_skill_dirs(tmp_path: Path) -> None:
    """GOV040/041/042 fire for skill roots the catalog validator never admits."""

    from agent_config_bridge.catalog import Artifact, CatalogInventory
    from agent_config_bridge.governance import validate_governance

    no_skill_md = tmp_path / "no-skill-md"
    no_skill_md.mkdir()
    no_frontmatter = tmp_path / "no-frontmatter"
    no_frontmatter.mkdir()
    (no_frontmatter / "SKILL.md").write_text("# just a title\n", encoding="utf-8")
    bad_yaml = tmp_path / "bad-yaml"
    bad_yaml.mkdir()
    (bad_yaml / "SKILL.md").write_text("---\nname: bad-yaml\ndescription: d\nbad: [unclosed\n---\n", encoding="utf-8")
    inventory = CatalogInventory(
        root=tmp_path,
        skills=(
            Artifact(name="no-skill-md", path=no_skill_md),
            Artifact(name="no-frontmatter", path=no_frontmatter),
            Artifact(name="bad-yaml", path=bad_yaml),
        ),
        plugins=(),
        hooks=(),
        settings=(),
        schedules=(),
        hook_version=None,
    )

    findings = validate_governance((), inventory, GovernanceMode.AUDIT)
    codes_by_ref = {finding.artifact_ref: finding.code for finding in findings if finding.code.startswith("GOV04")}

    assert codes_by_ref["skills/no-skill-md"] == "GOV040"
    assert codes_by_ref["skills/no-frontmatter"] == "GOV041"
    assert codes_by_ref["skills/bad-yaml"] == "GOV042"


def test_yaml_parse_error_skill_passes_discovery_but_warns_in_governance(tmp_path: Path) -> None:
    """A skill the line-based catalog validator admits still gets GOV042."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(
        catalog / "skills" / "tricky" / "SKILL.md",
        "---\nname: tricky\ndescription: d\nbad: [unclosed\n---\n# tricky\n",
    )
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert any(finding.code == "GOV042" and finding.artifact_ref == "skills/tricky" for finding in report.findings)


def test_agent_schedule_refs_must_live_under_schedules(tmp_path: Path) -> None:
    """GOV024: an agent-schedule pointing at a skill artifact is rejected."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(
        catalog / "governance" / "sched.toml",
        _manifest("sched", kind="agent-schedule", ref="skills/good"),
    )
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert any(finding.code == "GOV024" for finding in report.findings)


def test_active_capability_requires_owner_and_last_reviewed(tmp_path: Path) -> None:
    """GOV025: lifecycle=active without owner/last_reviewed is an error."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _manifest("bare", lifecycle="active", provenance=True)
    manifest = manifest.replace('owner = "test"\n', "").replace('last_reviewed = "2026-07-16"\n', "")
    _write(catalog / "governance" / "bare.toml", manifest)
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    gov025 = [finding for finding in report.findings if finding.code == "GOV025"]
    assert {finding.detail for finding in gov025} == {
        "active capability missing required field 'owner'",
        "active capability missing required field 'last_reviewed'",
    }


def test_symlinked_governance_dir_and_manifest_are_refused(tmp_path: Path) -> None:
    """The governance trust boundary rejects symlinked dirs and manifests."""

    from agent_config_bridge.governance import load_governance

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "sneaky.toml", _manifest("sneaky"))
    governance = catalog / "governance"
    try:
        governance.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - host symlink policy
        pytest.skip(f"directory symlinks are unavailable in this test environment: {exc}")
    with pytest.raises(GovernanceError):
        load_governance(governance)
    governance.unlink()

    governance.mkdir()
    (governance / "sneaky.toml").symlink_to(outside / "sneaky.toml")
    with pytest.raises(GovernanceError):
        load_governance(governance)
