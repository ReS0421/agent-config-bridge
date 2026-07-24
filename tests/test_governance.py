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
        "[distribution]",
        'redistribution = "blocked"',
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


@pytest.mark.parametrize(
    "origin",
    ("local-original", "imported-git", "imported-marketplace", "orca-runtime"),
)
def test_provenance_origin_uses_closed_vocabulary(tmp_path: Path, origin: str) -> None:
    """Every ADR-2 provenance origin is accepted by the core validator."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _manifest("good", lifecycle="active", provenance=True).replace(
        'origin = "local-original"', f'origin = "{origin}"'
    )
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert not [finding for finding in report.findings if finding.code == "GOV028"]


@pytest.mark.parametrize(
    "provenance_block",
    ('origin = "unknown-source"', 'origin = ""', "origin = 42"),
)
def test_unknown_or_invalid_provenance_origin_is_rejected(tmp_path: Path, provenance_block: str) -> None:
    """An open-ended or mistyped origin must not enter the generated registry."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _manifest("good", lifecycle="active", provenance=True).replace(
        'origin = "local-original"', provenance_block
    )
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    findings = [finding for finding in report.findings if finding.code == "GOV028"]
    assert len(findings) == 1
    assert findings[0].capability_id == "good"
    assert findings[0].artifact_ref == "skills/good"


@pytest.mark.parametrize("redistribution", ("", "public", 42))
def test_redistribution_uses_closed_vocabulary(tmp_path: Path, redistribution: object) -> None:
    """GOV029 rejects open-ended and non-string distribution values."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _manifest("good")
    value = f'"{redistribution}"' if isinstance(redistribution, str) else str(redistribution)
    manifest = manifest.replace('redistribution = "blocked"', f"redistribution = {value}")
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    findings = [finding for finding in run_governance(discover_catalog(config)).findings if finding.code == "GOV029"]

    assert len(findings) == 1
    assert findings[0].capability_id == "good"


def test_distribution_must_be_a_table_when_declared(tmp_path: Path) -> None:
    """A scalar distribution declaration cannot bypass the vocabulary gate."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _manifest("good").replace('[distribution]\nredistribution = "blocked"\n', 'distribution = "allowed"\n')
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    findings = [finding for finding in run_governance(discover_catalog(config)).findings if finding.code == "GOV029"]

    assert len(findings) == 1


def _redistributable_manifest(*, overrides: dict[str, str] | None = None) -> str:
    """Return one complete allowed imported-Git manifest for focused validation."""

    values = {
        "expected_upstream_digest": "sha256:" + "a" * 64,
        "source_url": "https://example.invalid/upstream.git",
        "source_revision": "b" * 40,
        "source_subpath": "skills/good",
        "license_concluded": "MIT",
        "rights_basis": "upstream-license",
        "license_evidence": '["LICENSE"]',
        "attribution_files": '["NOTICE"]',
        "origin": "imported-git",
    }
    values.update(overrides or {})
    return (
        'id = "good"\n'
        'capability_kind = "skill"\n'
        'delivery = "standalone"\n'
        'lifecycle = "active"\n'
        'failure_policy = "advisory"\n'
        'owner = "test"\n'
        'last_reviewed = "2026-07-23"\n'
        "[distribution]\n"
        'redistribution = "allowed"\n'
        "[[artifacts]]\n"
        'ref = "skills/good"\n'
        f'expected_upstream_digest = "{values["expected_upstream_digest"]}"\n'
        "[artifacts.provenance]\n"
        f'origin = "{values["origin"]}"\n'
        f'source_url = "{values["source_url"]}"\n'
        f'source_revision = "{values["source_revision"]}"\n'
        f'source_subpath = "{values["source_subpath"]}"\n'
        f'license_concluded = "{values["license_concluded"]}"\n'
        f'rights_basis = "{values["rights_basis"]}"\n'
        f"license_evidence = {values['license_evidence']}\n"
        f"attribution_files = {values['attribution_files']}\n"
    )


