"""Tests for TOML configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_config_bridge.config import ConfigError, load_config
from agent_config_bridge.models import Component, LinkMode, Platform, Product, Surface
from agent_config_bridge.platforms import current_platform
from tests.conftest import symlink_directory_or_skip


def _write_config(
    tmp_path: Path,
    *,
    bridge_extra: str = "",
    target_extra: str = "",
    target_home: Path | None = None,
) -> Path:
    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    home = target_home or tmp_path / "home"
    home.mkdir(exist_ok=True)
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = "./catalog"
state_dir = "./var/state"
link_mode = "auto"
components = ["skills", "plugins", "hooks"]
{bridge_extra}
[[targets]]
name = "local-codex"
product = "codex"
platform = "auto"
user_home = {str(home)!r}
surfaces = ["cli", "desktop"]
enabled = true
{target_extra}
""",
        encoding="utf-8",
    )
    return config_path


def test_load_config_builds_typed_immutable_model(tmp_path: Path) -> None:
    """Relative bridge paths resolve and target components inherit."""
    config_path = _write_config(tmp_path)

    config = load_config(config_path)

    assert config.schema_version == 1
    assert config.catalog == (tmp_path / "catalog").resolve()
    assert config.state_dir == (tmp_path / "var/state").resolve()
    assert config.config_path == config_path.resolve()
    assert config.link_mode is LinkMode.AUTO
    assert config.components == frozenset({Component.SKILLS, Component.PLUGINS, Component.HOOKS})
    assert isinstance(config.targets, tuple)

    target = config.targets[0]
    assert target.name == "local-codex"
    assert target.product is Product.CODEX
    assert target.platform is current_platform()
    assert target.platform is not Platform.AUTO
    assert target.config_home == (tmp_path / "home/.codex").resolve()
    assert target.components is config.components
    assert target.surfaces == frozenset(Surface)
    assert target.enabled is True


