from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_contract.py"


def run(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def write_metadata(repo: Path, version: str, *, changelog: bool = True) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "agent-config-bridge"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "agent-config-bridge"\n'
        f'version = "{version}"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )
    section = f"## [{version}]\n\n- release notes\n" if changelog else "## [Unreleased]\n"
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n{section}", encoding="utf-8")


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    run(tmp_path, "git", "init", "-q")
    run(tmp_path, "git", "config", "user.name", "Release Test")
    run(tmp_path, "git", "config", "user.email", "release-test@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bridge.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Bridge\n", encoding="utf-8")
    write_metadata(tmp_path, "0.3.0")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "-qm", "release 0.3.0")
    run(tmp_path, "git", "tag", "v0.3.0")
    return tmp_path


def check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def commit_source_fix(repo: Path) -> None:
    (repo / "src" / "bridge.py").write_text("VALUE = 2\n", encoding="utf-8")
    run(repo, "git", "add", "src/bridge.py")
    run(repo, "git", "commit", "-qm", "fix bridge")


def commit_release(repo: Path, version: str, *, changelog: bool = True) -> None:
    write_metadata(repo, version, changelog=changelog)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-qm", f"release {version}")
    run(repo, "git", "tag", f"v{version}")


def test_rejects_post_tag_source_change_without_version_bump(release_repo: Path) -> None:
    (release_repo / "src" / "bridge.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = check(release_repo)

    assert result.returncode == 1
    assert "release-impacting changes after v0.3.0 must not retain version 0.3.0" in result.stderr


def test_rejects_tracked_distribution_change_without_version_bump(release_repo: Path) -> None:
    (release_repo / "README.md").write_text("# Changed Bridge\n", encoding="utf-8")

    result = check(release_repo)

    assert result.returncode == 1
    assert "release-impacting changes after v0.3.0 must not retain version 0.3.0" in result.stderr


def test_rejects_untracked_distribution_file_without_version_bump(release_repo: Path) -> None:
    (release_repo / "release-note.txt").write_text("not ignored\n", encoding="utf-8")

    result = check(release_repo)

    assert result.returncode == 1
    assert "release-impacting changes after v0.3.0 must not retain version 0.3.0" in result.stderr


def test_accepts_next_patch_with_matching_lock_and_changelog(release_repo: Path) -> None:
    commit_source_fix(release_repo)
    write_metadata(release_repo, "0.3.1")

    result = check(release_repo)

    assert result.returncode == 0
    assert result.stdout == "release-contract: ok\n"


def test_accepts_populated_unreleased_section(release_repo: Path) -> None:
    commit_source_fix(release_repo)
    write_metadata(release_repo, "0.3.1", changelog=False)
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text(encoding="utf-8") + "\n- pending fix\n", encoding="utf-8")

    result = check(release_repo)

    assert result.returncode == 0


def test_rejects_empty_changelog_section(release_repo: Path) -> None:
    commit_source_fix(release_repo)
    write_metadata(release_repo, "0.3.1", changelog=False)

    result = check(release_repo)

    assert result.returncode == 1
    assert "needs a populated [0.3.1] or [Unreleased] section" in result.stderr


def test_rejects_skipped_patch_and_lock_drift(release_repo: Path) -> None:
    commit_source_fix(release_repo)
    write_metadata(release_repo, "0.3.2")
    lock = release_repo / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace('version = "0.3.2"', 'version = "0.3.1"'),
        encoding="utf-8",
    )

    result = check(release_repo)

    assert result.returncode == 1
    assert "does not match uv.lock version 0.3.1" in result.stderr
    assert "must use 0.3.1, not 0.3.2" in result.stderr


def test_rejects_exact_head_tag_that_disagrees_with_metadata(release_repo: Path) -> None:
    run(release_repo, "git", "tag", "v0.3.1")

    result = check(release_repo)

    assert result.returncode == 1
    assert "exact tag v0.3.1 points at HEAD but package metadata is 0.3.0" in result.stderr


def test_rejects_exact_tag_with_unreleased_notes_only(release_repo: Path) -> None:
    write_metadata(release_repo, "0.3.1", changelog=False)
    changelog = release_repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text(encoding="utf-8") + "\n- pending fix\n", encoding="utf-8")
    run(release_repo, "git", "add", ".")
    run(release_repo, "git", "commit", "-qm", "release 0.3.1 without version notes")
    run(release_repo, "git", "tag", "v0.3.1")

    result = check(release_repo)

    assert result.returncode == 1
    assert "requires a populated [0.3.1] CHANGELOG.md section" in result.stderr


def test_accepts_matching_clean_exact_tag_with_version_notes(release_repo: Path) -> None:
    commit_release(release_repo, "0.3.1")

    result = check(release_repo)

    assert result.returncode == 0


def test_rejects_dirty_exact_tag_worktree(release_repo: Path) -> None:
    commit_release(release_repo, "0.3.1")
    (release_repo / "README.md").write_text("# Dirty Bridge\n", encoding="utf-8")

    result = check(release_repo)

    assert result.returncode == 1
    assert "exact stable tag on HEAD requires a clean worktree" in result.stderr


def test_rejects_v_prefixed_package_metadata(release_repo: Path) -> None:
    write_metadata(release_repo, "v0.3.0")

    result = check(release_repo)

    assert result.returncode == 1
    assert "pyproject.toml version must be exact stable SemVer (X.Y.Z)" in result.stderr
