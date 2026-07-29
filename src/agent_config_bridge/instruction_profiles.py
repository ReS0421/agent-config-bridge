"""Deterministic Codex developer-instruction profile projections.

An Instruction bundle may declare ``[[codex_profiles]]`` in its top-level
``projections.toml``.  Each declaration projects one canonical direct
``codex/model-instructions/*.md`` source into ``codex/<name>.config.toml``.
The generated file is ordinary Instruction content after validation; the
descriptor itself is metadata and is never deployed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "CodexProfileProjection",
    "InstructionProfileError",
    "InstructionProfileReport",
    "InstructionProfileResult",
    "check_instruction_profile_bundle",
    "check_instruction_profiles",
    "generate_instruction_profiles",
]

_DESCRIPTOR_NAME = "projections.toml"
_PROFILE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"\\|?*')


class InstructionProfileError(ValueError):
    """Raised when a profile descriptor, source, or generated output is unsafe."""


@dataclass(frozen=True, slots=True)
class CodexProfileProjection:
    """One validated descriptor entry and its deterministic output."""

    bundle: str
    name: str
    source_relpath: str
    destination_relpath: str
    source: Path
    destination: Path
    expected: bytes
    prompt: str


@dataclass(frozen=True, slots=True)
class InstructionProfileResult:
    """One content-free generate/check result."""

    bundle: str
    name: str
    source: str
    destination: str
    status: str


@dataclass(frozen=True, slots=True)
class InstructionProfileReport:
    """Content-free aggregate status for Catalog profile projections."""

    catalog: Path
    profiles: tuple[InstructionProfileResult, ...]
    changed: int
    valid: bool


def generate_instruction_profiles(catalog_root: Path) -> InstructionProfileReport:
    """Atomically create or update every declared Codex profile output."""

    root, projections = _scan_catalog(catalog_root)
    results: list[InstructionProfileResult] = []
    changed = 0
    for projection in projections:
        destination = projection.destination
        _require_unchanged_source(projection, "generation")
        if _lexists(destination):
            _require_real_regular_file(destination, "generated Codex profile destination")
            current = _read_real_regular_bytes(destination, "generated Codex profile")
            if current == projection.expected:
                status = "current"
            else:
                _atomic_write(destination, projection.expected)
                status = "updated"
                changed += 1
        else:
            _atomic_write(destination, projection.expected)
            status = "created"
            changed += 1
        results.append(_result(projection, status))
    return InstructionProfileReport(
        catalog=root,
        profiles=tuple(results),
        changed=changed,
        valid=True,
    )


def check_instruction_profiles(catalog_root: Path) -> InstructionProfileReport:
    """Strictly byte-compare generated profiles without writing any path."""

    root, projections = _scan_catalog(catalog_root)
    results: list[InstructionProfileResult] = []
    valid = True
    for projection in projections:
        _require_unchanged_source(projection, "check")
        status = _profile_status(projection)
        if status != "current":
            valid = False
        results.append(_result(projection, status))
    return InstructionProfileReport(
        catalog=root,
        profiles=tuple(results),
        changed=0,
        valid=valid,
    )


def check_instruction_profile_bundle(bundle: Path) -> tuple[CodexProfileProjection, ...]:
    """Validate one bundle and require every declared output to be current."""

    bundle_root = _require_real_directory(bundle, "instruction bundle")
    projections = _load_bundle_projections(bundle_root)
    _validate_declared_output_set(bundle_root, projections)
    for projection in projections:
        _require_unchanged_source(projection, "Catalog validation")
        status = _profile_status(projection)
        if status != "current":
            raise InstructionProfileError(
                f"generated Codex profile is {status}: {projection.destination}; "
                "run 'agentbridge instructions generate'"
            )
    return projections


def _scan_catalog(catalog_root: Path) -> tuple[Path, tuple[CodexProfileProjection, ...]]:
    root = _require_real_directory(catalog_root, "catalog")
    instructions = root / "instructions"
    if not _lexists(instructions):
        return root, ()
    instructions_root = _require_real_directory(instructions, "catalog instructions group")

    projections: list[CodexProfileProjection] = []
    destinations: dict[str, tuple[str, str]] = {}
    for entry in sorted(instructions_root.iterdir(), key=lambda path: path.name):
        if entry.name.startswith("."):
            continue
        bundle = _require_real_directory(entry, "instruction bundle")
        bundle_projections = _load_bundle_projections(bundle)
        _validate_declared_output_set(bundle, bundle_projections)
        for projection in bundle_projections:
            key = projection.destination.name.casefold()
            if previous := destinations.get(key):
                previous_bundle, previous_name = previous
                raise InstructionProfileError(
                    "generated Codex profile destinations collide on case-insensitive filesystems: "
                    f"{previous_bundle!r}/{previous_name!r} and "
                    f"{projection.bundle!r}/{projection.destination.name!r}"
                )
            destinations[key] = (projection.bundle, projection.destination.name)
            projections.append(projection)
    return root, tuple(projections)


def _load_bundle_projections(bundle: Path) -> tuple[CodexProfileProjection, ...]:
    descriptor = bundle / _DESCRIPTOR_NAME
    if not _lexists(descriptor):
        return ()
    try:
        payload = tomllib.loads(_read_real_regular_bytes(descriptor, "instruction profile descriptor").decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstructionProfileError(f"invalid instruction profile descriptor {descriptor}: {exc}") from exc

    if set(payload) != {"schema_version", "codex_profiles"}:
        raise InstructionProfileError(
            f"instruction profile descriptor must contain exactly schema_version and codex_profiles: {descriptor}"
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise InstructionProfileError(f"instruction profile descriptor schema_version must be integer 1: {descriptor}")
    entries = payload["codex_profiles"]
    if not isinstance(entries, list) or not entries:
        raise InstructionProfileError(
            f"instruction profile descriptor codex_profiles must be a non-empty array of tables: {descriptor}"
        )

    raw_entries: list[tuple[str, str, int]] = []
    names: dict[str, tuple[str, int]] = {}
    for index, entry in enumerate(entries):
        context = f"{descriptor}: codex_profiles[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"name", "source"}:
            raise InstructionProfileError(f"{context} must contain exact keys name and source")
        name = entry["name"]
        source_relpath = entry["source"]
        if not isinstance(name, str) or not name:
            raise InstructionProfileError(f"{context}.name must be a non-empty string")
        if not isinstance(source_relpath, str) or not source_relpath:
            raise InstructionProfileError(f"{context}.source must be a non-empty string")
        key = name.casefold()
        if previous := names.get(key):
            previous_name, previous_index = previous
            raise InstructionProfileError(
                f"{descriptor}: profile names collide on case-insensitive filesystems: "
                f"codex_profiles[{previous_index}]={previous_name!r} and codex_profiles[{index}]={name!r}"
            )
        names[key] = (name, index)
        raw_entries.append((name, source_relpath, index))

    projections = [
        _build_projection(bundle, descriptor, name, source_relpath, index)
        for name, source_relpath, index in raw_entries
    ]
    return tuple(sorted(projections, key=lambda item: item.destination_relpath))


def _build_projection(
    bundle: Path,
    descriptor: Path,
    name: str,
    source_relpath: str,
    index: int,
) -> CodexProfileProjection:
    context = f"{descriptor}: codex_profiles[{index}]"
    _validate_profile_name(name, context)
    parsed_source = PurePosixPath(source_relpath)
    if (
        "\\" in source_relpath
        or parsed_source.as_posix() != source_relpath
        or parsed_source.is_absolute()
        or len(parsed_source.parts) != 3
        or parsed_source.parts[:2] != ("codex", "model-instructions")
        or parsed_source.suffix != ".md"
        or len(parsed_source.name) <= len(".md")
    ):
        raise InstructionProfileError(f"{context}.source must be a direct codex/model-instructions/*.md path")
    source = bundle.joinpath(*parsed_source.parts)
    _validate_portable_source_name(parsed_source.name, context)
    _require_contained_real_source(bundle, source, context)
    normalized = _read_normalized_prompt(source)
    destination_relpath = f"codex/{name}.config.toml"
    destination = bundle / "codex" / f"{name}.config.toml"
    return CodexProfileProjection(
        bundle=bundle.name,
        name=name,
        source_relpath=source_relpath,
        destination_relpath=destination_relpath,
        source=source,
        destination=destination,
        expected=_render_profile(normalized),
        prompt=normalized,
    )


def _validate_profile_name(name: str, context: str) -> None:
    if _PROFILE_NAME.fullmatch(name) is None:
        raise InstructionProfileError(f"{context}.name must be a portable lowercase kebab-case identifier")
    if name.casefold() in _WINDOWS_DEVICE_NAMES:
        raise InstructionProfileError(f"{context}.name is reserved on Windows")
    if any(character in _WINDOWS_INVALID_CHARACTERS for character in name):
        raise InstructionProfileError(f"{context}.name is not portable to Windows")


def _validate_portable_source_name(name: str, context: str) -> None:
    if (
        not name
        or name.endswith((" ", "."))
        or any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in name)
        or name.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
    ):
        raise InstructionProfileError(f"{context}.source filename is not portable to Windows")


def _require_contained_real_source(bundle: Path, source: Path, context: str) -> None:
    for directory in (bundle / "codex", bundle / "codex" / "model-instructions"):
        _require_real_directory(directory, f"{context}.source parent")
    _require_real_regular_file(source, f"{context}.source")
    try:
        source.resolve(strict=True).relative_to(bundle.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstructionProfileError(f"{context}.source escapes its instruction bundle: {source}") from exc


def _read_normalized_prompt(source: Path) -> str:
    data = _read_real_regular_bytes(source, "instruction profile source")
    if data.startswith(b"\xef\xbb\xbf"):
        raise InstructionProfileError(f"instruction profile source must be UTF-8 without BOM: {source}")
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    try:
        text = normalized.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionProfileError(f"instruction profile source must be valid UTF-8: {source}: {exc}") from exc
    if not text.strip():
        raise InstructionProfileError(f"instruction profile source must not be blank: {source}")
    return text


def _require_unchanged_source(projection: CodexProfileProjection, operation: str) -> None:
    if _read_normalized_prompt(projection.source) != projection.prompt:
        raise InstructionProfileError(f"instruction profile source changed during {operation}: {projection.source}")


def _render_profile(prompt: str) -> bytes:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    rendered = (
        "# Generated by agentbridge instructions generate; DO NOT EDIT.\n"
        f"# source-sha256: {digest}\n"
        f"developer_instructions = {_toml_basic_string(prompt)}\n"
    )
    return rendered.encode("utf-8")


def _toml_basic_string(value: str) -> str:
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    encoded: list[str] = ['"']
    for character in value:
        if character in escapes:
            encoded.append(escapes[character])
        elif ord(character) <= 0x1F or ord(character) == 0x7F:
            encoded.append(f"\\u{ord(character):04X}")
        else:
            encoded.append(character)
    encoded.append('"')
    return "".join(encoded)


def _validate_declared_output_set(
    bundle: Path,
    projections: tuple[CodexProfileProjection, ...],
) -> None:
    declared = {projection.destination.name.casefold(): projection.destination.name for projection in projections}
    codex = bundle / "codex"
    if not _lexists(codex):
        return
    _require_real_directory(codex, "Codex instruction overlay")
    observed: dict[str, str] = {}
    for entry in sorted(codex.iterdir(), key=lambda path: path.name):
        folded = entry.name.casefold()
        if folded == "config.toml":
            raise InstructionProfileError(f"Codex base config.toml is never allowed as Instruction content: {entry}")
        if not folded.endswith(".config.toml"):
            continue
        if previous := observed.get(folded):
            raise InstructionProfileError(
                f"Codex profile outputs collide on case-insensitive filesystems: {previous!r} and {entry.name!r}"
            )
        observed[folded] = entry.name
        if folded not in declared:
            raise InstructionProfileError(f"undeclared generated Codex profile output: {entry}")
        if entry.name != declared[folded]:
            raise InstructionProfileError(
                f"generated Codex profile output case does not match its declaration: {entry}"
            )
        _require_real_regular_file(entry, "generated Codex profile destination")


def _profile_status(projection: CodexProfileProjection) -> str:
    destination = projection.destination
    if not _lexists(destination):
        return "missing"
    _require_real_regular_file(destination, "generated Codex profile destination")
    data = _read_real_regular_bytes(destination, "generated Codex profile")
    prompt_matches = _validate_profile_document(data, projection)
    return "current" if prompt_matches and data == projection.expected else "stale"


def _validate_profile_document(data: bytes, projection: CodexProfileProjection) -> bool:
    try:
        payload: dict[str, Any] = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstructionProfileError(f"malformed generated Codex profile {projection.destination}: {exc}") from exc
    if set(payload) != {"developer_instructions"}:
        raise InstructionProfileError(
            f"generated Codex profile may contain only developer_instructions as data: {projection.destination}"
        )
    prompt = payload["developer_instructions"]
    if not isinstance(prompt, str):
        raise InstructionProfileError(
            f"generated Codex profile developer_instructions must be a string: {projection.destination}"
        )
    if not prompt.strip():
        raise InstructionProfileError(
            f"generated Codex profile developer_instructions must not be blank: {projection.destination}"
        )
    return prompt == projection.prompt


def _result(projection: CodexProfileProjection, status: str) -> InstructionProfileResult:
    return InstructionProfileResult(
        bundle=projection.bundle,
        name=projection.name,
        source=projection.source_relpath,
        destination=projection.destination_relpath,
        status=status,
    )


def _atomic_write(destination: Path, data: bytes) -> None:
    if _lexists(destination):
        _require_real_regular_file(destination, "generated Codex profile destination")
    parent = _require_real_directory(destination.parent, "generated Codex profile directory")
    temporary = parent / f".{destination.name}.agentbridge.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _lexists(destination):
            _require_real_regular_file(destination, "generated Codex profile destination")
        os.replace(temporary, destination)
        _fsync_directory(parent)
    except (OSError, InstructionProfileError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        if isinstance(exc, InstructionProfileError):
            raise
        raise InstructionProfileError(
            f"could not atomically write generated Codex profile {destination}: {exc}"
        ) from exc


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
        directory_reparse_point = is_directory_reparse_point(path)
    except OSError as exc:
        raise InstructionProfileError(f"{label} does not exist or cannot be inspected: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or directory_reparse_point or not stat.S_ISDIR(metadata.st_mode):
        raise InstructionProfileError(
            f"{label} must be a real directory without a symlink or junction/reparse point: {path}"
        )
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstructionProfileError(f"{label} cannot be resolved: {path}: {exc}") from exc


def _require_real_regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstructionProfileError(f"{label} does not exist or cannot be inspected: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise InstructionProfileError(f"{label} must be a real regular file and not a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise InstructionProfileError(f"{label} must be a real regular file: {path}")


def _read_real_regular_bytes(path: Path, label: str) -> bytes:
    _require_real_regular_file(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise InstructionProfileError(f"{label} must remain a real regular file while reading: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read()
    except InstructionProfileError:
        raise
    except OSError as exc:
        raise InstructionProfileError(f"cannot securely read {label} {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