def test_allowed_redistribution_accepts_complete_contained_evidence(tmp_path: Path) -> None:
    """Complete pinned provenance with real in-artifact evidence is accepted."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "skills" / "good" / "LICENSE", "MIT\n")
    _write(catalog / "skills" / "good" / "NOTICE", "Attribution\n")
    _write(catalog / "governance" / "good.toml", _redistributable_manifest())
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert not [finding for finding in report.findings if finding.code in {f"GOV{code}" for code in range(29, 38)}]


def test_allowed_redistribution_requires_provenance_table(tmp_path: Path) -> None:
    """GOV033 remains stable when an allowed artifact omits provenance."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    manifest = _redistributable_manifest().split("[artifacts.provenance]", 1)[0]
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert any(finding.code == "GOV033" and finding.artifact_ref == "skills/good" for finding in report.findings)


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"expected_upstream_digest": ""}, "GOV032"),
        ({"origin": "local-original"}, "GOV034"),
        ({"source_url": ""}, "GOV034"),
        ({"source_revision": "main"}, "GOV034"),
        ({"source_subpath": "../good"}, "GOV034"),
        ({"license_concluded": "NOASSERTION"}, "GOV035"),
        ({"rights_basis": "none"}, "GOV035"),
        ({"license_evidence": "[]"}, "GOV036"),
        ({"attribution_files": "[]"}, "GOV036"),
        ({"license_evidence": '["missing.txt"]'}, "GOV037"),
        ({"license_evidence": '["LICENSE", "missing.txt"]'}, "GOV037"),
        ({"attribution_files": '["../NOTICE"]'}, "GOV037"),
    ),
)
def test_allowed_redistribution_rejects_incomplete_metadata_or_evidence(
    tmp_path: Path,
    overrides: dict[str, str],
    code: str,
) -> None:
    """Each allowed-redistribution prerequisite has a stable diagnostic."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "skills" / "good" / "LICENSE", "MIT\n")
    _write(catalog / "skills" / "good" / "NOTICE", "Attribution\n")
    _write(catalog / "governance" / "good.toml", _redistributable_manifest(overrides=overrides))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert any(finding.code == code and finding.artifact_ref == "skills/good" for finding in report.findings)


def test_allowed_redistribution_rejects_symlinked_evidence(tmp_path: Path) -> None:
    """Evidence must be a real file, even when a symlink remains contained."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "skills" / "good" / "LICENSE.real", "MIT\n")
    _write(catalog / "skills" / "good" / "NOTICE", "Attribution\n")
    try:
        (catalog / "skills" / "good" / "LICENSE").symlink_to("LICENSE.real")
    except OSError as exc:  # pragma: no cover - host symlink policy
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")
    _write(catalog / "governance" / "good.toml", _redistributable_manifest())
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))

    assert any(finding.code == "GOV037" and "must not traverse" in finding.detail for finding in report.findings)


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


def _required_manifest(identifier: str, *, lifecycle: str = "active", targets: str | None = None) -> str:
    """A manifest with provenance and explicit codex/linux target blocks."""

    target_blocks = (
        targets
        if targets is not None
        else '[[targets]]\nproduct = "codex"\nplatform = "linux"\nsurfaces = ["cli", "desktop"]\n'
    )
    return (
        f'id = "{identifier}"\n'
        'capability_kind = "skill"\n'
        'delivery = "standalone"\n'
        f'lifecycle = "{lifecycle}"\n'
        'failure_policy = "advisory"\n'
        'owner = "test"\n'
        'last_reviewed = "2026-07-16"\n'
        "[distribution]\n"
        'redistribution = "blocked"\n'
        f"{target_blocks}"
        "[[artifacts]]\n"
        f'ref = "skills/{identifier}"\n'
        "[artifacts.provenance]\n"
        'origin = "local-original"\n'
    )


