"""Discover and validate canonical Agent Config Bridge catalog artifacts."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_config_bridge.models import BridgeConfig
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "Artifact",
    "CatalogError",
    "CatalogInventory",
    "discover_catalog",
    "is_windows_portable_path_component",
]

_ARTIFACT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_HOOK_PLUGIN_NAME = "agent-config-bridge-hooks"
_MANAGED_MARKER = ".agent-config-bridge.json"
_INSTRUCTION_ROOT_MARKER = "AGENTBRIDGE-MANAGED.json"
# ADR-5 decision 2: instructions deploy to overlay-relative paths under a
# product config_home, gated by this per-product destination allowlist so the
# component cannot become an arbitrary config-home file writer.
_INSTRUCTION_ALLOWED_ROOT_FILES = {
    "claude-code": frozenset({"CLAUDE.md"}),
    "codex": frozenset({"AGENTS.md"}),
}
_INSTRUCTION_ALLOWED_DIRS = {
    "claude-code": frozenset({"rules", "agents", "commands"}),
    "codex": frozenset({"agents"}),
}
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"\\|?*')


class CatalogError(ValueError):
    """Raised when a canonical catalog is malformed."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """A named artifact directory in the canonical catalog."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class CatalogInventory:
    """Validated component artifacts discovered in a canonical catalog."""

    root: Path
    skills: tuple[Artifact, ...]
    plugins: tuple[Artifact, ...]
    hooks: tuple[Artifact, ...]
    settings: tuple[Artifact, ...]
    schedules: tuple[Artifact, ...]
    hook_version: str | None
    instructions: tuple[Artifact, ...] = ()


def discover_catalog(config: BridgeConfig) -> CatalogInventory:
    """Discover catalog artifacts and validate their required entry points.

    Args:
        config: Loaded bridge configuration.

    Returns:
        A deterministic catalog inventory sorted by artifact name.

    Raises:
        CatalogError: If the catalog or an artifact is malformed.
    """

    root = _strict_resolve(config.catalog, "catalog")
    if not root.is_dir():
        raise CatalogError(f"catalog is not a directory: {config.catalog}")

    skills = _discover_group(root / "skills", root, _validate_skill)
    plugins = _discover_group(root / "plugins", root, _validate_plugin)
    hooks = _discover_group(root / "hooks", root, _validate_hook)
    settings = _discover_group(root / "settings", root, _validate_settings)
    schedules = _discover_group(root / "schedules", root, _validate_schedule)
    instructions = _discover_group(root / "instructions", root, _validate_instructions)
    _validate_instruction_collisions(instructions)
    hook_version = _read_hook_version(root / "hooks", root, required=bool(hooks))
    return CatalogInventory(
        root=root,
        skills=skills,
        plugins=plugins,
        hooks=hooks,
        settings=settings,
        schedules=schedules,
        hook_version=hook_version,
        instructions=instructions,
    )


def _discover_group(
    group: Path,
    catalog_root: Path,
    validator: Callable[[Path], None],
) -> tuple[Artifact, ...]:
    if not _lexists(group):
        return ()
    _reject_directory_link(group, "catalog group")
    group_root = _resolve_within(group, catalog_root, "catalog group")
    if not group_root.is_dir():
        raise CatalogError(f"catalog group is not a directory: {group}")

    artifact_paths = tuple(
        path for path in sorted(group.iterdir(), key=lambda item: item.name) if not path.name.startswith(".")
    )
    portable_names: dict[str, str] = {}
    for path in artifact_paths:
        portable_name = _windows_portable_name(path.name)
        if previous := portable_names.get(portable_name):
            raise CatalogError(
                f"artifact names collide on case-insensitive filesystems: {previous!r} and {path.name!r} in {group}"
            )
        portable_names[portable_name] = path.name

    artifacts: list[Artifact] = []
    for path in artifact_paths:
        _reject_directory_link(path, "artifact root")
        artifact_root = _resolve_within(path, group_root, "artifact root")
        if not artifact_root.is_dir():
            raise CatalogError(f"artifact must be a directory: {path}")
        _validate_name(path.name, path)
        _validate_artifact_tree(artifact_root)
        validator(path)
        artifacts.append(Artifact(name=path.name, path=artifact_root))
    return tuple(artifacts)


def _validate_name(name: str, path: Path) -> None:
    if _windows_portable_name(name) in _WINDOWS_DEVICE_NAMES:
        raise CatalogError(f"artifact name is reserved on Windows: {path}")
    if not _ARTIFACT_NAME.fullmatch(name):
        raise CatalogError(f"artifact name must be kebab-case: {path}")


