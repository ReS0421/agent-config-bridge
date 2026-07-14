"""Shared test builders for bridge catalogs and typed configurations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from agent_config_bridge.models import (
    BridgeConfig,
    Component,
    LinkMode,
    Platform,
    Product,
    Surface,
    TargetConfig,
)


def make_catalog(
    root: Path,
    *,
    skills: Iterable[str] = ("hello",),
    plugins: Iterable[str] = (),
    hooks: Iterable[str] = (),
) -> Path:
    """Create a small valid canonical catalog."""

    hooks = tuple(hooks)
    for group in ("skills", "plugins", "hooks"):
        (root / group).mkdir(parents=True, exist_ok=True)
    for name in skills:
        path = root / "skills" / name
        path.mkdir()
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test {name}.\n---\n\nRun the test workflow.\n",
            encoding="utf-8",
        )
    for name in plugins:
        common = root / "plugins" / name / "common" / "skills" / "inside"
        common.mkdir(parents=True)
        (common / "SKILL.md").write_text(
            "---\nname: inside\ndescription: Bundled test.\n---\n\nTest.\n",
            encoding="utf-8",
        )
        codex_manifest = root / "plugins" / name / "codex" / ".codex-plugin" / "plugin.json"
        claude_manifest = root / "plugins" / name / "claude-code" / ".claude-plugin" / "plugin.json"
        codex_manifest.parent.mkdir(parents=True)
        claude_manifest.parent.mkdir(parents=True)
        manifest = {"name": name, "version": "0.1.0", "description": "Test plugin"}
        codex_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        claude_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    for name in hooks:
        path = root / "hooks" / name / "common"
        (path / "scripts").mkdir(parents=True)
        (path / "scripts" / "allow.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (path / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (f'python "${{CLAUDE_PLUGIN_ROOT}}/scripts/{name}/allow.py"'),
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
    if hooks:
        (root / "hooks" / ".version").write_text("0.1.0\n", encoding="utf-8")
    return root


def make_config(
    tmp_path: Path,
    catalog: Path,
    *,
    product: Product = Product.CODEX,
    platform: Platform = Platform.LINUX,
    mode: LinkMode = LinkMode.SYMLINK,
    components: frozenset[Component] = frozenset({Component.SKILLS}),
    target_name: str = "target",
) -> BridgeConfig:
    """Build a typed single-target configuration."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config_home = home / (".codex" if product is Product.CODEX else ".claude")
    target = TargetConfig(
        name=target_name,
        product=product,
        platform=platform,
        user_home=home,
        config_home=config_home,
        components=components,
        surfaces=frozenset({Surface.CLI, Surface.DESKTOP}),
        enabled=True,
    )
    return BridgeConfig(
        schema_version=1,
        catalog=catalog,
        state_dir=tmp_path / "state",
        link_mode=mode,
        components=components,
        targets=(target,),
    )


def require_directory_symlink_support(root: Path) -> None:
    """Skip the caller when this host cannot create directory symlinks."""

    target = root / ".agentbridge-symlink-probe-target"
    link = root / ".agentbridge-symlink-probe"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable in this test environment: {exc}")
    finally:
        if link.is_symlink():
            link.unlink()
        target.rmdir()


def symlink_directory_or_skip(link: Path, target: Path) -> None:
    """Create a directory symlink or skip when host policy forbids it."""

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable in this test environment: {exc}")
