#!/usr/bin/env python3
"""Validate package metadata against changelog and exact stable Git tags."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PACKAGE_NAME = "agent-config-bridge"
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HEADING_PATTERN = re.compile(r"^## \[(?P<label>[^\]]+)\](?:[ \t]+-[^\n]+)?$", re.MULTILINE)

Version = tuple[int, int, int]


def parse_version(value: object, *, source: str) -> Version:
    if not isinstance(value, str):
        raise ValueError(f"{source} version must be a string")
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"{source} version must be exact stable SemVer (X.Y.Z): {value!r}")
    parts = tuple(int(part) for part in match.groups())
    return (parts[0], parts[1], parts[2])


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def parse_tag(value: str) -> Version | None:
    match = TAG_PATTERN.fullmatch(value)
    if match is None:
        return None
    parts = tuple(int(part) for part in match.groups())
    return (parts[0], parts[1], parts[2])


def project_version(repo: Path) -> Version:
    with (repo / "pyproject.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    return parse_version(payload.get("project", {}).get("version"), source="pyproject.toml")


def lock_version(repo: Path) -> Version:
    with (repo / "uv.lock").open("rb") as handle:
        payload = tomllib.load(handle)
    matches = [
        package.get("version")
        for package in payload.get("package", [])
        if package.get("name") == PACKAGE_NAME and package.get("source", {}).get("editable") == "."
    ]
    if len(matches) != 1:
        raise ValueError(f"uv.lock must contain exactly one editable {PACKAGE_NAME!r} package")
    return parse_version(matches[0], source="uv.lock")


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def reachable_stable_tags(repo: Path) -> list[tuple[Version, str]]:
    tags: list[tuple[Version, str]] = []
    for tag in git(repo, "tag", "--merged", "HEAD", "--list").splitlines():
        version = parse_tag(tag)
        if version is not None:
            tags.append((version, tag))
    return sorted(tags)


def exact_head_tags(repo: Path) -> list[tuple[Version, str]]:
    tags: list[tuple[Version, str]] = []
    for tag in git(repo, "tag", "--points-at", "HEAD", "--list").splitlines():
        version = parse_tag(tag)
        if version is not None:
            tags.append((version, tag))
    return sorted(tags)


def has_release_impacting_changes(repo: Path, tag: str) -> bool:
    tracked = git(repo, "diff", "--name-only", tag, "--", ".").strip()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "--", ".").strip()
    return bool(tracked or untracked)


def has_populated_changelog_section(repo: Path, label: str) -> bool:
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = list(HEADING_PATTERN.finditer(text))
    for index, match in enumerate(headings):
        if match.group("label") != label:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[match.end() : end]
        return any(line.startswith("- ") for line in section.splitlines())
    return False


def validate(repo: Path) -> list[str]:
    errors: list[str] = []
    try:
        shallow = git(repo, "rev-parse", "--is-shallow-repository").strip()
        current = project_version(repo)
        locked = lock_version(repo)
        tags = reachable_stable_tags(repo)
        head_tags = exact_head_tags(repo)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return [str(exc)]

    if shallow == "true":
        errors.append("release contract requires complete Git history and tags; this clone is shallow")
    if locked != current:
        errors.append(
            f"pyproject.toml version {format_version(current)} does not match uv.lock version {format_version(locked)}"
        )
    version_text = format_version(current)
    if head_tags:
        if git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip():
            errors.append("an exact stable tag on HEAD requires a clean worktree")
        if not has_populated_changelog_section(repo, version_text):
            errors.append(f"an exact stable tag on HEAD requires a populated [{version_text}] CHANGELOG.md section")
    elif not (
        has_populated_changelog_section(repo, version_text) or has_populated_changelog_section(repo, "Unreleased")
    ):
        errors.append(f"CHANGELOG.md needs a populated [{version_text}] or [Unreleased] section")

    for tagged_version, tag in head_tags:
        if tagged_version != current:
            errors.append(f"exact tag {tag} points at HEAD but package metadata is {format_version(current)}")

    if not tags:
        return errors

    latest, latest_tag = tags[-1]
    if current < latest:
        errors.append(f"package version {format_version(current)} is older than latest reachable tag {latest_tag}")
    if has_release_impacting_changes(repo, latest_tag):
        if current == latest:
            errors.append(
                f"release-impacting changes after {latest_tag} must not retain version {format_version(current)}"
            )
        elif current[:2] == latest[:2] and current[2] != latest[2] + 1:
            errors.append(
                f"a patch release after {latest_tag} must use "
                f"{latest[0]}.{latest[1]}.{latest[2] + 1}, not {format_version(current)}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's repository)",
    )
    args = parser.parse_args(argv)
    errors = validate(args.repo.resolve())
    if errors:
        for error in errors:
            print(f"release-contract: {error}", file=sys.stderr)
        return 1
    print("release-contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