def _validate_skill(path: Path) -> None:
    marker = path / _MANAGED_MARKER
    if _lexists(marker):
        raise CatalogError(f"Skill root path is reserved for managed-copy ownership metadata: {marker}")
    skill = path / "SKILL.md"
    if not skill.is_file():
        raise CatalogError(f"skill is missing SKILL.md: {path}")
    try:
        lines = skill.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CatalogError(f"cannot read Skill manifest {skill}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        raise CatalogError(f"Skill manifest must start with YAML frontmatter: {skill}")
    try:
        closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise CatalogError(f"Skill manifest has no closing YAML frontmatter delimiter: {skill}") from exc
    frontmatter: dict[str, str] = {}
    current_key: str | None = None
    continuations: dict[str, list[str]] = {}
    for line in lines[1:closing_index]:
        if line[:1].isspace():
            if current_key in {"name", "description"} and line.strip():
                continuations.setdefault(current_key, []).append(line.strip())
            continue
        if ":" not in line:
            current_key = None
            continue
        key, value = line.split(":", maxsplit=1)
        current_key = key.strip()
        frontmatter[current_key] = value.strip()
    for key, parts in continuations.items():
        prefix = frontmatter.get(key, "")
        if prefix in {"", ">", ">-", ">+", "|", "|-", "|+"}:
            frontmatter[key] = " ".join(parts)
        elif parts:
            frontmatter[key] = " ".join((prefix, *parts))
    raw_name = frontmatter.get("name", "")
    name = raw_name[1:-1] if len(raw_name) >= 2 and raw_name[0] == raw_name[-1] and raw_name[0] in "\"'" else raw_name
    if name != path.name:
        raise CatalogError(f"Skill frontmatter name {name!r} does not match directory {path.name!r}: {skill}")
    if not frontmatter.get("description"):
        raise CatalogError(f"Skill frontmatter must contain a description: {skill}")


def _validate_plugin(path: Path) -> None:
    if path.name == _HOOK_PLUGIN_NAME:
        raise CatalogError(f"plugin name is reserved for generated hooks: {path}")

    source_roots = (path / "common", path / "codex", path / "claude-code")
    if not any(candidate.is_dir() for candidate in source_roots):
        raise CatalogError(f"plugin must contain common/, codex/, or claude-code/ source overlays: {path}")

    forbidden_metadata_namespaces = (
        (path / "common", ".codex-plugin", "Codex metadata namespace must not be placed under common/"),
        (path / "common", ".claude-plugin", "Claude Code metadata namespace must not be placed under common/"),
        (path / "codex", ".claude-plugin", "Claude Code metadata namespace must not be placed under codex/"),
        (path / "claude-code", ".codex-plugin", "Codex metadata namespace must not be placed under claude-code/"),
    )
    for overlay, namespace_name, message in forbidden_metadata_namespaces:
        namespace = _find_portable_child(overlay, namespace_name)
        if namespace is not None:
            raise CatalogError(f"{message}: {namespace}")

    codex_manifest = path / "codex" / ".codex-plugin" / "plugin.json"
    claude_manifest = path / "claude-code" / ".claude-plugin" / "plugin.json"

    codex_version = _validate_plugin_manifest(codex_manifest, path.name, "Codex")
    claude_version = _validate_plugin_manifest(claude_manifest, path.name, "Claude Code")
    if codex_version != claude_version:
        raise CatalogError(
            f"plugin manifest versions do not match for {path.name!r}: "
            f"Codex has {codex_version!r}, Claude Code has {claude_version!r}"
        )


def _validate_hook(path: Path) -> None:
    candidates = (
        path / "common" / "hooks.json",
        path / "codex" / "hooks.json",
        path / "claude-code" / "hooks.json",
    )
    existing = tuple(candidate for candidate in candidates if candidate.is_file())
    if not existing:
        raise CatalogError(f"hook must contain common/hooks.json or a product override: {path}")
    for candidate in existing:
        payload = _load_json(candidate)
        if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), dict):
            raise CatalogError(f"hook document must contain a hooks object: {candidate}")
        _validate_hook_groups(payload["hooks"], candidate)


