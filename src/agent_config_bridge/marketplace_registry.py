"""Read-only product marketplace registry probes and schema validation."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from typing import Any

from agent_config_bridge.models import Platform, Product, TargetConfig
from agent_config_bridge.path_safety import (
    is_absolute_target_path,
    target_path_comparison_key,
)

__all__ = [
    "MarketplaceRegistryError",
    "parse_marketplace_source",
    "probe_marketplace_source",
    "run_utf8_json_command",
]

_BRIDGE_MARKETPLACE = "agent-config-bridge"
_PROBE_TIMEOUT_SECONDS = 5


class MarketplaceRegistryError(RuntimeError):
    """Raised when a product marketplace registry cannot be trusted."""


def run_utf8_json_command(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout: int = _PROBE_TIMEOUT_SECONDS,
) -> object:
    """Run one read-only vendor JSON command and decode stdout as strict UTF-8."""

    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            env=dict(environment),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MarketplaceRegistryError("vendor JSON command timed out") from exc
    except OSError as exc:
        raise MarketplaceRegistryError("vendor JSON command could not be executed") from exc

    if result.returncode != 0:
        raise MarketplaceRegistryError(f"vendor JSON command exited with status {result.returncode}")
    try:
        if isinstance(result.stdout, bytes):
            stdout = result.stdout.decode("utf-8", errors="strict")
        elif isinstance(result.stdout, str):
            # Test adapters written for the former text-mode runner may still
            # return str. The production subprocess above always returns bytes.
            stdout = result.stdout
        else:
            raise MarketplaceRegistryError("vendor JSON command returned an unsupported output type")
    except UnicodeDecodeError as exc:
        raise MarketplaceRegistryError("vendor JSON command returned non-UTF-8 JSON") from exc
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MarketplaceRegistryError("vendor JSON command returned invalid JSON") from exc


def probe_marketplace_source(
    target: TargetConfig,
    executable: str,
    environment: Mapping[str, str],
) -> str | None:
    """Return the normalized bridge marketplace source reported by a product."""

    payload = run_utf8_json_command(
        (executable, "plugin", "marketplace", "list", "--json"),
        environment,
    )
    return parse_marketplace_source(payload, target)


def parse_marketplace_source(payload: object, target: TargetConfig) -> str | None:
    """Validate one product registry payload and return its bridge source."""

    raw_source = (
        _codex_marketplace_source(payload, target)
        if target.product is Product.CODEX
        else _claude_marketplace_source(payload, target)
    )
    if raw_source is None:
        return None
    return target_path_comparison_key(raw_source, windows=target.platform is Platform.WINDOWS)


def _codex_marketplace_source(payload: object, target: TargetConfig) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("marketplaces"), list):
        raise MarketplaceRegistryError(f"target {target.name!r}: unexpected Codex marketplace-list JSON")
    entries = payload["marketplaces"]
    if not all(isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in entries):
        raise MarketplaceRegistryError(f"target {target.name!r}: unexpected Codex marketplace-list JSON")
    matches = [entry for entry in entries if entry["name"] == _BRIDGE_MARKETPLACE]
    if not matches:
        return None
    if len(matches) != 1:
        raise MarketplaceRegistryError(f"target {target.name!r}: duplicate Codex bridge marketplaces")

    entry: dict[str, Any] = matches[0]
    root = _absolute_source(entry.get("root"), target, "Codex bridge marketplace root")
    if "marketplaceSource" not in entry:
        # Codex 0.144.4 reports only the physical root for local marketplaces.
        return root

    marketplace_source = entry["marketplaceSource"]
    if not isinstance(marketplace_source, dict) or marketplace_source.get("sourceType") != "local":
        raise MarketplaceRegistryError(f"target {target.name!r}: unexpected Codex bridge marketplace source")
    source = _absolute_source(
        marketplace_source.get("source"),
        target,
        "Codex bridge marketplace source",
    )
    windows = target.platform is Platform.WINDOWS
    if target_path_comparison_key(root, windows=windows) != target_path_comparison_key(source, windows=windows):
        raise MarketplaceRegistryError(f"target {target.name!r}: inconsistent Codex bridge marketplace source")
    return source


def _claude_marketplace_source(payload: object, target: TargetConfig) -> str | None:
    if not isinstance(payload, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in payload
    ):
        raise MarketplaceRegistryError(f"target {target.name!r}: unexpected Claude marketplace-list JSON")
    matches = [entry for entry in payload if entry["name"] == _BRIDGE_MARKETPLACE]
    if not matches:
        return None
    if len(matches) != 1:
        raise MarketplaceRegistryError(f"target {target.name!r}: duplicate Claude bridge marketplaces")
    entry = matches[0]
    source_path = _absolute_source(entry.get("path"), target, "Claude bridge marketplace path")
    install_location = _absolute_source(
        entry.get("installLocation"),
        target,
        "Claude bridge marketplace install location",
    )
    if entry.get("source") != "directory":
        raise MarketplaceRegistryError(f"target {target.name!r}: unexpected Claude bridge marketplace source")
    windows = target.platform is Platform.WINDOWS
    if target_path_comparison_key(source_path, windows=windows) != target_path_comparison_key(
        install_location,
        windows=windows,
    ):
        raise MarketplaceRegistryError(f"target {target.name!r}: inconsistent Claude bridge marketplace source")
    return source_path


def _absolute_source(value: object, target: TargetConfig, label: str) -> str:
    windows = target.platform is Platform.WINDOWS
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not is_absolute_target_path(value, windows=windows)
    ):
        raise MarketplaceRegistryError(f"target {target.name!r}: invalid {label}")
    return value