def test_load_config_accepts_settings_and_schedules_components(tmp_path: Path) -> None:
    """The v1 schema can opt into the two additive v0.2 component values."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'components = ["skills", "plugins", "hooks"]',
        'components = ["settings", "schedules"]',
    )
    config_path.write_text(text, encoding="utf-8")

    config = load_config(config_path)

    assert config.components == frozenset({Component.SETTINGS, Component.SCHEDULES})
    assert config.targets[0].components is config.components


def test_load_config_resolves_optional_target_executable_from_user_home(tmp_path: Path) -> None:
    """A relative Schedule CLI override becomes a stable absolute target path."""

    config_path = _write_config(tmp_path, target_extra='executable = "bin/codex"')

    target = load_config(config_path).targets[0]

    assert target.executable == (target.user_home / "bin/codex").resolve()


def test_load_config_requires_cli_surface_for_schedules(tmp_path: Path) -> None:
    """Host-managed schedules always invoke the configured product CLI."""

    config_path = _write_config(tmp_path)
    text = (
        config_path.read_text(encoding="utf-8")
        .replace('components = ["skills", "plugins", "hooks"]', 'components = ["schedules"]')
        .replace('surfaces = ["cli", "desktop"]', 'surfaces = ["desktop"]')
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="schedules.*cli surface"):
        load_config(config_path)


def test_load_config_expands_environment_and_user_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX and percent-style environment references are expanded."""
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BRIDGE_CATALOG", str(catalog))
    monkeypatch.setenv("BRIDGE_HOME", str(home))
    monkeypatch.setenv("BRIDGE_CONFIG", str(tmp_path / "bridge.toml"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        """\
schema_version = 1

[bridge]
catalog = "${BRIDGE_CATALOG}"
state_dir = "%BRIDGE_HOME%/state"
link_mode = "copy"
components = ["skills"]

[[targets]]
name = "claude"
product = "claude-code"
platform = "linux"
user_home = "~"
config_home = "agent-config/claude"
components = ["hooks", "plugins"]
surfaces = ["desktop"]
enabled = true
""",
        encoding="utf-8",
    )

    config = load_config("${BRIDGE_CONFIG}")

    assert config.catalog == catalog.resolve()
    assert config.state_dir == (home / "state").resolve()
    target = config.targets[0]
    assert target.product is Product.CLAUDE_CODE
    assert target.platform is Platform.LINUX
    assert target.user_home == home.resolve()
    assert target.config_home == (home / "agent-config/claude").resolve()
    assert target.components == frozenset({Component.HOOKS, Component.PLUGINS})
    assert target.surfaces == frozenset({Surface.DESKTOP})


def test_load_config_allows_missing_runtime_directories(tmp_path: Path) -> None:
    """State and config homes may be created later by the bridge."""
    config_path = _write_config(
        tmp_path,
        target_extra='config_home = "new/config/home"',
    )

    config = load_config(config_path)

    assert not config.state_dir.exists()
    assert config.targets[0].config_home == (tmp_path / "home/new/config/home").resolve()
    assert not config.targets[0].config_home.exists()


def test_load_config_allows_missing_home_for_disabled_target(tmp_path: Path) -> None:
    """Disabled targets need not be reachable on the current machine."""
    missing_home = tmp_path / "other-machine"
    config_path = _write_config(
        tmp_path,
        target_extra="enabled = false",
        target_home=missing_home,
    )
    missing_home.rmdir()
    text = config_path.read_text(encoding="utf-8").replace("enabled = true\n", "")
    config_path.write_text(text, encoding="utf-8")

    config = load_config(config_path)

    assert config.targets[0].enabled is False
    assert config.targets[0].user_home == missing_home.resolve()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('link_mode = "auto"', "link_mode"),
        ('components = ["skills", "skills"]', "duplicate"),
        ('components = ["skills", "unknown"]', "unknown"),
    ],
)
def test_load_config_rejects_invalid_bridge_values(
    tmp_path: Path,
    replacement: str,
    message: str,
) -> None:
    """Invalid enum values and duplicate selections fail clearly."""
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    if replacement == 'link_mode = "auto"':
        text = text.replace(replacement, 'link_mode = "hardlink"')
    else:
        text = text.replace('components = ["skills", "plugins", "hooks"]', replacement)
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


@pytest.mark.parametrize(
    "field_line",
    [
        'product = "other"',
        'platform = "macos"',
        'surfaces = ["terminal"]',
        'surfaces = ["cli", "cli"]',
        'enabled = "yes"',
    ],
)
def test_load_config_rejects_invalid_target_values(tmp_path: Path, field_line: str) -> None:
    """Target values are checked against the exact typed schema."""
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    field = field_line.split(" =", maxsplit=1)[0]
    old_line = next(line for line in text.splitlines() if line.startswith(f"{field} ="))
    config_path.write_text(text.replace(old_line, field_line), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_load_config_rejects_duplicate_target_names(tmp_path: Path) -> None:
    """Target names are unique identifiers."""
    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""\

[[targets]]
name = "local-codex"
product = "claude-code"
platform = "windows"
user_home = {str(tmp_path / "home")!r}
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="duplicate target name"):
        load_config(config_path)


@pytest.mark.parametrize("name", ["../escape", "Local-Codex", "local_codex", "-local"])
def test_load_config_rejects_unsafe_target_names(tmp_path: Path, name: str) -> None:
    """Target IDs used in ownership and backup paths are safe kebab-case."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace('name = "local-codex"', f"name = {name!r}")
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="kebab-case"):
        load_config(config_path)


@pytest.mark.parametrize("name", ["con", "aux", "com1", "lpt9"])
def test_load_config_rejects_windows_device_target_names(tmp_path: Path, name: str) -> None:
    """Target IDs must be usable as state and backup directory names on Windows."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace('name = "local-codex"', f'name = "{name}"')
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="reserved on Windows"):
        load_config(config_path)