def _validate_hook_groups(hooks: dict[Any, Any], path: Path) -> None:
    for event, groups in hooks.items():
        if not isinstance(event, str) or not event:
            raise CatalogError(f"hook event names must be non-empty strings: {path}")
        if not isinstance(groups, list):
            raise CatalogError(f"hook event {event!r} must map to an array of matcher groups: {path}")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise CatalogError(f"hook event {event!r} contains an invalid matcher group: {path}")
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise CatalogError(f"hook event {event!r} matcher must be a string: {path}")
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
                    raise CatalogError(f"hook event {event!r} contains an invalid handler: {path}")
                if handler["type"] == "command" and (
                    not isinstance(handler.get("command"), str) or not handler["command"]
                ):
                    raise CatalogError(f"command hook for event {event!r} must contain a command: {path}")
                timeout = handler.get("timeout")
                if timeout is not None and (
                    isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
                ):
                    raise CatalogError(f"hook timeout for event {event!r} must be a positive number: {path}")


def _validate_settings(path: Path) -> None:
    allowed_roots = {"codex", "claude-code"}
    children = {child.name for child in path.iterdir()}
    unknown = sorted(children - allowed_roots)
    if unknown:
        raise CatalogError(
            f"settings bundle contains unsupported roots {', '.join(repr(name) for name in unknown)}: {path}"
        )

    codex_fragment = path / "codex" / "config.toml"
    claude_fragment = path / "claude-code" / "settings.json"
    if not codex_fragment.is_file() and not claude_fragment.is_file():
        raise CatalogError(f"settings bundle must contain codex/config.toml or claude-code/settings.json: {path}")

    for product_root, expected_name in ((path / "codex", "config.toml"), (path / "claude-code", "settings.json")):
        if not product_root.exists():
            continue
        if product_root.is_symlink() or not product_root.is_dir():
            raise CatalogError(f"settings product root must be a real directory: {product_root}")
        entries = tuple(product_root.iterdir())
        if len(entries) != 1 or entries[0].name != expected_name or entries[0].is_symlink():
            raise CatalogError(f"settings product root may contain only a real {expected_name}: {product_root}")

    if codex_fragment.is_file():
        try:
            with codex_fragment.open("rb") as stream:
                payload = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CatalogError(f"invalid Codex settings fragment: {codex_fragment}: {exc}") from exc
        if not payload:
            raise CatalogError(f"Codex settings fragment must contain at least one setting: {codex_fragment}")

    if claude_fragment.is_file():
        payload = _load_json(claude_fragment)
        if not isinstance(payload, dict) or not payload:
            raise CatalogError(f"Claude Code settings fragment must contain a non-empty object: {claude_fragment}")


def _validate_schedule(path: Path) -> None:
    expected = {"schedule.toml", "PROMPT.md"}
    children = {child.name for child in path.iterdir()}
    if children != expected:
        raise CatalogError(
            f"schedule must contain exactly schedule.toml and PROMPT.md: {path}; "
            f"found {', '.join(sorted(children)) or 'nothing'}"
        )
    document = path / "schedule.toml"
    prompt = path / "PROMPT.md"
    if document.is_symlink() or prompt.is_symlink() or not document.is_file() or not prompt.is_file():
        raise CatalogError(f"schedule sources must be real regular files: {path}")
    try:
        with document.open("rb") as stream:
            payload = tomllib.load(stream)
        prompt_text = prompt.read_text(encoding="utf-8")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"invalid schedule source: {path}: {exc}") from exc
    if not payload:
        raise CatalogError(f"schedule.toml must contain a schedule definition: {document}")
    if not prompt_text.strip():
        raise CatalogError(f"schedule prompt must not be empty: {prompt}")


def _validate_instructions(path: Path) -> None:
    """Validate one instruction bundle's product overlays (ADR-5).

    There is deliberately no ``common/`` cross-product overlay: the products
    consume differently shaped files, and a shared-source merge is exactly the
    content transformation the Bridge refuses elsewhere.
    """

    allowed_roots = set(_INSTRUCTION_ALLOWED_ROOT_FILES)
    children = {child.name for child in path.iterdir()}
    unknown = sorted(children - allowed_roots)
    if unknown:
        raise CatalogError(
            f"instruction bundle contains unsupported roots {', '.join(repr(name) for name in unknown)}: {path}"
        )
    overlays = tuple(path / product for product in sorted(allowed_roots) if (path / product).is_dir())
    if not overlays:
        raise CatalogError(f"instruction bundle must contain a codex/ or claude-code/ product overlay: {path}")

    for overlay in overlays:
        files = tuple(sorted(candidate for candidate in overlay.rglob("*") if not candidate.is_dir()))
        if not files:
            raise CatalogError(f"instruction product overlay contains no files: {overlay}")
        for file in files:
            relative = file.relative_to(overlay)
            _validate_instruction_destination(relative, overlay.name, file)
            _validate_instruction_content(file)


