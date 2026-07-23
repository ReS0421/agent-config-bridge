# Agent Config Bridge Onboarding
**Version 1.1.0** · Agent Config Bridge · 2026-07-15 · Windows, Linux, WSL

---

## AI READING INSTRUCTION

Read every `[SPEC]` and `[BUG]` block before proposing commands or changing a
catalog. Read `[NOTE]` blocks when choosing host paths or resolving migration
conflicts. Never recommend `apply` or `register` while a `[BUG]` precondition is
unresolved.

---

## 1. Safety Boundary

**[SPEC]**
- Share only selected Skills, Plugins, Hooks, public Settings, and host-managed Schedules.
- Keep authentication, sessions, caches, logs, databases, trust decisions, and whole product homes local.
- Keep one canonical catalog accessible to every host, but use one host-local `state_dir` per Windows, Linux, or WSL runtime.
- Run `register` on the target operating system. Do not register a Windows target from WSL or a WSL target from PowerShell.
- Keep a personal migrated catalog private until Skill provenance and redistribution rights are verified.
- Treat `plan` and `doctor` as mandatory gates before every `apply` or `register`.

**[NOTE]**
A catalog on an NTFS directory is directly accessible as `C:\...` on Windows
and `/mnt/c/...` in WSL. Linux targets can link to it, while Windows targets use
managed copies in `link_mode = "auto"`.

## 2. Prerequisites

**[SPEC]**
| Host | Required | Verification |
| --- | --- | --- |
| All | Python 3.11+ | `python --version` |
| Development | `uv` | `uv --version` |
| Codex Plugins | Codex with `plugin marketplace` and `plugin add/remove` | `codex plugin --help` |
| Claude Plugins | Claude Code with JSON marketplace listing and supported uninstall flags | `claude plugin --help` |
| Linux/WSL Schedules | `crontab` | `command -v crontab` |
| Windows Schedules | native `.exe` or `.com` Agent Bridge and product CLIs | `Get-Command agentbridge, codex, claude` |

Install from a clone:

```bash
uv sync --extra dev
uv run agentbridge --help
```

For a stable console entry point used by Schedules, install with `pipx`, `pip`,
or `uv tool`; do not rely on a temporary shell alias. Native Windows Schedule
registration rejects a PowerShell or CMD wrapper: both `agentbridge` and the
selected product command must resolve to reviewed `.exe` or `.com` files.

**[BUG] Incompatible product CLI**
- Symptom: Plugin preflight rejects a JSON listing, or add/remove/install/uninstall flags are unknown.
- Cause: the product CLI selected by `target.executable` or `PATH` lacks the Plugin workflow, returned undecodable JSON, or changed to an unknown schema. Codex 0.144.4's absolute `root`-only local marketplace record and 0.144.6's expanded local record are both supported, so a 0.144.4 root-only listing alone is not an upgrade signal.
- Fix: run `doctor` on the target host and inspect `plugins.marketplace-preflight`. Upgrade the host-native CLI only when its command surface is missing or its schema is unsupported, or set `target.executable` to a reviewed absolute executable. Codex must support `plugin marketplace list --json` plus Plugin and marketplace add/remove. Claude Code must support `plugin marketplace list --json`, `plugin list --json`, marketplace add/remove, and Plugin install/uninstall.

## 3. Create or Migrate the Canonical Catalog

**[SPEC]**
For a new empty catalog, run `agentbridge init`. For existing user Skill roots,
run a dry migration first. Source order is canonical priority:

```bash
agentbridge migrate-skills \
  --source linux-agents="$HOME/.agents/skills" \
  --source windows-agents=/mnt/c/Users/USER/.agents/skills \
  --source windows-codex=/mnt/c/Users/USER/.codex/skills \
  --source windows-claude=/mnt/c/Users/USER/.claude/skills \
  --catalog /mnt/c/Users/USER/AgentConfig/catalog \
  --conflicts /mnt/c/Users/USER/AgentConfig/conflicts \
  --report /mnt/c/Users/USER/AgentConfig/reports/skill-migration.md \
  --repair-legacy-frontmatter
```

Review the printed plan, then repeat with `--yes`. The command:

- deduplicates text content across CRLF and LF checkouts;
- materializes contained file links as portable regular files;
- excludes `__pycache__`, `.pyc`, `.pyo`, and `.DS_Store` artifacts;
- enforces a 100 MiB per-Skill read bound and scans every accepted file for high-confidence secret formats without writing secret values to reports;
- adds minimal `name` and `description` frontmatter only when explicitly requested;
- chooses the first source for same-name conflicts and preserves every distinct variant below `conflicts/`;
- rejects symlink, junction, or reparse-point redirects in catalog, conflict, and report output paths;
- writes a HADS Markdown report and an adjacent JSON report.

