"""Governance core: manifests as source of truth, registry fully generated.

Implements ADR-2 §3 of the agent-harness design (catalog repo,
``docs/agent-harness/adr/ADR-2-governance-schema-and-registry-check.md``):
``load_governance`` / ``resolve_artifact_refs`` / ``validate_governance`` /
``build_registry_payload`` shared by ``registry generate``, ``registry check``,
and the future runtime desired-inventory resolver. Diagnostics are
``GovernanceFinding`` values (never deployment ``Disposition``), and the
registry serialization is byte-deterministic: key-sorted, UTF-8, LF, no
timestamps.

Governance never gates runtime selection in ``audit`` mode; the active mode is
a committed catalog policy (``governance/policy.toml``), not a CLI flag.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agent_config_bridge.catalog import CatalogInventory
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "GOVERNANCE_POLICY_FILENAME",
    "GovernanceError",
    "GovernanceFinding",
    "GovernanceManifest",
    "GovernanceMode",
    "GovernanceReport",
    "GovernanceSeverity",
    "build_registry_payload",
    "governance_root",
    "load_governance",
    "read_governance_mode",
    "registry_path",
    "resolve_artifact_refs",
    "run_governance",
    "serialize_registry",
    "validate_governance",
]

GOVERNANCE_POLICY_FILENAME = "policy.toml"

_CAPABILITY_KINDS = frozenset({"instruction", "skill", "event-handler", "agent-schedule", "host-job", "tool"})
_DELIVERIES = frozenset({"standalone", "plugin", "mcp-registration", "settings-fragment"})
_FAILURE_POLICIES = frozenset({"advisory", "block", "escalate"})
_LIFECYCLES = frozenset({"proposed", "active", "deprecated", "quarantined", "removed"})
_DEPLOYABLE_LIFECYCLES = frozenset({"active", "deprecated"})
_LEGAL_KIND_DELIVERY = frozenset(
    {
        ("instruction", "standalone"),
        ("skill", "standalone"),
        ("event-handler", "plugin"),
        ("event-handler", "settings-fragment"),
        ("agent-schedule", "standalone"),
        ("host-job", "standalone"),
        ("tool", "mcp-registration"),
    }
)
# No "tools" or "instructions" component exists yet, so kind=tool (ADR-6) and
# kind=instruction (ADR-5) manifests cannot carry resolvable artifact refs today.
_ARTIFACT_COMPONENTS = ("hooks", "plugins", "schedules", "settings", "skills")
# Agent Skills spec: conservative allowed top-level frontmatter keys.
_ALLOWED_SKILL_FRONTMATTER = frozenset(
    {"name", "description", "license", "metadata", "allowed-tools", "argument-hint", "model"}
)


class GovernanceError(RuntimeError):
    """Raised when governance policy or registry files cannot be used safely."""


class GovernanceMode(StrEnum):
    """Staged governance modes; the active mode is committed catalog policy."""

    AUDIT = "audit"
    REQUIRED = "required"
    PUBLIC_EXPORT = "public-export"


class GovernanceSeverity(StrEnum):
    """Diagnostic severity for governance findings."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class GovernanceFinding:
    """One governance diagnostic (ADR-2 §5), never a deployment disposition."""

    code: str
    severity: GovernanceSeverity
    capability_id: str | None = None
    artifact_ref: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class GovernanceManifest:
    """One parsed hand-authored governance manifest."""

    id: str
    path: Path
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GovernanceReport:
    """Manifests, findings, and mode from one governance evaluation."""

    mode: GovernanceMode
    manifests: tuple[GovernanceManifest, ...]
    findings: tuple[GovernanceFinding, ...]

    @property
    def has_error(self) -> bool:
        """Return whether any finding is an error."""

        return any(finding.severity is GovernanceSeverity.ERROR for finding in self.findings)


def governance_root(inventory: CatalogInventory) -> Path:
    """Return the catalog directory that holds governance manifests."""

    return inventory.root / "governance"


def registry_path(inventory: CatalogInventory) -> Path:
    """Return the committed registry snapshot path."""

    return inventory.root / "registry.json"


