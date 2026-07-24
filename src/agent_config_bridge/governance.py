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
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from agent_config_bridge.catalog import Artifact, CatalogInventory
from agent_config_bridge.models import BridgeConfig, Component, Product, TargetConfig
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "GOVERNANCE_POLICY_FILENAME",
    "GovernanceError",
    "GovernanceFinding",
    "GovernanceManifest",
    "GovernanceMode",
    "GovernanceReport",
    "GovernanceSeverity",
    "ResolvedInventory",
    "build_registry_payload",
    "governance_root",
    "load_governance",
    "read_governance_mode",
    "registry_path",
    "resolve_artifact_refs",
    "resolve_inventory",
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
_PROVENANCE_ORIGINS = frozenset({"local-original", "imported-git", "imported-marketplace", "orca-runtime"})
_REDISTRIBUTIONS = frozenset({"allowed", "blocked"})
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PINNED_GIT_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
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
# No "tools" component exists yet, so kind=tool (ADR-6) manifests cannot carry
# resolvable artifact refs today.
_ARTIFACT_COMPONENTS = ("hooks", "instructions", "plugins", "schedules", "settings", "skills")
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


@dataclass(frozen=True, slots=True)
class ResolvedInventory:
    """The governance-gated desired inventory (ADR-2 §3 runtime resolver).

    Owned by the governance core so plan/apply consume one desired-state
    definition instead of re-deriving it from raw catalog contents.
    """

    report: GovernanceReport
    inventory: CatalogInventory

    def skills_for_target(self, target: TargetConfig) -> tuple[Artifact, ...]:
        """Return the standalone Skills this target should deploy.

        Surface matching is any-overlap: standalone Skills share one
        filesystem root per target, so a manifest cannot exclude one surface
        of a target that has several — declaring ``surfaces = ["cli"]`` does
        not hide the skill from Desktop on a cli+desktop target.
        """

        return self._artifacts_for_target("skills", self.inventory.skills, target)

    def instructions_for_target(self, target: TargetConfig) -> tuple[Artifact, ...]:
        """Return the instruction bundles this target should deploy (ADR-5).

        The gate is the ``skills_for_target`` analogue: in ``required`` mode a
        bundle deploys only when a deployable governing manifest matches this
        target. Surface matching is any-overlap because instruction files share
        one ``config_home`` per target.
        """

        return self._artifacts_for_target("instructions", self.inventory.instructions, target)

    def hooks_for_target(self, target: TargetConfig) -> tuple[Artifact, ...]:
        """Return the hook bundles governance gates in for this single target.

        Note this is the target's *own* gated set. What actually renders into a
        product's shared hook plugin is the product-wide union
        (:meth:`hooks_for_product`), because one plugin serves every enabled
        target of a product on a host. Surface matching is any-overlap like
        skills.
        """

        return self._artifacts_for_target("hooks", self.inventory.hooks, target)

    def hooks_for_product(self, config: BridgeConfig, product: Product) -> tuple[Artifact, ...]:
        """Return the hooks actually delivered to every target of ``product``.

        Hooks render into one plugin per product per host, so the delivered
        set is the union of :meth:`hooks_for_target` over that product's
        enabled hook targets. This is what installs on each such target, so it
        is the correct basis for both rendering and security review.
        """

        included: dict[str, Artifact] = {}
        for target in config.targets:
            if target.enabled and target.product is product and Component.HOOKS in target.components:
                for hook in self.hooks_for_target(target):
                    included[hook.name] = hook
        return tuple(included[name] for name in sorted(included))

    def _artifacts_for_target(
        self,
        component: str,
        artifacts: tuple[Artifact, ...],
        target: TargetConfig,
    ) -> tuple[Artifact, ...]:
        if self.report.mode is GovernanceMode.AUDIT:
            return artifacts
        prefix = f"{component}/"
        desired: set[str] = set()
        for manifest in self.report.manifests:
            data = manifest.data
            if data.get("lifecycle") not in _DEPLOYABLE_LIFECYCLES:
                continue
            if not _manifest_supports_target(data.get("targets", []), target):
                continue
            for artifact in data.get("artifacts", []):
                if isinstance(artifact, dict):
                    ref = artifact.get("ref", "")
                    if isinstance(ref, str) and ref.startswith(prefix):
                        desired.add(ref.removeprefix(prefix))
        return tuple(artifact for artifact in artifacts if artifact.name in desired)


