"""Unit tests for catalog-declared MCP server manifests and rendering (ADR-0009)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_config_bridge import cli, mcp
from agent_config_bridge.catalog import CatalogError, discover_catalog
from agent_config_bridge.config import load_config
from agent_config_bridge.governance import GovernanceSeverity, resolve_inventory, run_governance
from agent_config_bridge.mcp import (
    McpAdoptionStatus,
    McpError,
    claude_command_json,
    claude_spec_digest,
    claude_user_config_path,
    codex_adoption_entry,
    codex_unowned_server_keys,
    discover_mcp_servers,
    mcp_leaves_digest,
    normalize_claude_spec,
    parse_mcp_manifest,
    read_claude_mcp_servers,
    render_claude_spec,
    render_codex_leaves,
    server_review,
)
from agent_config_bridge.models import Component, Platform, Product, Surface, TargetConfig
from agent_config_bridge.platforms import current_platform, default_config_home
from agent_config_bridge.settings import (
    SettingIssue,
    SettingsError,
    discover_settings_fragments,
    safe_key,
    setting_value_digest,
)
from tests.conftest import make_catalog, make_config

PLAYWRIGHT = """\
schema_version = 1
transport = "stdio"
command = "npx"
args = ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]

[package]
ecosystem = "npm"
name = "@playwright/mcp"
version = "0.0.78"

[access]
mode = "read-write"
paths = []
"""

GIT = """\
schema_version = 1
transport = "stdio"
command = "uvx"
args = ["--with", "mcp<2", "mcp-server-git==2026.7.10"]

[package]
ecosystem = "pypi"
name = "mcp-server-git"
version = "2026.7.10"

[access]
mode = "read-only"
paths = ["{user_home}/workspace"]
"""

MEMORY = """\
schema_version = 1
transport = "http"
url = "http://127.0.0.1:7337/mcp"

[package]
ecosystem = "none"

[access]
mode = "read-write"
"""

GITHUB = """\
schema_version = 1
transport = "stdio"
command = "{user_home}/.local/bin/orca-github-mcp"
env_vars = ["GITHUB_TOKEN"]

[package]
ecosystem = "local"
name = "orca-github-mcp"
version = "1.4.0"

[access]
mode = "read-write"
"""

BINARY = """\
schema_version = 1
transport = "stdio"
command = "/opt/tools/time-mcp"
args = ["--utc"]

[package]
ecosystem = "binary"
name = "time-mcp"
version = "2.0.0"