def read_governance_mode(root: Path) -> GovernanceMode:
    """Read the committed governance mode; an absent policy means audit.

    Raises:
        GovernanceError: If the policy file is unreadable, malformed, or names
            an unknown mode or schema version.
    """

    policy = root / GOVERNANCE_POLICY_FILENAME
    if not policy.exists():
        if policy.is_symlink():
            raise GovernanceError(f"governance policy is a broken symlink: {policy}")
        return GovernanceMode.AUDIT
    if policy.is_symlink() or not policy.is_file():
        raise GovernanceError(f"governance policy is not a regular file: {policy}")
    try:
        payload = tomllib.loads(policy.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GovernanceError(f"invalid governance policy: {policy}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise GovernanceError(f"unsupported governance policy schema_version: {policy}")
    mode_value = payload.get("mode")
    if not isinstance(mode_value, str):
        raise GovernanceError(f"unknown governance mode in policy: {policy}")
    try:
        return GovernanceMode(mode_value)
    except ValueError as exc:
        raise GovernanceError(f"unknown governance mode in policy: {policy}") from exc


def load_governance(root: Path) -> tuple[tuple[GovernanceManifest, ...], tuple[GovernanceFinding, ...]]:
    """Parse every manifest under a governance directory.

    ``policy.toml`` is the reserved mode policy and is never a manifest.
    Malformed manifests, missing ids, and duplicate ids are errors in every
    mode; a missing governance directory simply yields no manifests.
    """

    manifests: list[GovernanceManifest] = []
    findings: list[GovernanceFinding] = []
    if root.is_symlink():
        raise GovernanceError(f"governance directory must not be a symlink or junction: {root}")
    if not root.is_dir():
        return (), ()
    if is_directory_reparse_point(root):
        raise GovernanceError(f"governance directory must not be a symlink or junction: {root}")
    seen: dict[str, Path] = {}
    for path in sorted(root.glob("*.toml"), key=lambda item: item.name):
        if path.name == GOVERNANCE_POLICY_FILENAME:
            continue
        if path.is_symlink():
            raise GovernanceError(f"governance manifest must not be a symlink: {path}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            findings.append(
                GovernanceFinding("GOV001", GovernanceSeverity.ERROR, path.stem, detail=f"malformed manifest: {exc}")
            )
            continue
        manifest_id = data.get("id")
        if not manifest_id or not isinstance(manifest_id, str):
            findings.append(
                GovernanceFinding("GOV002", GovernanceSeverity.ERROR, path.stem, detail="missing/invalid top-level id")
            )
            continue
        if manifest_id in seen:
            findings.append(
                GovernanceFinding(
                    "GOV003",
                    GovernanceSeverity.ERROR,
                    manifest_id,
                    detail=f"duplicate id also in {seen[manifest_id].name}",
                )
            )
            continue
        seen[manifest_id] = path
        manifests.append(GovernanceManifest(id=manifest_id, path=path, data=data))
    return tuple(manifests), tuple(findings)


def resolve_artifact_refs(
    manifest: GovernanceManifest,
    inventory: CatalogInventory,
) -> tuple[GovernanceFinding, ...]:
    """Check every ``[[artifacts]].ref`` against the validated inventory.

    Inventory membership implies the artifact already passed its component
    validator during ``discover_catalog``, satisfying ADR-2's "exists and
    passes ``_validate_*``" rule without a second validation path.
    """

    findings: list[GovernanceFinding] = []
    artifacts = manifest.data.get("artifacts", [])
    if not artifacts and manifest.data.get("lifecycle") != "removed":
        findings.append(
            GovernanceFinding(
                "GOV010",
                GovernanceSeverity.ERROR,
                manifest.id,
                detail="no [[artifacts]] (only a removed tombstone may have none)",
            )
        )
    known = _inventory_refs(inventory)
    for artifact in artifacts:
        ref = artifact.get("ref", "") if isinstance(artifact, dict) else ""
        component = ref.split("/", 1)[0] if "/" in ref else ""
        if component not in _ARTIFACT_COMPONENTS:
            findings.append(
                GovernanceFinding(
                    "GOV011",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    ref or None,
                    detail=f"ref not under a known component {list(_ARTIFACT_COMPONENTS)}",
                )
            )
            continue
        if ref not in known:
            findings.append(
                GovernanceFinding(
                    "GOV012",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    ref,
                    detail="dangling artifact_ref (no validated catalog artifact)",
                )
            )
    return tuple(findings)


def validate_governance(
    manifests: tuple[GovernanceManifest, ...],
    inventory: CatalogInventory,
    mode: GovernanceMode,
) -> tuple[GovernanceFinding, ...]:
    """Validate axis legality, reservations, coverage, and skill frontmatter."""

    findings: list[GovernanceFinding] = []
    governed_refs: set[str] = set()
    for manifest in manifests:
        data = manifest.data
        kind = data.get("capability_kind")
        delivery = data.get("delivery")
        lifecycle = data.get("lifecycle")
        for field_name, allowed in (
            ("capability_kind", _CAPABILITY_KINDS),
            ("delivery", _DELIVERIES),
            ("failure_policy", _FAILURE_POLICIES),
            ("lifecycle", _LIFECYCLES),
        ):
            value = data.get(field_name)
            if value not in allowed:
                findings.append(
                    GovernanceFinding(
                        "GOV020",
                        GovernanceSeverity.ERROR,
                        manifest.id,
                        detail=f"{field_name}={value!r} not in {sorted(allowed)}",
                    )
                )
        if kind in _CAPABILITY_KINDS and delivery in _DELIVERIES and (kind, delivery) not in _LEGAL_KIND_DELIVERY:
            findings.append(
                GovernanceFinding(
                    "GOV021",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    detail=f"illegal (kind,delivery)=({kind},{delivery})",
                )
            )
        if kind == "host-job" and lifecycle in _DEPLOYABLE_LIFECYCLES:
            findings.append(
                GovernanceFinding(
                    "GOV022",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    detail="host-job is reserved until ADR-4; deployable/active rejected",
                )
            )
        findings.extend(resolve_artifact_refs(manifest, inventory))
        artifacts = data.get("artifacts", [])
        for artifact in artifacts:
            if isinstance(artifact, dict):
                governed_refs.add(artifact.get("ref", ""))
                if lifecycle == "active" and "provenance" not in artifact:
                    findings.append(
                        GovernanceFinding(
                            "GOV023",
                            GovernanceSeverity.ERROR,
                            manifest.id,
                            artifact.get("ref"),
                            detail="active artifact missing [artifacts.provenance]",
                        )
                    )
                # ADR-2 §6: agent-schedule structure (schedule.toml+PROMPT.md) is
                # enforced by inventory membership, but only if the ref actually
                # points at the schedules component.
                ref = artifact.get("ref", "")
                if kind == "agent-schedule" and isinstance(ref, str) and not ref.startswith("schedules/"):
                    findings.append(
                        GovernanceFinding(
                            "GOV024",
                            GovernanceSeverity.ERROR,
                            manifest.id,
                            ref or None,
                            detail="agent-schedule artifacts must live under schedules/",
                        )
                    )
        if lifecycle == "active":
            for required_field in ("owner", "last_reviewed"):
                if not data.get(required_field):
                    findings.append(
                        GovernanceFinding(
                            "GOV025",
                            GovernanceSeverity.ERROR,
                            manifest.id,
                            detail=f"active capability missing required field {required_field!r}",
                        )
                    )
        # Deferred ADR-2 §6 rule: per-target delivery mutual-exclusivity
        # (plugin XOR settings-fragment for one capability on one target) only
        # becomes checkable once event-handler manifests exist; it lands with
        # the ADR-3 hook-delivery work.

    coverage_severity = GovernanceSeverity.WARNING if mode is GovernanceMode.AUDIT else GovernanceSeverity.ERROR
    for ref in _inventory_refs(inventory):
        if ref not in governed_refs:
            findings.append(
                GovernanceFinding(
                    "GOV030",
                    coverage_severity,
                    None,
                    ref,
                    detail="artifact has no governance manifest",
                )
            )
    for skill in inventory.skills:
        findings.extend(_check_skill_frontmatter(skill.path, f"skills/{skill.name}"))
    return tuple(findings)


def run_governance(inventory: CatalogInventory) -> GovernanceReport:
    """Load policy and manifests, validate, and return one combined report.

    Raises:
        GovernanceError: If the committed policy names a mode this Bridge
            version cannot enforce yet; an unenforceable mode must not pass
            silently.
    """

    root = governance_root(inventory)
    mode = read_governance_mode(root)
    if mode is not GovernanceMode.AUDIT:
        raise GovernanceError(
            f"governance mode {mode.value!r} is committed in policy but not implemented yet; "
            "this Bridge version enforces audit only"
        )
    manifests, load_findings = load_governance(root)
    findings = load_findings + validate_governance(manifests, inventory, mode)
    return GovernanceReport(mode=mode, manifests=manifests, findings=findings)


def build_registry_payload(
    manifests: tuple[GovernanceManifest, ...],
    inventory: CatalogInventory,
) -> dict[str, Any]:
    """Build the deterministic registry payload from manifests and artifacts."""

    capabilities = []
    for manifest in sorted(manifests, key=lambda item: item.id):
        data = manifest.data
        artifacts = []
        for artifact in data.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            ref = artifact.get("ref", "")
            entry: dict[str, Any] = {
                "ref": ref,
                "description": _artifact_description(inventory, ref),
                "computed_artifact_digest": _digest_artifact(inventory, ref),
            }
            if artifact.get("expected_upstream_digest"):
                entry["expected_upstream_digest"] = artifact["expected_upstream_digest"]
            if "provenance" in artifact:
                entry["provenance"] = artifact["provenance"]
            artifacts.append(entry)
        capabilities.append(
            {
                "id": manifest.id,
                "capability_kind": data.get("capability_kind"),
                "delivery": data.get("delivery"),
                "lifecycle": data.get("lifecycle"),
                "quality_tier": data.get("quality_tier"),
                "domains": data.get("domains", []),
                "failure_policy": data.get("failure_policy"),
                "targets": data.get("targets", []),
                "distribution": data.get("distribution", {}),
                "relationships": {
                    "triggers": data.get("triggers", []),
                    "enforces_subset_of": data.get("enforces_subset_of", []),
                    "fallback_skill": data.get("fallback_skill", ""),
                },
                "artifacts": artifacts,
            }
        )
    return {"schema_version": 1, "capabilities": capabilities}


def serialize_registry(payload: dict[str, Any]) -> bytes:
    """Serialize byte-deterministically: sorted keys, UTF-8, LF, no timestamps."""

    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _inventory_refs(inventory: CatalogInventory) -> set[str]:
    return {
        f"{component}/{artifact.name}"
        for component, artifacts in (
            ("hooks", inventory.hooks),
            ("plugins", inventory.plugins),
            ("schedules", inventory.schedules),
            ("settings", inventory.settings),
            ("skills", inventory.skills),
        )
        for artifact in artifacts
    }


def _read_frontmatter(skill_md: Path) -> dict[str, Any] | None:
    if not skill_md.is_file():
        return None
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {"__parse_error__": True}
    return loaded if isinstance(loaded, dict) else None


def _check_skill_frontmatter(skill_dir: Path, ref: str) -> tuple[GovernanceFinding, ...]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return (GovernanceFinding("GOV040", GovernanceSeverity.ERROR, None, ref, detail="skill has no SKILL.md"),)
    frontmatter = _read_frontmatter(skill_md)
    if frontmatter is None:
        return (GovernanceFinding("GOV041", GovernanceSeverity.WARNING, None, ref, detail="no YAML frontmatter"),)
    if frontmatter.get("__parse_error__"):
        return (
            GovernanceFinding("GOV042", GovernanceSeverity.ERROR, None, ref, detail="frontmatter YAML parse error"),
        )
    extra = set(frontmatter) - _ALLOWED_SKILL_FRONTMATTER
    if extra:
        return (
            GovernanceFinding(
                "GOV043",
                GovernanceSeverity.WARNING,
                None,
                ref,
                detail=f"nonstandard top-level frontmatter keys: {sorted(extra)}",
            ),
        )
    return ()


def _digest_artifact(inventory: CatalogInventory, ref: str) -> str:
    base = inventory.root / ref
    digest = hashlib.sha256()
    if base.is_dir():
        # Sort by the POSIX relative path string: sorting Path objects would
        # case-fold on Windows and break cross-platform byte determinism.
        files = sorted(
            (path for path in base.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(base).as_posix(),
        )
    elif base.is_file():
        files = [base]
    else:
        files = []
    for path in files:
        relative = path.relative_to(base).as_posix() if base.is_dir() else path.name
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _artifact_description(inventory: CatalogInventory, ref: str) -> str:
    if ref.startswith("skills/"):
        frontmatter = _read_frontmatter(inventory.root / ref / "SKILL.md")
        if frontmatter and not frontmatter.get("__parse_error__"):
            return str(frontmatter.get("description", ""))
    return ""