_TARGET_PRODUCTS = frozenset({"codex", "claude-code"})
_TARGET_PLATFORMS = frozenset({"linux", "windows"})
_TARGET_SURFACES = frozenset({"cli", "desktop"})


def _check_target_blocks(
    manifest: GovernanceManifest,
    mode: GovernanceMode,
) -> tuple[GovernanceFinding, ...]:
    """Validate [[targets]] field values so a typo cannot silently match nothing.

    A block with an unknown product/platform, or a missing/empty/unknown
    surfaces list, would pass tomllib and GOV026 yet never match any
    configured target — retracting the capability everywhere without a single
    finding. That is exactly the silent failure required mode exists to
    prevent, so it is an error there (warning in audit).
    """

    severity = GovernanceSeverity.ERROR if mode is GovernanceMode.REQUIRED else GovernanceSeverity.WARNING
    findings: list[GovernanceFinding] = []
    targets = manifest.data.get("targets", [])
    if not isinstance(targets, list):
        return (GovernanceFinding("GOV027", severity, manifest.id, detail="targets must be [[targets]] tables"),)
    for index, block in enumerate(targets):
        if not isinstance(block, dict):
            findings.append(
                GovernanceFinding("GOV027", severity, manifest.id, detail=f"targets[{index}] is not a table")
            )
            continue
        problems: list[str] = []
        if block.get("product") not in _TARGET_PRODUCTS:
            problems.append(f"product={block.get('product')!r} not in {sorted(_TARGET_PRODUCTS)}")
        if block.get("platform") not in _TARGET_PLATFORMS:
            problems.append(f"platform={block.get('platform')!r} not in {sorted(_TARGET_PLATFORMS)}")
        surfaces = block.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces or not set(surfaces) <= _TARGET_SURFACES:
            problems.append(f"surfaces={surfaces!r} must be a non-empty subset of {sorted(_TARGET_SURFACES)}")
        if problems:
            findings.append(
                GovernanceFinding(
                    "GOV027",
                    severity,
                    manifest.id,
                    detail=f"targets[{index}]: " + "; ".join(problems),
                )
            )
    return tuple(findings)


def _manifest_supports_target(targets: object, target: TargetConfig) -> bool:
    if not isinstance(targets, list):
        return False
    for block in targets:
        if not isinstance(block, dict):
            continue
        surfaces = block.get("surfaces", [])
        if (
            block.get("product") == target.product.value
            and block.get("platform") == target.platform.value
            and isinstance(surfaces, list)
            and any(surface.value in surfaces for surface in target.surfaces)
        ):
            return True
    return False


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
    governed_by: dict[str, list[str]] = {}
    for manifest in manifests:
        data = manifest.data
        kind = data.get("capability_kind")
        delivery = data.get("delivery")
        lifecycle = data.get("lifecycle")
        distribution = data.get("distribution")
        redistribution = distribution.get("redistribution") if isinstance(distribution, dict) else None
        if distribution is not None and redistribution not in _REDISTRIBUTIONS:
            findings.append(
                GovernanceFinding(
                    "GOV029",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    detail=f"distribution.redistribution={redistribution!r} not in {sorted(_REDISTRIBUTIONS)}",
                )
            )
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
                governed_by.setdefault(artifact.get("ref", ""), []).append(manifest.id)
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
                provenance = artifact.get("provenance")
                if provenance is not None and (
                    not isinstance(provenance, dict) or provenance.get("origin") not in _PROVENANCE_ORIGINS
                ):
                    origin = provenance.get("origin") if isinstance(provenance, dict) else provenance
                    findings.append(
                        GovernanceFinding(
                            "GOV028",
                            GovernanceSeverity.ERROR,
                            manifest.id,
                            artifact.get("ref"),
                            detail=(f"provenance origin={origin!r} not in {sorted(_PROVENANCE_ORIGINS)}"),
                        )
                    )
                if redistribution == "allowed":
                    findings.extend(_check_redistributable_artifact(manifest, artifact, inventory))
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
        if lifecycle in _DEPLOYABLE_LIFECYCLES and not data.get("targets"):
            # In required mode a deployable manifest with no targets would be
            # silently retracted everywhere; make that loud instead.
            findings.append(
                GovernanceFinding(
                    "GOV026",
                    GovernanceSeverity.ERROR if mode is GovernanceMode.REQUIRED else GovernanceSeverity.WARNING,
                    manifest.id,
                    detail="deployable capability declares no [[targets]]",
                )
            )
        findings.extend(_check_target_blocks(manifest, mode))
        # Deferred ADR-2 §6 rule: per-target delivery mutual-exclusivity
        # (plugin XOR settings-fragment for one capability on one target) only
        # becomes checkable once event-handler manifests exist; it lands with
        # the ADR-3 hook-delivery work.

    coverage_severity = GovernanceSeverity.WARNING if mode is GovernanceMode.AUDIT else GovernanceSeverity.ERROR
    for ref in _inventory_refs(inventory):
        if ref not in governed_by:
            findings.append(
                GovernanceFinding(
                    "GOV030",
                    coverage_severity,
                    None,
                    ref,
                    detail="artifact has no governance manifest",
                )
            )
    for ref, owners in governed_by.items():
        if len(owners) > 1:
            findings.append(
                GovernanceFinding(
                    "GOV031",
                    GovernanceSeverity.WARNING,
                    None,
                    ref or None,
                    detail=f"artifact is governed by multiple manifests: {sorted(owners)}",
                )
            )
    for skill in inventory.skills:
        findings.extend(_check_skill_frontmatter(skill.path, f"skills/{skill.name}"))
    return tuple(findings)