def test_load_config_rejects_duplicate_product_home(tmp_path: Path) -> None:
    """Two targets cannot race while managing the same product home."""

    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "second-codex"
product = "codex"
platform = "linux"
user_home = {str(tmp_path / "home")!r}
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="same codex config_home"):
        load_config(config_path)


def test_load_config_rejects_product_home_symlink_alias(tmp_path: Path) -> None:
    """Existing symlink ancestors cannot disguise a duplicate destination."""

    config_path = _write_config(tmp_path)
    home = tmp_path / "home"
    alias = tmp_path / "home-alias"
    symlink_directory_or_skip(alias, home)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "alias-codex"
product = "codex"
platform = "auto"
user_home = {str(alias)!r}
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="same codex config_home"):
        load_config(config_path)


def test_load_config_casefolds_windows_target_destinations(tmp_path: Path) -> None:
    """Windows targets cannot claim config homes that differ only by casing."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace('platform = "auto"', 'platform = "windows"')
    with config_path.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.write(
            f"""

[[targets]]
name = "second-codex"
product = "codex"
platform = "windows"
user_home = {str(tmp_path / "home")!r}
config_home = ".CODEX"
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="same codex config_home"):
        load_config(config_path)


def test_load_config_casefolds_cross_product_windows_skill_destinations(tmp_path: Path) -> None:
    """Case variants cannot make Codex and Claude manage one Windows Skill root."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace('platform = "auto"', 'platform = "windows"')
    with config_path.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.write(
            f"""

[[targets]]
name = "claude"
product = "claude-code"
platform = "windows"
user_home = {str(tmp_path / "home")!r}
config_home = ".AGENTS"
components = ["skills"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="same skill destination"):
        load_config(config_path)


def test_load_config_rejects_state_inside_catalog_artifact(tmp_path: Path) -> None:
    """Generated state cannot recursively become canonical Plugin input."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'state_dir = "./var/state"',
        'state_dir = "./catalog/plugins/example/generated-state"',
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"state_dir must not overlap bridge\.catalog"):
        load_config(config_path)


def test_load_config_rejects_catalog_inside_state(tmp_path: Path) -> None:
    """Canonical input cannot live below generated bridge state either."""

    shared = tmp_path / "shared"
    catalog = shared / "catalog"
    catalog.mkdir(parents=True)
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('catalog = "./catalog"', f"catalog = {str(catalog)!r}")
    text = text.replace('state_dir = "./var/state"', f"state_dir = {str(shared)!r}")
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"state_dir must not overlap bridge\.catalog"):
        load_config(config_path)


def test_load_config_rejects_state_under_future_skill_root(tmp_path: Path) -> None:
    """A discovery root stays isolated even when Skills are not selected."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        'state_dir = "./var/state"',
        'state_dir = "./home/.agents/skills/hello/bridge-state"',
    )
    text = text.replace(
        'components = ["skills", "plugins", "hooks"]',
        'components = ["plugins"]',
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"state_dir must not overlap target 'local-codex' Skill root"):
        load_config(config_path)


def test_load_config_rejects_catalog_under_config_home(tmp_path: Path) -> None:
    """Canonical inputs cannot be nested inside vendor runtime state."""

    catalog = tmp_path / "home/.codex/source-catalog"
    catalog.mkdir(parents=True)
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'catalog = "./catalog"',
        f"catalog = {str(catalog)!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"catalog must not overlap target 'local-codex' config_home"):
        load_config(config_path)


def test_load_config_rejects_catalog_under_skill_root(tmp_path: Path) -> None:
    """A projected Skill root cannot contain its own canonical source."""

    catalog = tmp_path / "home/.agents/skills/source-catalog"
    catalog.mkdir(parents=True)
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'catalog = "./catalog"',
        f"catalog = {str(catalog)!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"catalog must not overlap target 'local-codex' Skill root"):
        load_config(config_path)


