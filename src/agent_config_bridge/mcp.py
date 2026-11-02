"""Catalog-declared MCP servers and their product-specific registrations (ADR-0009).

One product-neutral manifest per server lives at ``catalog/mcp/<name>/server.toml``.
Codex receives owned ``mcp_servers.<name>.*`` leaves in its ``config.toml``
through the Settings owned-leaf engine. Claude Code receives
``claude mcp add-json/remove --scope user`` commands; its user config is only
read, for drift checks and adoption, never written by the Bridge.

The manifest schema has no field for environment values, headers, or tokens:
environment variables are declared by name and rendered only as names (Codex)
or ``${NAME}`` references (Claude Code). ``command``, ``args``, and ``url`` are
free text; common token shapes and credential flags in them are rejected, but
catalog review remains the control. Error messages name fields and argument
indexes, never manifest or product-config values.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from urllib.parse import urlsplit

from agent_config_bridge.models import Platform, Product, TargetConfig
from agent_config_bridge.platforms import product_home_environment_unsets
from agent_config_bridge.settings import (
    OwnedTableRule,
    SettingDisposition,
    SettingIssue,
    SettingLeafSpec,
    SettingValue,
    safe_key,
    setting_value_digest,
)

__all__ = [
    "CLAUDE_USER_CONFIG_MAX_BYTES",
    "CODEX_ALLOWED_UNOWNED_KEYS",
    "CODEX_SERVER_KEYS",
    "MANIFEST_FILENAME",
    "ClaudeMcpChange",
    "ClaudeMcpPlan",
    "McpAdoptionEntry",
    "McpAdoptionStatus",
    "McpAccess",
    "McpAccessMode",
    "McpEcosystem",
    "McpError",
    "McpPackage",
    "McpServer",
    "McpTransport",
    "claude_adoption_entry",
    "claude_command_json",
    "claude_spec_digest",
    "claude_user_config_path",
    "codex_adoption_entry",
    "codex_owned_table_rules",
    "codex_unowned_server_keys",
    "discover_mcp_servers",
    "expand_server",
    "mcp_leaves_digest",
    "normalize_claude_spec",
    "parse_mcp_manifest",
    "read_claude_mcp_servers",
    "reconcile_claude_mcp",
    "release_hint",
    "render_claude_spec",
    "render_codex_leaves",
    "server_review",
    "validate_mcp_bundle",
]

MANIFEST_FILENAME = "server.toml"
CODEX_SERVER_KEYS = ("command", "args", "env_vars", "url", "bearer_token_env_var")
"""Codex ``mcp_servers.<name>`` keys the Bridge may declare."""
CODEX_ALLOWED_UNOWNED_KEYS = ("tools",)
"""The only unowned child an owned Codex server table may carry: user tool approvals."""
_CODEX_EMPTY_AS_ABSENT = ("args", "env_vars")
CLAUDE_USER_CONFIG_MAX_BYTES = 64 * 1024 * 1024
"""Largest Claude user config the Bridge reads; larger files are refused unread."""

_SCHEMA_VERSION = 1
_OPTIONAL_BUNDLE_FILES = frozenset({"README.md"})
_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "transport", "command", "args", "url", "env_vars", "bearer_token_env_var", "package", "access"}
)
_PACKAGE_KEYS = frozenset({"ecosystem", "name", "version"})
_ACCESS_KEYS = frozenset({"mode", "paths"})
_ARTIFACT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_ENV_VAR_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_USER_HOME = "{user_home}"
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_CONTROL_CHARACTERS = ("\x00", "\r", "\n")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TOKEN_SHAPE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_]"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|(?:sk_live|sk_test|rk_live)_[A-Za-z0-9]"
    r"|xox[abprs]-"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+"
    r")"
)
_CREDENTIAL_WORDS = frozenset({"token", "secret", "password", "passwd", "apikey", "auth", "authorization"})
_CREDENTIAL_FLAG = re.compile(r"^--?(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)(?P<assign>=.*)?$", re.DOTALL)
_ENV_ASSIGNMENT = re.compile(r"(?:^|\s)[A-Za-z_][A-Za-z0-9_]*=")
_ENV_FLAGS = frozenset({"-e", "--env"})
_HEADER_FLAGS = frozenset({"-H", "--header"})
_NAME_VALUE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=.+$", re.DOTALL)
_HEADER_VALUE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)\s*:", re.DOTALL)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOML_POSITION = re.compile(r"\((?:at line \d+, column \d+|at end of document)\)")


class McpError(RuntimeError):
    """Raised when an MCP manifest, ownership record, or product config is unsafe."""


class McpTransport(StrEnum):
    """How a product reaches one MCP server."""

    STDIO = "stdio"
    HTTP = "http"


class McpEcosystem(StrEnum):
    """Supply-chain origin of the server implementation."""

    NPM = "npm"
    PYPI = "pypi"
    BINARY = "binary"
    LOCAL = "local"
    NONE = "none"


class McpAccessMode(StrEnum):
    """Least-privilege declaration surfaced in plan reviews."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


