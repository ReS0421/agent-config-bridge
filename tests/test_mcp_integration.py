"""Integration tests for MCP registration through plan, apply, register, adopt, and doctor (ADR-0009)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agent_config_bridge import applier as applier_module
from agent_config_bridge import cli
from agent_config_bridge.applier import ApplyError, apply_plan
from agent_config_bridge.catalog import discover_catalog
from agent_config_bridge.config import load_config
from agent_config_bridge.doctor import CheckLevel, run_doctor
from agent_config_bridge.models import BridgeConfig, Component, Platform, Product
from agent_config_bridge.planner import Action, Disposition, Operation, SyncPlan, build_plan
from agent_config_bridge.platforms import current_platform
from agent_config_bridge.state import (
    BridgeStateError,
    find_orphaned_target_states,
    mcp_state_path,
    read_claude_mcp_ownership,
    read_mcp_leaf_state,
    read_mcp_state,
    read_settings_state,
    registration_state_path,
    settings_state_path,
    write_claude_mcp_ownership,
    write_registered_plugins,
)
from tests.conftest import make_catalog, make_config

BROWSER = """\
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

USER_CONFIG = """\
# Personal Codex configuration; keep this comment.
model = "gpt-5"

# Servers I manage by hand.
[mcp_servers.personal]
command = "personal-mcp"
"""


def _catalog(tmp_path: Path, **servers: str) -> Path:
    catalog = make_catalog(tmp_path / "catalog", skills=())
    for name, text in (servers or {"browser": BROWSER, "shared-agent-memory": MEMORY}).items():
        _write_server(catalog, name, text)
    return catalog


def _write_server(catalog: Path, name: str, text: str) -> None:
    path = catalog / "mcp" / name / "server.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _with_components(config: BridgeConfig, components: frozenset[Component]) -> BridgeConfig:
    target = replace(config.targets[0], components=components)
    return replace(config, components=components, targets=(target,))


def _mcp_action(plan: SyncPlan) -> Action:
    return next(action for action in plan.actions if action.component is Component.MCP)


def _plan(config: BridgeConfig) -> tuple[SyncPlan, object]:
    inventory = discover_catalog(config)
    return build_plan(config, inventory), inventory


def _apply(config: BridgeConfig) -> SyncPlan:
    inventory = discover_catalog(config)
    plan = build_plan(config, inventory)
    apply_plan(config, inventory, plan)
    return plan


def _codex_document(config: BridgeConfig) -> dict[str, object]:
    return tomllib.loads((config.targets[0].config_home / "config.toml").read_text(encoding="utf-8"))


# --- Task 3: Codex MCP leaves through plan/apply ---------------------------------------------------------------


def test_codex_mcp_create_noop_and_comment_preservation(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(USER_CONFIG, encoding="utf-8")

    plan, _ = _plan(config)
    action = _mcp_action(plan)

    assert action.operation is Operation.PATCH
    assert action.name == "mcp-servers"
    assert action.disposition is Disposition.UPDATE
    assert action.destination == destination
    assert action.source_digest
    assert any("MCP server browser transport=stdio" in review for review in plan.reviews)
    assert any("package=npm:@playwright/mcp@0.0.78" in review for review in plan.reviews)
    assert not plan.has_conflicts

    apply_plan(config, discover_catalog(config), plan)

    text = destination.read_text(encoding="utf-8")
    assert text.startswith("# Personal Codex configuration; keep this comment.\n")
    assert "# Servers I manage by hand." in text
    assert _codex_document(config)["mcp_servers"] == {
        "personal": {"command": "personal-mcp"},
        "browser": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]},
        "shared-agent-memory": {"url": "http://127.0.0.1:7337/mcp"},
    }
    state = read_mcp_leaf_state(config, config.targets[0])
    assert {entry.source_id for entry in state.entries} == {"mcp/browser", "mcp/shared-agent-memory"}
    assert set(state.created_containers) == {("mcp_servers", "browser"), ("mcp_servers", "shared-agent-memory")}
    if os.name != "nt":
        assert mcp_state_path(config, config.targets[0]).stat().st_mode & 0o777 == 0o600

    followup, _ = _plan(config)
    assert _mcp_action(followup).disposition is Disposition.NOOP
    assert not followup.has_changes


def test_codex_mcp_creates_absent_config_and_prunes_bridge_created_tables(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))

    plan, _ = _plan(config)
    assert _mcp_action(plan).disposition is Disposition.CREATE
    _apply(config)
    assert _codex_document(config)["mcp_servers"] == {
        "browser": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]}
    }

    deselected = _with_components(config, frozenset())
    removal, _ = _plan(deselected)
    assert _mcp_action(removal).disposition is Disposition.REMOVE
    _apply(deselected)

    assert _codex_document(config) == {}
    assert not mcp_state_path(config, config.targets[0]).exists()
    assert not any(action.component is Component.MCP for action in _plan(deselected)[0].actions)


def test_codex_mcp_update_after_catalog_change(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    _apply(config)

    _write_server(catalog, "browser", BROWSER.replace('"--isolated"', '"--isolated", "--no-sandbox"'))
    plan, _ = _plan(config)

    assert _mcp_action(plan).disposition is Disposition.UPDATE
    apply_plan(config, discover_catalog(config), plan)
    assert _codex_document(config)["mcp_servers"]["browser"]["args"][-1] == "--no-sandbox"  # type: ignore[index]
    assert _mcp_action(_plan(config)[0]).disposition is Disposition.NOOP


def _codex_memory_with_tools(tmp_path: Path, extra: str = "") -> tuple[BridgeConfig, Path]:
    catalog = _catalog(tmp_path, **{"shared-agent-memory": MEMORY})
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text('[mcp_servers.personal]\ncommand = "personal-mcp"\n', encoding="utf-8")
    _apply(config)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(f'{extra}\n[mcp_servers.shared-agent-memory.tools.memory_search]\napproval_mode = "approve"\n')
    return config, destination


def test_codex_mcp_deselection_removes_the_server_table_with_its_tool_approvals(tmp_path: Path) -> None:
    config, _ = _codex_memory_with_tools(tmp_path)
    assert _mcp_action(_plan(config)[0]).disposition is Disposition.NOOP

    deselected = _with_components(config, frozenset())
    removal, _ = _plan(deselected)
    assert "remove=2" in _mcp_action(removal).detail
    _apply(deselected)

    # Codex refuses a server table without a transport, so approvals go with the server.
    assert _codex_document(config) == {"mcp_servers": {"personal": {"command": "personal-mcp"}}}
    assert not mcp_state_path(config, config.targets[0]).exists()


def test_codex_mcp_removal_also_drops_empty_args_and_env_vars(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    _apply(config)
    destination = config.targets[0].config_home / "config.toml"
    destination.write_text(
        destination.read_text(encoding="utf-8").replace(
            "[mcp_servers.browser]\n", "[mcp_servers.browser]\nenv_vars = []\n"
        ),
        encoding="utf-8",
    )

    _apply(_with_components(config, frozenset()))

    assert _codex_document(config) == {}


def test_codex_mcp_removal_with_other_unowned_keys_is_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, destination = _codex_memory_with_tools(
        tmp_path, extra='\n[mcp_servers.shared-agent-memory.env]\nTOKEN = "SECRET"\n'
    )
    before = destination.read_bytes()
    deselected = _with_components(config, frozenset())

    plan, inventory = _plan(deselected)
    action = _mcp_action(plan)

    assert action.disposition is Disposition.CONFLICT
    assert "shared-agent-memory: leftover" in action.detail
    assert "SECRET" not in action.detail
    with pytest.raises(ApplyError, match="refusing to apply a plan with conflicts"):
        apply_plan(deselected, inventory, plan)  # type: ignore[arg-type]
    assert destination.read_bytes() == before
    assert read_mcp_leaf_state(config, config.targets[0]).entries

    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)
    blocked = [check for check in run_doctor(deselected, inventory, plan) if check.code == "mcp.removal-blocked"]  # type: ignore[arg-type]
    assert [check.level for check in blocked] == [CheckLevel.ERROR]
    assert "shared-agent-memory (env)" in blocked[0].message
    assert "SECRET" not in blocked[0].message


def test_codex_mcp_unowned_existing_entry_is_a_conflict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, **{"shared-agent-memory": MEMORY})
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    original = '[mcp_servers.shared-agent-memory]\nurl = "http://127.0.0.1:7337/mcp"\n'
    destination.write_text(original, encoding="utf-8")

    plan, inventory = _plan(config)

    assert _mcp_action(plan).disposition is Disposition.CONFLICT
    with pytest.raises(ApplyError, match="refusing to apply a plan with conflicts"):
        apply_plan(config, inventory, plan)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original
    assert not mcp_state_path(config, config.targets[0]).exists()