def test_load_config_rejects_bridge_path_symlink_alias(tmp_path: Path) -> None:
    """Existing directory aliases cannot hide state below a product home."""

    config_path = _write_config(tmp_path)
    alias_home = tmp_path / "home-alias"
    symlink_directory_or_skip(alias_home, tmp_path / "home")
    text = config_path.read_text(encoding="utf-8").replace(
        'state_dir = "./var/state"',
        f"state_dir = {str(alias_home / '.codex/state')!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"state_dir must not overlap target 'local-codex' config_home"):
        load_config(config_path)


def test_load_config_fails_closed_when_overlap_path_cannot_resolve(tmp_path: Path) -> None:
    """A symlink loop cannot downgrade physical isolation to textual checks."""

    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable in this test environment: {error}")
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'state_dir = "./var/state"',
        f"state_dir = {str(loop / 'state')!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="could not physically resolve bridge.state_dir"):
        load_config(config_path)


def test_load_config_casefolds_windows_bridge_target_overlap(tmp_path: Path) -> None:
    """Windows casing cannot disguise state nested below a product home."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('platform = "auto"', 'platform = "windows"')
    text = text.replace(
        'state_dir = "./var/state"',
        f"state_dir = {str(tmp_path / 'home/.CODEX/state')!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"state_dir must not overlap target 'local-codex' config_home"):
        load_config(config_path)


def test_load_config_rejects_cross_product_nested_config_homes(tmp_path: Path) -> None:
    """Codex and Claude runtime homes cannot contain one another."""

    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "nested-claude"
product = "claude-code"
platform = "linux"
user_home = {str(tmp_path / "home")!r}
config_home = ".codex/claude-runtime"
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="overlapping config_home"):
        load_config(config_path)


def test_load_config_allows_passive_codex_targets_to_share_skill_root(tmp_path: Path) -> None:
    """Separate Codex installations may consume one root when neither writes it."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'components = ["skills", "plugins", "hooks"]',
        'components = ["plugins"]',
    )
    config_path.write_text(text, encoding="utf-8")
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "orca-codex"
product = "codex"
platform = "linux"
user_home = {str(tmp_path / "home")!r}
config_home = ".orca/codex-runtime"
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    config = load_config(config_path)

    assert [target.name for target in config.targets] == ["local-codex", "orca-codex"]


def test_load_config_allows_one_writer_with_passive_shared_skill_consumer(tmp_path: Path) -> None:
    """A non-writing Codex installation may observe the active writer's root."""

    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "orca-codex"
product = "codex"
platform = "linux"
user_home = {str(tmp_path / "home")!r}
config_home = ".orca/codex-runtime"
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    config = load_config(config_path)

    assert Component.SKILLS in config.targets[0].components
    assert Component.SKILLS not in config.targets[1].components


def test_load_config_rejects_two_writers_for_shared_skill_root(tmp_path: Path) -> None:
    """Two Codex installations cannot both write their shared discovery root."""

    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "orca-codex"
product = "codex"
platform = "linux"
user_home = {str(tmp_path / "home")!r}
config_home = ".orca/codex-runtime"
components = ["skills"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match="both select skills.*same skill destination"):
        load_config(config_path)


def test_load_config_rejects_config_home_inside_another_skill_root(tmp_path: Path) -> None:
    """A vendor runtime home cannot become a projected Skill directory."""

    second_home = tmp_path / "second-home"
    second_home.mkdir()
    config_path = _write_config(tmp_path)
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"""

[[targets]]
name = "second-codex"
product = "codex"
platform = "linux"
user_home = {str(second_home)!r}
config_home = {str(tmp_path / "home/.agents/skills/vendor-runtime")!r}
components = ["plugins"]
surfaces = ["cli"]
enabled = true
"""
        )

    with pytest.raises(ConfigError, match=r"config_home overlaps target 'local-codex' Skill root"):
        load_config(config_path)