@dataclass(frozen=True, slots=True)
class McpPackage:
    """A supply-chain pin; ``npm`` and ``pypi`` pins must appear in ``args``."""

    ecosystem: McpEcosystem
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class McpAccess:
    """Declared access mode and filesystem scope; paths may contain ``{user_home}``."""

    mode: McpAccessMode
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class McpServer:
    """One validated MCP server manifest.

    Placeholders remain unexpanded until :func:`expand_server` binds the
    server to a target.
    """

    name: str
    path: Path
    transport: McpTransport
    command: str | None
    args: tuple[str, ...]
    url: str | None
    env_vars: tuple[str, ...]
    bearer_token_env_var: str | None
    package: McpPackage
    access: McpAccess


@dataclass(frozen=True, slots=True)
class ClaudeMcpChange:
    """One planned Claude Code user-scope MCP server reconciliation.

    ``spec_json`` is the rendered ``add-json`` payload for create and update;
    it contains environment-variable references, never their values.
    """

    name: str
    disposition: SettingDisposition
    detail: str
    spec_json: str | None = None
    issue: SettingIssue | None = None


@dataclass(frozen=True, slots=True)
class ClaudeMcpPlan:
    """A read-only Claude MCP reconciliation for one target.

    ``error`` records an unreadable or invalid Claude user config; such a plan
    is a conflict as a whole and carries no per-server changes.
    """

    config_path: Path
    changes: tuple[ClaudeMcpChange, ...]
    previous_ownership: tuple[tuple[str, str], ...]
    resulting_ownership: tuple[tuple[str, str], ...]
    desired_digest: str
    error: str | None = None

    @property
    def has_conflicts(self) -> bool:
        """Return whether registration must not proceed for this target."""

        return self.error is not None or any(
            change.disposition is SettingDisposition.CONFLICT for change in self.changes
        )

    @property
    def has_commands(self) -> bool:
        """Return whether product CLI commands are required."""

        return any(
            change.disposition in {SettingDisposition.CREATE, SettingDisposition.UPDATE, SettingDisposition.REMOVE}
            for change in self.changes
        )

    @property
    def needs_state_write(self) -> bool:
        """Return whether recorded ownership differs from the reconciled result."""

        return self.previous_ownership != self.resulting_ownership


def release_hint(target: str, name: str) -> str:
    """Return the recovery hint for an owned entry that is missing or drifted.

    The hint names the target so that releasing it cannot also forget a
    healthy same-name entry owned for another target or product.
    """

    return f"to forget it, run `agentbridge mcp release --target {target} --name {name}`"