def _check_redistributable_artifact(
    manifest: GovernanceManifest,
    artifact: dict[str, Any],
    inventory: CatalogInventory,
) -> tuple[GovernanceFinding, ...]:
    """Validate the evidence required before an artifact may be redistributed."""

    ref = artifact.get("ref")
    artifact_ref = ref if isinstance(ref, str) and ref else None
    findings: list[GovernanceFinding] = []
    digest = artifact.get("expected_upstream_digest")
    if not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
        findings.append(
            GovernanceFinding(
                "GOV032",
                GovernanceSeverity.ERROR,
                manifest.id,
                artifact_ref,
                detail="redistribution=allowed requires expected_upstream_digest as sha256:<64 lowercase hex>",
            )
        )

    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        findings.append(
            GovernanceFinding(
                "GOV033",
                GovernanceSeverity.ERROR,
                manifest.id,
                artifact_ref,
                detail="redistribution=allowed requires [artifacts.provenance]",
            )
        )
        return tuple(findings)

    source_problems: list[str] = []
    if provenance.get("origin") != "imported-git":
        source_problems.append("origin='imported-git'")
    source_url = provenance.get("source_url")
    if (
        not isinstance(source_url, str)
        or not source_url.strip()
        or any(character.isspace() for character in source_url)
    ):
        source_problems.append("non-empty source_url without whitespace")
    source_subpath = provenance.get("source_subpath")
    if not isinstance(source_subpath, str) or not source_subpath.strip():
        source_problems.append("non-empty source_subpath")
    else:
        parsed_subpath = PurePosixPath(source_subpath)
        if parsed_subpath.is_absolute() or parsed_subpath == PurePosixPath(".") or ".." in parsed_subpath.parts:
            source_problems.append("relative source_subpath without parent traversal")
    revision = provenance.get("source_revision")
    if not isinstance(revision, str) or _PINNED_GIT_REVISION.fullmatch(revision) is None:
        source_problems.append("source_revision (full 40-64 hex commit)")
    if source_problems:
        findings.append(
            GovernanceFinding(
                "GOV034",
                GovernanceSeverity.ERROR,
                manifest.id,
                artifact_ref,
                detail="redistribution=allowed requires pinned imported-git provenance: " + ", ".join(source_problems),
            )
        )

    license_concluded = provenance.get("license_concluded")
    rights_basis = provenance.get("rights_basis")
    if (
        not isinstance(license_concluded, str)
        or not license_concluded.strip()
        or license_concluded == "NOASSERTION"
        or not isinstance(rights_basis, str)
        or not rights_basis.strip()
        or rights_basis == "none"
    ):
        findings.append(
            GovernanceFinding(
                "GOV035",
                GovernanceSeverity.ERROR,
                manifest.id,
                artifact_ref,
                detail="redistribution=allowed requires a concluded license and non-none rights_basis",
            )
        )

    for field in ("license_evidence", "attribution_files"):
        declared_paths = provenance.get(field)
        if (
            not isinstance(declared_paths, list)
            or not declared_paths
            or any(not isinstance(value, str) or not value.strip() for value in declared_paths)
        ):
            findings.append(
                GovernanceFinding(
                    "GOV036",
                    GovernanceSeverity.ERROR,
                    manifest.id,
                    artifact_ref,
                    detail=f"redistribution=allowed requires a non-empty {field} string list",
                )
            )
            continue
        if artifact_ref is None:
            continue
        artifact_root = inventory.root / artifact_ref
        for declared_path in declared_paths:
            assert isinstance(declared_path, str)
            problem = _evidence_path_problem(artifact_root, declared_path)
            if problem is not None:
                findings.append(
                    GovernanceFinding(
                        "GOV037",
                        GovernanceSeverity.ERROR,
                        manifest.id,
                        artifact_ref,
                        detail=f"{field} path {declared_path!r} {problem}",
                    )
                )
    return tuple(findings)