def test_load_config_rejects_codex_home_overlapping_own_skill_root(tmp_path: Path) -> None:
    """Codex runtime state cannot own or live below its discovery root."""

    config_path = _write_config(tmp_path, target_extra='config_home = ".agents"')

    with pytest.raises(ConfigError, match=r"config_home overlaps target 'local-codex' Skill root"):
        load_config(config_path)


def test_load_config_allows_isolated_siblings_under_user_home(tmp_path: Path) -> None:
    """Sharing a user-home ancestor alone is not an overlap violation."""

    catalog = tmp_path / "home/source/catalog"
    catalog.mkdir(parents=True)
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('catalog = "./catalog"', f"catalog = {str(catalog)!r}")
    text = text.replace(
        'state_dir = "./var/state"',
        f"state_dir = {str(tmp_path / 'home/bridge-state')!r}",
    )
    config_path.write_text(text, encoding="utf-8")

    config = load_config(config_path)

    assert config.catalog == catalog
    assert config.state_dir == tmp_path / "home/bridge-state"


def test_load_config_rejects_empty_surfaces(tmp_path: Path) -> None:
    """Every enabled or disabled target identifies at least one product surface."""

    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace('surfaces = ["cli", "desktop"]', "surfaces = []")
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="at least one"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("scope", "extra"),
    [
        ("top", "unexpected = true\n"),
        ("bridge", "unexpected = true\n"),
        ("target", "unexpected = true\n"),
    ],
)
def test_load_config_rejects_unknown_keys(tmp_path: Path, scope: str, extra: str) -> None:
    """Typos do not silently change bridge behavior."""
    if scope == "bridge":
        config_path = _write_config(tmp_path, bridge_extra=extra)
    elif scope == "target":
        config_path = _write_config(tmp_path, target_extra=extra)
    else:
        config_path = _write_config(tmp_path)
        text = config_path.read_text(encoding="utf-8")
        config_path.write_text(extra + text, encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown"):
        load_config(config_path)


@pytest.mark.parametrize(
    "line_to_remove",
    [
        "schema_version = 1\n",
        'catalog = "./catalog"\n',
        'state_dir = "./var/state"\n',
        'link_mode = "auto"\n',
        'components = ["skills", "plugins", "hooks"]\n',
        'name = "local-codex"\n',
        'product = "codex"\n',
        'platform = "auto"\n',
        "user_home = ",
        'surfaces = ["cli", "desktop"]\n',
        "enabled = true\n",
    ],
)
def test_load_config_rejects_missing_required_fields(tmp_path: Path, line_to_remove: str) -> None:
    """Only target config_home and components may be omitted."""
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    if line_to_remove == "user_home = ":
        text = "\n".join(line for line in text.splitlines() if not line.startswith(line_to_remove)) + "\n"
    else:
        text = text.replace(line_to_remove, "")
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="required"):
        load_config(config_path)


def test_load_config_rejects_missing_or_invalid_directories(tmp_path: Path) -> None:
    """Canonical catalog and enabled user homes must be directories."""
    config_path = _write_config(tmp_path)
    (tmp_path / "catalog").rmdir()

    with pytest.raises(ConfigError, match="catalog.*does not exist"):
        load_config(config_path)

    (tmp_path / "catalog").mkdir()
    home = tmp_path / "home"
    home.rmdir()
    home.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="user_home.*directory"):
        load_config(config_path)


def test_load_config_rejects_unsupported_schema_and_bad_toml(tmp_path: Path) -> None:
    """Version skew and malformed TOML are reported as configuration errors."""
    config_path = _write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8").replace("schema_version = 1", "schema_version = 2")
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="schema_version"):
        load_config(config_path)

    config_path.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(config_path)


def test_load_config_rejects_missing_file(tmp_path: Path) -> None:
    """A nonexistent config path fails with the public error type."""
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.toml")