def reconcile_claude_mcp(
    config_path: Path,
    desired: Mapping[str, Mapping[str, Any]],
    owned: Mapping[str, str],
    live: Mapping[str, Mapping[str, Any]] | None,
    *,
    target: str,
    error: str | None = None,
) -> ClaudeMcpPlan:
    """Compare desired, owned, and live Claude MCP servers without side effects.

    New servers may claim only absent names. A live entry with a desired name
    is a conflict unless ownership records it, and an owned entry may be
    updated or removed only while its normalized digest matches the record.

    Args:
        config_path: The Claude user config that was read.
        desired: Rendered specs keyed by server name.
        owned: Recorded ``name -> spec digest`` ownership.
        live: Current user-scope servers, or ``None`` when ``error`` is set.
        target: Target name, used in recovery hints.
        error: A content-free reason the user config could not be read.
    """

    desired_digest = hashlib.sha256(
        json.dumps(
            {name: claude_spec_digest(spec) for name, spec in desired.items()},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    previous = tuple(sorted(owned.items()))
    if error is not None or live is None:
        return ClaudeMcpPlan(
            config_path=config_path,
            changes=(),
            previous_ownership=previous,
            resulting_ownership=previous,
            desired_digest=desired_digest,
            error=error or "Claude user config was not read",
        )

    changes: list[ClaudeMcpChange] = []
    resulting: dict[str, str] = {}
    for name in sorted(set(desired) | set(owned)):
        spec = desired.get(name)
        recorded = owned.get(name)
        current = live.get(name)
        current_digest = claude_spec_digest(current) if current is not None else None
        if spec is not None:
            spec_digest = claude_spec_digest(spec)
            spec_json = claude_command_json(spec)
            if recorded is None:
                if current is None:
                    changes.append(
                        ClaudeMcpChange(name, SettingDisposition.CREATE, "register absent server", spec_json)
                    )
                    resulting[name] = spec_digest
                else:
                    changes.append(
                        ClaudeMcpChange(
                            name,
                            SettingDisposition.CONFLICT,
                            "existing MCP server is not bridge-owned",
                            issue=SettingIssue.UNOWNED,
                        )
                    )
                continue
            resulting[name] = recorded
            if current is None:
                changes.append(
                    ClaudeMcpChange(
                        name,
                        SettingDisposition.CONFLICT,
                        f"previously registered MCP server is missing; {release_hint(target, name)}",
                        issue=SettingIssue.MISSING,
                    )
                )
            elif current_digest != recorded:
                changes.append(
                    ClaudeMcpChange(
                        name,
                        SettingDisposition.CONFLICT,
                        f"registered MCP server changed after registration; {release_hint(target, name)}",
                        issue=SettingIssue.DRIFT,
                    )
                )
            elif recorded == spec_digest:
                changes.append(ClaudeMcpChange(name, SettingDisposition.NOOP, "registered server is current"))
            else:
                changes.append(ClaudeMcpChange(name, SettingDisposition.UPDATE, "re-register owned server", spec_json))
                resulting[name] = spec_digest
            continue

        assert recorded is not None
        if current is None:
            changes.append(
                ClaudeMcpChange(name, SettingDisposition.NOOP, "formerly registered server is already absent")
            )
        elif current_digest != recorded:
            resulting[name] = recorded
            changes.append(
                ClaudeMcpChange(
                    name,
                    SettingDisposition.CONFLICT,
                    f"formerly registered MCP server changed after registration; {release_hint(target, name)}",
                    issue=SettingIssue.DRIFT,
                )
            )
        else:
            changes.append(ClaudeMcpChange(name, SettingDisposition.REMOVE, "remove deselected owned server"))
    return ClaudeMcpPlan(
        config_path=config_path,
        changes=tuple(changes),
        previous_ownership=previous,
        resulting_ownership=tuple(sorted(resulting.items())),
        desired_digest=desired_digest,
    )


class McpAdoptionStatus(StrEnum):
    """The outcome of comparing one unowned live entry with its desired spec."""

    ADOPTABLE = "adoptable"
    ADOPTED = "adopted"
    MISMATCH = "mismatch"
    OWNED = "owned"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class McpAdoptionEntry:
    """One value-proven adoption decision (ADR-0009).

    ``detail`` and ``unowned_keys`` name keys only, never live values. For
    Codex, ``leaves`` are the declared leaves to own; for Claude Code,
    ``digest`` is the normalized spec digest to record.
    """

    target: str
    product: Product
    name: str
    status: McpAdoptionStatus
    detail: str
    unowned_keys: tuple[str, ...] = ()
    leaves: tuple[SettingLeafSpec, ...] = ()
    digest: str | None = None


def codex_adoption_entry(
    target: str,
    name: str,
    leaves: Sequence[SettingLeafSpec],
    document: Mapping[str, Any],
    *,
    owned_paths: frozenset[tuple[str, ...]],
    foreign_paths: frozenset[tuple[str, ...]],
) -> McpAdoptionEntry:
    """Decide whether an existing Codex ``mcp_servers.<name>`` table may be adopted.

    Every declared leaf must exist with an equal value. The only other key the
    table may hold is ``tools`` (user approval state), which stays unowned; an
    empty ``args`` or ``env_vars`` array counts as absent. Any other key, such
    as ``env``, ``cwd``, ``enabled``, a timeout, or headers, is a mismatch.
    Details name keys only, never values.

    Args:
        target: Target name for reporting.
        name: Server name.
        leaves: Rendered desired leaves for this server and target.
        document: The current, validated Codex ``config.toml`` document.
        owned_paths: Leaf paths already owned as MCP leaves for this target.
        foreign_paths: Leaf paths owned by another component (Settings).
    """

    paths = {leaf.path for leaf in leaves}
    if paths & owned_paths:
        status = McpAdoptionStatus.OWNED if paths <= owned_paths else McpAdoptionStatus.MISMATCH
        detail = "already bridge-owned" if paths <= owned_paths else "partially bridge-owned; inspect manually"
        return McpAdoptionEntry(target, Product.CODEX, name, status, detail)
    if paths & foreign_paths:
        return McpAdoptionEntry(
            target, Product.CODEX, name, McpAdoptionStatus.MISMATCH, "declared leaves are owned by a Settings bundle"
        )
    servers = document.get("mcp_servers")
    table = servers.get(name) if isinstance(servers, Mapping) else None
    if servers is not None and not isinstance(servers, Mapping):
        return McpAdoptionEntry(target, Product.CODEX, name, McpAdoptionStatus.MISMATCH, "mcp_servers is not a table")
    if table is None:
        return McpAdoptionEntry(target, Product.CODEX, name, McpAdoptionStatus.ABSENT, "not present; apply creates it")
    if not isinstance(table, Mapping):
        return McpAdoptionEntry(
            target, Product.CODEX, name, McpAdoptionStatus.MISMATCH, f"mcp_servers.{name} is not a table"
        )
    declared = {leaf.path[-1] for leaf in leaves}
    present = {key for key in table if not (key in _CODEX_EMPTY_AS_ABSENT and table[key] == [])}
    missing = sorted(leaf.path[-1] for leaf in leaves if leaf.path[-1] not in present)
    differing = sorted(
        leaf.path[-1]
        for leaf in leaves
        if leaf.path[-1] in present
        and (
            isinstance(table[leaf.path[-1]], Mapping)
            or setting_value_digest(cast(SettingValue, table[leaf.path[-1]])) != leaf.digest
        )
    )
    undeclared = sorted(
        safe_key(key) for key in present if key not in declared and key not in CODEX_ALLOWED_UNOWNED_KEYS
    )
    problems = [
        f"{label}: {', '.join(keys)}"
        for label, keys in (("differs", differing), ("missing", missing), ("undeclared", undeclared))
        if keys
    ]
    if problems:
        return McpAdoptionEntry(target, Product.CODEX, name, McpAdoptionStatus.MISMATCH, "; ".join(problems))
    return McpAdoptionEntry(
        target,
        Product.CODEX,
        name,
        McpAdoptionStatus.ADOPTABLE,
        "every declared leaf matches",
        unowned_keys=tuple(sorted(set(table) - declared)),
        leaves=tuple(leaves),
    )


def codex_owned_table_rules(owned_paths: Iterable[tuple[str, ...]]) -> tuple[OwnedTableRule, ...]:
    """Return the table rules that remove an owned server table as a unit.

    Codex refuses a server table without a transport, so once the last owned
    leaves of a server are removed, its ``tools`` approvals and empty
    ``args``/``env_vars`` arrays are removed with it; any other remaining key
    is a conflict (ADR-0009).
    """

    names = sorted({path[1] for path in owned_paths if len(path) == 3 and path[0] == "mcp_servers"})
    return tuple(
        OwnedTableRule(
            path=("mcp_servers", name),
            removable_children=CODEX_ALLOWED_UNOWNED_KEYS,
            removable_when_empty=_CODEX_EMPTY_AS_ABSENT,
        )
        for name in names
    )


def codex_unowned_server_keys(
    document: Mapping[str, Any],
    owned_paths: Iterable[tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """List unexpected unowned keys in owned Codex server tables, by server name.

    ``tools`` and empty ``args``/``env_vars`` arrays are expected. Only key
    names are returned, never values.
    """

    owned_by_server: dict[str, set[str]] = {}
    for path in owned_paths:
        if len(path) == 3 and path[0] == "mcp_servers":
            owned_by_server.setdefault(path[1], set()).add(path[2])
    servers = document.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for name, owned_keys in sorted(owned_by_server.items()):
        table = servers.get(name)
        if not isinstance(table, Mapping):
            continue
        extra = tuple(
            sorted(
                key
                for key in table
                if key not in owned_keys
                and key not in CODEX_ALLOWED_UNOWNED_KEYS
                and not (key in _CODEX_EMPTY_AS_ABSENT and table[key] == [])
            )
        )
        if extra:
            result[name] = extra
    return result


def claude_adoption_entry(
    target: str,
    name: str,
    desired: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    owned_digest: str | None,
) -> McpAdoptionEntry:
    """Decide whether an existing Claude Code user-scope entry may be adopted.

    The normalized live entry must equal the normalized rendered spec.
    """

    if owned_digest is not None:
        return McpAdoptionEntry(target, Product.CLAUDE_CODE, name, McpAdoptionStatus.OWNED, "already bridge-owned")
    if live is None:
        return McpAdoptionEntry(
            target, Product.CLAUDE_CODE, name, McpAdoptionStatus.ABSENT, "not registered; register adds it"
        )
    expected = normalize_claude_spec(desired)
    actual = normalize_claude_spec(live)
    if actual != expected:
        keys = sorted(safe_key(key) for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))
        return McpAdoptionEntry(
            target,
            Product.CLAUDE_CODE,
            name,
            McpAdoptionStatus.MISMATCH,
            f"live entry differs in: {', '.join(keys)}",
        )
    return McpAdoptionEntry(
        target,
        Product.CLAUDE_CODE,
        name,
        McpAdoptionStatus.ADOPTABLE,
        "normalized entry matches",
        digest=claude_spec_digest(desired),
    )


def discover_mcp_servers(catalog_root: Path) -> tuple[McpServer, ...]:
    """Discover and strictly validate ``catalog/mcp/<name>/server.toml`` bundles.

    A bundle may contain only ``server.toml`` and an optional ``README.md``.

    Args:
        catalog_root: Root of the canonical catalog, not its ``mcp`` child.

    Returns:
        Validated servers ordered by name.

    Raises:
        McpError: If any bundle or manifest is malformed or unsafe.
    """

    group = catalog_root / "mcp"
    if not os.path.lexists(group):
        return ()
    _require_real_directory(group, "MCP catalog group")
    servers: list[McpServer] = []
    portable_names: dict[str, str] = {}
    for bundle in sorted(group.iterdir(), key=lambda candidate: candidate.name):
        if bundle.name.startswith("."):
            continue
        portable_name = bundle.name.rstrip(" .").casefold()
        if previous := portable_names.get(portable_name):
            raise McpError(
                f"MCP server names collide on case-insensitive filesystems: {previous!r} and {bundle.name!r}"
            )
        portable_names[portable_name] = bundle.name
        validate_mcp_bundle(bundle)
        servers.append(parse_mcp_manifest(bundle / MANIFEST_FILENAME))
    return tuple(servers)


def validate_mcp_bundle(bundle: Path) -> None:
    """Validate one MCP bundle's name and layout without parsing its manifest."""

    if bundle.name.rstrip(" .").casefold() in _WINDOWS_DEVICE_NAMES:
        raise McpError(f"MCP server name is reserved on Windows: {bundle}")
    if _ARTIFACT_NAME.fullmatch(bundle.name) is None:
        raise McpError(f"MCP server name must be portable lowercase kebab-case: {bundle}")
    _require_real_directory(bundle, "MCP server bundle")
    names = {child.name for child in bundle.iterdir()}
    unknown = sorted(names - {MANIFEST_FILENAME} - _OPTIONAL_BUNDLE_FILES)
    if unknown:
        raise McpError(
            f"MCP server bundle may contain only {MANIFEST_FILENAME} and README.md: {bundle}: {', '.join(unknown)}"
        )
    if MANIFEST_FILENAME not in names:
        raise McpError(f"MCP server bundle is missing {MANIFEST_FILENAME}: {bundle}")
    for name in names:
        _require_real_regular_file(bundle / name, "MCP server bundle file")


def parse_mcp_manifest(path: Path) -> McpServer:
    """Parse and validate one ``server.toml`` against MCP manifest schema v1.

    The server name is the bundle directory name.

    Raises:
        McpError: If the manifest violates the schema.
    """

    _require_real_regular_file(path, "MCP server manifest")
    name = path.parent.name
    if _ARTIFACT_NAME.fullmatch(name) is None:
        raise McpError(f"MCP server name must be portable lowercase kebab-case: {path.parent}")
    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        raise McpError(f"MCP server manifest is not UTF-8: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        position = _TOML_POSITION.search(str(exc))
        where = f" {position.group(0)}" if position else ""
        raise McpError(f"invalid TOML in MCP server manifest: {path}{where}") from None
    except OSError as exc:
        raise McpError(f"could not read MCP server manifest: {path}: {exc.strerror or type(exc).__name__}") from None
    context = f"MCP server manifest {path}"
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, context)

    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise McpError(f"{context}: schema_version must be integer {_SCHEMA_VERSION}")
    transport = _parse_enum(document.get("transport"), McpTransport, f"{context}: transport")

    command: str | None = None
    url: str | None = None
    if transport is McpTransport.STDIO:
        if "url" in document:
            raise McpError(f"{context}: url is only valid for transport 'http'")
        if "command" not in document:
            raise McpError(f"{context}: command is required for transport 'stdio'")
        command = _parse_text(document["command"], f"{context}: command")
        args = _parse_string_list(document.get("args", []), f"{context}: args")
        env_vars = _parse_env_names(document.get("env_vars", []), f"{context}: env_vars")
        if _optional_string(document.get("bearer_token_env_var", ""), f"{context}: bearer_token_env_var"):
            raise McpError(f"{context}: bearer_token_env_var is only valid for transport 'http'")
        bearer: str | None = None
    else:
        if "command" in document:
            raise McpError(f"{context}: command is only valid for transport 'stdio'")
        if "url" not in document:
            raise McpError(f"{context}: url is required for transport 'http'")
        if _parse_string_list(document.get("args", []), f"{context}: args"):
            raise McpError(f"{context}: args is only valid for transport 'stdio'")
        if _parse_env_names(document.get("env_vars", []), f"{context}: env_vars"):
            raise McpError(f"{context}: env_vars is only valid for transport 'stdio'")
        url = _parse_text(document["url"], f"{context}: url")
        _validate_url(url, f"{context}: url")
        args = ()
        env_vars = ()
        bearer = _optional_string(document.get("bearer_token_env_var", ""), f"{context}: bearer_token_env_var") or None
        if bearer is not None and _ENV_VAR_NAME.fullmatch(bearer) is None:
            raise McpError(f"{context}: bearer_token_env_var must be an environment variable name like TOKEN_NAME")
        if bearer is not None:
            parts = urlsplit(url)
            if parts.scheme != "https" and (parts.hostname or "") not in _LOOPBACK_HOSTS:
                raise McpError(f"{context}: bearer_token_env_var requires an https url or a loopback http host")

    package = _parse_package(document.get("package"), transport, args, context)
    access = _parse_access(document.get("access"), context)

    for field, values in (
        ("command", (command,) if command is not None else ()),
        ("args", args),
        ("url", (url,) if url is not None else ()),
        ("access.paths", access.paths),
    ):
        for value in values:
            _validate_placeholders(value, f"{context}: {field}")
    _reject_credential_like_values(command, args, url, context)
    for field, values in (
        ("package.name", (package.name,)),
        ("package.version", (package.version,)),
        ("access.paths", access.paths),
    ):
        for index, value in enumerate(values):
            if _TOKEN_SHAPE.search(value):
                where = f"{field}[{index}]" if field == "access.paths" else field
                raise McpError(f"{context}: {where} looks like it contains a credential")

    return McpServer(
        name=name,
        path=path,
        transport=transport,
        command=command,
        args=args,
        url=url,
        env_vars=env_vars,
        bearer_token_env_var=bearer,
        package=package,
        access=access,
    )


def expand_server(server: McpServer, target: TargetConfig) -> McpServer:
    """Return ``server`` with ``{user_home}`` bound to the target's native home path."""

    home = _native_user_home(target)

    def expand(value: str) -> str:
        return value.replace(_USER_HOME, home)

    return replace(
        server,
        command=expand(server.command) if server.command is not None else None,
        args=tuple(expand(value) for value in server.args),
        url=expand(server.url) if server.url is not None else None,
        access=replace(server.access, paths=tuple(expand(value) for value in server.access.paths)),
    )


def render_codex_leaves(server: McpServer, target: TargetConfig) -> tuple[SettingLeafSpec, ...]:
    """Render the owned Codex ``mcp_servers.<name>`` leaves for one target.

    Empty ``args``, ``env_vars``, and ``bearer_token_env_var`` are omitted so an
    existing entry without those keys can match exactly.
    """

    expanded = expand_server(server, target)
    values: list[tuple[str, SettingValue]] = []
    if expanded.transport is McpTransport.STDIO:
        assert expanded.command is not None
        values.append(("command", expanded.command))
        if expanded.args:
            values.append(("args", list(expanded.args)))
        if expanded.env_vars:
            values.append(("env_vars", list(expanded.env_vars)))
    else:
        assert expanded.url is not None
        values.append(("url", expanded.url))
        if expanded.bearer_token_env_var:
            values.append(("bearer_token_env_var", expanded.bearer_token_env_var))
    source_id = f"mcp/{server.name}"
    return tuple(
        SettingLeafSpec(
            source_id=source_id,
            path=("mcp_servers", server.name, key),
            value=value,
            digest=setting_value_digest(value),
        )
        for key, value in values
    )


def render_claude_spec(server: McpServer, target: TargetConfig) -> dict[str, Any]:
    """Render the Claude Code user-scope server JSON for one target.

    Environment variables and bearer tokens render only as ``${NAME}``
    references; the spec never carries their values.
    """

    expanded = expand_server(server, target)
    if expanded.transport is McpTransport.STDIO:
        return {
            "type": "stdio",
            "command": expanded.command,
            "args": list(expanded.args),
            "env": {name: f"${{{name}}}" for name in expanded.env_vars},
        }
    spec: dict[str, Any] = {"type": "http", "url": expanded.url}
    if expanded.bearer_token_env_var:
        spec["headers"] = {"Authorization": f"Bearer ${{{expanded.bearer_token_env_var}}}"}
    return spec


def normalize_claude_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Claude Code server entry for comparison.

    A missing ``type`` means ``stdio``; a stdio entry's missing ``args`` and
    ``env`` mean ``[]`` and ``{}``; an http entry's empty ``headers`` is absent.
    """

    normalized = cast(dict[str, Any], json.loads(json.dumps(dict(spec))))
    normalized.setdefault("type", "stdio")
    if normalized["type"] == "stdio":
        normalized.setdefault("args", [])
        normalized.setdefault("env", {})
    elif normalized.get("headers") == {}:
        del normalized["headers"]
    return normalized


def claude_spec_digest(spec: Mapping[str, Any]) -> str:
    """Return the stable SHA-256 identity of one normalized Claude server entry."""

    # ASCII escaping keeps a lone surrogate from a foreign entry encodable.
    canonical = json.dumps(normalize_claude_spec(spec), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def claude_command_json(spec: Mapping[str, Any]) -> str:
    """Serialize a rendered Claude server entry for ``claude mcp add-json``."""

    return json.dumps(dict(spec), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def mcp_leaves_digest(leaves: Sequence[SettingLeafSpec]) -> str:
    """Return the identity of a desired Codex MCP leaf set, including leaf paths."""

    digest = hashlib.sha256()
    for leaf in sorted(leaves, key=lambda item: item.path):
        digest.update(leaf.source_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(json.dumps(list(leaf.path), ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")
        digest.update(leaf.digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def claude_user_config_path(target: TargetConfig) -> Path:
    """Return the Claude Code user config that holds user-scope MCP servers.

    The default profile keeps it at ``<user_home>/.claude.json``; a custom
    ``CLAUDE_CONFIG_DIR`` profile keeps it at ``<config_home>/.claude.json``.
    """

    if target.product is not Product.CLAUDE_CODE:
        raise McpError(f"target {target.name!r} is not a Claude Code target")
    if product_home_environment_unsets(target):
        return target.user_home / ".claude.json"
    return target.config_home / ".claude.json"


def read_claude_mcp_servers(target: TargetConfig) -> dict[str, dict[str, Any]]:
    """Read user-scope MCP servers from the Claude user config without writing it.

    An absent file has no servers. The file is opened read-only and without
    blocking, must be a regular file of at most
    :data:`CLAUDE_USER_CONFIG_MAX_BYTES`, and must be strict JSON (no duplicate
    keys or non-finite numbers). The rest of the document (projects, history,
    OAuth metadata) is parsed only to reach ``mcpServers`` and never retained
    or reported; errors give the path and at most a line and column.

    Raises:
        McpError: If the config is unreadable, not strict JSON, or malformed.
    """

    path = claude_user_config_path(target)
    if not os.path.lexists(path):
        return {}
    raw = _read_bounded_regular_file(path, "Claude user config", CLAUDE_USER_CONFIG_MAX_BYTES)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise McpError(f"Claude user config is not UTF-8: {path}") from None
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except json.JSONDecodeError as exc:
        raise McpError(
            f"Claude user config is not valid JSON: {path} (line {exc.lineno}, column {exc.colno})"
        ) from None
    except RecursionError:
        raise McpError(f"Claude user config is nested too deeply: {path}") from None
    except ValueError:
        raise McpError(f"Claude user config has duplicate keys or non-finite numbers: {path}") from None
    if not isinstance(document, dict):
        raise McpError(f"Claude user config must contain a top-level object: {path}")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise McpError(f"Claude user config mcpServers must be an object: {path}")
    result: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            # Never echo the name: it may exist only in this product-owned file.
            raise McpError(f"Claude user config has an mcpServers entry that is not an object: {path}")
        result[name] = entry
    return result


def _read_bounded_regular_file(path: Path, description: str, limit: int) -> bytes:
    """Read a regular file of at most ``limit`` bytes without blocking on special files.

    The path is checked before opening (so directories and FIFOs are never
    opened) and the open descriptor is checked again, which closes the window
    in which the path could be swapped for a special file.
    """

    try:
        status = path.stat()
    except OSError as exc:
        raise McpError(f"could not inspect {description}: {path}: {exc.strerror or type(exc).__name__}") from None
    if not stat.S_ISREG(status.st_mode):
        raise McpError(f"{description} is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise McpError(f"could not open {description}: {path}: {exc.strerror or type(exc).__name__}") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise McpError(f"{description} is not a regular file: {path}")
        if status.st_size > limit:
            raise McpError(f"{description} is larger than {limit} bytes: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise McpError(f"could not read {description}: {path}: {exc.strerror or type(exc).__name__}") from None
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise McpError(f"{description} is larger than {limit} bytes: {path}")
    return raw


def server_review(server: McpServer, target: TargetConfig) -> str:
    """Return the plan review line for one server delivered to one target."""

    expanded = expand_server(server, target)
    if expanded.transport is McpTransport.STDIO:
        endpoint = f"command={expanded.command!r} args={list(expanded.args)!r}"
        secrets = f"env_vars={list(expanded.env_vars)!r}"
    else:
        endpoint = f"url={expanded.url!r}"
        secrets = f"bearer_token_env_var={expanded.bearer_token_env_var or ''!r}"
    package = expanded.package
    if package.ecosystem is McpEcosystem.NONE:
        pin = "none"
    elif package.ecosystem is McpEcosystem.PYPI:
        pin = f"pypi:{package.name}=={package.version}"
    else:
        pin = f"{package.ecosystem.value}:{package.name}@{package.version}"
    if package.ecosystem in {McpEcosystem.BINARY, McpEcosystem.LOCAL}:
        pin = f"{pin} (pin recorded, not verified)"
    return (
        f"{target.name}: MCP server {server.name} transport={expanded.transport.value} {endpoint} "
        f"access={expanded.access.mode.value} paths={list(expanded.access.paths)!r} package={pin} {secrets}"
    )


def _parse_package(
    value: object,
    transport: McpTransport,
    args: tuple[str, ...],
    context: str,
) -> McpPackage:
    if not isinstance(value, dict):
        raise McpError(f"{context}: [package] table is required")
    table = cast(dict[str, object], value)
    _reject_unknown_keys(table, _PACKAGE_KEYS, f"{context}: [package]")
    ecosystem = _parse_enum(table.get("ecosystem"), McpEcosystem, f"{context}: package.ecosystem")
    if ecosystem is McpEcosystem.NONE:
        if transport is not McpTransport.HTTP:
            raise McpError(f"{context}: package.ecosystem 'none' is only for http servers")
        name = _optional_string(table.get("name", ""), f"{context}: package.name")
        version = _optional_string(table.get("version", ""), f"{context}: package.version")
        if name or version:
            raise McpError(f"{context}: package.name and package.version must be empty for ecosystem 'none'")
        return McpPackage(ecosystem=ecosystem, name="", version="")

    name = _parse_text(table.get("name"), f"{context}: package.name")
    version = _optional_string(table.get("version", ""), f"{context}: package.version")
    if ecosystem in {McpEcosystem.NPM, McpEcosystem.PYPI}:
        if not version:
            raise McpError(f"{context}: package.version is required for ecosystem {ecosystem.value!r}")
        expected = f"{name}@{version}" if ecosystem is McpEcosystem.NPM else f"{name}=={version}"
        if expected not in args:
            shape = "<name>@<version>" if ecosystem is McpEcosystem.NPM else "<name>==<version>"
            raise McpError(
                f"{context}: package.name and package.version must appear exactly as one args element {shape}"
            )
    return McpPackage(ecosystem=ecosystem, name=name, version=version)


def _parse_access(value: object, context: str) -> McpAccess:
    if not isinstance(value, dict):
        raise McpError(f"{context}: [access] table is required")
    table = cast(dict[str, object], value)
    _reject_unknown_keys(table, _ACCESS_KEYS, f"{context}: [access]")
    mode = _parse_enum(table.get("mode"), McpAccessMode, f"{context}: access.mode")
    paths = _parse_string_list(table.get("paths", []), f"{context}: access.paths")
    if any(not item for item in paths):
        raise McpError(f"{context}: access.paths must not contain empty strings")
    return McpAccess(mode=mode, paths=paths)


def _validate_url(url: str, context: str) -> None:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise McpError(f"{context} is not a valid URL") from None
    if parts.scheme not in {"http", "https"}:
        raise McpError(f"{context} must use http or https")
    if not parts.hostname:
        raise McpError(f"{context} must name a host")
    if "@" in parts.netloc:
        raise McpError(f"{context} must not contain userinfo; declare bearer_token_env_var instead")
    if parts.query or "?" in url:
        raise McpError(f"{context} must not contain a query string")
    if parts.fragment or "#" in url:
        raise McpError(f"{context} must not contain a fragment")
    if ";" in parts.path:
        raise McpError(f"{context} must not contain path parameters")


def _reject_credential_like_values(
    command: str | None,
    args: tuple[str, ...],
    url: str | None,
    context: str,
) -> None:
    """Reject likely credentials in free-text fields, naming only the field and index.

    This is a heuristic for common accidents, not a secret scanner: the schema
    offers no field for secret values, and catalog review remains the control.
    """

    advice = "reference credentials through env_vars or bearer_token_env_var"
    if command is not None:
        if _TOKEN_SHAPE.search(command):
            raise McpError(f"{context}: command looks like it contains a credential; {advice}")
        if _ENV_ASSIGNMENT.search(command):
            raise McpError(f"{context}: command must not assign environment variables; declare env_vars instead")
    for index, argument in enumerate(args):
        if _TOKEN_SHAPE.search(argument):
            raise McpError(f"{context}: args[{index}] looks like it contains a credential; {advice}")
        following = args[index + 1] if index + 1 < len(args) else None
        option, _, inline = argument.partition("=") if argument.startswith("--") else (argument, "", "")
        if option in _ENV_FLAGS or option in _HEADER_FLAGS:
            value_index, value = (index, inline) if inline else (index + 1, following)
            if value is not None and _is_credential_option_value(option in _ENV_FLAGS, value):
                raise McpError(f"{context}: args[{value_index}] passes a credential through {option}; {advice}")
            continue
        flag = _CREDENTIAL_FLAG.fullmatch(argument)
        if flag is None or not _is_credential_flag(flag.group("name")):
            continue
        if flag.group("assign") is not None:
            raise McpError(f"{context}: args[{index}] passes a credential flag value; {advice}")
        if following is not None and not following.startswith("-"):
            raise McpError(f"{context}: args[{index + 1}] follows a credential flag; {advice}")
    if url is not None and _TOKEN_SHAPE.search(url):
        raise McpError(f"{context}: url looks like it contains a credential; {advice}")


def _is_credential_flag(name: str) -> bool:
    """Return whether a flag or variable name is made of credential words.

    Names split on ``-``, ``_``, and camelCase boundaries, so ``--api-key``,
    ``--accessToken``, and ``GITHUB_TOKEN`` match while ``--author`` and
    ``--oauth-port`` do not.
    """

    words = [word for word in re.split(r"[-_]", _CAMEL_BOUNDARY.sub("-", name).lower()) if word]
    if any(word in _CREDENTIAL_WORDS for word in words):
        return True
    return any(first == "api" and second == "key" for first, second in zip(words, words[1:], strict=False))


def _is_credential_option_value(environment: bool, value: str) -> bool:
    """Return whether an ``-e``/``--env`` or ``-H``/``--header`` value carries a credential.

    ``-e NAME`` (forwarding a variable by name) is allowed; ``-e NAME=value``
    with a credential-like name is not. A header is rejected when its name is
    ``Authorization`` or another credential-like name.
    """

    match = (_NAME_VALUE if environment else _HEADER_VALUE).match(value)
    return match is not None and _is_credential_flag(match.group("name"))


def _validate_placeholders(value: str, context: str) -> None:
    remainder = value.replace(_USER_HOME, "")
    if "{" in remainder or "}" in remainder:
        raise McpError(f"{context} contains an unsupported placeholder; only {_USER_HOME} is allowed")


def _native_user_home(target: TargetConfig) -> str:
    if target.platform is Platform.WINDOWS:
        return str(PureWindowsPath(os.fspath(target.user_home)))
    return target.user_home.as_posix()


def _parse_enum(value: object, enum_type: type[Any], context: str) -> Any:
    allowed = ", ".join(member.value for member in enum_type)
    if not isinstance(value, str):
        raise McpError(f"{context} must be one of: {allowed}")
    try:
        return enum_type(value)
    except ValueError:
        raise McpError(f"{context} must be one of: {allowed}") from None


def _parse_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise McpError(f"{context} must be a non-empty string")
    if any(character in value for character in _CONTROL_CHARACTERS):
        raise McpError(f"{context} must not contain control characters")
    return value


def _optional_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise McpError(f"{context} must be a string")
    if any(character in value for character in _CONTROL_CHARACTERS):
        raise McpError(f"{context} must not contain control characters")
    return value


def _parse_string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise McpError(f"{context} must be an array of strings")
    items: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise McpError(f"{context} must be an array of strings")
        if any(character in item for character in _CONTROL_CHARACTERS):
            raise McpError(f"{context} must not contain control characters")
        items.append(item)
    return tuple(items)


def _parse_env_names(value: object, context: str) -> tuple[str, ...]:
    names = _parse_string_list(value, context)
    for name in names:
        if _ENV_VAR_NAME.fullmatch(name) is None:
            raise McpError(f"{context} contains an invalid environment variable name; use names like TOKEN_NAME")
    if len(set(names)) != len(names):
        raise McpError(f"{context} contains duplicate names")
    return names


def _reject_unknown_keys(table: Mapping[str, object], allowed: Iterable[str], context: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise McpError(f"{context} contains unknown keys: {', '.join(repr(key) for key in unknown)}")


def _require_real_directory(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise McpError(f"could not inspect {description}: {path}: {exc.strerror or type(exc).__name__}") from None
    attributes = getattr(status, "st_file_attributes", 0)
    if not stat.S_ISDIR(status.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise McpError(f"{description} must be a real directory: {path}")


def _require_real_regular_file(path: Path, description: str) -> None:
    try:
        status = path.lstat()
    except OSError as exc:
        raise McpError(f"could not inspect {description}: {path}: {exc.strerror or type(exc).__name__}") from None
    attributes = getattr(status, "st_file_attributes", 0)
    if not stat.S_ISREG(status.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise McpError(f"{description} must be a real regular file: {path}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number