def _evidence_path_problem(artifact_root: Path, declared_path: str) -> str | None:
    """Return why an evidence path is not a contained real regular file."""

    relative = Path(declared_path)
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        return "must be a relative path without parent traversal"
    candidate = artifact_root / relative
    try:
        resolved_root = artifact_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return "does not resolve to an existing file"
    if not resolved.is_relative_to(resolved_root):
        return "resolves outside the governed artifact root"
    current = artifact_root
    for part in relative.parts:
        current /= part
        try:
            redirected = current.is_symlink() or (current.is_dir() and is_directory_reparse_point(current))
        except OSError:
            return "cannot be inspected safely"
        if redirected:
            return "must not traverse a symlink, junction, or reparse point"
    if not candidate.is_file() or candidate.is_symlink():
        return "must resolve to a real regular file"
    return None


def run_governance(inventory: CatalogInventory) -> GovernanceReport:
    """Load policy and manifests, validate, and return one combined report.

    Raises:
        GovernanceError: If the committed policy names a mode this Bridge
            version cannot enforce yet; an unenforceable mode must not pass
            silently.
    """

    root = governance_root(inventory)
    mode = read_governance_mode(root)
    if mode is GovernanceMode.PUBLIC_EXPORT:
        raise GovernanceError(
            "governance mode 'public-export' is committed in policy but not implemented yet; "
            "this Bridge version enforces audit and required only"
        )
    manifests, load_findings = load_governance(root)
    findings = load_findings + validate_governance(manifests, inventory, mode)
    return GovernanceReport(mode=mode, manifests=manifests, findings=findings)


def resolve_inventory(inventory: CatalogInventory) -> ResolvedInventory:
    """Resolve the governance-gated desired inventory for runtime consumers.

    In ``audit`` mode governance never gates runtime selection, so the desired
    set is the full catalog inventory. In ``required`` mode a standalone Skill
    is desired only when a governing manifest exists, its ``lifecycle`` is
    deployable (active/deprecated), and the consuming target matches one of
    the manifest's ``[[targets]]`` blocks.

    Raises:
        GovernanceError: In ``required`` mode when governance findings contain
            any error — a desired state derived from an invalid ledger would
            silently deploy or retract the wrong artifacts.
    """

    report = run_governance(inventory)
    if report.mode is GovernanceMode.REQUIRED and report.has_error:
        errors = sorted({finding.code for finding in report.findings if finding.severity is GovernanceSeverity.ERROR})
        raise GovernanceError(
            "governance mode is 'required' but the ledger has errors "
            f"({', '.join(errors)}); run `agentbridge registry check` and fix them before planning"
        )
    return ResolvedInventory(report=report, inventory=inventory)


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
            ("instructions", inventory.instructions),
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
