"""Focused tests for scheduled vendor executable resolution."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_config_bridge import scheduler_registration
from agent_config_bridge.models import Platform, Product, TargetConfig
from agent_config_bridge.scheduler_registration import (
    SchedulerRegistrationError,
    resolve_agentbridge_executable,
    resolve_vendor_executable,
    validate_vendor_executable,
)
from tests.conftest import make_catalog, make_config


def _target(tmp_path: Path, *, product: Product = Product.CODEX, platform: Platform = Platform.LINUX) -> TargetConfig:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    return make_config(tmp_path, catalog, product=product, platform=platform).targets[0]


def _write_launcher(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _os_with_access(*, allowed: bool) -> SimpleNamespace:
    return SimpleNamespace(path=os.path, fspath=os.fspath, X_OK=os.X_OK, access=lambda _path, _mode: allowed)


def test_explicit_executable_returns_stable_symlink_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    physical = _write_launcher(tmp_path / "releases" / "codex-1")
    stable = tmp_path / "bin" / "codex"
    stable.parent.mkdir()
    try:
        stable.symlink_to(physical)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")

    target = replace(_target(tmp_path), executable=stable)
    monkeypatch.setattr(scheduler_registration, "os", _os_with_access(allowed=True))

    assert resolve_vendor_executable(target) == stable
    assert stable.resolve(strict=True) == physical


def test_missing_linux_launcher_is_rejected(tmp_path: Path) -> None:
    target = _target(tmp_path, platform=Platform.LINUX)
    missing = tmp_path / "bin" / "codex"

    with pytest.raises(SchedulerRegistrationError, match=r"could not resolve the codex executable"):
        validate_vendor_executable(target, missing)


def test_non_executable_linux_launcher_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target(tmp_path, platform=Platform.LINUX)
    launcher = _write_launcher(tmp_path / "bin" / "codex")
    launcher.chmod(0o600)
    monkeypatch.setattr(scheduler_registration, "os", _os_with_access(allowed=False))

    with pytest.raises(SchedulerRegistrationError, match=r"resolved codex command is not executable"):
        validate_vendor_executable(target, launcher)


@pytest.mark.parametrize("suffix", [".exe", ".EXE", ".com", ".CoM"])
def test_windows_launcher_accepts_native_suffixes_without_host_spoofing(tmp_path: Path, suffix: str) -> None:
    host_os_name = os.name
    target = _target(tmp_path, platform=Platform.WINDOWS)
    launcher = _write_launcher(tmp_path / "bin" / f"codex{suffix}")

    assert validate_vendor_executable(target, launcher) == launcher
    assert os.name == host_os_name


@pytest.mark.parametrize("suffix", ["", ".cmd", ".ps1"])
def test_windows_launcher_rejects_non_native_suffixes_without_host_spoofing(tmp_path: Path, suffix: str) -> None:
    host_os_name = os.name
    target = _target(tmp_path, platform=Platform.WINDOWS)
    launcher = _write_launcher(tmp_path / "bin" / f"codex{suffix}")

    with pytest.raises(SchedulerRegistrationError, match=r"native \.exe or \.com codex launcher"):
        validate_vendor_executable(target, launcher)
    assert os.name == host_os_name


@pytest.mark.parametrize(
    ("product", "command"),
    [(Product.CODEX, "codex"), (Product.CLAUDE_CODE, "claude")],
)
def test_default_resolver_uses_product_command_on_process_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    product: Product,
    command: str,
) -> None:
    target = _target(tmp_path, product=product, platform=Platform.LINUX)
    launcher = _write_launcher(tmp_path / "bin" / command)
    monkeypatch.setenv("PATH", str(launcher.parent))

    assert resolve_vendor_executable(target) == launcher


def test_default_resolver_excludes_current_directory_even_when_path_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untrusted checkout launcher cannot be selected before a trusted PATH entry."""

    target = _target(tmp_path, platform=Platform.LINUX)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _write_launcher(checkout / "codex")
    trusted = _write_launcher(tmp_path / "trusted/bin/codex")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("PATH", os.pathsep.join((str(checkout), ".", str(trusted.parent))))

    assert resolve_vendor_executable(target) == trusted


def test_agentbridge_resolver_prefers_validated_absolute_current_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The already-running console entry point wins over later PATH lookup."""

    current = _write_launcher(tmp_path / "installed/bin/agentbridge")
    malicious = _write_launcher(tmp_path / "checkout/agentbridge")
    monkeypatch.setattr(sys, "argv", [str(current)])
    monkeypatch.setenv("PATH", str(malicious.parent))

    assert resolve_agentbridge_executable() == current


def test_agentbridge_path_fallback_excludes_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback discovery never pins a same-directory agentbridge replacement."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _write_launcher(checkout / "agentbridge")
    trusted = _write_launcher(tmp_path / "trusted/bin/agentbridge")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setenv("PATH", os.pathsep.join((str(checkout), str(trusted.parent))))

    assert resolve_agentbridge_executable() == trusted