def _required_catalog(tmp_path: Path, *skills: str) -> Path:
    catalog = make_catalog(tmp_path / "catalog", skills=skills)
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    return catalog


def test_required_mode_escalates_coverage_to_error(tmp_path: Path) -> None:
    """GOV030 is an error under required, so resolution refuses to run."""

    from agent_config_bridge.governance import resolve_inventory

    catalog = _required_catalog(tmp_path, "good")
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    gov030 = [finding for finding in report.findings if finding.code == "GOV030"]
    assert gov030 and all(finding.severity is GovernanceSeverity.ERROR for finding in gov030)

    with pytest.raises(GovernanceError, match="GOV030"):
        resolve_inventory(discover_catalog(config))


def test_resolved_inventory_gates_by_lifecycle_and_target(tmp_path: Path) -> None:
    """Only deployable manifests matching the target's product/platform deploy."""

    from agent_config_bridge.governance import resolve_inventory

    catalog = _required_catalog(tmp_path, "active-one", "quarantined-one", "other-product")
    _write(catalog / "governance" / "active-one.toml", _required_manifest("active-one"))
    _write(
        catalog / "governance" / "quarantined-one.toml", _required_manifest("quarantined-one", lifecycle="quarantined")
    )
    _write(
        catalog / "governance" / "other-product.toml",
        _required_manifest(
            "other-product",
            targets='[[targets]]\nproduct = "claude-code"\nplatform = "windows"\nsurfaces = ["cli"]\n',
        ),
    )
    config = make_config(tmp_path, catalog)

    resolved = resolve_inventory(discover_catalog(config))
    names = [skill.name for skill in resolved.skills_for_target(config.targets[0])]

    assert names == ["active-one"]


def test_audit_mode_resolution_never_gates(tmp_path: Path) -> None:
    """Audit mode resolves the full inventory even without manifests."""

    from agent_config_bridge.governance import resolve_inventory

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    config = make_config(tmp_path, catalog)

    resolved = resolve_inventory(discover_catalog(config))

    assert [skill.name for skill in resolved.skills_for_target(config.targets[0])] == ["good"]


def test_required_plan_deploys_gated_set_and_retracts_quarantined(tmp_path: Path) -> None:
    """The planner consumes ResolvedInventory: quarantine retracts a deployed skill."""

    from agent_config_bridge.applier import apply_plan
    from agent_config_bridge.planner import Disposition, build_plan
    from tests.conftest import require_directory_symlink_support

    require_directory_symlink_support(tmp_path)
    catalog = _required_catalog(tmp_path, "keeper", "victim")
    _write(catalog / "governance" / "keeper.toml", _required_manifest("keeper"))
    _write(catalog / "governance" / "victim.toml", _required_manifest("victim"))
    config = make_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    apply_plan(config, inventory, build_plan(config, inventory))
    root = tmp_path / "home/.agents/skills"
    assert (root / "keeper").is_symlink() and (root / "victim").is_symlink()

    _write(catalog / "governance" / "victim.toml", _required_manifest("victim", lifecycle="quarantined"))
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    removals = [action for action in plan.actions if action.disposition is Disposition.REMOVE]
    assert [action.name for action in removals] == ["victim"]

    apply_plan(config, inventory, plan)
    assert (root / "keeper").is_symlink()
    assert not (root / "victim").exists()

    from agent_config_bridge.state import read_skill_state

    assert [entry.name for entry in read_skill_state(config, config.targets[0])] == ["keeper"]
    followup = build_plan(config, discover_catalog(config))
    assert all(action.disposition.value == "noop" for action in followup.actions)