def _validate_instruction_destination(relative: Path, product: str, file: Path) -> None:
    # Only the top-level segment needs allowlisting here: byte-level path
    # safety for every nested segment (Windows-invalid characters, reserved
    # device names, case-insensitive collisions) is guaranteed earlier by
    # _validate_artifact_tree, which runs for the whole artifact before this
    # validator. Reordering or removing that call would reopen the gap.
    parts = relative.parts
    if relative.name == _INSTRUCTION_ROOT_MARKER:
        raise CatalogError(f"instruction file name is reserved for bridge provenance markers: {file}")
    if len(parts) == 1:
        if parts[0] not in _INSTRUCTION_ALLOWED_ROOT_FILES[product]:
            raise CatalogError(
                f"instruction destination {relative.as_posix()!r} is not allowed for {product} "
                f"(allowed root files: {sorted(_INSTRUCTION_ALLOWED_ROOT_FILES[product])}, "
                f"allowed directories: {sorted(_INSTRUCTION_ALLOWED_DIRS[product])}): {file}"
            )
        return
    if parts[0] not in _INSTRUCTION_ALLOWED_DIRS[product]:
        raise CatalogError(
            f"instruction destination {relative.as_posix()!r} is outside the allowed {product} "
            f"directories {sorted(_INSTRUCTION_ALLOWED_DIRS[product])}: {file}"
        )


def _validate_instruction_content(file: Path) -> None:
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read instruction file {file}: {exc}") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        raise CatalogError(f"instruction file must be UTF-8 without BOM: {file}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError(f"instruction file must be valid UTF-8: {file}: {exc}") from exc
    if not text.strip():
        raise CatalogError(f"instruction file must not be empty: {file}")


def _validate_instruction_collisions(instructions: tuple[Artifact, ...]) -> None:
    """Reject two bundles shipping one destination relpath for one product.

    Per-file single ownership (ADR-5 decision 3) is a catalog-wide invariant,
    not a selection-time concern: governance gating must never be what keeps
    two artifacts from owning the same destination file.
    """

    # Case-folded keys: two bundles shipping relpaths that differ only by case
    # would collide on a case-insensitive destination filesystem, so treat
    # that as the same catalog-time single-ownership violation.
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for bundle in instructions:
        for product in sorted(_INSTRUCTION_ALLOWED_ROOT_FILES):
            overlay = bundle.path / product
            if not overlay.is_dir():
                continue
            for file in sorted(candidate for candidate in overlay.rglob("*") if not candidate.is_dir()):
                relpath = file.relative_to(overlay).as_posix()
                key = (product, relpath.casefold())
                if previous := owners.get(key):
                    previous_bundle, previous_relpath = previous
                    raise CatalogError(
                        f"instruction destination {relpath!r} for {product} collides with "
                        f"{previous_relpath!r}: owned by both {previous_bundle!r} and {bundle.name!r}; "
                        "each destination file has exactly one owning bundle"
                    )
                owners[key] = (bundle.name, relpath)


def _validate_plugin_manifest(path: Path, artifact_name: str, product_name: str) -> str:
    if not path.is_file():
        raise CatalogError(f"plugin has no {product_name} manifest overlay: {path.parent.parent.parent}")

    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        raise CatalogError(f"plugin manifest must contain a string name: {path}")
    if payload["name"] != artifact_name:
        raise CatalogError(
            f"plugin manifest name {payload['name']!r} does not match directory {artifact_name!r}: {path}"
        )

    version = payload.get("version")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise CatalogError(f"plugin manifest version must be strict SemVer: {path}")
    return version


def _read_hook_version(group: Path, catalog_root: Path, *, required: bool) -> str | None:
    version_file = group / ".version"
    if not _lexists(version_file):
        if required:
            raise CatalogError(f"hooks catalog is missing required strict-SemVer .version file: {version_file}")
        return None

    group_root = _resolve_within(group, catalog_root, "hooks catalog group")
    resolved = _resolve_within(version_file, group_root, "hooks version file")
    if not resolved.is_file():
        raise CatalogError(f"hooks .version is not a file: {version_file}")
    try:
        version = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CatalogError(f"cannot read hooks .version file {version_file}: {exc}") from exc
    if not _SEMVER.fullmatch(version):
        raise CatalogError(f"hooks .version must contain strict SemVer: {version_file}")
    return version