The report path must end in `.md` and remain outside every source, the catalog,
and the conflict store. `--json` emits exactly one machine-readable document;
without `--yes`, it is a non-writing dry run.

Exit code `1` means review remains, including an unapplied dry-run or retained
content conflicts. Exit code `2` means validation or execution failed.

**[BUG] Same-name Skill conflict**
- Symptom: the migration report marks a Skill as `conflict`.
- Cause: two source roots contain different content under the same Skill name.
- Fix: compare the preserved variants and create a product-neutral canonical version. Record the resolution in version control, then either harmonize or retire the divergent legacy sources before rerunning migration; rerunning the unchanged sources will retain the conflict. Never discard variants before review.

**[BUG] Missing redistribution rights**
- Symptom: a selected Skill has no license or pinned provenance in the migration report or accompanying audit.
- Cause: local installation metadata does not prove public redistribution permission.
- Fix: keep the catalog private, record `source_url`, revision, license, and attribution, and publish only after rights are confirmed.

## 4. Configure Hosts

**[SPEC]**
Use separate configuration files and state directories for WSL and Windows.
The following WSL pattern supports a default Codex CLI and a launcher-injected
Codex home that consume the same `~/.agents/skills`. Exactly one target selects
`skills`; the other is a passive consumer.

```toml
schema_version = 1

[bridge]
catalog = "/mnt/c/Users/USER/AgentConfig/catalog"
state_dir = "/home/USER/.local/state/agent-config-bridge/wsl"
link_mode = "auto"
components = ["skills", "plugins", "hooks", "settings", "schedules"]

[[targets]]
name = "wsl-codex-launcher"
product = "codex"
platform = "linux"
user_home = "/home/USER"
config_home = "/absolute/launcher/injected/CODEX_HOME"
executable = "/absolute/path/to/codex"
components = ["skills", "plugins", "hooks", "settings"]
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "wsl-codex-cli"
product = "codex"
platform = "linux"
user_home = "/home/USER"
config_home = "/home/USER/.codex"
executable = "/absolute/path/to/codex"
components = ["plugins", "hooks", "settings", "schedules"]
surfaces = ["cli"]
enabled = true

[[targets]]
name = "wsl-claude-code"
product = "claude-code"
platform = "linux"
user_home = "/home/USER"
config_home = "/home/USER/.claude"
executable = "/absolute/path/to/claude"
components = ["skills", "plugins", "hooks", "settings"]
surfaces = ["cli"]
enabled = true
```

Use a native Windows configuration with Windows paths and a different
`state_dir`. One target can cover CLI and Desktop when both use the same product
home:

```toml
schema_version = 1

[bridge]
catalog = 'C:\Users\USER\AgentConfig\catalog'
state_dir = 'C:\Users\USER\AppData\Local\AgentConfigBridge\state'
link_mode = "auto"
components = ["skills", "plugins", "hooks", "settings", "schedules"]

[[targets]]
name = "windows-codex"
product = "codex"
platform = "windows"
user_home = 'C:\Users\USER'
config_home = 'C:\Users\USER\.codex'
executable = 'C:\absolute\path\to\codex.exe'
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "windows-claude-code"
product = "claude-code"
platform = "windows"
user_home = 'C:\Users\USER'
config_home = 'C:\Users\USER\.claude'
executable = 'C:\absolute\path\to\claude.exe'
components = ["skills", "plugins", "hooks", "settings"]
surfaces = ["cli", "desktop"]
enabled = true
```

Target-level `components` replaces the bridge-level list. Use it to prevent
duplicate Schedule execution and to assign one writer for a shared Skill root.
The WSL Claude target above covers the WSL CLI only. Register Plugins for Claude
Code Desktop from the native Windows target and product home; a WSL registration
does not configure the Windows Desktop process.

**[BUG] Default Claude profile redirected**
- Symptom: Claude creates or reads `<user_home>/.claude/.claude.json` instead of the normal `<user_home>/.claude.json`.
- Cause: a launcher or older Bridge version set `CLAUDE_CONFIG_DIR` to the default `.claude` directory.
- Fix: leave `CLAUDE_CONFIG_DIR` unset for the default profile. Agent Config Bridge removes an inherited override for default-profile subprocesses and sets it only for a genuinely custom `config_home`.

**[BUG] Skill discovery root redirected**
- Symptom: `doctor` reports `skills.discovery-root-redirected`.
- Cause: the Skill root or an existing parent is a symlink, junction, or reparse point, often redirecting WSL into a Windows product home.
- Fix: migrate content first, replace the root-level redirect with separate local roots, then let Bridge create individual managed links or copies. Do not remove the redirect before preserving its content.

**[BUG] WSL product home inherited by native Windows**

- Symptom: a Windows Codex installer or CLI launched through `powershell.exe`
  from WSL treats a Linux `CODEX_HOME` as a `\\wsl.localhost\...` path, emits
  path warnings, or waits on the wrong installation lock.