def test_deployable_manifest_without_targets_is_loud(tmp_path: Path) -> None:
    """GOV026: silent everywhere-retraction is surfaced as a finding."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "good.toml", _manifest("good", lifecycle="active", provenance=True))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    gov026 = [finding for finding in report.findings if finding.code == "GOV026"]
    assert len(gov026) == 1
    assert gov026[0].severity is GovernanceSeverity.WARNING

    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    report = run_governance(discover_catalog(config))
    gov026 = [finding for finding in report.findings if finding.code == "GOV026"]
    assert gov026[0].severity is GovernanceSeverity.ERROR


def test_public_export_mode_still_refuses(tmp_path: Path) -> None:
    """public-export remains unimplemented and must not run."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "public-export"\n')
    config = make_config(tmp_path, catalog)

    with pytest.raises(GovernanceError):
        run_governance(discover_catalog(config))


def test_typoed_target_block_is_loud_not_silently_unmatched(tmp_path: Path) -> None:
    """GOV027: a product typo or missing surfaces cannot silently retract."""

    from agent_config_bridge.governance import resolve_inventory

    catalog = _required_catalog(tmp_path, "good")
    _write(
        catalog / "governance" / "good.toml",
        _required_manifest(
            "good",
            targets='[[targets]]\nproduct = "claude"\nplatform = "linux"\nsurfaces = ["cli"]\n',
        ),
    )
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    gov027 = [finding for finding in report.findings if finding.code == "GOV027"]
    assert len(gov027) == 1
    assert gov027[0].severity is GovernanceSeverity.ERROR
    assert "product='claude'" in gov027[0].detail
    with pytest.raises(GovernanceError, match="GOV027"):
        resolve_inventory(discover_catalog(config))

    _write(
        catalog / "governance" / "good.toml",
        _required_manifest("good", targets='[[targets]]\nproduct = "codex"\nplatform = "linux"\n'),
    )
    report = run_governance(discover_catalog(config))
    gov027 = [finding for finding in report.findings if finding.code == "GOV027"]
    assert gov027 and "surfaces=None" in gov027[0].detail