def test_codex_mcp_drifted_owned_leaf_is_a_conflict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    _apply(config)
    destination = config.targets[0].config_home / "config.toml"
    destination.write_text(destination.read_text(encoding="utf-8").replace('"npx"', '"pnpx"'), encoding="utf-8")

    plan, _ = _plan(config)
    action = _mcp_action(plan)

    assert action.disposition is Disposition.CONFLICT
    assert "conflict=1" in action.detail
    assert plan.has_conflicts


def test_settings_and_mcp_ownership_stay_independent(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    fragment = catalog / "settings" / "defaults" / "codex" / "config.toml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text('model = "gpt-5"\n', encoding="utf-8")
    both = make_config(tmp_path, catalog, components=frozenset({Component.SETTINGS, Component.MCP}))
    _apply(both)
    target = both.targets[0]
    assert {entry.path for entry in read_settings_state(both, target).entries} == {("model",)}
    assert {entry.source_id for entry in read_mcp_leaf_state(both, target).entries} == {"mcp/browser"}

    only_mcp = _with_components(both, frozenset({Component.MCP}))
    plan, _ = _plan(only_mcp)
    settings_action = next(action for action in plan.actions if action.component is Component.SETTINGS)
    assert settings_action.disposition is Disposition.REMOVE
    assert _mcp_action(plan).disposition is Disposition.NOOP
    _apply(only_mcp)
    assert "model" not in _codex_document(both)
    assert "browser" in _codex_document(both)["mcp_servers"]  # type: ignore[operator]
    assert not settings_state_path(both, target).exists()

    _apply(both)
    only_settings = _with_components(both, frozenset({Component.SETTINGS}))
    _apply(only_settings)
    assert _codex_document(both) == {"model": "gpt-5"}
    assert read_settings_state(both, target).entries
    assert not mcp_state_path(both, target).exists()


def test_codex_mcp_plan_is_read_only(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(USER_CONFIG, encoding="utf-8")
    before = destination.stat().st_mtime_ns

    for _ in range(2):
        _plan(config)

    assert destination.stat().st_mtime_ns == before
    assert destination.read_text(encoding="utf-8") == USER_CONFIG
    assert not config.state_dir.exists()


def test_codex_mcp_respects_governance_gating(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    governance = catalog / "governance"
    governance.mkdir()
    (governance / "policy.toml").write_text('schema_version = 1\nmode = "required"\n', encoding="utf-8")
    (governance / "mcp-browser.toml").write_text(
        'id = "mcp-browser"\ncapability_kind = "tool"\ndelivery = "mcp-registration"\nlifecycle = "active"\n'
        'failure_policy = "advisory"\nowner = "test"\nlast_reviewed = "2026-11-01"\n'
        '[distribution]\nredistribution = "blocked"\n'
        '[[targets]]\nproduct = "claude-code"\nplatform = "linux"\nsurfaces = ["cli"]\n'
        '[[artifacts]]\nref = "mcp/browser"\n[artifacts.provenance]\norigin = "local-original"\n',
        encoding="utf-8",
    )
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))

    plan, _ = _plan(config)

    assert not any(action.component is Component.MCP for action in plan.actions)
    assert not any("MCP server browser" in review for review in plan.reviews)


def test_mcp_ownership_state_counts_for_orphan_detection(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    _apply(config)
    renamed = replace(config, targets=(replace(config.targets[0], name="renamed"),))

    assert find_orphaned_target_states(renamed) == ("target",)
    with pytest.raises(ApplyError, match="ownership state has no enabled target"):
        apply_plan(renamed, discover_catalog(renamed), build_plan(renamed, discover_catalog(renamed)))


# --- Task 4: Claude MCP through plan/register --------------------------------------------------------------------


class FakeClaude:
    """Emulate ``claude mcp add-json/remove --scope user`` against a temporary user config.

    Like the real CLI, the user config is ``$CLAUDE_CONFIG_DIR/.claude.json`` when that
    variable is set and ``<HOME>/.claude.json`` otherwise.
    """

    def __init__(self, home: Path, *, fail: frozenset[tuple[str, str]] = frozenset()) -> None:
        self.home = home
        self.fail = fail
        self.fail_after: frozenset[tuple[str, str]] = frozenset()
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []

    def config_path(self, environment: dict[str, str]) -> Path:
        override = environment.get("CLAUDE_CONFIG_DIR")
        return Path(override) / ".claude.json" if override else self.home / ".claude.json"

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = tuple(argv)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        self.calls.append(argv)
        self.environments.append(environment)
        assert argv[1] == "mcp" and argv[3:5] == ("--scope", "user"), argv
        verb, name = argv[2], argv[5]
        if (verb, name) in self.fail:
            raise subprocess.CalledProcessError(1, argv)
        path = self.config_path(environment)
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        servers = document.setdefault("mcpServers", {})
        if verb == "add-json":
            if name in servers:
                raise subprocess.CalledProcessError(1, argv)
            servers[name] = json.loads(argv[6])
        elif verb == "remove":
            if name not in servers:
                raise subprocess.CalledProcessError(1, argv)
            del servers[name]
        else:  # pragma: no cover - the Bridge emits only these verbs
            raise AssertionError(argv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        if (verb, name) in self.fail_after:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _claude_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    components: tuple[str, ...] = ("mcp",),
    config_home: Path | None = None,
    fail: frozenset[tuple[str, str]] = frozenset(),
    **servers: str,
) -> tuple[Path, FakeClaude]:
    catalog = tmp_path / "catalog"
    if not catalog.exists():
        _catalog(tmp_path, **servers)
    config_path = _write_claude_config(tmp_path, components=components, config_home=config_home)
    fake = FakeClaude(tmp_path / "home", fail=fail)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    return config_path, fake


def _write_claude_config(tmp_path: Path, *, components: tuple[str, ...], config_home: Path | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    extra = f"config_home = {json.dumps(config_home.as_posix())}\n" if config_home is not None else ""
    config_path = tmp_path / "agentbridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = {json.dumps((tmp_path / "catalog").as_posix())}
state_dir = {json.dumps((tmp_path / "state").as_posix())}
link_mode = "copy"
components = {json.dumps(list(components))}

[[targets]]
name = "local-claude"
product = "claude-code"
platform = {json.dumps(current_platform().value)}
user_home = {json.dumps((tmp_path / "home").as_posix())}
{extra}surfaces = ["cli"]
enabled = true
""",
        encoding="utf-8",
    )
    return config_path


def _user_servers(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})


def _owned(config_path: Path) -> dict[str, str]:
    config = load_config(config_path)
    return dict(read_claude_mcp_ownership(config, config.targets[0]))


def _plan_json(config_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = cli.main(["plan", "--config", str(config_path), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _claude_action(payload: dict[str, object]) -> dict[str, object]:
    actions = payload["actions"]
    assert isinstance(actions, list)
    return next(action for action in actions if action["component"] == "mcp")


def test_claude_mcp_create_registers_and_records_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch)
    user_config = tmp_path / "home" / ".claude.json"
    user_config.write_text(json.dumps({"projects": {}, "mcpServers": {"personal": {"command": "x"}}}), "utf-8")

    code, payload = _plan_json(config_path, capsys)
    action = _claude_action(payload)
    commands = [command["argv"] for command in payload["commands"]]  # type: ignore[union-attr]

    assert code == 0
    assert action["operation"] == "register"
    assert action["disposition"] == "noop"
    assert "create=2" in str(action["detail"])
    assert action["destination"] == str(user_config)
    assert commands == [
        ["claude", "mcp", "add-json", "--scope", "user", "browser", _browser_json()],
        [
            "claude",
            "mcp",
            "add-json",
            "--scope",
            "user",
            "shared-agent-memory",
            '{"type":"http","url":"http://127.0.0.1:7337/mcp"}',
        ],
    ]
    assert all(command["component"] == "mcp" for command in payload["commands"])  # type: ignore[union-attr]

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert [call[2] for call in fake.calls] == ["add-json", "add-json"]
    assert set(_user_servers(user_config)) == {"personal", "browser", "shared-agent-memory"}
    assert set(_owned(config_path)) == {"browser", "shared-agent-memory"}

    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert _claude_action(payload)["detail"].startswith("register user-scope MCP servers")  # type: ignore[union-attr]
    assert "noop=2" in str(_claude_action(payload)["detail"])
    assert payload["commands"] == []
    fake.calls.clear()
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    assert fake.calls == []


def _browser_json() -> str:
    return '{"args":["-y","@playwright/mcp@0.0.78","--headless","--isolated"],"command":"npx","env":{},"type":"stdio"}'


def test_claude_mcp_update_removes_then_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    first_digest = _owned(config_path)["browser"]
    _write_server(tmp_path / "catalog", "browser", BROWSER.replace('"--isolated"', '"--isolated", "--no-sandbox"'))
    fake.calls.clear()

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert [call[2] for call in fake.calls] == ["remove", "add-json"]
    live = _user_servers(tmp_path / "home" / ".claude.json")["browser"]
    assert live["args"][-1] == "--no-sandbox"  # type: ignore[index]
    assert _owned(config_path)["browser"] != first_digest
    capsys.readouterr()


def test_claude_mcp_deselection_removes_only_owned_unchanged_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    user_config = tmp_path / "home" / ".claude.json"
    user_config.write_text(json.dumps({"mcpServers": {"personal": {"command": "x"}}}), "utf-8")
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    _write_claude_config(tmp_path, components=())
    fake.calls.clear()

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert fake.calls == [("claude", "mcp", "remove", "--scope", "user", "browser")]
    assert _user_servers(user_config) == {"personal": {"command": "x"}}
    assert _owned(config_path) == {}
    config = load_config(config_path)
    assert not mcp_state_path(config, config.targets[0]).exists()
    assert not registration_state_path(config, config.targets[0]).exists()


def test_claude_mcp_deselection_of_drifted_entry_is_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    user_config = tmp_path / "home" / ".claude.json"
    document = json.loads(user_config.read_text(encoding="utf-8"))
    document["mcpServers"]["browser"]["args"] = ["edited-by-user"]
    user_config.write_text(json.dumps(document), encoding="utf-8")
    _write_claude_config(tmp_path, components=())
    fake.calls.clear()
    capsys.readouterr()

    code, payload = _plan_json(config_path, capsys)
    assert code == 1
    assert _claude_action(payload)["disposition"] == "conflict"
    assert payload["commands"] == []
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2
    assert fake.calls == []
    assert "browser" in _owned(config_path)


def test_claude_mcp_unowned_same_name_entry_is_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    user_config = tmp_path / "home" / ".claude.json"
    user_config.write_text(json.dumps({"mcpServers": {"browser": json.loads(_browser_json())}}), "utf-8")

    code, payload = _plan_json(config_path, capsys)
    action = _claude_action(payload)

    assert code == 1
    assert action["disposition"] == "conflict"
    assert "browser" in str(action["detail"]) and "not bridge-owned" in str(action["detail"])
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2
    assert "resolve MCP registration conflicts" in capsys.readouterr().err
    assert fake.calls == []
    assert _owned(config_path) == {}


def test_claude_mcp_drifted_owned_entry_is_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    user_config = tmp_path / "home" / ".claude.json"
    document = json.loads(user_config.read_text(encoding="utf-8"))
    document["mcpServers"]["browser"]["env"] = {"DEBUG": "1"}
    user_config.write_text(json.dumps(document), encoding="utf-8")
    capsys.readouterr()

    code, payload = _plan_json(config_path, capsys)

    assert code == 1
    assert "changed after registration" in str(_claude_action(payload)["detail"])


def test_claude_mcp_invalid_user_config_is_a_content_free_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    (tmp_path / "home" / ".claude.json").write_text('{"oauthAccount": "SECRET-VALUE",', encoding="utf-8")

    code, payload = _plan_json(config_path, capsys)

    assert code == 1
    assert _claude_action(payload)["disposition"] == "conflict"
    assert "SECRET-VALUE" not in json.dumps(payload)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2
    assert fake.calls == []


def test_claude_mcp_ownership_is_written_per_successful_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(
        tmp_path,
        monkeypatch,
        fail=frozenset({("add-json", "shared-agent-memory")}),
    )

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert [call[5] for call in fake.calls] == ["browser", "shared-agent-memory"]
    assert set(_owned(config_path)) == {"browser"}
    assert set(_user_servers(tmp_path / "home" / ".claude.json")) == {"browser"}
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert [command["argv"][5] for command in payload["commands"]] == ["shared-agent-memory"]  # type: ignore[index,union-attr]


def test_claude_mcp_failed_command_writes_no_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, fail=frozenset({("add-json", "browser")}), browser=BROWSER)

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    config = load_config(config_path)
    assert not mcp_state_path(config, config.targets[0]).exists()
    assert not registration_state_path(config, config.targets[0]).exists()


def test_claude_mcp_failed_add_after_remove_leaves_a_recreatable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    _write_server(tmp_path / "catalog", "browser", BROWSER.replace('"--isolated"', '"--isolated", "--no-sandbox"'))
    fake.fail = frozenset({("add-json", "browser")})

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert _owned(config_path) == {}
    assert _user_servers(tmp_path / "home" / ".claude.json") == {}
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert "create=1" in str(_claude_action(payload)["detail"])


def test_claude_mcp_stale_ownership_for_absent_entry_is_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    (tmp_path / "home" / ".claude.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    _write_claude_config(tmp_path, components=())
    fake.calls.clear()

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert fake.calls == []
    assert _owned(config_path) == {}


def test_claude_mcp_default_profile_unsets_inherited_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "inherited-wrong-profile"))

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert fake.environments and all("CLAUDE_CONFIG_DIR" not in env for env in fake.environments)
    assert "browser" in _user_servers(tmp_path / "home" / ".claude.json")
    assert not (tmp_path / "inherited-wrong-profile").exists()


def test_claude_mcp_custom_profile_sets_config_dir_and_reads_its_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "claude-profile"
    config_path, fake = _claude_setup(tmp_path, monkeypatch, config_home=profile, browser=BROWSER)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "inherited-wrong-profile"))

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert all(env["CLAUDE_CONFIG_DIR"] == str(profile) for env in fake.environments)
    assert "browser" in _user_servers(profile / ".claude.json")
    assert not (tmp_path / "home" / ".claude.json").exists()
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert payload["commands"] == []


def test_claude_mcp_argv_and_state_never_contain_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, github=GITHUB)
    monkeypatch.setenv("GITHUB_TOKEN", "SECRET-VALUE")

    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    rendered = json.dumps(payload)
    assert "SECRET-VALUE" not in rendered
    assert '\\"env\\":{\\"GITHUB_TOKEN\\":\\"${GITHUB_TOKEN}\\"}' in rendered
    assert any("env_vars=['GITHUB_TOKEN']" in review for review in payload["reviews"])  # type: ignore[union-attr]

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert all("SECRET-VALUE" not in " ".join(call) for call in fake.calls)
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert "SECRET-VALUE" not in path.read_text(encoding="utf-8")
    assert _user_servers(tmp_path / "home" / ".claude.json")["github"]["env"] == {  # type: ignore[index]
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
    }
    capsys.readouterr()


def test_claude_mcp_register_detects_user_config_location_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    fake.home = tmp_path / "elsewhere"

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert "browser" in _owned(config_path)


def test_claude_mcp_pending_registration_does_not_block_apply_or_skill_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, components=("skills", "mcp"), browser=BROWSER)

    assert cli.main(["apply", "--config", str(config_path), "--yes"]) == 0
    assert cli.main(["sync-skills", "--config", str(config_path), "--yes"]) == 0

    assert fake.calls == []
    assert not (tmp_path / "home" / ".claude.json").exists()


def test_claude_mcp_is_not_planned_for_another_platform(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    other = Platform.WINDOWS if current_platform() is Platform.LINUX else Platform.LINUX
    config = make_config(
        tmp_path,
        catalog,
        product=Product.CLAUDE_CODE,
        platform=other,
        components=frozenset({Component.MCP}),
    )

    plan, _ = _plan(config)

    assert not any(action.component is Component.MCP for action in plan.actions)
    assert plan.commands == ()
    assert any("MCP registration" in warning for warning in plan.warnings)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang launcher")
def test_claude_mcp_register_runs_a_real_fake_executable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog(tmp_path, browser=BROWSER)
    launcher = tmp_path / "tools" / "claude"
    launcher.parent.mkdir()
    log = tmp_path / "argv.log"
    launcher.write_text(
        f"""#!{sys.executable}
import json, os, pathlib, sys
argv = sys.argv[1:]
pathlib.Path({str(log)!r}).open("a").write(json.dumps(argv) + "\\n")
home = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or {str(tmp_path / "home")!r})
path = home / ".claude.json"
document = json.loads(path.read_text()) if path.exists() else {{}}
servers = document.setdefault("mcpServers", {{}})
if argv[:4] == ["mcp", "add-json", "--scope", "user"]:
    servers[argv[4]] = json.loads(argv[5])
elif argv[:4] == ["mcp", "remove", "--scope", "user"]:
    del servers[argv[4]]
else:
    sys.exit(3)
path.write_text(json.dumps(document))
""",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    config_path = _write_claude_config(tmp_path, components=("mcp",))
    with config_path.open("a", encoding="utf-8") as stream:
        stream.write(f"executable = {json.dumps(launcher.as_posix())}\n")

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    logged = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert logged == [["mcp", "add-json", "--scope", "user", "browser", _browser_json()]]
    assert "browser" in _owned(config_path)
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert payload["commands"] == []


# --- Task 5: explicit adoption and doctor -------------------------------------------------------------------------

LIVE_CODEX = """\
# Codex servers; this comment must survive adoption.
[mcp_servers.browser]
command = "npx"
args = ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]

[mcp_servers.github]
command = '{home}/.local/bin/orca-github-mcp'
env_vars = ["GITHUB_TOKEN"]

[mcp_servers.shared-agent-memory]
url = "http://127.0.0.1:7337/mcp"

[mcp_servers.shared-agent-memory.tools.memory_search]
approval_mode = "approve"
"""


def _native_home(tmp_path: Path) -> str:
    """Return the test home the way ``{user_home}`` expands for a target on this host."""
    home = tmp_path / "home"
    return str(home) if current_platform() is Platform.WINDOWS else home.as_posix()


def _codex_adopt_setup(tmp_path: Path, live: str = LIVE_CODEX) -> tuple[Path, Path]:
    _catalog(tmp_path, browser=BROWSER, github=GITHUB, **{"shared-agent-memory": MEMORY})
    config_path = _write_target_config(tmp_path, product="codex")
    destination = tmp_path / "home" / ".codex" / "config.toml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(live.replace("{home}", _native_home(tmp_path)), encoding="utf-8")
    return config_path, destination


def _write_target_config(
    tmp_path: Path,
    *,
    product: str,
    platform: str | None = None,
    components: tuple[str, ...] = ("mcp",),
) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config_path = tmp_path / "agentbridge.toml"
    config_path.write_text(
        f"""\
schema_version = 1

[bridge]
catalog = {json.dumps((tmp_path / "catalog").as_posix())}
state_dir = {json.dumps((tmp_path / "state").as_posix())}
link_mode = "copy"
components = {json.dumps(list(components))}

[[targets]]
name = "local"
product = {json.dumps(product)}
platform = {json.dumps(platform or current_platform().value)}
user_home = {json.dumps(home.as_posix())}
surfaces = ["cli"]
enabled = true
""",
        encoding="utf-8",
    )
    return config_path


def _adopt(config_path: Path, *args: str) -> int:
    return cli.main(["mcp", "adopt", "--config", str(config_path), *args])


def _adopt_json(config_path: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = _adopt(config_path, "--json", *args)
    return code, json.loads(capsys.readouterr().out)


def test_codex_adoption_records_exact_matches_without_touching_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)
    before = destination.read_bytes()
    _, conflicted = _plan_json(config_path, capsys)
    assert _claude_action(conflicted)["disposition"] == "conflict"

    code, preview = _adopt_json(config_path, capsys)

    assert code == 0
    assert preview["applied"] is False
    entries = {entry["name"]: entry for entry in preview["entries"]}  # type: ignore[union-attr]
    assert {name: entry["status"] for name, entry in entries.items()} == {
        "browser": "adoptable",
        "github": "adoptable",
        "shared-agent-memory": "adoptable",
    }
    assert entries["browser"]["unowned_keys"] == []
    assert entries["shared-agent-memory"]["unowned_keys"] == ["tools"]
    config = load_config(config_path)
    assert not mcp_state_path(config, config.targets[0]).exists()

    code, result = _adopt_json(config_path, capsys, "--yes")

    assert code == 0
    assert result["applied"] is True
    assert destination.read_bytes() == before
    state = read_mcp_leaf_state(config, config.targets[0])
    assert {entry.source_id for entry in state.entries} == {"mcp/browser", "mcp/github", "mcp/shared-agent-memory"}
    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert _claude_action(payload)["disposition"] == "noop"

    _write_target_config(tmp_path, product="codex", components=())
    assert cli.main(["apply", "--config", str(config_path), "--yes"]) == 0
    remaining = tomllib.loads(destination.read_text(encoding="utf-8"))
    assert remaining.get("mcp_servers", {}) == {}
    assert destination.read_text(encoding="utf-8").startswith("# Codex servers; this comment must survive adoption.")


def test_codex_adoption_rejects_tables_with_undeclared_runtime_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = LIVE_CODEX.replace(
        "[mcp_servers.github]\n", '[mcp_servers.github]\nstartup_timeout_sec = 30\ncwd = "/srv"\n'
    )
    config_path, _ = _codex_adopt_setup(tmp_path, live)

    code, result = _adopt_json(config_path, capsys, "--name", "github", "--yes")

    assert code == 1
    assert result["entries"][0]["status"] == "mismatch"  # type: ignore[index]
    assert result["entries"][0]["detail"] == "undeclared: cwd, startup_timeout_sec"  # type: ignore[index]
    assert "/srv" not in json.dumps(result)
    config = load_config(config_path)
    assert not mcp_state_path(config, config.targets[0]).exists()


def test_codex_adoption_lists_mismatches_and_never_adopts_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = LIVE_CODEX.replace('"--isolated"]', '"--isolated", "--extra"]').replace(
        'env_vars = ["GITHUB_TOKEN"]', 'env_vars = ["GITHUB_TOKEN"]\nargs = ["--verbose"]'
    )
    config_path, _ = _codex_adopt_setup(tmp_path, live)

    code, result = _adopt_json(config_path, capsys, "--yes")

    assert code == 1
    entries = {entry["name"]: entry for entry in result["entries"]}  # type: ignore[union-attr]
    assert entries["browser"]["status"] == "mismatch"
    assert "args" in entries["browser"]["detail"]
    assert entries["github"]["status"] == "mismatch"
    assert "undeclared" in entries["github"]["detail"]
    assert entries["shared-agent-memory"]["status"] == "adopted"
    config = load_config(config_path)
    state = read_mcp_leaf_state(config, config.targets[0])
    assert {entry.source_id for entry in state.entries} == {"mcp/shared-agent-memory"}
    assert "--extra" not in json.dumps(result)


def test_claude_adoption_records_normalized_exact_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER, github=GITHUB)
    home = _native_home(tmp_path)
    user_config = tmp_path / "home" / ".claude.json"
    user_config.write_text(
        json.dumps(
            {
                "projects": {"/x": {"history": ["SECRET-HISTORY"]}},
                "mcpServers": {
                    "browser": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]},
                    "github": {
                        "type": "stdio",
                        "command": f"{home}/.local/bin/orca-github-mcp",
                        "args": ["--repository", "/somewhere"],
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    before = user_config.read_bytes()

    code, result = _adopt_json(config_path, capsys, "--yes")

    assert code == 1
    entries = {entry["name"]: entry for entry in result["entries"]}  # type: ignore[union-attr]
    assert entries["browser"]["status"] == "adopted"
    assert entries["github"]["status"] == "mismatch"
    assert entries["github"]["detail"] == "live entry differs in: args"
    assert "SECRET-HISTORY" not in json.dumps(result)
    assert "/somewhere" not in json.dumps(result)
    assert user_config.read_bytes() == before
    assert set(_owned(config_path)) == {"browser"}
    assert fake.calls == []

    _, payload = _plan_json(config_path, capsys)
    assert "browser: " not in str(_claude_action(payload)["detail"])
    assert "github: existing MCP server is not bridge-owned" in str(_claude_action(payload)["detail"])


def test_adoption_preview_without_yes_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _codex_adopt_setup(tmp_path)

    assert _adopt(config_path) == 2
    output = capsys.readouterr()
    assert "ADOPTABLE" in output.out
    assert "cancelled" in output.err
    code, _ = _adopt_json(config_path, capsys)
    assert code == 0
    assert not (tmp_path / "state").exists()


def test_adoption_filters_names_and_rejects_unknown_ones(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _codex_adopt_setup(tmp_path)

    code, result = _adopt_json(config_path, capsys, "--name", "github", "--yes")
    assert code == 0
    assert [entry["name"] for entry in result["entries"]] == ["github"]  # type: ignore[union-attr]
    config = load_config(config_path)
    assert {entry.source_id for entry in read_mcp_leaf_state(config, config.targets[0]).entries} == {"mcp/github"}

    code, again = _adopt_json(config_path, capsys, "--name", "github")
    assert code == 0
    assert again["entries"][0]["status"] == "owned"  # type: ignore[index]

    assert _adopt(config_path, "--name", "missing", "--yes") == 2
    assert "missing" in capsys.readouterr().err


def test_adoption_refuses_a_target_on_another_platform(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog(tmp_path, browser=BROWSER)
    other = "windows" if current_platform() is Platform.LINUX else "linux"
    config_path = _write_target_config(tmp_path, product="codex", platform=other)

    assert _adopt(config_path, "--target", "local", "--yes") == 2
    assert "run adopt from that platform" in capsys.readouterr().err
    assert _adopt(config_path, "--yes") == 2
    assert not (tmp_path / "state").exists()


def test_adoption_fails_closed_on_orphaned_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _codex_adopt_setup(tmp_path)
    orphan = tmp_path / "state" / "targets" / "old-target"
    orphan.mkdir(parents=True)
    (orphan / "mcp.json").write_text("{}", encoding="utf-8")

    assert _adopt(config_path, "--yes") == 2
    assert "ownership state has no enabled target" in capsys.readouterr().err


def _no_product_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Doctor version-probes PATH-selected launchers; never reach a real product CLI.
    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)


def _doctor(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, list[dict[str, object]]]:
    _no_product_executable(monkeypatch)
    capsys.readouterr()
    cli.main(["doctor", "--config", str(config_path), "--json"])
    checks = json.loads(capsys.readouterr().out)["checks"]
    grouped: dict[str, list[dict[str, object]]] = {}
    for check in checks:
        grouped.setdefault(check["code"], []).append(check)
    return grouped


def test_doctor_reports_codex_drift_missing_and_unowned_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--name", "browser", "--name", "github", "--yes") == 0
    text = destination.read_text(encoding="utf-8").replace('"npx"', '"pnpx"')
    text = text.replace('env_vars = ["GITHUB_TOKEN"]\n', "")
    destination.write_text(text, encoding="utf-8")

    checks = _doctor(config_path, monkeypatch, capsys)

    assert checks["mcp.manifests"][0]["level"] == "ok"
    assert [check["level"] for check in checks["mcp.drift"]] == ["error"]
    assert "browser" in str(checks["mcp.drift"][0]["message"])
    assert "github" in str(checks["mcp.missing"][0]["message"])
    assert "agentbridge mcp release --target local" in str(checks["mcp.missing"][0]["message"])
    assert "agentbridge mcp release --target local" in str(checks["mcp.drift"][0]["message"])
    assert "shared-agent-memory" in str(checks["mcp.unowned-conflict"][0]["message"])
    assert "pnpx" not in json.dumps(checks)


def test_doctor_reports_claude_conflicts_and_invalid_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _claude_setup(tmp_path, monkeypatch, browser=BROWSER, github=GITHUB)
    assert cli.main(["register", "--config", str(config_path), "--target", "local-claude", "--yes"]) == 0
    user_config = tmp_path / "home" / ".claude.json"
    document = json.loads(user_config.read_text(encoding="utf-8"))
    document["mcpServers"]["browser"]["args"] = ["edited"]
    del document["mcpServers"]["github"]
    user_config.write_text(json.dumps(document), encoding="utf-8")

    checks = _doctor(config_path, monkeypatch, capsys)
    assert "browser" in str(checks["mcp.drift"][0]["message"])
    assert "github" in str(checks["mcp.missing"][0]["message"])

    user_config.write_text('{"oauthAccount": "SECRET-VALUE"', encoding="utf-8")
    checks = _doctor(config_path, monkeypatch, capsys)
    assert checks["mcp.claude-config"][0]["level"] == "error"
    assert "SECRET-VALUE" not in json.dumps(checks)


def test_doctor_reports_manifest_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _catalog(tmp_path, browser=BROWSER.replace("[package]", 'tools = ["x"]\n\n[package]'))
    config_path = _write_target_config(tmp_path, product="codex")
    _no_product_executable(monkeypatch)

    capsys.readouterr()
    assert cli.main(["doctor", "--config", str(config_path), "--json"]) == 1
    checks = {check["code"]: check for check in json.loads(capsys.readouterr().out)["checks"]}

    assert checks["mcp.manifests"]["level"] == "error"
    assert "unknown keys: 'tools'" in str(checks["mcp.manifests"]["message"])


# --- Correction round: table removal, ownership records, doctor, release, and races -------------------------------


def test_plugin_and_mcp_ownership_records_never_touch_each_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _ = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    config = load_config(config_path)
    target = config.targets[0]
    plugins_path = registration_state_path(config, target)

    write_registered_plugins(config, target, ("hello@agent-config-bridge",))
    plugin_bytes = plugins_path.read_bytes()
    assert set(json.loads(plugin_bytes)) == {"schema_version", "target", "identity", "marketplace_source", "plugins"}

    write_claude_mcp_ownership(config, target, (("browser", "a" * 64),))
    assert plugins_path.read_bytes() == plugin_bytes
    mcp_bytes = mcp_state_path(config, target).read_bytes()
    assert json.loads(mcp_bytes)["claude_servers"] == {"browser": "a" * 64}

    write_registered_plugins(config, target, ())
    assert not plugins_path.exists()
    assert mcp_state_path(config, target).read_bytes() == mcp_bytes
    assert read_claude_mcp_ownership(config, target) == (("browser", "a" * 64),)

    write_registered_plugins(config, target, ("hello@agent-config-bridge",))
    write_claude_mcp_ownership(config, target, ())
    assert not mcp_state_path(config, target).exists()
    assert plugins_path.read_bytes() == plugin_bytes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mcp_leaves", [{"source_id": "settings/x/codex", "path": ["mcp_servers", "x", "command"]}]),
        ("mcp_leaves", [{"source_id": "mcp/x", "path": ["mcp_servers", "x"]}]),
        ("mcp_leaves", [{"source_id": "mcp/x", "path": ["mcp_servers", "x", "tools", "y"]}]),
        ("mcp_leaves", [{"source_id": "mcp/x", "path": ["mcp_servers", "other", "command"]}]),
        ("mcp_leaves", [{"source_id": "mcp/x", "path": ["mcp_servers", "x", "env"]}]),
        ("mcp_leaves", [{"source_id": "mcp/x", "path": ["mcp_servers", "x", "tools"]}]),
        ("created_containers", [["mcp_servers", "x", "tools"]]),
        ("created_containers", [["model"]]),
        ("claude_servers", {"x": "a" * 64}),
        ("claude_servers", []),
    ],
)
def test_mcp_state_reader_rejects_tampered_entries(tmp_path: Path, field: str, value: object) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    _apply(config)
    target = config.targets[0]
    path = mcp_state_path(config, target)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert read_mcp_state(config, target).leaves.entries
    if field == "mcp_leaves":
        for entry in value:  # type: ignore[attr-defined]
            entry["value_digest"] = "a" * 64
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BridgeStateError):
        read_mcp_state(config, target)


def test_plan_and_doctor_warn_about_unowned_keys_in_owned_codex_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--yes") == 0
    destination.write_text(
        destination.read_text(encoding="utf-8").replace(
            "[mcp_servers.browser]\n", '[mcp_servers.browser]\nstartup_timeout_sec = 30\nenv = { TOKEN = "SECRET" }\n'
        ),
        encoding="utf-8",
    )
    capsys.readouterr()

    code, payload = _plan_json(config_path, capsys)
    checks = _doctor(config_path, monkeypatch, capsys)

    assert code == 0
    assert any("browser" in warning and "env, startup_timeout_sec" in warning for warning in payload["warnings"])  # type: ignore[union-attr]
    assert [check["level"] for check in checks["mcp.unowned-keys"]] == ["warning"]
    assert "browser (env, startup_timeout_sec)" in str(checks["mcp.unowned-keys"][0]["message"])
    assert "SECRET" not in json.dumps(checks) + json.dumps(payload)
    assert checks["mcp.entries"][0]["level"] == "ok"


def test_doctor_reports_pending_claude_registration_without_marketplace_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)

    def no_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an MCP-only target must not probe the plugin marketplace")

    monkeypatch.setattr("agent_config_bridge.doctor.probe_marketplace_source", no_probe)
    checks = _doctor(config_path, monkeypatch, capsys)

    assert "plugins.marketplace-preflight" not in checks
    assert [check["level"] for check in checks["mcp.pending-registration"]] == ["warning"]
    message = str(checks["mcp.pending-registration"][0]["message"])
    assert "browser" in message and "agentbridge register --target local-claude" in message
    assert fake.calls == []

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    checks = _doctor(config_path, monkeypatch, capsys)
    assert "mcp.pending-registration" not in checks
    assert checks["mcp.entries"][0]["level"] == "ok"


def _release(config_path: Path, *args: str) -> int:
    return cli.main(["mcp", "release", "--config", str(config_path), *args])


def _release_json(config_path: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, object]]:
    capsys.readouterr()
    code = _release(config_path, "--json", *args)
    return code, json.loads(capsys.readouterr().out)


def test_release_recovers_a_missing_owned_codex_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--yes") == 0
    edited = destination.read_text(encoding="utf-8").replace(
        '[mcp_servers.browser]\ncommand = "npx"\nargs = ["-y", "@playwright/mcp@0.0.78", "--headless", "--isolated"]\n',
        "",
    )
    destination.write_text(edited, encoding="utf-8")
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 1
    assert "agentbridge mcp release --target local --name browser" in str(_claude_action(payload)["detail"])
    assert "agentbridge mcp release" in str(_doctor(config_path, monkeypatch, capsys)["mcp.missing"][0]["message"])

    code, preview = _release_json(config_path, capsys, "--name", "browser")
    assert code == 0
    assert preview["applied"] is False
    assert preview["entries"] == [  # type: ignore[comparison-overlap]
        {
            "target": "local",
            "product": "codex",
            "name": "browser",
            "status": "releasable",
            "detail": "forget owned leaves: args, command",
        }
    ]
    config = load_config(config_path)
    assert {entry.source_id for entry in read_mcp_leaf_state(config, config.targets[0]).entries} >= {"mcp/browser"}

    code, result = _release_json(config_path, capsys, "--name", "browser", "--yes")
    assert code == 0
    assert result["entries"][0]["status"] == "released"  # type: ignore[index]
    assert destination.read_text(encoding="utf-8") == edited
    state = read_mcp_leaf_state(config, config.targets[0])
    assert {entry.source_id for entry in state.entries} == {"mcp/github", "mcp/shared-agent-memory"}
    assert ("mcp_servers", "browser") not in state.created_containers

    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert "create=2" in str(_claude_action(payload)["detail"])
    assert cli.main(["apply", "--config", str(config_path), "--yes"]) == 0
    assert "browser" in tomllib.loads(destination.read_text(encoding="utf-8"))["mcp_servers"]
    capsys.readouterr()


def test_release_recovers_drifted_and_missing_claude_registrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER, github=GITHUB)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    user_config = tmp_path / "home" / ".claude.json"
    document = json.loads(user_config.read_text(encoding="utf-8"))
    document["mcpServers"]["browser"]["args"] = ["edited"]
    del document["mcpServers"]["github"]
    user_config.write_text(json.dumps(document), encoding="utf-8")
    before = user_config.read_bytes()
    capsys.readouterr()
    code, payload = _plan_json(config_path, capsys)
    assert code == 1
    detail = str(_claude_action(payload)["detail"])
    assert "agentbridge mcp release --target local-claude --name browser" in detail
    assert "agentbridge mcp release --target local-claude --name github" in detail

    assert _release(config_path) == 2
    assert "cancelled" in capsys.readouterr().err
    assert set(_owned(config_path)) == {"browser", "github"}

    assert _release(config_path, "--yes") == 0
    assert user_config.read_bytes() == before
    assert _owned(config_path) == {}
    fake.calls.clear()
    capsys.readouterr()

    code, payload = _plan_json(config_path, capsys)
    detail = str(_claude_action(payload)["detail"])
    assert code == 1
    assert "browser: existing MCP server is not bridge-owned" in detail
    assert [command["argv"][2:6] for command in payload["commands"]] == [  # type: ignore[union-attr]
        ["add-json", "--scope", "user", "github"]
    ]


def test_release_frees_a_deselected_target_from_a_drifted_owned_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    user_config = tmp_path / "home" / ".claude.json"
    document = json.loads(user_config.read_text(encoding="utf-8"))
    document["mcpServers"]["browser"]["args"] = ["edited"]
    user_config.write_text(json.dumps(document), encoding="utf-8")
    _write_claude_config(tmp_path, components=())
    capsys.readouterr()
    assert _plan_json(config_path, capsys)[0] == 1

    assert _adopt(config_path, "--yes") == 2
    assert "select the mcp component" in capsys.readouterr().err
    assert _release(config_path, "--target", "local-claude", "--yes") == 0
    assert "product configuration was not changed" in capsys.readouterr().out

    code, payload = _plan_json(config_path, capsys)
    assert code == 0
    assert not any(action["component"] == "mcp" for action in payload["actions"])  # type: ignore[union-attr]
    assert _user_servers(user_config)["browser"]["args"] == ["edited"]  # type: ignore[index]
    assert fake.calls == [("claude", "mcp", "add-json", "--scope", "user", "browser", _browser_json())]


def test_release_rejects_unknown_names_other_platforms_and_orphaned_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--name", "github", "--yes") == 0
    capsys.readouterr()

    assert _release(config_path, "--name", "browser", "--yes") == 2
    assert "not owned by the selected targets: browser" in capsys.readouterr().err

    other = "windows" if current_platform() is Platform.LINUX else "linux"
    _write_target_config(tmp_path, product="codex", platform=other)
    assert _release(config_path, "--target", "local", "--yes") == 2
    assert "run release from that platform" in capsys.readouterr().err

    _write_target_config(tmp_path, product="codex")
    orphan = tmp_path / "state" / "targets" / "old-target"
    orphan.mkdir(parents=True)
    (orphan / "mcp.json").write_text("{}", encoding="utf-8")
    assert _release(config_path, "--yes") == 2
    assert "ownership state has no enabled target" in capsys.readouterr().err
    config = load_config(config_path)
    assert {entry.source_id for entry in read_mcp_leaf_state(config, config.targets[0]).entries} == {"mcp/github"}


def _mutate_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mutate: Callable[[], None],
    *,
    before: bool,
) -> None:
    original = getattr(cli, name)
    calls = 0

    def wrapper(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2 and before:
            mutate()
        result = original(*args, **kwargs)
        if calls == 2 and not before:
            mutate()
        return result

    monkeypatch.setattr(cli, name, wrapper)


def _add_unowned_browser(user_config: Path) -> None:
    document = json.loads(user_config.read_text(encoding="utf-8")) if user_config.exists() else {}
    document.setdefault("mcpServers", {})["browser"] = {"command": "user-browser"}
    user_config.write_text(json.dumps(document), encoding="utf-8")


def test_register_refuses_an_entry_that_appeared_after_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    user_config = tmp_path / "home" / ".claude.json"
    _mutate_on_second_call(monkeypatch, "_claude_mcp_plans", lambda: _add_unowned_browser(user_config), before=False)

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert "MCP server 'browser' appeared after review" in capsys.readouterr().err
    assert fake.calls == []
    assert _owned(config_path) == {}
    assert _user_servers(user_config) == {"browser": {"command": "user-browser"}}


def test_register_refuses_to_remove_an_entry_that_changed_after_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    owned = _owned(config_path)
    _write_claude_config(tmp_path, components=())
    user_config = tmp_path / "home" / ".claude.json"

    def edit() -> None:
        document = json.loads(user_config.read_text(encoding="utf-8"))
        document["mcpServers"]["browser"]["args"] = ["edited"]
        user_config.write_text(json.dumps(document), encoding="utf-8")

    _mutate_on_second_call(monkeypatch, "_claude_mcp_plans", edit, before=False)
    fake.calls.clear()

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert "MCP server 'browser' changed after review" in capsys.readouterr().err
    assert fake.calls == []
    assert _owned(config_path) == owned
    assert _user_servers(user_config)["browser"]["args"] == ["edited"]  # type: ignore[index]


def test_register_refuses_when_the_fresh_mcp_plan_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    user_config = tmp_path / "home" / ".claude.json"
    _mutate_on_second_call(monkeypatch, "_claude_mcp_plans", lambda: _add_unowned_browser(user_config), before=True)

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert "Claude MCP registration state changed" in capsys.readouterr().err
    assert fake.calls == []
    assert _owned(config_path) == {}


def test_failed_remove_of_an_already_removed_entry_still_releases_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    _write_claude_config(tmp_path, components=())
    fake.fail_after = frozenset({("remove", "browser")})

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0

    assert _owned(config_path) == {}
    assert _user_servers(tmp_path / "home" / ".claude.json") == {}


def test_failed_remove_of_a_present_entry_keeps_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, fake = _claude_setup(tmp_path, monkeypatch, browser=BROWSER)
    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 0
    _write_claude_config(tmp_path, components=())
    fake.fail = frozenset({("remove", "browser")})

    assert cli.main(["register", "--config", str(config_path), "--yes"]) == 2

    assert set(_owned(config_path)) == {"browser"}


def test_adoption_refuses_when_entries_change_before_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)

    def edit() -> None:
        destination.write_text(
            destination.read_text(encoding="utf-8").replace('"--isolated"]', '"--isolated", "--extra"]'),
            encoding="utf-8",
        )

    _mutate_on_second_call(monkeypatch, "adoption_entries", edit, before=True)

    assert _adopt(config_path, "--yes") == 2

    assert "changed during adoption" in capsys.readouterr().err
    config = load_config(config_path)
    assert not mcp_state_path(config, config.targets[0]).exists()


def test_apply_refuses_mcp_sources_that_change_after_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    plan, inventory = _plan(config)
    original = applier_module.discover_mcp_servers

    def edited_sources(root: Path) -> object:
        _write_server(catalog, "browser", BROWSER.replace('"--isolated"', '"--isolated", "--no-sandbox"'))
        return original(root)

    monkeypatch.setattr(applier_module, "discover_mcp_servers", edited_sources)

    with pytest.raises(ApplyError, match="MCP server sources changed after planning"):
        apply_plan(config, inventory, plan)  # type: ignore[arg-type]

    assert not (config.targets[0].config_home / "config.toml").exists()
    assert not mcp_state_path(config, config.targets[0]).exists()


# --- Correction round 2: claimed server tables, comments, escaping, and release races ------------------------


@pytest.mark.parametrize(
    ("name", "existing"),
    [
        pytest.param("browser", '[mcp_servers.browser]\nenv = { TOKEN = "SECRET-VALUE" }\n', id="env"),
        pytest.param(
            "shared-agent-memory",
            '[mcp_servers.shared-agent-memory]\nhttp_headers = { Authorization = "Bearer SECRET-VALUE" }\n',
            id="http_headers",
        ),
        pytest.param(
            "shared-agent-memory", '[mcp_servers.shared-agent-memory]\nbearer_token = "SECRET-VALUE"\n', id="bearer"
        ),
        pytest.param(
            "browser", '[mcp_servers.browser.tools.browser_navigate]\napproval_mode = "approve"\n', id="tools-only"
        ),
        pytest.param(
            "shared-agent-memory",
            '[mcp_servers.shared-agent-memory]\ncommand = "SECRET-VALUE-launcher"\n',
            id="mixed-transport",
        ),
    ],
)
def test_codex_mcp_never_merges_into_an_existing_unowned_server_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    existing: str,
) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER, **{"shared-agent-memory": MEMORY})
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(existing, encoding="utf-8")

    plan, inventory = _plan(config)
    action = _mcp_action(plan)

    assert action.disposition is Disposition.CONFLICT
    assert f"{name}: unowned" in action.detail
    assert "SECRET-VALUE" not in json.dumps([action.detail, *plan.warnings, *plan.reviews])
    with pytest.raises(ApplyError, match="refusing to apply a plan with conflicts"):
        apply_plan(config, inventory, plan)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == existing
    assert not mcp_state_path(config, config.targets[0]).exists()

    monkeypatch.setattr("agent_config_bridge.doctor.shutil.which", lambda _name: None)
    unowned = [check for check in run_doctor(config, inventory, plan) if check.code == "mcp.unowned-conflict"]  # type: ignore[arg-type]
    assert [check.level for check in unowned] == [CheckLevel.ERROR]
    assert name in unowned[0].message
    assert "SECRET-VALUE" not in unowned[0].message


def test_codex_mcp_deselection_keeps_the_comment_above_the_next_table(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, browser=BROWSER)
    config = make_config(tmp_path, catalog, components=frozenset({Component.MCP}))
    destination = config.targets[0].config_home / "config.toml"
    destination.parent.mkdir(parents=True)
    destination.write_text(USER_CONFIG, encoding="utf-8")
    _apply(config)
    note = "# NOTE: zeta stays; it is managed by hand.\n"
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(f'\n{note}[mcp_servers.zeta]\ncommand = "zeta-mcp"\n')

    _apply(_with_components(config, frozenset()))

    text = destination.read_text(encoding="utf-8")
    assert f'{note}[mcp_servers.zeta]\ncommand = "zeta-mcp"\n' in text
    assert text.startswith(USER_CONFIG)
    assert "browser" not in text


def test_codex_mcp_removal_warns_about_the_tool_approvals_it_deletes(tmp_path: Path) -> None:
    config, destination = _codex_memory_with_tools(tmp_path)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write('[mcp_servers.shared-agent-memory.tools.memory_get]\napproval_mode = "SECRET-MODE"\n')

    plan, _ = _plan(_with_components(config, frozenset()))

    assert (
        f"{config.targets[0].name}: removing mcp_servers.shared-agent-memory also deletes its tools approvals (2)"
        in (plan.warnings)
    )
    assert "SECRET-MODE" not in json.dumps(plan.warnings)


def test_live_key_names_are_escaped_in_plan_doctor_and_adoption_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, destination = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--name", "browser", "--yes") == 0
    destination.write_text(
        destination.read_text(encoding="utf-8")
        .replace("[mcp_servers.browser]\n", '[mcp_servers.browser]\n"\\u001b[2Jbrowser" = 1\n')
        .replace("[mcp_servers.github]\n", '[mcp_servers.github]\n"\\u001b]0;github\\u0007" = 1\n'),
        encoding="utf-8",
    )
    capsys.readouterr()

    _, payload = _plan_json(config_path, capsys)
    checks = _doctor(config_path, monkeypatch, capsys)
    capsys.readouterr()
    _adopt(config_path, "--name", "github")
    adoption_text = capsys.readouterr().out

    warning = next(warning for warning in payload["warnings"] if "browser" in warning)  # type: ignore[union-attr]
    assert "\\x1b[2Jbrowser" in warning
    message = str(checks["mcp.unowned-keys"][0]["message"])
    assert "\\x1b[2Jbrowser" in message
    assert "undeclared: \\x1b]0;github\\x07" in adoption_text
    for text in (warning, message, adoption_text):
        assert "\x1b" not in text
        assert "\x07" not in text


def test_release_refuses_when_ownership_changes_before_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _ = _codex_adopt_setup(tmp_path)
    assert _adopt(config_path, "--name", "browser", "--name", "github", "--yes") == 0

    def adopt_another() -> None:
        assert _adopt(config_path, "--name", "shared-agent-memory", "--yes") == 0

    _mutate_on_second_call(monkeypatch, "release_entries", adopt_another, before=True)
    capsys.readouterr()

    assert _release(config_path, "--yes") == 2

    assert "ownership changed during release" in capsys.readouterr().err
    config = load_config(config_path)
    assert {entry.source_id for entry in read_mcp_leaf_state(config, config.targets[0]).entries} == {
        "mcp/browser",
        "mcp/github",
        "mcp/shared-agent-memory",
    }
