"""CLI tests for Skill migration planning and application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_config_bridge import cli
from tests.conftest import symlink_directory_or_skip


def _skill(root: Path, name: str, body: str = "Run this workflow.\n") -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test {name}.\n---\n\n{body}",
        encoding="utf-8",
    )
    return path


def test_migrate_skills_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(tmp_path / "canonical/reports/migration.md"),
            "--json",
        ]
    )

    assert result == 1
    assert not catalog.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["create"] == 1


def test_migrate_skills_requires_yes_even_in_an_interactive_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration never turns an omitted confirmation flag into an interactive write."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    monkeypatch.setattr(
        cli,
        "_confirm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("migration prompted interactively")),
    )

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(tmp_path / "canonical/reports/migration.md"),
        ]
    )

    assert result == 1
    assert not catalog.exists()


def test_migrate_skills_apply_json_stdout_is_one_document(tmp_path: Path, capsys) -> None:
    """Machine-readable apply output never appends human status lines."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    report = tmp_path / "canonical/reports/migration.md"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(report),
            "--json",
            "--yes",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["create"] == 1
    assert (catalog / "skills/hello/SKILL.md").is_file()
    assert report.is_file()
    assert report.with_suffix(".json").is_file()


def test_migrate_skills_no_pending_json_stdout_is_one_document(tmp_path: Path, capsys) -> None:
    """A confirmed no-op may write reports without corrupting JSON stdout."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    _skill(catalog / "skills", "hello")
    report = tmp_path / "canonical/reports/migration.md"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(report),
            "--json",
            "--yes",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["create"] == 0
    assert payload["summary"]["unchanged"] == 1
    assert report.is_file()
    assert report.with_suffix(".json").is_file()


def test_migrate_skills_apply_writes_catalog_and_reports(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    report = tmp_path / "canonical/reports/migration.md"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(report),
            "--yes",
        ]
    )

    assert result == 0
    assert (catalog / "skills/hello/SKILL.md").is_file()
    assert report.is_file()
    assert report.with_suffix(".json").is_file()


def test_migrate_skills_exits_nonzero_for_rejected_duplicate_after_safe_apply(tmp_path: Path, capsys) -> None:
    """Applying a safe selection does not clear a rejected same-name observation."""

    first = tmp_path / "first"
    second = tmp_path / "second"
    _skill(first, "shared", "Known-good workflow.\n")
    rejected = _skill(second, "shared")
    (rejected / "SKILL.md").write_text("# Missing frontmatter\n", encoding="utf-8")
    catalog = tmp_path / "canonical/catalog"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"first={first}",
            "--source",
            f"second={second}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(tmp_path / "canonical/reports/migration.md"),
            "--json",
            "--yes",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["execution"]["applied"] is True
    assert payload["summary"]["blocked"] == 1
    assert payload["summary"]["blocked_observations"] == 1
    assert (catalog / "skills/shared/SKILL.md").is_file()


def test_migrate_skills_repairs_only_migrated_legacy_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    legacy = source / "legacy"
    legacy.mkdir(parents=True)
    original = "# Legacy\n\nA legacy workflow.\n"
    (legacy / "SKILL.md").write_text(original, encoding="utf-8")
    catalog = tmp_path / "canonical/catalog"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"legacy={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(tmp_path / "canonical/reports/migration.md"),
            "--repair-legacy-frontmatter",
            "--yes",
        ]
    )

    assert result == 0
    assert (legacy / "SKILL.md").read_text(encoding="utf-8") == original
    assert (catalog / "skills/legacy/SKILL.md").read_text(encoding="utf-8").startswith("---\nname: legacy")


@pytest.mark.parametrize("report_root", ["source", "catalog", "conflicts"])
def test_migrate_skills_rejects_report_inside_managed_roots(
    tmp_path: Path,
    capsys,
    report_root: str,
) -> None:
    """Reports cannot mutate an input, canonical output, or conflict store."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    conflicts = tmp_path / "canonical/conflicts"
    roots = {"source": source, "catalog": catalog, "conflicts": conflicts}
    report = roots[report_root] / "reports/migration.md"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(conflicts),
            "--report",
            str(report),
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "report" in captured.err.casefold()
    assert not report.exists()
    assert not (catalog / "skills/hello").exists()


def test_migrate_skills_rejects_json_report_same_path(tmp_path: Path, capsys) -> None:
    """A ``.json`` report name cannot alias the Markdown and JSON outputs."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    report = tmp_path / "canonical/reports/migration.json"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(report),
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "report" in captured.err.casefold()
    assert not report.exists()
    assert not (catalog / "skills/hello").exists()


def test_migrate_skills_rejects_redirected_report_parent(tmp_path: Path, capsys) -> None:
    """A symlinked parent cannot redirect either atomic report write."""

    source = tmp_path / "source"
    _skill(source, "hello")
    catalog = tmp_path / "canonical/catalog"
    physical_reports = tmp_path / "physical-reports"
    physical_reports.mkdir()
    redirected_reports = tmp_path / "redirected-reports"
    symlink_directory_or_skip(redirected_reports, physical_reports)
    report = redirected_reports / "migration.md"

    result = cli.main(
        [
            "migrate-skills",
            "--source",
            f"source={source}",
            "--catalog",
            str(catalog),
            "--conflicts",
            str(tmp_path / "canonical/conflicts"),
            "--report",
            str(report),
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "report" in captured.err.casefold()
    assert not (physical_reports / "migration.md").exists()
    assert not (physical_reports / "migration.json").exists()
    assert not (catalog / "skills/hello").exists()