def test_target_block_typo_is_only_a_warning_in_audit(tmp_path: Path) -> None:
    """The same GOV027 finding stays advisory while migrating in audit mode."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(
        catalog / "governance" / "good.toml",
        _required_manifest(
            "good",
            targets='[[targets]]\nproduct = "claude"\nplatform = "linux"\nsurfaces = ["cli"]\n',
        ),
    )
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    gov027 = [finding for finding in report.findings if finding.code == "GOV027"]

    assert gov027 and gov027[0].severity is GovernanceSeverity.WARNING


def test_resolve_refuses_on_gov026_alone(tmp_path: Path) -> None:
    """A deployable manifest without targets blocks required-mode resolution."""

    from agent_config_bridge.governance import resolve_inventory

    catalog = _required_catalog(tmp_path, "good")
    manifest = _manifest("good", lifecycle="active", provenance=True)
    _write(catalog / "governance" / "good.toml", manifest)
    config = make_config(tmp_path, catalog)

    with pytest.raises(GovernanceError, match="GOV026"):
        resolve_inventory(discover_catalog(config))


def test_multiple_manifests_governing_one_artifact_warn(tmp_path: Path) -> None:
    """GOV031 flags accidental duplicate governance of one artifact ref."""

    catalog = make_catalog(tmp_path / "catalog", skills=("good",))
    _write(catalog / "governance" / "one.toml", _manifest("one", ref="skills/good"))
    _write(catalog / "governance" / "two.toml", _manifest("two", ref="skills/good"))
    config = make_config(tmp_path, catalog)

    report = run_governance(discover_catalog(config))
    gov031 = [finding for finding in report.findings if finding.code == "GOV031"]

    assert len(gov031) == 1
    assert gov031[0].artifact_ref == "skills/good"
    assert "['one', 'two']" in gov031[0].detail


def _hook_manifest(identifier: str, *, lifecycle: str = "active", products: tuple[str, ...] = ("claude-code",)) -> str:
    target_blocks = "".join(
        f'[[targets]]\nproduct = "{product}"\nplatform = "linux"\nsurfaces = ["cli", "desktop"]\n'
        for product in products
    )
    return (
        f'id = "{identifier}"\n'
        'capability_kind = "event-handler"\n'
        'delivery = "plugin"\n'
        f'lifecycle = "{lifecycle}"\n'
        'failure_policy = "advisory"\n'
        'owner = "test"\n'
        'last_reviewed = "2026-07-18"\n'
        "[distribution]\n"
        'redistribution = "blocked"\n'
        f"{target_blocks}"
        "[[artifacts]]\n"
        f'ref = "hooks/{identifier}"\n'
        "[artifacts.provenance]\n"
        'origin = "local-original"\n'
    )


def _two_product_config(tmp_path: Path, catalog: Path):
    from agent_config_bridge.models import BridgeConfig, Component, Platform, Product, Surface, TargetConfig

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    components = frozenset({Component.HOOKS})

    def target(name: str, product: Product) -> TargetConfig:
        return TargetConfig(
            name=name,
            product=product,
            platform=Platform.LINUX,
            user_home=home,
            config_home=home / (".codex" if product is Product.CODEX else ".claude"),
            components=components,
            surfaces=frozenset({Surface.CLI}),
            enabled=True,
        )

    from agent_config_bridge.models import LinkMode

    return BridgeConfig(
        schema_version=1,
        catalog=catalog,
        state_dir=tmp_path / "state",
        link_mode=LinkMode.SYMLINK,
        components=components,
        targets=(target("claude", Product.CLAUDE_CODE), target("codex", Product.CODEX)),
    )


def test_required_mode_gates_hooks_per_product(tmp_path: Path) -> None:
    """A claude-only hook manifest keeps the hook out of the codex plugin."""

    from agent_config_bridge.renderer import render_marketplace

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("guard",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write(catalog / "governance" / "guard.toml", _hook_manifest("guard"))
    config = _two_product_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    from agent_config_bridge.governance import resolve_inventory

    resolved = resolve_inventory(inventory)
    claude_target, codex_target = config.targets
    assert [hook.name for hook in resolved.hooks_for_target(claude_target)] == ["guard"]
    assert resolved.hooks_for_target(codex_target) == ()

    rendered = render_marketplace(config, inventory, resolved=resolved)
    assert rendered.claude_plugins == ("agent-config-bridge-hooks",)
    assert rendered.codex_plugins == ()
    assert (rendered.root / "plugins" / "claude-code" / "agent-config-bridge-hooks").is_dir()
    assert not (rendered.root / "plugins" / "codex" / "agent-config-bridge-hooks").exists()


def test_governance_changes_invalidate_the_marketplace_digest(tmp_path: Path) -> None:
    """Quarantining a hook must change the digest, or renders go stale."""

    from agent_config_bridge.renderer import marketplace_digest

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("guard",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write(catalog / "governance" / "guard.toml", _hook_manifest("guard"))
    config = _two_product_config(tmp_path, catalog)

    active_digest = marketplace_digest(config, discover_catalog(config))
    _write(catalog / "governance" / "guard.toml", _hook_manifest("guard", lifecycle="quarantined"))
    quarantined_digest = marketplace_digest(config, discover_catalog(config))

    assert active_digest != quarantined_digest


def test_audit_mode_hooks_render_ungated(tmp_path: Path) -> None:
    """Without a required policy, every hook renders for every product."""

    from agent_config_bridge.governance import resolve_inventory
    from agent_config_bridge.renderer import render_marketplace

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("guard",))
    config = _two_product_config(tmp_path, catalog)
    inventory = discover_catalog(config)

    rendered = render_marketplace(config, inventory, resolved=resolve_inventory(inventory))

    assert rendered.claude_plugins == ("agent-config-bridge-hooks",)
    assert rendered.codex_plugins == ("agent-config-bridge-hooks",)


def test_desired_plugin_names_drop_hook_plugin_when_fully_gated(tmp_path: Path) -> None:
    """A target whose gated hook set is empty stops desiring the hook plugin."""

    from agent_config_bridge.governance import resolve_inventory
    from agent_config_bridge.state import desired_plugin_names

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("guard",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write(catalog / "governance" / "guard.toml", _hook_manifest("guard"))
    config = _two_product_config(tmp_path, catalog)
    inventory = discover_catalog(config)
    resolved = resolve_inventory(inventory)
    claude_target, codex_target = config.targets

    assert desired_plugin_names(claude_target, inventory, resolved.hooks_for_target(claude_target)) == (
        "agent-config-bridge-hooks",
    )
    assert desired_plugin_names(codex_target, inventory, resolved.hooks_for_target(codex_target)) == ()


def test_hook_version_bump_does_not_rebuild_product_without_gated_hooks(tmp_path: Path) -> None:
    """Bumping hooks/.version must not change the digest of a hook-free product."""

    from agent_config_bridge.renderer import marketplace_digest

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("guard",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write(catalog / "governance" / "guard.toml", _hook_manifest("guard"))  # claude-code only
    # Codex-only config: no gated hook renders for it, so a version bump is inert.
    from agent_config_bridge.models import (
        BridgeConfig,
        Component,
        LinkMode,
        Platform,
        Product,
        Surface,
        TargetConfig,
    )

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    codex_target = TargetConfig(
        name="codex",
        product=Product.CODEX,
        platform=Platform.LINUX,
        user_home=home,
        config_home=home / ".codex",
        components=frozenset({Component.HOOKS}),
        surfaces=frozenset({Surface.CLI}),
        enabled=True,
    )
    config = BridgeConfig(
        schema_version=1,
        catalog=catalog,
        state_dir=tmp_path / "state",
        link_mode=LinkMode.SYMLINK,
        components=frozenset({Component.HOOKS}),
        targets=(codex_target,),
    )

    before = marketplace_digest(config, discover_catalog(config))
    (catalog / "hooks" / ".version").write_text("0.2.0\n", encoding="utf-8")
    after = marketplace_digest(config, discover_catalog(config))

    assert before == after


def test_hooks_for_product_unions_same_product_targets(tmp_path: Path) -> None:
    """A hook gated in for one target of a product delivers to all of them."""

    from agent_config_bridge.governance import resolve_inventory
    from agent_config_bridge.models import (
        BridgeConfig,
        Component,
        LinkMode,
        Platform,
        Product,
        Surface,
        TargetConfig,
    )

    catalog = make_catalog(tmp_path / "catalog", skills=(), hooks=("desktop-only",))
    _write(catalog / "governance" / "policy.toml", 'schema_version = 1\nmode = "required"\n')
    _write(
        catalog / "governance" / "desktop-only.toml",
        _hook_manifest("desktop-only").replace('surfaces = ["cli", "desktop"]', 'surfaces = ["desktop"]'),
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    def claude(name: str, surface: Surface) -> TargetConfig:
        return TargetConfig(
            name=name,
            product=Product.CLAUDE_CODE,
            platform=Platform.LINUX,
            user_home=home,
            config_home=home / f".claude-{name}",
            components=frozenset({Component.HOOKS}),
            surfaces=frozenset({surface}),
            enabled=True,
        )

    cli_target = claude("cli", Surface.CLI)
    desktop_target = claude("desktop", Surface.DESKTOP)
    config = BridgeConfig(
        schema_version=1,
        catalog=catalog,
        state_dir=tmp_path / "state",
        link_mode=LinkMode.SYMLINK,
        components=frozenset({Component.HOOKS}),
        targets=(cli_target, desktop_target),
    )
    resolved = resolve_inventory(discover_catalog(config))

    # The cli-only target does not match the desktop-scoped manifest on its own,
    assert resolved.hooks_for_target(cli_target) == ()
    # but the product union (what actually installs) includes the hook.
    assert [hook.name for hook in resolved.hooks_for_product(config, Product.CLAUDE_CODE)] == ["desktop-only"]