- Cause: the native child process inherits the WSL session environment.
- Fix: prefer a normal native PowerShell window. For an intentional cross-launch,
  clear `$env:CODEX_HOME` before invoking native Codex or Agent Bridge; keep the
  target's native `config_home` and executable explicit in the Windows TOML.

## 5. Adopt Existing Destination Roots

**[SPEC]**

Bridge deliberately refuses to adopt an unmanaged directory or link, even when
its bytes match the catalog. An adoption is therefore an explicit root
transition, not an overwrite and not a fabricated ownership record:

1. Close every Codex, Claude Code, and launcher process that can recreate or
   write the destination. Take both an immutable archive and a same-filesystem
   live hold of every complete root.
2. Verify archive hashes and compare the archives with the live roots.
3. Move each complete unmanaged root to its live hold. Preserve root-level
   redirects as links; do not dereference them or move only the names that
   currently appear as conflicts.
4. Create an empty local destination root. For WSL Claude, this means replacing
   a Windows-home redirect with a real WSL directory while leaving the Windows
   root for a separate native-Windows adoption.
5. Run `doctor`, then `plan`. Require zero errors, zero discovery-root redirect
   findings, and zero conflict actions. A first adoption plan should contain
   only reviewed `create` actions.
6. Run `apply`, then rerun `doctor` and `plan`. Convergence requires zero
   conflicts, zero changes, and only `noop` actions.
7. Repeat independently in native Windows with its own config, physical roots,
   and state directory. Restart products once to refresh discovery.

If a removed root redirect reappears, a product or synchronization process is
still active. Stop the transition, preserve the recreated link, close the
writer, and repeat the zero-conflict gate. Do not delete it repeatedly or let
`apply` run against the redirected root.

**[BUG] Importing a managed destination as a new source**

- Symptom: a later migration recreates retired Skills or reports old conflicts.
- Cause: the one-time importer was pointed at a Bridge-rendered, linked, or
  copied consumer root.
- Fix: make future changes only in the canonical catalog. Keep migration source
  roots retired after adoption.

**[NOTE]**

`apply` is sequential rather than transactionally atomic across every target.
For rollback, first move the new roots and host-local state into a fresh
forensic directory, then restore the held pre-transition roots. Never delete
partial output or ownership state before preserving it. `register` is not
needed for a Skill-only catalog.

## 6. Validate, Apply, and Register

**[SPEC]**
Run this sequence independently on each host:

```bash
agentbridge validate -c /path/to/host.toml
agentbridge doctor -c /path/to/host.toml
agentbridge plan -c /path/to/host.toml
agentbridge apply -c /path/to/host.toml --yes
agentbridge register -c /path/to/host.toml --yes
```

Proceed only when:

- the catalog validates;
- no plan action is `conflict`;
- every discovery-root redirect warning is understood and intentionally resolved;
- every Hook command and Plugin MCP command/URL review item is approved;
- `doctor` reports the intended configured or PATH-selected host-native
  executable and its expected `--version` output;
- each recurring Schedule is assigned to exactly the intended target.

`apply` writes standalone Skills, selected Settings, rendered marketplace state,
and Schedule snapshots. `register` performs product CLI registration and host
scheduler reconciliation. Product permission and trust prompts remain
product-owned.

After registration, rerun `doctor` and `plan`, inspect the product's Plugin list,
and confirm the scheduler heartbeat on the same host. Convergence means no
unexpected conflict, no unrelated marketplace owner, the intended Plugin set is
installed, and only the intended Schedule target owns a heartbeat.

## 7. Update and Recovery Workflow

**[SPEC]**
- Edit only the canonical catalog, then rerun `validate → doctor → plan → apply → register` on each host.
- Bump Plugin manifest versions and `hooks/.version` when rendered content changes.
- Before moving or deleting a target, restore its old identity, set `components = []`, and reconcile `apply` plus `register`.
- Never delete ownership state to force adoption.
- Keep conflict variants and migration reports until every target has converged and a backup has been verified.
- Keep an external backup of every pre-existing destination. Bridge retains backups for selected managed Skill copy updates, but it does not provide a universal automatic rollback for product CLI registration, Settings changes, symlinks, or host scheduler state.

**[BUG] Partial external registration**
- Symptom: a product command succeeds but ownership state is not written because the process stops.
- Cause: product CLI mutation and Bridge state writes are separate operations.
- Fix: rerun `plan` and `register`. Ownership preflight permits only the expected Bridge marketplace source and fails closed on an unrelated owner.

## 8. Changelog

**[SPEC]**
- 1.0.0: Added end-to-end Windows, Linux, WSL onboarding; private Skill migration; shared-root single-writer configuration; Plugin executable selection; and failure recovery.
- 1.1.0: Added backed-up unmanaged-root adoption, redirect-recreation handling,
  zero-conflict convergence, and root-based rollback.