def _validate_artifact_tree(root: Path) -> None:
    def raise_walk_error(exc: OSError) -> None:
        raise CatalogError(f"cannot inspect artifact tree {root}: {exc}") from exc

    for directory, directory_names, file_names in os.walk(root, followlinks=False, onerror=raise_walk_error):
        current = Path(directory)
        portable_names: dict[str, str] = {}
        for name in (*directory_names, *file_names):
            candidate = current / name
            _validate_windows_path_component(name, candidate)
            portable_name = name.rstrip(" .").casefold()
            if previous := portable_names.get(portable_name):
                raise CatalogError(
                    f"paths collide on case-insensitive filesystems: {previous!r} and {name!r} in {current}"
                )
            portable_names[portable_name] = name
            try:
                directory_reparse_point = is_directory_reparse_point(candidate)
            except OSError as exc:
                raise CatalogError(f"cannot inspect artifact path {candidate}: {exc}") from exc
            if directory_reparse_point:
                raise CatalogError(
                    f"directory symlinks are not supported; junctions are not supported "
                    f"in catalog artifacts: {candidate}"
                )
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise CatalogError(f"cannot inspect artifact path {candidate}: {exc}") from exc
            if stat.S_ISLNK(mode):
                resolved = _resolve_within(candidate, root, "artifact symlink")
                try:
                    target_mode = resolved.stat().st_mode
                except OSError as exc:
                    raise CatalogError(
                        f"cannot inspect artifact symlink target {candidate} -> {resolved}: {exc}"
                    ) from exc
                if stat.S_ISDIR(target_mode):
                    raise CatalogError(f"directory symlinks are not supported in catalog artifacts: {candidate}")
                if not stat.S_ISREG(target_mode):
                    raise CatalogError(
                        f"artifact symlink target must be a contained regular file; "
                        f"unsupported target at {candidate} -> {resolved}"
                    )
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise CatalogError(
                    f"unsupported filesystem node in catalog artifact: {candidate}; "
                    "only real directories, regular files, and contained symlinks to regular files are allowed"
                )


def is_windows_portable_path_component(name: str) -> bool:
    """Return whether one path component is portable and safe on Windows.

    Shared with ownership-state validation so a state-file path segment can
    never smuggle a drive-letter (``D:evil``), reserved device name, or other
    component the catalog itself would reject.
    """

    if not name or name != name.rstrip(" ."):
        return False
    if any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in name):
        return False
    return _windows_portable_name(name) not in _WINDOWS_DEVICE_NAMES


def _validate_windows_path_component(name: str, path: Path) -> None:
    if name != name.rstrip(" ."):
        raise CatalogError(f"path component ends in a dot or space and is not portable to Windows: {path}")
    if any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in name):
        raise CatalogError(f"path component contains a character that is not portable to Windows: {path}")
    if _windows_portable_name(name) in _WINDOWS_DEVICE_NAMES:
        raise CatalogError(f"path component is reserved on Windows: {path}")


def _find_portable_child(directory: Path, name: str) -> Path | None:
    """Return a direct child matching ``name`` under Windows path semantics."""

    if not directory.is_dir():
        return None
    portable_name = name.rstrip(" .").casefold()
    try:
        return next(
            (candidate for candidate in directory.iterdir() if candidate.name.rstrip(" .").casefold() == portable_name),
            None,
        )
    except OSError as exc:
        raise CatalogError(f"cannot inspect product overlay {directory}: {exc}") from exc


def _reject_directory_link(path: Path, label: str) -> None:
    try:
        directory_reparse_point = is_directory_reparse_point(path)
    except OSError as exc:
        raise CatalogError(f"cannot inspect {label} {path}: {exc}") from exc
    if path.is_symlink() or directory_reparse_point:
        raise CatalogError(f"{label} must not be a directory symlink or junction: {path}")


def _resolve_within(path: Path, root: Path, label: str) -> Path:
    resolved = _strict_resolve(path, label)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CatalogError(f"{label} escapes {root}: {path} -> {resolved}") from exc
    return resolved


def _strict_resolve(path: Path, label: str) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CatalogError(f"{label} is missing, broken, or unresolvable: {path}: {exc}") from exc


def _lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _windows_portable_name(name: str) -> str:
    return name.rstrip(" .").split(".", maxsplit=1)[0].casefold()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"invalid JSON file {path}: {exc}") from exc