[access]
mode = "read-only"
"""


def _manifest(tmp_path: Path, text: str, name: str = "server") -> Path:
    path = tmp_path / "catalog" / "mcp" / name / "server.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _target(
    home: Path,
    *,
    product: Product = Product.CODEX,
    platform: Platform = Platform.LINUX,
    config_home: Path | None = None,
) -> TargetConfig:
    return TargetConfig(
        name="target",
        product=product,
        platform=platform,
        user_home=home,
        config_home=config_home if config_home is not None else default_config_home(product, home),
        components=frozenset({Component.MCP}),
        surfaces=frozenset({Surface.CLI}),
        enabled=True,
    )


def test_valid_stdio_npm_manifest_parses_every_declared_field(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT, "browser"))

    assert server.name == "browser"
    assert server.transport.value == "stdio"
    assert server.command == "npx"
    assert server.args == ("-y", "@playwright/mcp@0.0.78", "--headless", "--isolated")
    assert server.url is None
    assert server.env_vars == ()
    assert server.package.ecosystem.value == "npm"
    assert server.package.name == "@playwright/mcp"
    assert server.access.mode.value == "read-write"


def test_valid_pypi_uvx_manifest_accepts_name_equals_version_arg(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, GIT, "git"))

    assert server.args == ("--with", "mcp<2", "mcp-server-git==2026.7.10")
    assert server.access.paths == ("{user_home}/workspace",)


def test_valid_http_manifest_without_package_pin(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, MEMORY, "shared-agent-memory"))

    assert server.transport.value == "http"
    assert server.url == "http://127.0.0.1:7337/mcp"
    assert server.command is None
    assert server.args == ()
    assert server.package.ecosystem.value == "none"
    assert server.package.name == ""
    assert server.access.paths == ()


@pytest.mark.parametrize("text", [BINARY, GITHUB])
def test_binary_and_local_versions_are_metadata_only(tmp_path: Path, text: str) -> None:
    review = server_review(parse_mcp_manifest(_manifest(tmp_path, text, "tool")), _target(Path("/home/res")))
    assert "(pin recorded, not verified)" in review
    server = parse_mcp_manifest(_manifest(tmp_path, text))

    assert server.package.ecosystem.value in {"binary", "local"}


def test_http_manifest_may_carry_empty_stdio_defaults(tmp_path: Path) -> None:
    text = MEMORY.replace('url = "', 'args = []\nenv_vars = []\nbearer_token_env_var = ""\nurl = "')

    server = parse_mcp_manifest(_manifest(tmp_path, text))

    assert server.bearer_token_env_var is None


@pytest.mark.parametrize(
    "key",
    [
        "env",
        "headers",
        "http_headers",
        "bearer_token",
        "env_http_headers",
        "tools",
        "enabled_tools",
        "startup_timeout_sec",
    ],
)
def test_secret_or_runtime_keys_are_rejected(tmp_path: Path, key: str) -> None:
    value = "30" if key == "startup_timeout_sec" else '"x"'
    text = PLAYWRIGHT.replace("[package]", f"{key} = {value}\n\n[package]")

    with pytest.raises(McpError, match=f"unknown keys: '{key}'"):
        parse_mcp_manifest(_manifest(tmp_path, text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('version = "0.0.78"', 'version = "0.0.78"\nregistry = "x"'),
        ('mode = "read-write"', 'mode = "read-write"\nnetwork = true'),
    ],
)
def test_unknown_nested_keys_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(McpError, match="unknown keys"):
        parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT.replace(old, new)))


@pytest.mark.parametrize("name", ["github_token", "1TOKEN", "TOKEN-NAME", "", "TOKEN NAME"])
def test_env_var_names_must_be_uppercase_identifiers(tmp_path: Path, name: str) -> None:
    text = GITHUB.replace('env_vars = ["GITHUB_TOKEN"]', f"env_vars = [{json.dumps(name)}]")

    with pytest.raises(McpError, match="environment variable name"):
        parse_mcp_manifest(_manifest(tmp_path, text))


def test_bearer_token_env_var_must_be_a_name(tmp_path: Path) -> None:
    text = MEMORY.replace('url = "', 'bearer_token_env_var = "Bearer abc"\nurl = "')

    with pytest.raises(McpError, match="environment variable name"):
        parse_mcp_manifest(_manifest(tmp_path, text))


def test_duplicate_env_vars_are_rejected(tmp_path: Path) -> None:
    text = GITHUB.replace('["GITHUB_TOKEN"]', '["GITHUB_TOKEN", "GITHUB_TOKEN"]')

    with pytest.raises(McpError, match="duplicate"):
        parse_mcp_manifest(_manifest(tmp_path, text))


@pytest.mark.parametrize(
    ("text", "old", "new"),
    [
        (PLAYWRIGHT, '"@playwright/mcp@0.0.78"', '"@playwright/mcp@latest"'),
        (GIT, '"mcp-server-git==2026.7.10"', '"mcp-server-git"'),
        (GIT, '"mcp-server-git==2026.7.10"', '"mcp-server-git>=2026.7.10"'),
    ],
)
def test_npm_and_pypi_pins_must_match_an_argument(tmp_path: Path, text: str, old: str, new: str) -> None:
    with pytest.raises(McpError, match="must appear exactly as one args element") as raised:
        parse_mcp_manifest(_manifest(tmp_path, text.replace(old, new)))

    # Field names only: the declared pin is not echoed.
    assert "0.0.78" not in str(raised.value)
    assert "2026.7.10" not in str(raised.value)


def test_unpinned_ecosystem_is_only_for_http(tmp_path: Path) -> None:
    text = PLAYWRIGHT.replace('ecosystem = "npm"\nname = "@playwright/mcp"\nversion = "0.0.78"', 'ecosystem = "none"')

    with pytest.raises(McpError, match="only for http"):
        parse_mcp_manifest(_manifest(tmp_path, text))


def test_unpinned_ecosystem_requires_empty_name_and_version(tmp_path: Path) -> None:
    text = MEMORY.replace('ecosystem = "none"', 'ecosystem = "none"\nname = "memory"')

    with pytest.raises(McpError, match="must be empty"):
        parse_mcp_manifest(_manifest(tmp_path, text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"--isolated"]', '"--profile={home}"]'),
        ("paths = []", 'paths = ["{USER_HOME}/x"]'),
        ('command = "npx"', 'command = "{user_home"'),
    ],
)
def test_unknown_placeholders_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(McpError, match="placeholder"):
        parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT.replace(old, new)))


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@127.0.0.1:7337/mcp",
        "https://token@example.com/mcp",
        "http://127.0.0.1:7337/mcp?token=abc",
        "ftp://127.0.0.1/mcp",
        "http:///mcp",
    ],
)
def test_urls_reject_userinfo_queries_and_non_http_schemes(tmp_path: Path, url: str) -> None:
    text = MEMORY.replace("http://127.0.0.1:7337/mcp", url)

    with pytest.raises(McpError, match="url"):
        parse_mcp_manifest(_manifest(tmp_path, text))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('transport = "http"', 'transport = "sse"', "transport"),
        ('url = "http://127.0.0.1:7337/mcp"', "", "url is required"),
        ('url = "http://127.0.0.1:7337/mcp"', 'url = "http://x/mcp"\ncommand = "node"', "command"),
        ("schema_version = 1", "schema_version = 2", "schema_version"),
        ('mode = "read-write"', 'mode = "admin"', "access.mode"),
    ],
)
def test_transport_and_schema_values_are_strict(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(McpError, match=message):
        parse_mcp_manifest(_manifest(tmp_path, MEMORY.replace(old, new)))


def test_stdio_manifest_rejects_url_and_requires_command(tmp_path: Path) -> None:
    with pytest.raises(McpError, match="url"):
        parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT.replace('command = "npx"', 'command = "npx"\nurl = "x"')))
    with pytest.raises(McpError, match="command is required"):
        parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT.replace('command = "npx"\n', "")))


def test_missing_package_or_access_table_is_rejected(tmp_path: Path) -> None:
    without_access = PLAYWRIGHT.split("[access]", 1)[0]

    with pytest.raises(McpError, match="access"):
        parse_mcp_manifest(_manifest(tmp_path, without_access))


def test_invalid_toml_error_does_not_echo_file_content(tmp_path: Path) -> None:
    with pytest.raises(McpError) as raised:
        parse_mcp_manifest(_manifest(tmp_path, 'schema_version = 1\ncommand = "SECRET-VALUE\n'))

    assert "SECRET-VALUE" not in str(raised.value)
    assert "(at line 2, column" in str(raised.value)


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('transport = "http"', 'transport = "SECRET-TRANSPORT"', "transport"),
        ('[access]\nmode = "read-write"', '[access]\nmode = "SECRET-MODE"', "access.mode"),
        ('ecosystem = "none"', 'ecosystem = "SECRET-ECOSYSTEM"', "package.ecosystem"),
    ],
)
def test_enum_errors_name_the_field_without_the_value(tmp_path: Path, old: str, new: str, field: str) -> None:
    with pytest.raises(McpError, match=field) as raised:
        parse_mcp_manifest(_manifest(tmp_path, MEMORY.replace(old, new)))

    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    ("args", "index"),
    [
        ('["--token=SECRET-VALUE-1234"]', 0),
        ('["--api-key", "SECRET-VALUE-1234"]', 1),
        ('["-y", "--github-token", "SECRET-VALUE-1234"]', 2),
        ('["--password=SECRET"]', 0),
        ('["--client_secret", "SECRET"]', 1),
        ('["--apikey=SECRET"]', 0),
        ('["--auth", "SECRET"]', 1),
        ('["ghp_SECRETVALUE1234567890"]', 0),
        ('["--x", "github_pat_SECRETVALUE"]', 1),
        ('["sk-SECRETVALUE12345678"]', 0),
        ('["xoxb-SECRET"]', 0),
        ('["AKIASECRETVALUE12345"]', 0),
        ('["eyJhbGciOi.eyJzdWIiOi.SECRETsig"]', 0),
        ('["glpat-SECRETVALUE"]', 0),
        ('["--x", "npm_SECRETVALUE0123456789abcdefghijklmnopq"]', 1),
        ('["AIzaSECRETVALUE0123456789abcdefghijklmn"]', 0),
        ('["sk_live_SECRETVALUE"]', 0),
        ('["sk_test_SECRETVALUE"]', 0),
        ('["rk_live_SECRETVALUE"]', 0),
        ('["hf_SECRETVALUE0123456789abcdefghijk"]', 0),
        ('["-e", "GITHUB_TOKEN=SECRET"]', 1),
        ('["run", "--env", "OPENAI_API_KEY=SECRET"]', 2),
        ('["--env=AWS_SECRET_ACCESS_KEY=SECRET"]', 0),
        ('["--header", "Authorization: Bearer SECRET"]', 1),
        ('["-H", "X-Api-Key: SECRET"]', 1),
        ('["--header=authorization:SECRET"]', 0),
        ('["--accessToken", "SECRET"]', 1),
        ('["--apiKey=SECRET"]', 0),
        ('["--clientSecret", "SECRET"]', 1),
    ],
)
def test_credential_like_args_are_rejected_by_index(tmp_path: Path, args: str, index: int) -> None:
    text = BINARY.replace('args = ["--utc"]', f"args = {args}")

    with pytest.raises(McpError, match=rf"args\[{index}\]") as raised:
        parse_mcp_manifest(_manifest(tmp_path, text))

    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    "command",
    ["GITHUB_TOKEN=SECRET /opt/tools/time-mcp", "env API_KEY=SECRET time-mcp", "/opt/ghp_SECRETVALUE/bin/mcp"],
)
def test_credential_like_commands_are_rejected(tmp_path: Path, command: str) -> None:
    text = BINARY.replace('command = "/opt/tools/time-mcp"', f"command = {json.dumps(command)}")

    with pytest.raises(McpError, match="command") as raised:
        parse_mcp_manifest(_manifest(tmp_path, text))

    assert "SECRET" not in str(raised.value)


def test_current_catalog_style_args_and_harmless_flags_pass(tmp_path: Path) -> None:
    text = BINARY.replace(
        'args = ["--utc"]',
        'args = ["-y", "--with", "mcp<2", "--local-timezone=Asia/Seoul", "--headless", "--isolated", '
        '"--author=res", "--oauth-port=8080", "--no-auth", "--headless", "task-runner-for-everything-mcp", '
        '"-e", "GITHUB_TOKEN", "--env", "LOG_LEVEL=debug", "--header", "Accept: application/json", '
        '"--oauthPort=8080", "--skip-npm_check"]',
    )

    assert parse_mcp_manifest(_manifest(tmp_path, text)).args[0] == "-y"
    assert parse_mcp_manifest(_manifest(tmp_path, GIT, "git")).args == ("--with", "mcp<2", "mcp-server-git==2026.7.10")
    assert parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT, "browser")).args[-2:] == ("--headless", "--isolated")


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('name = "time-mcp"', 'name = "ghp_SECRETVALUE"', "package.name"),
        ('version = "2.0.0"', 'version = "glpat-SECRETVALUE"', "package.version"),
        (
            'mode = "read-only"',
            'mode = "read-only"\npaths = ["/srv", "/srv/sk_live_SECRETVALUE"]',
            r"access.paths\[1\]",
        ),
    ],
)
def test_token_shapes_in_package_and_access_fields_are_rejected(
    tmp_path: Path,
    old: str,
    new: str,
    field: str,
) -> None:
    with pytest.raises(McpError, match=field) as raised:
        parse_mcp_manifest(_manifest(tmp_path, BINARY.replace(old, new)))

    assert "SECRET" not in str(raised.value)


def test_claude_spec_digest_accepts_a_lone_surrogate_from_a_foreign_entry(tmp_path: Path) -> None:
    live = json.loads('{"type": "http", "url": "\\ud800"}')

    digest = claude_spec_digest(live)
    plan = mcp.reconcile_claude_mcp(
        tmp_path / ".claude.json",
        {"memory": {"type": "http", "url": "http://127.0.0.1:7337/mcp"}},
        {},
        {"memory": live},
        target="local",
    )

    assert len(digest) == 64
    assert [change.issue for change in plan.changes] == [SettingIssue.UNOWNED]


def test_release_hints_name_the_target() -> None:
    assert mcp.release_hint("wsl-claude", "github") == (
        "to forget it, run `agentbridge mcp release --target wsl-claude --name github`"
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("startup_timeout_sec", "startup_timeout_sec"),
        ("\x1b[2Jcleared", "\\x1b[2Jcleared"),
        ("zero\u200bwidth", "zero\\u200bwidth"),
        ("tab\tkey", "tab\\x09key"),
        ("한글", "한글"),
    ],
)
def test_safe_key_escapes_control_and_non_printable_characters(key: str, expected: str) -> None:
    assert safe_key(key) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/mcp#SECRET",
        "https://example.com/mcp;token=SECRET",
        "https://example.com/ghp_SECRETVALUE/mcp",
    ],
)
def test_urls_reject_fragments_path_parameters_and_token_shapes(tmp_path: Path, url: str) -> None:
    with pytest.raises(McpError, match="url") as raised:
        parse_mcp_manifest(_manifest(tmp_path, MEMORY.replace("http://127.0.0.1:7337/mcp", url)))

    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://mcp.example.com/mcp", True),
        ("http://127.0.0.1:7337/mcp", True),
        ("http://localhost:7337/mcp", True),
        ("http://[::1]:7337/mcp", True),
        ("http://mcp.example.com/mcp", False),
        ("http://10.0.0.5:7337/mcp", False),
    ],
)
def test_bearer_token_requires_https_or_loopback_http(tmp_path: Path, url: str, accepted: bool) -> None:
    text = MEMORY.replace("http://127.0.0.1:7337/mcp", url).replace(
        'url = "', 'bearer_token_env_var = "MEMORY_TOKEN"\nurl = "'
    )
    path = _manifest(tmp_path, text)

    if accepted:
        assert parse_mcp_manifest(path).bearer_token_env_var == "MEMORY_TOKEN"
    else:
        with pytest.raises(McpError, match="https"):
            parse_mcp_manifest(path)


def test_user_home_expands_in_native_linux_form(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, GITHUB, "github"))
    target = _target(Path("/home/res"))

    leaves = render_codex_leaves(server, target)

    assert {leaf.path: leaf.value for leaf in leaves} == {
        ("mcp_servers", "github", "command"): "/home/res/.local/bin/orca-github-mcp",
        ("mcp_servers", "github", "env_vars"): ["GITHUB_TOKEN"],
    }
    review = server_review(server, target)
    assert "/home/res/.local/bin/orca-github-mcp" in review
    assert "env_vars=['GITHUB_TOKEN']" in review


def test_user_home_expands_in_native_windows_form(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, GIT, "git"))
    github = parse_mcp_manifest(_manifest(tmp_path, GITHUB, "github"))
    target = _target(Path("C:/Users/Res"), platform=Platform.WINDOWS, config_home=Path("C:/Users/Res/.codex"))

    review = server_review(server, target)
    spec = render_claude_spec(github, target)

    assert "paths=['C:\\\\Users\\\\Res/workspace']" in review
    assert spec["command"] == "C:\\Users\\Res/.local/bin/orca-github-mcp"


def test_codex_leaves_omit_empty_args_and_env_vars(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, GITHUB.replace('env_vars = ["GITHUB_TOKEN"]\n', ""), "github"))

    leaves = render_codex_leaves(server, _target(Path("/home/res")))

    assert [leaf.path for leaf in leaves] == [("mcp_servers", "github", "command")]
    assert all(leaf.source_id == "mcp/github" for leaf in leaves)
    assert all(leaf.digest == setting_value_digest(leaf.value) for leaf in leaves)


def test_codex_leaves_render_stdio_args_and_http_bearer_name(tmp_path: Path) -> None:
    browser = parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT, "browser"))
    memory = parse_mcp_manifest(
        _manifest(tmp_path, MEMORY.replace('url = "', 'bearer_token_env_var = "MEMORY_TOKEN"\nurl = "'), "memory")
    )
    target = _target(Path("/home/res"))

    assert {leaf.path[-1]: leaf.value for leaf in render_codex_leaves(browser, target)} == {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"],
    }
    assert {leaf.path[-1]: leaf.value for leaf in render_codex_leaves(memory, target)} == {
        "url": "http://127.0.0.1:7337/mcp",
        "bearer_token_env_var": "MEMORY_TOKEN",
    }


def test_claude_spec_renders_env_names_as_references_only(tmp_path: Path) -> None:
    github = parse_mcp_manifest(_manifest(tmp_path, GITHUB, "github"))
    memory = parse_mcp_manifest(
        _manifest(tmp_path, MEMORY.replace('url = "', 'bearer_token_env_var = "MEMORY_TOKEN"\nurl = "'), "memory")
    )
    target = _target(Path("/home/res"), product=Product.CLAUDE_CODE)

    assert render_claude_spec(github, target) == {
        "type": "stdio",
        "command": "/home/res/.local/bin/orca-github-mcp",
        "args": [],
        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
    }
    assert render_claude_spec(memory, target) == {
        "type": "http",
        "url": "http://127.0.0.1:7337/mcp",
        "headers": {"Authorization": "Bearer ${MEMORY_TOKEN}"},
    }
    assert json.loads(claude_command_json(render_claude_spec(memory, target))) == render_claude_spec(memory, target)


def test_claude_normalization_fills_product_defaults() -> None:
    live = {"command": "/home/res/.local/bin/orca-github-mcp"}
    rendered = {"type": "stdio", "command": "/home/res/.local/bin/orca-github-mcp", "args": [], "env": {}}

    assert normalize_claude_spec(live) == rendered
    assert normalize_claude_spec({"type": "http", "url": "http://x/mcp", "headers": {}}) == {
        "type": "http",
        "url": "http://x/mcp",
    }
    assert claude_spec_digest(live) == claude_spec_digest(rendered)


def test_digests_are_stable_and_identity_sensitive(tmp_path: Path) -> None:
    first = {"type": "stdio", "command": "npx", "args": ["-y"], "env": {}}
    reordered = {"env": {}, "args": ["-y"], "command": "npx", "type": "stdio"}

    assert claude_spec_digest(first) == claude_spec_digest(reordered)
    assert claude_spec_digest(first) != claude_spec_digest({**first, "args": ["-y", "x"]})

    browser = parse_mcp_manifest(_manifest(tmp_path, PLAYWRIGHT, "browser"))
    target = _target(Path("/home/res"))
    leaves = render_codex_leaves(browser, target)
    assert mcp_leaves_digest(leaves) == mcp_leaves_digest(tuple(reversed(leaves)))
    stdio = parse_mcp_manifest(_manifest(tmp_path, BINARY.replace('args = ["--utc"]\n', ""), "same"))
    http = parse_mcp_manifest(
        _manifest(
            tmp_path,
            MEMORY.replace("http://127.0.0.1:7337/mcp", "http://example.com/opt/tools/time-mcp"),
            "same",
        )
    )
    assert mcp_leaves_digest(render_codex_leaves(stdio, target)) != mcp_leaves_digest(render_codex_leaves(http, target))


def test_claude_user_config_path_follows_profile_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"

    assert claude_user_config_path(_target(home, product=Product.CLAUDE_CODE)) == home / ".claude.json"
    custom = tmp_path / "profiles" / "work"
    assert claude_user_config_path(_target(home, product=Product.CLAUDE_CODE, config_home=custom)) == (
        custom / ".claude.json"
    )


def test_claude_user_config_path_uses_windows_case_insensitive_default(tmp_path: Path) -> None:
    home = tmp_path / "Home"
    target = _target(
        home,
        product=Product.CLAUDE_CODE,
        platform=Platform.WINDOWS,
        config_home=tmp_path / "HOME" / ".CLAUDE",
    )

    assert claude_user_config_path(target) == home / ".claude.json"


def test_read_claude_servers_is_strict_and_content_free(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = _target(home, product=Product.CLAUDE_CODE)
    config_path = home / ".claude.json"

    assert read_claude_mcp_servers(target) == {}

    config_path.write_text(
        json.dumps(
            {
                "projects": {"/x": {"history": ["SECRET-HISTORY"]}},
                "mcpServers": {"browser": {"type": "stdio", "command": "npx", "args": [], "env": {}}},
            }
        ),
        encoding="utf-8",
    )
    assert read_claude_mcp_servers(target) == {"browser": {"type": "stdio", "command": "npx", "args": [], "env": {}}}

    config_path.write_text('{"oauth": "SECRET-TOKEN", ', encoding="utf-8")
    with pytest.raises(McpError, match="not valid JSON") as raised:
        read_claude_mcp_servers(target)
    assert "SECRET-TOKEN" not in str(raised.value)

    config_path.write_text('{"mcpServers": {}, "mcpServers": {"SECRET-KEY": {}}}', encoding="utf-8")
    with pytest.raises(McpError) as raised:
        read_claude_mcp_servers(target)
    assert "SECRET-KEY" not in str(raised.value)

    config_path.write_text('{"mcpServers": []}', encoding="utf-8")
    with pytest.raises(McpError, match="mcpServers"):
        read_claude_mcp_servers(target)

    config_path.write_text('{"mcpServers": {"foreign-only": "npx"}}', encoding="utf-8")
    with pytest.raises(McpError, match="not an object") as raised:
        read_claude_mcp_servers(target)
    assert "foreign-only" not in str(raised.value)

    config_path.unlink()
    config_path.mkdir()
    with pytest.raises(McpError, match="regular file"):
        read_claude_mcp_servers(target)


def _claude_reader_target(tmp_path: Path) -> tuple[TargetConfig, Path]:
    home = tmp_path / "home"
    home.mkdir()
    return _target(home, product=Product.CLAUDE_CODE), home / ".claude.json"


def test_read_claude_servers_converts_deep_nesting_to_a_content_free_error(tmp_path: Path) -> None:
    target, config_path = _claude_reader_target(tmp_path)
    config_path.write_text('{"x": ' + "[" * 200_000 + "]" * 200_000 + "}", encoding="utf-8")

    with pytest.raises(McpError, match="nested too deeply"):
        read_claude_mcp_servers(target)


def test_read_claude_servers_refuses_oversized_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, config_path = _claude_reader_target(tmp_path)
    config_path.write_text('{"mcpServers": {}, "padding": "' + "x" * 64 + '"}', encoding="utf-8")
    monkeypatch.setattr(mcp, "CLAUDE_USER_CONFIG_MAX_BYTES", 32)

    with pytest.raises(McpError, match="larger than 32 bytes"):
        read_claude_mcp_servers(target)
    monkeypatch.undo()
    assert read_claude_mcp_servers(target) == {}


def test_read_claude_servers_rejects_non_finite_numbers(tmp_path: Path) -> None:
    target, config_path = _claude_reader_target(tmp_path)
    for text in ('{"limit": 1e999}', '{"limit": -1e999}', '{"limit": NaN}', '{"limit": Infinity}'):
        config_path.write_text(text, encoding="utf-8")
        with pytest.raises(McpError, match="non-finite"):
            read_claude_mcp_servers(target)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_read_claude_servers_never_opens_a_fifo(tmp_path: Path) -> None:
    target, config_path = _claude_reader_target(tmp_path)
    os.mkfifo(config_path)

    with pytest.raises(McpError, match="regular file"):
        read_claude_mcp_servers(target)


def test_codex_adoption_allows_only_tools_and_treats_empty_arrays_as_absent(tmp_path: Path) -> None:
    server = parse_mcp_manifest(_manifest(tmp_path, GITHUB.replace('env_vars = ["GITHUB_TOKEN"]\n', ""), "github"))
    leaves = render_codex_leaves(server, _target(Path("/home/res")))
    command = "/home/res/.local/bin/orca-github-mcp"

    def entry(table: dict[str, object]) -> tuple[McpAdoptionStatus, str, tuple[str, ...]]:
        result = codex_adoption_entry(
            "t",
            "github",
            leaves,
            {"mcp_servers": {"github": table}},
            owned_paths=frozenset(),
            foreign_paths=frozenset(),
        )
        return result.status, result.detail, result.unowned_keys

    tools = {"tools": {"search": {"approval_mode": "approve"}}}
    assert entry({"command": command, **tools})[0] is McpAdoptionStatus.ADOPTABLE
    assert entry({"command": command, "args": [], "env_vars": []}) == (
        McpAdoptionStatus.ADOPTABLE,
        "every declared leaf matches",
        ("args", "env_vars"),
    )
    for extra in ("env", "cwd", "enabled", "enabled_tools", "disabled_tools", "startup_timeout_sec", "bearer_token"):
        status, detail, _ = entry({"command": command, extra: "SECRET-VALUE"})
        assert status is McpAdoptionStatus.MISMATCH
        assert detail == f"undeclared: {extra}"
    status, detail, _ = entry({"command": command, "args": ["--verbose"]})
    assert (status, detail) == (McpAdoptionStatus.MISMATCH, "undeclared: args")


def test_codex_unowned_server_keys_lists_unexpected_keys_by_name_only() -> None:
    owned = [("mcp_servers", "browser", "command"), ("mcp_servers", "github", "command")]
    document = {
        "mcp_servers": {
            "browser": {"command": "npx", "startup_timeout_sec": 30, "env": {"TOKEN": "SECRET"}, "tools": {}},
            "github": {"command": "x", "args": []},
            "unowned": {"command": "y", "cwd": "/tmp"},
        }
    }

    assert codex_unowned_server_keys(document, owned) == {"browser": ("env", "startup_timeout_sec")}


def test_discover_servers_allows_only_manifest_and_readme(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    assert discover_mcp_servers(catalog) == ()

    _manifest(tmp_path, PLAYWRIGHT, "browser")
    _manifest(tmp_path, GIT, "git")
    (catalog / "mcp" / "git" / "README.md").write_text("# git\n", encoding="utf-8")

    assert [server.name for server in discover_mcp_servers(catalog)] == ["browser", "git"]

    (catalog / "mcp" / "git" / "notes.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(McpError, match="notes.txt"):
        discover_mcp_servers(catalog)


def test_discover_servers_requires_manifest_and_portable_name(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    (catalog / "mcp" / "empty").mkdir(parents=True)
    with pytest.raises(McpError, match="server.toml"):
        discover_mcp_servers(catalog)

    (catalog / "mcp" / "empty").rmdir()
    (catalog / "mcp" / "Bad_Name").mkdir()
    with pytest.raises(McpError, match="kebab-case"):
        discover_mcp_servers(catalog)


def test_config_accepts_mcp_component(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = {json.dumps(catalog.as_posix())}
state_dir = {json.dumps((tmp_path / "state").as_posix())}
link_mode = "copy"
components = ["mcp"]

[[targets]]
name = "local"
product = "codex"
platform = "linux"
user_home = {json.dumps(home.as_posix())}
surfaces = ["cli"]
enabled = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.components == frozenset({Component.MCP})
    assert config.targets[0].components == frozenset({Component.MCP})


# --- Task 2: catalog discovery, governance, and the Settings guard ---------------------------------------------


def _governed_catalog(tmp_path: Path, *, mode: str = "required") -> Path:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _manifest(tmp_path, PLAYWRIGHT, "browser")
    governance = catalog / "governance"
    governance.mkdir(exist_ok=True)
    (governance / "policy.toml").write_text(f'schema_version = 1\nmode = "{mode}"\n', encoding="utf-8")
    return catalog


def _tool_manifest(
    identifier: str,
    *,
    ref: str = "mcp/browser",
    kind: str = "tool",
    delivery: str = "mcp-registration",
    targets: tuple[tuple[str, str], ...] = (("codex", "linux"),),
) -> str:
    blocks = "".join(
        f'[[targets]]\nproduct = "{product}"\nplatform = "{platform}"\nsurfaces = ["cli", "desktop"]\n'
        for product, platform in targets
    )
    return (
        f'id = "{identifier}"\n'
        f'capability_kind = "{kind}"\n'
        f'delivery = "{delivery}"\n'
        'lifecycle = "active"\n'
        'failure_policy = "advisory"\n'
        'owner = "test"\n'
        'last_reviewed = "2026-11-01"\n'
        "[distribution]\n"
        'redistribution = "blocked"\n'
        f"{blocks}"
        "[[artifacts]]\n"
        f'ref = "{ref}"\n'
        "[artifacts.provenance]\n"
        'origin = "local-original"\n'
    )


def test_catalog_inventory_discovers_mcp_bundles(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _manifest(tmp_path, PLAYWRIGHT, "browser")

    inventory = discover_catalog(make_config(tmp_path, catalog))

    assert [artifact.name for artifact in inventory.mcp] == ["browser"]


def test_catalog_inventory_rejects_extra_mcp_bundle_files(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _manifest(tmp_path, PLAYWRIGHT, "browser")
    (catalog / "mcp" / "browser" / "secrets.env").write_text("TOKEN=x\n", encoding="utf-8")

    with pytest.raises(CatalogError, match="server.toml and README.md"):
        discover_catalog(make_config(tmp_path, catalog))


def test_missing_governance_manifest_for_mcp_is_an_error_in_required_mode(tmp_path: Path) -> None:
    catalog = _governed_catalog(tmp_path)

    report = run_governance(discover_catalog(make_config(tmp_path, catalog)))

    coverage = [finding for finding in report.findings if finding.code == "GOV030"]
    assert [(finding.artifact_ref, finding.severity) for finding in coverage] == [
        ("mcp/browser", GovernanceSeverity.ERROR)
    ]


def test_tool_mcp_registration_manifest_resolves_mcp_refs(tmp_path: Path) -> None:
    catalog = _governed_catalog(tmp_path)
    (catalog / "governance" / "mcp-browser.toml").write_text(_tool_manifest("mcp-browser"), encoding="utf-8")

    report = run_governance(discover_catalog(make_config(tmp_path, catalog)))

    assert not report.has_error, report.findings


@pytest.mark.parametrize(
    ("kind", "delivery", "ref"),
    [
        ("skill", "standalone", "mcp/browser"),
        ("tool", "mcp-registration", "skills/browser"),
    ],
)
def test_mcp_refs_and_tool_capabilities_must_pair(tmp_path: Path, kind: str, delivery: str, ref: str) -> None:
    catalog = _governed_catalog(tmp_path)
    if ref.startswith("skills/"):
        make_catalog(catalog, skills=("browser",))
    (catalog / "governance" / "mcp-browser.toml").write_text(
        _tool_manifest("mcp-browser", kind=kind, delivery=delivery, ref=ref), encoding="utf-8"
    )

    report = run_governance(discover_catalog(make_config(tmp_path, catalog)))

    assert "GOV038" in {finding.code for finding in report.findings}


def test_governance_targets_gate_mcp_servers_by_product_and_platform(tmp_path: Path) -> None:
    catalog = _governed_catalog(tmp_path)
    (catalog / "governance" / "mcp-browser.toml").write_text(
        _tool_manifest("mcp-browser", targets=(("claude-code", "linux"), ("codex", "windows"))),
        encoding="utf-8",
    )
    codex = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    claude = make_config(tmp_path, catalog, product=Product.CLAUDE_CODE, components=frozenset({Component.MCP}))

    resolved = resolve_inventory(discover_catalog(codex))

    assert resolved.mcp_for_target(codex.targets[0]) == ()
    assert [artifact.name for artifact in resolved.mcp_for_target(claude.targets[0])] == ["browser"]


def test_audit_mode_delivers_mcp_servers_ungated(tmp_path: Path) -> None:
    catalog = _governed_catalog(tmp_path, mode="audit")
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))

    resolved = resolve_inventory(discover_catalog(config))

    assert [artifact.name for artifact in resolved.mcp_for_target(config.targets[0])] == ["browser"]


def test_registry_generate_and_check_include_mcp_capabilities(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _governed_catalog(tmp_path)
    (catalog / "governance" / "mcp-browser.toml").write_text(_tool_manifest("mcp-browser"), encoding="utf-8")
    config_path = _write_cli_config(tmp_path, catalog)

    assert cli.main(["registry", "generate", "--config", str(config_path)]) == 0
    payload = json.loads((catalog / "registry.json").read_text(encoding="utf-8"))
    capability = next(item for item in payload["capabilities"] if item["id"] == "mcp-browser")
    assert capability["capability_kind"] == "tool"
    assert capability["delivery"] == "mcp-registration"
    assert capability["artifacts"][0]["ref"] == "mcp/browser"
    assert capability["artifacts"][0]["computed_artifact_digest"].startswith("sha256:")

    assert cli.main(["registry", "check", "--config", str(config_path)]) == 0
    (catalog / "mcp" / "browser" / "server.toml").write_text(
        PLAYWRIGHT.replace("--headless", "--headed"), encoding="utf-8"
    )
    assert cli.main(["registry", "check", "--config", str(config_path)]) == 1
    capsys.readouterr()


def test_codex_settings_fragment_must_not_claim_mcp_servers(tmp_path: Path) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    fragment = catalog / "settings" / "servers" / "codex" / "config.toml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text('[mcp_servers.git]\ncommand = "uvx"\n', encoding="utf-8")

    with pytest.raises(SettingsError, match="mcp_servers"):
        discover_settings_fragments(catalog)
    with pytest.raises(CatalogError, match="mcp_servers"):
        discover_catalog(make_config(tmp_path, catalog))


def test_validate_reports_mcp_count_and_rejects_invalid_manifests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    _manifest(tmp_path, PLAYWRIGHT, "browser")
    config_path = _write_cli_config(tmp_path, catalog)

    assert cli.main(["validate", "--config", str(config_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mcp"] == 1

    (catalog / "mcp" / "browser" / "server.toml").write_text(
        PLAYWRIGHT.replace("[package]", 'env = {TOKEN = "SECRET"}\n\n[package]'), encoding="utf-8"
    )
    assert cli.main(["validate", "--config", str(config_path), "--json"]) == 2
    error = capsys.readouterr().err
    assert "unknown keys: 'env'" in error
    assert "SECRET" not in error


def _write_cli_config(tmp_path: Path, catalog: Path, *, product: str = "codex", extra: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = {json.dumps(catalog.as_posix())}
state_dir = {json.dumps((tmp_path / "state").as_posix())}
link_mode = "copy"
components = ["mcp"]

[[targets]]
name = "local"
product = {json.dumps(product)}
platform = {json.dumps(current_platform().value)}
user_home = {json.dumps(home.as_posix())}
surfaces = ["cli", "desktop"]
enabled = true
{extra}
""",
        encoding="utf-8",
    )
    return config_path
