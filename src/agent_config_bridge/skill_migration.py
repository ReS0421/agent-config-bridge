"""Plan and apply conservative imports from existing user Skill roots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "MigrationError",
    "MigrationPlan",
    "MigrationSource",
    "apply_skill_migration",
    "build_skill_migration_plan",
    "migration_report_json",
    "migration_report_markdown",
    "write_migration_reports",
]

_ARTIFACT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SOURCE_LABEL = _ARTIFACT_NAME
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_LICENSE_NAMES = frozenset(
    {
        "copying",
        "copying.md",
        "copying.txt",
        "license",
        "license.md",
        "license.txt",
        "notice",
        "notice.md",
        "notice.txt",
    }
)
_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".env",
        ".credentials.json",
        "auth.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SECRET_PATTERNS = (
    (
        "private-key",
        re.compile(
            rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----\s+"
            rb"[A-Za-z0-9+/=\r\n]{64,}\s+-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
        ),
    ),
    ("openai-or-anthropic-token", re.compile(rb"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_SKILL_BYTES = 100 * 1024 * 1024


class MigrationError(RuntimeError):
    """Raised when a migration cannot be planned or applied safely."""


class _SizeLimitExceeded(MigrationError):
    """Raised internally when a bounded input read reaches its hard limit."""


class MigrationDisposition(StrEnum):
    """The action selected for one Skill name."""

    CREATE = "create"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MigrationSource:
    """A labelled Skill discovery root, ordered by canonical preference."""

    label: str
    root: Path


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """One materializable directory or regular file in a Skill snapshot."""

    relative: Path
    source: Path
    kind: str
    size: int = 0
    digest: str | None = None
    planned_contents: bytes | None = None


@dataclass(frozen=True, slots=True)
class SkillObservation:
    """A validated or blocked Skill found in one source root."""

    name: str
    source_label: str
    lexical_path: Path
    resolved_path: Path | None
    digest: str | None
    entries: tuple[SnapshotEntry, ...]
    file_count: int
    byte_count: int
    issues: tuple[str, ...]
    secret_findings: tuple[str, ...]
    license_files: tuple[str, ...]
    normalizations: tuple[str, ...]
    root_alias: bool

    @property
    def eligible(self) -> bool:
        """Return whether the observation may become canonical."""

        return self.digest is not None and not self.issues and not self.secret_findings


@dataclass(frozen=True, slots=True)
class MigrationDecision:
    """The deterministic outcome for one Skill name."""

    name: str
    disposition: MigrationDisposition
    selected: SkillObservation | None
    observations: tuple[SkillObservation, ...]
    distinct_digests: tuple[str, ...]
    detail: str

    @property
    def blocked_observations(self) -> tuple[SkillObservation, ...]:
        """Return every source or destination observation rejected as unsafe."""

        return tuple(observation for observation in self.observations if not observation.eligible)


@dataclass(frozen=True, slots=True)
class IgnoredEntry:
    """A source entry that was not a direct Skill."""

    source_label: str
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """A complete, non-mutating Skill migration decision set."""

    catalog: Path
    conflicts: Path
    sources: tuple[MigrationSource, ...]
    physical_source_roots: tuple[Path, ...]
    decisions: tuple[MigrationDecision, ...]
    ignored: tuple[IgnoredEntry, ...]
    report: Path | None
    json_report: Path | None

    @property
    def has_conflicts(self) -> bool:
        return any(decision.disposition is MigrationDisposition.CONFLICT for decision in self.decisions)

    @property
    def has_blocked(self) -> bool:
        return any(decision.blocked_observations for decision in self.decisions)


def build_skill_migration_plan(
    sources: tuple[MigrationSource, ...],
    *,
    catalog: Path,
    conflicts: Path,
    report: Path | None = None,
    repair_legacy_frontmatter: bool = False,
) -> MigrationPlan:
    """Inspect source roots and choose one canonical version per Skill name.

    Source order is preference order. Divergent lower-priority variants are
    retained in the conflict store by :func:`apply_skill_migration`.
    """

    if not sources:
        raise MigrationError("at least one Skill source is required")
    labels: set[str] = set()
    physical_roots: list[Path] = []
    for source in sources:
        if _SOURCE_LABEL.fullmatch(source.label) is None:
            raise MigrationError(f"source label must be lowercase kebab-case: {source.label!r}")
        if source.label in labels:
            raise MigrationError(f"duplicate source label: {source.label!r}")
        labels.add(source.label)
        try:
            physical = source.root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MigrationError(f"cannot resolve Skill source {source.label!r}: {source.root}: {exc}") from exc
        if not physical.is_dir():
            raise MigrationError(f"Skill source is not a directory: {source.label}={source.root}")
        physical_roots.append(physical)

    catalog = catalog.expanduser().absolute()
    conflicts = conflicts.expanduser().absolute()
    report_path, json_report = _migration_report_paths(report)
    if _paths_overlap(catalog, conflicts):
        raise MigrationError(f"catalog and conflict store must not overlap: {catalog} <-> {conflicts}")
    for source, physical in zip(sources, physical_roots, strict=True):
        if _paths_overlap(catalog, physical) or _paths_overlap(conflicts, physical):
            raise MigrationError(f"migration output must not overlap source {source.label!r}: {source.root}")
    _validate_output_layout(
        catalog,
        conflicts,
        tuple(physical_roots),
        report=report_path,
        json_report=json_report,
    )

    observations: dict[str, list[SkillObservation]] = {}
    ignored: list[IgnoredEntry] = []
    for source in sources:
        try:
            entries = sorted(source.root.expanduser().iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise MigrationError(f"cannot enumerate Skill source {source.label!r}: {exc}") from exc
        for entry in entries:
            if entry.name.startswith("."):
                ignored.append(IgnoredEntry(source.label, entry.name, "hidden entry"))
                continue
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            if not is_directory:
                ignored.append(IgnoredEntry(source.label, entry.name, "not a directory"))
                continue
            if not (entry / "SKILL.md").is_file():
                ignored.append(IgnoredEntry(source.label, entry.name, "missing SKILL.md"))
                continue
            observation = _inspect_skill(
                entry,
                source.label,
                allowed_roots=tuple(physical_roots),
                repair_legacy_frontmatter=repair_legacy_frontmatter,
            )
            observations.setdefault(entry.name, []).append(observation)

    skills_root = catalog / "skills"
    decisions: list[MigrationDecision] = []
    for name in sorted(observations):
        found = tuple(observations[name])
        eligible = tuple(observation for observation in found if observation.eligible)
        digest_order = tuple(dict.fromkeys(observation.digest for observation in eligible if observation.digest))
        if not eligible:
            decisions.append(
                MigrationDecision(
                    name=name,
                    disposition=MigrationDisposition.BLOCKED,
                    selected=None,
                    observations=found,
                    distinct_digests=(),
                    detail="no source variant passed structural and secret scanning",
                )
            )
            continue

        selected = eligible[0]
        destination = skills_root / name
        if os.path.lexists(destination):
            existing = _inspect_skill(
                destination,
                "existing-catalog",
                allowed_roots=(skills_root,),
                repair_legacy_frontmatter=False,
                reject_root_alias=True,
            )
            found = (*found, existing)
            if existing.root_alias or not existing.eligible or existing.digest != selected.digest:
                decisions.append(
                    MigrationDecision(
                        name=name,
                        disposition=MigrationDisposition.CONFLICT,
                        selected=None,
                        observations=found,
                        distinct_digests=tuple(
                            dict.fromkeys(observation.digest for observation in found if observation.digest is not None)
                        ),
                        detail="existing canonical destination differs or is unsafe; it will not be replaced",
                    )
                )
                continue
            if len(digest_order) > 1:
                disposition = MigrationDisposition.CONFLICT
                detail = (
                    f"canonical destination matches selected {selected.source_label}; "
                    f"{len(digest_order)} distinct source variants remain retained for review"
                )
            else:
                disposition = MigrationDisposition.UNCHANGED
                detail = "canonical destination already matches the selected source"
        elif len(digest_order) > 1:
            disposition = MigrationDisposition.CONFLICT
            detail = (
                f"selected {selected.source_label} by source priority; "
                f"{len(digest_order)} distinct variants will be retained for review"
            )
        else:
            disposition = MigrationDisposition.CREATE
            detail = f"selected {selected.source_label}; all eligible duplicates are identical"
        decisions.append(
            MigrationDecision(
                name=name,
                disposition=disposition,
                selected=selected,
                observations=found,
                distinct_digests=digest_order,
                detail=detail,
            )
        )

    return MigrationPlan(
        catalog=catalog,
        conflicts=conflicts,
        sources=sources,
        physical_source_roots=tuple(physical_roots),
        decisions=tuple(decisions),
        ignored=tuple(ignored),
        report=report_path,
        json_report=json_report,
    )


def apply_skill_migration(plan: MigrationPlan) -> None:
    """Materialize a reviewed plan without modifying any source root."""

    physical_roots = _require_unchanged_source_roots(plan)
    _validate_output_layout(
        plan.catalog,
        plan.conflicts,
        physical_roots,
        report=plan.report,
        json_report=plan.json_report,
    )
    for group in ("skills", "plugins", "hooks", "settings", "schedules"):
        (plan.catalog / group).mkdir(parents=True, exist_ok=True)

    for decision in plan.decisions:
        if decision.selected is not None and decision.disposition in {
            MigrationDisposition.CREATE,
            MigrationDisposition.CONFLICT,
        }:
            destination = plan.catalog / "skills" / decision.name
            if not os.path.lexists(destination):
                _materialize(
                    decision.selected,
                    destination,
                    output_root=plan.catalog,
                    protected_roots=physical_roots,
                )
        if decision.disposition is not MigrationDisposition.CONFLICT:
            continue
        staged_digests: set[str] = set()
        for observation in decision.observations:
            if not observation.eligible or observation.digest is None or observation.digest in staged_digests:
                continue
            staged_digests.add(observation.digest)
            variant = (
                plan.conflicts / decision.name / f"{observation.source_label}-{observation.digest[:12]}" / decision.name
            )
            if os.path.lexists(variant):
                _require_matching_retained_variant(variant, observation, plan.conflicts)
                continue
            _materialize(
                observation,
                variant,
                output_root=plan.conflicts,
                protected_roots=physical_roots,
            )


def migration_report_json(plan: MigrationPlan) -> dict[str, object]:
    """Return a content-free, machine-readable migration report."""

    decisions_with_blocked = tuple(decision for decision in plan.decisions if decision.blocked_observations)
    summary = {
        "sources": len(plan.sources),
        "skill_names": len(plan.decisions),
        "create": sum(decision.disposition is MigrationDisposition.CREATE for decision in plan.decisions),
        "unchanged": sum(decision.disposition is MigrationDisposition.UNCHANGED for decision in plan.decisions),
        "conflict": sum(decision.disposition is MigrationDisposition.CONFLICT for decision in plan.decisions),
        "blocked": len(decisions_with_blocked),
        "blocked_observations": sum(len(decision.blocked_observations) for decision in decisions_with_blocked),
        "ignored": len(plan.ignored),
    }
    return {
        "schema_version": 1,
        "catalog": str(plan.catalog),
        "conflicts": str(plan.conflicts),
        "source_priority": [{"label": source.label, "root": str(source.root)} for source in plan.sources],
        "summary": summary,
        "skills": [
            {
                "name": decision.name,
                "disposition": decision.disposition.value,
                "selected_source": decision.selected.source_label if decision.selected else None,
                "selected_digest": decision.selected.digest if decision.selected else None,
                "distinct_digests": list(decision.distinct_digests),
                "blocked_observations": len(decision.blocked_observations),
                "detail": decision.detail,
                "observations": [
                    {
                        "source": observation.source_label,
                        "path": str(observation.lexical_path),
                        "resolved_path": str(observation.resolved_path) if observation.resolved_path else None,
                        "root_alias": observation.root_alias,
                        "digest": observation.digest,
                        "files": observation.file_count,
                        "bytes": observation.byte_count,
                        "issues": list(observation.issues),
                        "secret_findings": list(observation.secret_findings),
                        "license_files": list(observation.license_files),
                        "normalizations": list(observation.normalizations),
                    }
                    for observation in decision.observations
                ],
            }
            for decision in plan.decisions
        ],
        "ignored": [
            {"source": entry.source_label, "name": entry.name, "reason": entry.reason} for entry in plan.ignored
        ],
    }


def migration_report_markdown(plan: MigrationPlan) -> str:
    """Render a HADS conflict report suitable for human and agent review."""

    report = migration_report_json(plan)
    summary = report["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# Skill Migration Report",
        f"**Version 1.0.0** · Agent Config Bridge · {datetime.now(UTC).date().isoformat()}",
        "",
        "---",
        "",
        "## AI READING INSTRUCTION",
        "",
        "Read `[SPEC]` and `[BUG]` blocks before changing the canonical catalog.",
        "Read `[NOTE]` only for migration context. Treat unresolved conflicts as blocking review items.",
        "",
        "---",
        "",
        "## 1. Summary",
        "",
        "**[SPEC]**",
        f"- Canonical catalog: `{_markdown_escape(str(plan.catalog))}`",
        f"- Conflict store: `{_markdown_escape(str(plan.conflicts))}`",
        f"- Skill names inspected: {summary['skill_names']}",
        f"- New canonical Skills: {summary['create']}",
        f"- Already identical: {summary['unchanged']}",
        f"- Divergent or destination conflicts: {summary['conflict']}",
        f"- Skill names with rejected observations: {summary['blocked']}",
        f"- Rejected observations: {summary['blocked_observations']}",
        "- Source priority: " + " → ".join(_markdown_escape(source.label) for source in plan.sources),
        "",
        "## 2. Canonical Selection",
        "",
        "**[SPEC]**",
        "| Skill | Result | Selected source | Variants | Rejected | Normalization | License |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for decision in plan.decisions:
        selected_source_label = _markdown_escape(decision.selected.source_label) if decision.selected else "—"
        license_state = (
            ", ".join(_markdown_escape(value) for value in decision.selected.license_files)
            if decision.selected and decision.selected.license_files
            else "unknown"
        )
        normalization = (
            ", ".join(_markdown_escape(value) for value in decision.selected.normalizations)
            if decision.selected and decision.selected.normalizations
            else "none"
        )
        lines.append(
            f"| `{_markdown_escape(decision.name)}` | {decision.disposition.value} | {selected_source_label} | "
            f"{len(decision.distinct_digests)} | {len(decision.blocked_observations)} | "
            f"{normalization} | {license_state} |"
        )

    conflicts = [decision for decision in plan.decisions if decision.disposition is MigrationDisposition.CONFLICT]
    blocked = [decision for decision in plan.decisions if decision.blocked_observations]
    lines.extend(["", "## 3. Conflicts", ""])
    if conflicts:
        for decision in conflicts:
            lines.extend(
                [
                    f"**[BUG] `{_markdown_escape(decision.name)}` has divergent variants**",
                    f"- Symptom: {len(decision.distinct_digests)} distinct content digests were found.",
                    f"- Cause: {_markdown_escape(decision.detail)}",
                    (
                        "- Fix: review "
                        f"`{_markdown_escape(str(plan.conflicts / decision.name))}` and replace the canonical version "
                        "only after selecting one variant."
                    ),
                    "- Sources: "
                    + ", ".join(
                        f"{_markdown_escape(observation.source_label)}:{observation.digest[:12]}"
                        for observation in decision.observations
                        if observation.digest
                    ),
                    "",
                ]
            )
    else:
        lines.extend(["**[SPEC]**", "- No divergent Skill names were found.", ""])

    lines.extend(["## 4. Blocked Inputs", ""])
    if blocked:
        for decision in blocked:
            rejected = decision.blocked_observations
            selected_observation = decision.selected
            if selected_observation is None:
                title = f"**[BUG] `{_markdown_escape(decision.name)}` was not imported**"
                symptom = "- Symptom: no source variant was selected for the canonical catalog."
            else:
                title = f"**[BUG] `{_markdown_escape(decision.name)}` has rejected observations**"
                symptom = (
                    f"- Symptom: {len(rejected)} observation(s) were rejected while the safe "
                    f"`{_markdown_escape(selected_observation.source_label)}` variant remained selected."
                )
            lines.extend(
                [
                    title,
                    symptom,
                    "- Rejected observations:",
                ]
            )
            for observation in rejected:
                reasons = sorted({*observation.issues, *observation.secret_findings})
                lines.append(
                    f"  - `{_markdown_escape(observation.source_label)}`: "
                    + "; ".join(_markdown_escape(value) for value in reasons)
                )
            lines.extend(
                [
                    "- Fix: inspect the reported files, remove sensitive or non-portable content at the source, "
                    "then rerun migration. A selected safe variant does not clear this blocking review item.",
                    "",
                ]
            )
    else:
        lines.extend(["**[SPEC]**", "- No Skill was blocked by validation or secret scanning.", ""])

    lines.extend(
        [
            "## 5. Publication Safety",
            "",
            "**[NOTE]**",
            "This report records license-file presence, not redistribution permission. Keep the migrated catalog "
            "private until every selected Skill has an acceptable license or author approval. Secret findings list "
            "only rule names and relative files; secret values are never included.",
            "",
            "## 6. Changelog",
            "",
            "**[SPEC]**",
            "- 1.0.0: Initial deterministic migration report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_migration_reports(plan: MigrationPlan, markdown_path: Path | None = None) -> tuple[Path, Path]:
    """Atomically write HADS Markdown and adjacent JSON reports."""

    requested_report = markdown_path if markdown_path is not None else plan.report
    report_path, json_path = _migration_report_paths(requested_report)
    if report_path is None or json_path is None:
        raise MigrationError("a Markdown migration report path is required")
    if plan.report is not None and (report_path != plan.report or json_path != plan.json_report):
        raise MigrationError("migration report path differs from the validated plan output")
    _validate_output_layout(
        plan.catalog,
        plan.conflicts,
        _require_unchanged_source_roots(plan),
        report=report_path,
        json_report=json_path,
    )
    _atomic_write(report_path, migration_report_markdown(plan).encode())
    _atomic_write(json_path, (json.dumps(migration_report_json(plan), indent=2) + "\n").encode())
    return report_path, json_path


def _inspect_skill(
    lexical_path: Path,
    source_label: str,
    *,
    allowed_roots: tuple[Path, ...],
    repair_legacy_frontmatter: bool,
    reject_root_alias: bool = False,
) -> SkillObservation:
    issues: list[str] = []
    secret_findings: list[str] = []
    licenses: list[str] = []
    normalizations: list[str] = []
    entries: list[SnapshotEntry] = []
    name = lexical_path.name
    if _ARTIFACT_NAME.fullmatch(name) is None:
        issues.append("directory name is not portable lowercase kebab-case")
    root_alias = lexical_path.is_symlink()
    if not root_alias:
        try:
            root_alias = is_directory_reparse_point(lexical_path)
        except OSError as exc:
            issues.append(f"cannot inspect Skill root alias: {type(exc).__name__}")
    if root_alias and reject_root_alias:
        issues.append("existing canonical Skill root is a symlink, junction, or reparse point")
    try:
        resolved = lexical_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        issues.append(f"cannot resolve Skill root: {type(exc).__name__}")
        return SkillObservation(
            name,
            source_label,
            lexical_path,
            None,
            None,
            (),
            0,
            0,
            tuple(dict.fromkeys(issues)),
            (),
            (),
            (),
            root_alias,
        )
    if not resolved.is_dir():
        issues.append("Skill root does not resolve to a directory")
    if not any(_is_within(resolved, root) for root in allowed_roots):
        issues.append("Skill root alias resolves outside all declared source roots")

    manifest = resolved / "SKILL.md"
    manifest_override: bytes | None = None
    try:
        manifest_contents = _read_bytes_limited(manifest, _MAX_MANIFEST_BYTES)
        manifest_text = manifest_contents.decode("utf-8")
    except _SizeLimitExceeded:
        issues.append(f"SKILL.md exceeds {_MAX_MANIFEST_BYTES} byte manifest limit")
    except (OSError, UnicodeError) as exc:
        issues.append(f"cannot read SKILL.md as UTF-8: {type(exc).__name__}")
    else:
        frontmatter = _frontmatter(manifest_text)
        if frontmatter is None:
            if repair_legacy_frontmatter:
                description = _infer_description(manifest_text, name)
                manifest_override = (
                    "---\n"
                    f"name: {name}\n"
                    f"description: {json.dumps(description, ensure_ascii=False)}\n"
                    "---\n\n"
                    f"{manifest_text.lstrip()}"
                ).encode()
                normalizations.append("added required name/description frontmatter")
            else:
                issues.append("SKILL.md has invalid YAML frontmatter delimiters")
        else:
            raw_name = frontmatter.get("name", "")
            parsed_name = _unquote(raw_name)
            if parsed_name != name:
                issues.append(f"frontmatter name does not match directory: {parsed_name!r}")
            if not frontmatter.get("description"):
                issues.append("SKILL.md has no non-empty description")

    portable_names: dict[tuple[str, str], str] = {}
    byte_count = 0
    file_count = 0
    try:
        candidates = sorted(resolved.rglob("*"), key=lambda path: path.relative_to(resolved).as_posix())
    except (OSError, RuntimeError) as exc:
        candidates = []
        issues.append(f"cannot enumerate Skill tree: {type(exc).__name__}")
    for path in candidates:
        relative = path.relative_to(resolved)
        if (
            "__pycache__" in relative.parts
            or relative.suffix.casefold() in {".pyc", ".pyo"}
            or relative.name == ".DS_Store"
        ):
            if "excluded transient cache files" not in normalizations:
                normalizations.append("excluded transient cache files")
            continue
        portable_issue = _portable_path_issue(relative)
        if portable_issue:
            issues.append(f"{relative.as_posix()}: {portable_issue}")
        parent_key = relative.parent.as_posix().casefold()
        name_key = relative.name.casefold()
        previous = portable_names.get((parent_key, name_key))
        if previous is not None and previous != relative.name:
            issues.append(f"{relative.parent.as_posix()}: names collide on Windows: {previous!r} and {relative.name!r}")
        portable_names[(parent_key, name_key)] = relative.name
        try:
            reparse_point = is_directory_reparse_point(path)
        except OSError:
            reparse_point = False
        if reparse_point:
            issues.append(f"{relative.as_posix()}: directory junction/reparse point is unsupported")
            continue
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError):
                issues.append(f"{relative.as_posix()}: broken or unresolvable symlink")
                continue
            if target.is_dir():
                issues.append(f"{relative.as_posix()}: directory symlink is unsupported")
                continue
            if not target.is_file() or not _is_within(target, resolved):
                issues.append(f"{relative.as_posix()}: symlink must target a contained regular file")
                continue
            file_source = target
        elif path.is_dir():
            entries.append(SnapshotEntry(relative, path, "directory"))
            continue
        elif path.is_file():
            file_source = path
        else:
            issues.append(f"{relative.as_posix()}: unsupported filesystem node")
            continue

        try:
            status = file_source.stat()
            if not stat.S_ISREG(status.st_mode):
                issues.append(f"{relative.as_posix()}: source is not a regular file")
                continue
            remaining_bytes = _MAX_SKILL_BYTES - byte_count
            if status.st_size > remaining_bytes:
                issues.append(
                    f"{relative.as_posix()}: declared file size exceeds remaining "
                    f"{remaining_bytes} byte Skill migration limit"
                )
                continue
            contents = (
                manifest_override
                if relative.as_posix() == "SKILL.md" and manifest_override is not None
                else _read_bytes_limited(file_source, remaining_bytes)
            )
        except _SizeLimitExceeded:
            issues.append(
                f"{relative.as_posix()}: file contents exceed remaining "
                f"{max(_MAX_SKILL_BYTES - byte_count, 0)} byte Skill migration limit"
            )
            continue
        except OSError as exc:
            issues.append(f"{relative.as_posix()}: cannot read file: {type(exc).__name__}")
            continue
        if len(contents) > remaining_bytes:
            issues.append(
                f"{relative.as_posix()}: normalized contents exceed remaining "
                f"{remaining_bytes} byte Skill migration limit"
            )
            continue
        byte_count += len(contents)
        file_count += 1
        file_digest = _content_digest(contents)
        entries.append(
            SnapshotEntry(
                relative,
                file_source,
                "file",
                len(contents),
                file_digest,
                contents if relative.as_posix() == "SKILL.md" and manifest_override is not None else None,
            )
        )
        if relative.name.casefold() in _LICENSE_NAMES:
            licenses.append(relative.as_posix())
        if relative.name.casefold() in _SENSITIVE_FILE_NAMES:
            secret_findings.append(f"sensitive-filename:{relative.as_posix()}")
        for rule, pattern in _SECRET_PATTERNS:
            if pattern.search(contents):
                secret_findings.append(f"{rule}:{relative.as_posix()}")
    if any(entry.relative.as_posix() == ".agent-config-bridge.json" for entry in entries):
        issues.append("Skill contains reserved .agent-config-bridge.json root marker")

    digest = None if issues else _snapshot_digest(tuple(entries))
    return SkillObservation(
        name=name,
        source_label=source_label,
        lexical_path=lexical_path,
        resolved_path=resolved,
        digest=digest,
        entries=tuple(entries),
        file_count=file_count,
        byte_count=byte_count,
        issues=tuple(dict.fromkeys(issues)),
        secret_findings=tuple(dict.fromkeys(secret_findings)),
        license_files=tuple(sorted(set(licenses))),
        normalizations=tuple(normalizations),
        root_alias=root_alias,
    )


def _snapshot_digest(entries: tuple[SnapshotEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.relative.as_posix()):
        digest.update(entry.relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(b"D" if entry.kind == "directory" else b"F")
        if entry.digest:
            digest.update(entry.digest.encode())
    return digest.hexdigest()


def _materialize(
    observation: SkillObservation,
    destination: Path,
    *,
    output_root: Path,
    protected_roots: tuple[Path, ...],
) -> None:
    if not observation.eligible or observation.digest is None:
        raise MigrationError(f"refusing to materialize unsafe Skill: {observation.lexical_path}")
    _validate_materialization_destination(destination, output_root, protected_roots)
    if os.path.lexists(destination):
        raise MigrationError(f"refusing to replace existing migration destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_parent = destination.with_name(f".agentbridge-import.{uuid.uuid4().hex}.tmp")
    temporary = temporary_parent / destination.name
    temporary.mkdir(parents=True)
    try:
        for entry in observation.entries:
            staged = temporary / entry.relative
            if entry.kind == "directory":
                staged.mkdir(parents=True, exist_ok=True)
                continue
            staged.parent.mkdir(parents=True, exist_ok=True)
            current = (
                entry.planned_contents
                if entry.planned_contents is not None
                else _read_bytes_limited(entry.source, entry.size)
            )
            if _content_digest(current) != entry.digest:
                raise MigrationError(f"source changed after migration planning: {entry.source}")
            staged.write_bytes(current)
            with suppress(OSError):
                shutil.copystat(entry.source, staged, follow_symlinks=True)
        staged_observation = _inspect_skill(
            temporary,
            observation.source_label,
            allowed_roots=(temporary.parent,),
            repair_legacy_frontmatter=False,
        )
        if staged_observation.digest != observation.digest:
            raise MigrationError(f"staged Skill digest mismatch: {observation.lexical_path}")
        if os.path.lexists(destination):
            raise MigrationError(f"migration destination appeared while staging: {destination}")
        os.replace(temporary, destination)
        temporary_parent.rmdir()
    except Exception:
        shutil.rmtree(temporary_parent, ignore_errors=True)
        raise


def _require_matching_retained_variant(
    variant: Path,
    observation: SkillObservation,
    conflicts_root: Path,
) -> None:
    """Fail closed when an existing retained variant no longer matches its label."""

    _reject_redirected_ancestors(variant, "retained conflict variant")
    existing = _inspect_skill(
        variant,
        "existing-conflict",
        allowed_roots=(conflicts_root,),
        repair_legacy_frontmatter=False,
    )
    if existing.root_alias or not existing.eligible or existing.digest != observation.digest:
        raise MigrationError(f"retained conflict variant is missing, redirected, or modified: {variant}")


def _migration_report_paths(report: Path | None) -> tuple[Path | None, Path | None]:
    if report is None:
        return None, None
    markdown = report.expanduser().absolute()
    if markdown.suffix.casefold() != ".md":
        raise MigrationError("migration report path must use a .md suffix")
    machine = markdown.with_suffix(".json")
    if markdown == machine:
        raise MigrationError("migration report Markdown and JSON paths must be distinct")
    return markdown, machine


def _resolve_source_roots(sources: tuple[MigrationSource, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for source in sources:
        try:
            root = source.root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MigrationError(f"cannot resolve Skill source {source.label!r}: {source.root}: {exc}") from exc
        if not root.is_dir():
            raise MigrationError(f"Skill source is not a directory: {source.label}={source.root}")
        roots.append(root)
    return tuple(roots)


def _require_unchanged_source_roots(plan: MigrationPlan) -> tuple[Path, ...]:
    current = _resolve_source_roots(plan.sources)
    if current != plan.physical_source_roots:
        raise MigrationError("one or more Skill source roots changed after migration planning")
    return current


def _validate_output_layout(
    catalog: Path,
    conflicts: Path,
    protected_roots: tuple[Path, ...],
    *,
    report: Path | None,
    json_report: Path | None,
) -> None:
    """Reject output redirects and every physical overlap with migration inputs."""

    _validate_output_directory(catalog, "catalog")
    _validate_output_directory(conflicts, "conflict store")
    for group in ("skills", "plugins", "hooks", "settings", "schedules"):
        _validate_output_directory(catalog / group, f"catalog group {group!r}")

    outputs: list[tuple[str, Path]] = [("catalog", catalog), ("conflict store", conflicts)]
    if report is not None:
        _validate_report_file(report, "Markdown report")
        outputs.append(("migration report", report))
    if json_report is not None:
        _validate_report_file(json_report, "JSON report")
        outputs.append(("migration report", json_report))

    for label, output in outputs:
        for protected in protected_roots:
            if _paths_overlap(output, protected):
                raise MigrationError(f"{label} must not overlap a Skill source: {output} <-> {protected}")

    if report is not None and json_report is not None:
        if _paths_overlap(report, catalog) or _paths_overlap(report, conflicts):
            raise MigrationError("migration report must be outside the catalog and conflict store")
        if _paths_overlap(json_report, catalog) or _paths_overlap(json_report, conflicts):
            raise MigrationError("migration report must be outside the catalog and conflict store")


def _validate_output_directory(path: Path, label: str) -> None:
    _reject_redirected_ancestors(path, label)
    if os.path.lexists(path) and not path.is_dir():
        raise MigrationError(f"{label} must be a directory: {path}")


def _validate_report_file(path: Path, label: str) -> None:
    _reject_redirected_ancestors(path, label)
    if os.path.lexists(path) and not path.is_file():
        raise MigrationError(f"{label} must be a regular file: {path}")


def _reject_redirected_ancestors(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if not os.path.lexists(candidate):
            continue
        try:
            redirected = candidate.is_symlink() or is_directory_reparse_point(candidate)
        except OSError as exc:
            raise MigrationError(f"cannot inspect {label} path component {candidate}: {exc}") from exc
        if redirected:
            raise MigrationError(f"{label} path is redirected through symlink, junction, or reparse point: {candidate}")


def _validate_materialization_destination(
    destination: Path,
    output_root: Path,
    protected_roots: tuple[Path, ...],
) -> None:
    try:
        destination.absolute().relative_to(output_root.absolute())
    except ValueError as exc:
        raise MigrationError(f"migration destination escapes its output root: {destination}") from exc
    _validate_output_directory(output_root, "migration output root")
    _validate_output_directory(destination.parent, "migration destination parent")
    try:
        destination.parent.resolve(strict=False).relative_to(output_root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise MigrationError(f"migration destination resolves outside its output root: {destination}") from exc
    for protected in protected_roots:
        if _paths_overlap(destination, protected):
            raise MigrationError(
                f"migration destination must not overlap a Skill source: {destination} <-> {protected}"
            )


def _read_bytes_limited(path: Path, limit: int) -> bytes:
    if limit < 0:
        raise _SizeLimitExceeded(f"input exceeds byte limit before read: {path}")
    with path.open("rb") as stream:
        contents = stream.read(limit + 1)
    if len(contents) > limit:
        raise _SizeLimitExceeded(f"input exceeds {limit} byte limit: {path}")
    return contents


def _markdown_escape(value: str) -> str:
    """Render untrusted filesystem text without creating Markdown structure."""

    escaped: list[str] = []
    for character in value:
        if character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\t":
            escaped.append(r"\t")
        elif ord(character) < 32 or ord(character) == 127:
            escaped.append(f"\\u{ord(character):04x}")
        elif character == "`":
            escaped.append("&#96;")
        elif character == "|":
            escaped.append(r"\|")
        elif character == "#":
            escaped.append("&#35;")
        elif character == "<":
            escaped.append("&lt;")
        elif character == ">":
            escaped.append("&gt;")
        else:
            escaped.append(character)
    return "".join(escaped)


def _frontmatter(contents: str) -> dict[str, str] | None:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None
    parsed: dict[str, str] = {}
    current_key: str | None = None
    continuations: dict[str, list[str]] = {}
    for line in lines[1:closing]:
        if line[:1].isspace():
            if current_key in {"name", "description"} and line.strip():
                continuations.setdefault(current_key, []).append(line.strip())
            continue
        if ":" not in line:
            current_key = None
            continue
        key, value = line.split(":", maxsplit=1)
        current_key = key.strip()
        parsed[current_key] = value.strip()
    for key, parts in continuations.items():
        prefix = parsed.get(key, "")
        if prefix in {"", ">", ">-", ">+", "|", "|-", "|+"}:
            parsed[key] = " ".join(parts)
        elif parts:
            parsed[key] = " ".join((prefix, *parts))
    return parsed


def _content_digest(contents: bytes) -> str:
    """Treat text line endings as portable while preserving binary identity."""

    if b"\0" not in contents:
        try:
            normalized = contents.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode()
        except UnicodeError:
            normalized = contents
    else:
        normalized = contents
    return hashlib.sha256(normalized).hexdigest()


def _infer_description(contents: str, name: str) -> str:
    lines = contents.splitlines()
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if paragraph:
                break
            continue
        if stripped.startswith(("```", "---", "- ", "* ")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    description = " ".join(paragraph) or f"Run the {name} workflow."
    if "use " not in description.casefold():
        description += f" Use when a task needs the {name} workflow."
    return description[:500]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _portable_path_issue(relative: Path) -> str | None:
    for part in relative.parts:
        if not part or part in {".", ".."}:
            return "invalid path component"
        if part[-1:] in {" ", "."}:
            return "path component ends with a space or period"
        if any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in part):
            return "path component contains a Windows-invalid character"
        stem = part.split(".", maxsplit=1)[0].casefold()
        if stem in _WINDOWS_DEVICE_NAMES:
            return "path component is reserved on Windows"
    return None


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    try:
        left_resolved.relative_to(right_resolved)
    except ValueError:
        pass
    else:
        return True
    try:
        right_resolved.relative_to(left_resolved)
    except ValueError:
        return False
    return True


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(contents)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MigrationError(f"could not write migration report {path}: {exc}") from exc
