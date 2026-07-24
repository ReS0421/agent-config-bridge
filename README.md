# Agent Config Bridge

One canonical catalog for Skills, Plugins, Hooks, Settings, Schedules, and
Instructions across:

- Codex CLI and Codex Desktop
- Claude Code CLI and Claude Code Desktop
- native Windows, Linux, and WSL-accessible homes

Agent Config Bridge does not pretend the products use identical formats. Skills
can usually be shared directly. Plugins and Hooks are rendered into
product-specific packages, Settings are merged into public product files by
owned leaf, Schedules are executed through host schedulers and product CLIs,
and Instructions are per-product policy files linked or copied file-by-file
into the product configuration home. All six component classes still come from
one version-controlled catalog.

> **Status: alpha.** The safety model and core workflow are implemented, but product schemas continue to evolve. Always review `agentbridge plan` before applying or registering anything.

## Why this exists

Codex and Claude Code increasingly share the Agent Skills layout and similar plugin/hook concepts, but their discovery roots, manifests, marketplace files, caches, trust flows, and lifecycle events differ. Environment-specific homes such as `CODEX_HOME` make a directory-only symlink strategy brittle.

Agent Config Bridge separates four concerns:

1. **Canonical source** — one Git repository containing reviewed Skills, Plugin
   overlays, Hook bundles, product-specific Settings fragments, and portable
   Schedule definitions.
2. **Rendering** — separate Codex and Claude Code packages in immutable,
   content-addressed builds, plus immutable per-target Schedule snapshots.
3. **Installation** — conflict-aware links or managed copies for standalone
   Skills and owned-leaf Settings patches.
4. **Registration** — explicit product CLI registration for Plugins/Hooks and
   an ownership-aware host heartbeat for Schedules.

It never shares auth tokens, session databases, caches, logs, trust stores, or an entire product configuration home.

## Current capabilities

- Strict TOML configuration with global and per-target component selection
- Read-only `validate`, `plan`, and `doctor` commands
- Linux symlinks and a safe Windows copy fallback (`link_mode = "auto"`)
- Target-scoped ownership records for standalone Skills, Settings leaves,
  bridge-registered Plugins/Hooks, and host scheduler heartbeats
- Drift detection, deselection reconciliation, and retained backups for managed
  Skill copy updates/removals
- A visible `AGENTBRIDGE-MANAGED.json` provenance marker at every deployed
  skill root, so humans and agents inspecting the directory can tell it is a
  managed projection of the catalog rather than an accidental duplicate
  (written by `apply`, checked by `doctor`)
- A governance core (hand-authored `catalog/governance/*.toml` manifests as
  source of truth, ADR-1 capability axes, `GovernanceFinding` diagnostics) with
  `registry generate` writing a byte-deterministic committed
  `catalog/registry.json` and `registry check` gating drift; the active mode is
  the committed `catalog/governance/policy.toml` (audit implemented)
- An `instructions` component (ADR-5 in the catalog repo) deploying always-on
  policy files from per-product bundle overlays to a strict destination
  allowlist (Claude Code: `CLAUDE.md`, `rules/**`, `agents/**`, `commands/**`;
  Codex: `AGENTS.md`, `agents/**`) with per-file single ownership, no
  render-time merging, newline-normalized drift detection, and
  `AGENTBRIDGE-MANAGED.json` markers on managed instruction directories
- Canonical Codex Skill destination: `~/.agents/skills`
- Claude Code Skill destination: `~/.claude/skills`
- Dual plugin source overlays with `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json`
- Product-specific local marketplace catalogs backed by immutable builds and
  published at `<state_dir>/marketplace`
- Shared hook bundles packaged as a synthetic plugin instead of overwriting user settings
- Product-specific Codex `config.toml` and Claude Code `settings.json`
  fragments merged by explicit owned leaf, preserving unrelated local values
- Portable five-field cron definitions with IANA time zones, per-target
  immutable snapshots, overlap protection, and manual-run support
- One target-scoped, once-per-minute heartbeat in the Linux user crontab or
  Windows Task Scheduler; the heartbeat evaluates all due canonical Schedules
- Registration-time discovery or an optional per-target override pins the
  product CLI to an absolute executable path for unattended runs
- Explicit `register` command that refreshes and reconciles bridge-managed
  marketplace installations and host scheduler heartbeats
- POSIX and copyable PowerShell command previews according to target platform
- Catalog validation for every nested Windows path component, with only
  contained file symlinks accepted
- Conservative multi-root Skill migration with deterministic priority,
  conflict retention, secret-pattern scanning, legacy frontmatter repair, and
  HADS Markdown plus JSON reports

## Install for development

```bash
git clone https://github.com/ReS0421/agent-config-bridge.git
cd agent-config-bridge
uv sync --extra dev
uv run agentbridge --help
```

The runtime supports Python 3.11+, uses PyYAML for Skill metadata, and uses
`tomlkit` for comment-preserving Codex Settings edits. Native Windows also
installs `tzdata` so IANA Schedule timezones do not depend on a system timezone
database. `uv` is used for reproducible development, but the package can also
be installed with `pipx` or `pip`.

## Quick start

Create a new bridge catalog:

```bash
agentbridge init
```

Or migrate existing user Skill roots into a private canonical catalog before
configuring targets:

```bash
agentbridge migrate-skills \
  --source linux-agents="$HOME/.agents/skills" \
  --source windows-agents=/mnt/c/Users/USER/.agents/skills \
  --catalog /mnt/c/Users/USER/AgentConfig/catalog \
  --conflicts /mnt/c/Users/USER/AgentConfig/conflicts \
  --report /mnt/c/Users/USER/AgentConfig/reports/skill-migration.md
```

The command is read-only unless `--yes` is supplied. Its `.md` report must stay
outside every source, the catalog, and the conflict store; `--json` produces one
machine-readable document. Review retained conflicts and license/provenance
before applying the migrated catalog. See the
[Windows, Linux, and WSL onboarding guide](docs/onboarding.md) for the complete
workflow and four-root migration example.

Add canonical artifacts below `catalog/`, then inspect everything before writing:

```bash
agentbridge validate
agentbridge doctor
agentbridge plan
```

Apply standalone Skill links/copies, merge selected Settings, publish the local
marketplace, and render per-target Schedule snapshots:

```bash
agentbridge apply --yes
```

`apply` records the standalone Skills, Instruction files, and Settings leaves
it manages and prints the product and host commands needed to activate rendered
Plugins, Hooks, and Schedules.

For automation that must update only standalone Skills, use the fail-closed
skill-only command:

```bash
agentbridge sync-skills -c agentbridge.toml --yes
```

`sync-skills` still builds and rechecks the complete plan. It refuses to write
when any component conflicts or when Plugins, Hooks, Settings, Schedules, or
Instructions have a pending create, update, or removal. It never renders those
components and never runs registration commands. A converged run does not ask
for confirmation. When `link_mode` changes from `symlink` to `copy`, a
still-matching Bridge-owned Skill link is atomically replaced by a marked copy
and the prior link is retained in the backup tree; changed or unowned links
remain conflicts. If a multi-Skill migration stops after installing only some
copies, the next plan resumes copies whose ownership marker and installed,
current, and canonical source digests all still match. Each new Skill is
recorded in ownership state before its filesystem create, then that checkpoint
is rolled back if the create fails; an abrupt stop therefore leaves durable
evidence for any completed or pending create. Even a no-change invocation
revalidates the full plan and reconciles Skill ownership/provenance, allowing a
previously completed removal to clear stale state without prompting. Copy
updates first create and verify a non-destructive final backup snapshot. Only
after revalidating the live destination do they swap it aside on its own
filesystem, atomically install the staged copy, and remove the redundant swap.
Managed-copy removals use the same snapshot-and-revalidate rule. If removing
the local swap partially fails, the Bridge reconstructs and verifies the prior
destination from the retained backup through a fresh same-filesystem staging
path; it never restores the damaged swap or consumes the only verified backup.

Generated marketplace builds and managed Skill backups have bounded retention:

```bash
agentbridge state prune -c agentbridge.toml --json
agentbridge state prune -c agentbridge.toml --yes --json
```

The first command is a byte-preserving dry run. The second deletes only the
reviewed, revalidated candidates. Any malformed entry, redirected ancestor, or
ownership/digest mismatch found by the locked fresh plan blocks all deletion.
An identity change detected later stops before deleting that changed candidate;
earlier candidates from the same reviewed plan may already have been removed.
The currently published marketplace build is always retained, terminal symlink
snapshots are unlinked without following their target, and Instruction backups
are outside this retention policy. Applying deletions also fails closed on a
platform whose Python runtime lacks descriptor-anchored removal.

Registration remains deliberately separate:

```bash
agentbridge register --target local-codex --yes
agentbridge register --target local-claude-code --yes
```

Omit `--target` to reconcile every enabled target for the current operating
system; targets for another OS are left for registration on that OS. For a
target with `schedules` selected, `register` installs or updates one heartbeat
in that host's current-user scheduler. Run one published Schedule immediately
without changing its cadence with:

```bash
agentbridge schedule run --target local-codex --name daily-review
```

Run registration from the target operating system. A WSL process may render a
Windows target, but `plan` omits registration commands when the current host and
target platforms differ so Linux paths are never presented as copyable native
Windows commands.
The catalog stays canonical; host-specific TOML files may use different native
paths while pointing at that same catalog. Keep `state_dir` host-local at a
stable path—do not share one operational state directory between native Windows,
WSL, and Linux processes.

Use `agentbridge register`, rather than running the printed product commands by
hand, when you want later deselection to be reconciled automatically. Manual
commands bypass the bridge ownership record.

## Configuration

```toml
schema_version = 1

[bridge]
catalog = "./catalog"
state_dir = "./.agentbridge"
link_mode = "auto" # auto | symlink | copy
components = ["skills", "plugins", "hooks", "settings", "schedules", "instructions"]

[bridge.retention]
marketplace_builds = 20
skill_backups = 3

[[targets]]
name = "local-codex"
product = "codex"
platform = "auto" # auto | linux | windows
user_home = "~"
# executable = "/absolute/path/to/codex" # optional product CLI override
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "local-claude-code"
product = "claude-code"
platform = "auto"
user_home = "~"
components = ["skills", "hooks", "settings"] # optional target override
surfaces = ["cli", "desktop"]
enabled = true
```

`config_home` is optional and defaults to `<user_home>/.codex` or
`<user_home>/.claude`. Keep it explicit for nonstandard runtimes. For Claude
Code, standalone Skills are projected to `<config_home>/skills`, so a custom
`CLAUDE_CONFIG_DIR` layout is honored. For Claude's default
`<user_home>/.claude` home, Bridge removes any inherited `CLAUDE_CONFIG_DIR`
because setting it would select a nested `<user_home>/.claude/.claude.json`
profile; custom homes set the reviewed value explicitly.
Relative catalog and state paths resolve from the TOML file. The loader keeps
the catalog and generated state physically isolated from enabled product homes
and Skill discovery roots. Product homes must also remain disjoint from other
targets' discovery roots; Claude Code's own `<config_home>/skills` relationship
is the intended exception. Multiple installations may consume the same physical
Skill root when at most one selects `skills`; every other target must exclude
that component. A previous Skill ownership record remains a write claim until its
target completes cleanup, so handoffs still require two separate reconciliations.
Isolated sibling directories under one `user_home` remain valid.
`doctor` warns when a declared Skill discovery root, or one of its existing
parents, is redirected through a symlink, junction, or directory reparse point.
Validation and planning still use the effective physical path for isolation and
ownership checks. Standalone Skill links below the discovery root do not trigger
this warning.

The optional target `executable` selects the Codex or Claude Code CLI used by
Plugin/Hook registration, its marketplace ownership preflight, and Schedules.
An explicit path is normalized to an absolute host path (`user_home` is the base
for a relative value), then must resolve to a real file. Linux requires it to be
executable; Windows requires a native `.exe` or `.com` launcher. Without an
override, Plugin/Hook registration preserves the normal bare `codex` or
`claude` PATH lookup. Schedule registration separately resolves only absolute
non-CWD `PATH` entries and pins that validated path in the heartbeat. Later
Schedule ticks therefore do not depend on cron or Task Scheduler `PATH` lookup.

See [examples/bridge.toml](examples/bridge.toml) for a working selective-sharing example.

## Canonical catalog layout

```text
catalog/
├── skills/
│   └── hello/
│       └── SKILL.md
├── plugins/
│   └── hello-shared/
│       ├── common/                 # shared skills, scripts, assets
│       ├── codex/                  # Codex overlay
│       │   └── .codex-plugin/plugin.json
│       └── claude-code/            # Claude Code overlay
│           └── .claude-plugin/plugin.json
├── hooks/
│   ├── .version                 # version of the generated hook plugin
│   └── audit-event/
│       ├── common/
│       │   ├── hooks.json       # common event/handler subset
│       │   └── scripts/
│       ├── codex/               # optional additions
│       └── claude-code/         # optional additions
├── settings/
│   └── shared-defaults/
│       ├── codex/config.toml    # Codex-native fragment
│       └── claude-code/settings.json
├── schedules/
│   └── daily-review/
│       ├── schedule.toml        # cron, timezone, worktree, timeout
│       └── PROMPT.md            # prompt passed on standard input
└── instructions/
    └── global-policy/
        ├── claude-code/         # optional product overlay (no common/)
        │   ├── CLAUDE.md
        │   └── rules/git-workflow.md
        └── codex/
            └── AGENTS.md
```

Instruction bundles have no `common/` overlay and are never merged: each
destination file below the product configuration home has exactly one owning
bundle, and an existing unmanaged destination file is a conflict even when its
content matches. Sources must be non-empty UTF-8 without BOM; content identity
normalizes CRLF/CR to LF so a line-ending-only difference never reads as drift.

`common/` is copied into each package, followed by only that product's overlay.
Overlay files must be identical or non-overlapping; conflicting target files
fail rendering. Hook event arrays from `common/` and the selected product
overlay are additive. The bridge does not translate hook semantics between
products, so catalog authors must put only truly portable declarations in
`common/hooks.json`.

Rendered packages are separated by product under the immutable path
`<state_dir>/builds/<digest>/plugins/{codex,claude-code}/`. An
integrity-checked copy is published at the stable `<state_dir>/marketplace` path
registered with product CLIs, and each product marketplace lists only the
components selected for that product. Do not edit either generated location.

Plugin manifests must use strict SemVer and the Codex and Claude Code versions
for a canonical plugin must match. If any rendered plugin content changes, bump
that version in both manifests. Hook catalogs with content require a strict
SemVer in `catalog/hooks/.version`; bump it whenever the generated hook package
changes. When a package exists in both the current published snapshot and its
replacement, its new SemVer precedence must be strictly higher if the rendered
content changed. This is a cache-safety check against the current snapshot, not
a permanent release history.

Raw filesystem permission bits are not part of catalog, copy, overlay, or
marketplace identity. The bridge therefore does not promise that an executable
bit authored on one filesystem survives another OS or checkout. Prefer an
explicit interpreter in Hook/MCP commands (for example, `python script.py`) and
product manifest metadata instead of relying on ambient file mode.

Settings deliberately have no `common` cross-product document. Every catalog
bundle contributes its native fragment for a target's product; mappings are
containers and arrays are atomic leaf values. An existing unowned leaf is a
conflict even when it already equals the desired value. The bridge removes a
deselected leaf only while its current digest still proves bridge ownership.
Unrelated local settings remain in place, and Codex TOML comments are
preserved. The bridge never manages credentials, `~/.claude.json`, managed
policy, or whole product homes.

A Schedule directory contains exactly `schedule.toml` and `PROMPT.md`. The
definition uses a strict five-field numeric cron expression, an IANA timezone,
a portable working directory relative to the target `user_home`, and an
optional bounded timeout. `apply` must run before `register` so the heartbeat
can consume a current immutable snapshot. See [Schedules](docs/schedules.md)
for the schema and lifecycle.

## Safety model

- `plan` and `doctor` never write Bridge or product state. `doctor` invokes the
  selected product executable with `--version`, so review explicit executable
  paths as code before running it.
- Existing unmanaged destinations are conflicts, even when their content happens to match.
- A previously recorded Skill symlink is removed on deselection only while it
  still points to the recorded canonical source; links are never repointed or
  adopted automatically.
- A target with non-empty Skill ownership state reserves its physical Skill
  root even while `components = []`; another target may claim that root only
  after the old target completes an empty reconciliation.
- Copy mode updates only when the ownership marker matches and the installed digest has not drifted.
- Updated or deselected, unchanged managed copies are retained under the
  configured state directory.
- `state prune` is dry-run by default. Applying retention requires `--yes`,
  holds an exclusive retention lock, rebuilds the reviewed plan, and
  revalidates every deletion candidate immediately before descriptor-anchored
  removal. A later concurrent change stops subsequent deletion but does not
  roll back candidates already removed.
- Marketplace builds are immutable and addressed by source digest; the stable
  published snapshot is integrity-checked before reuse or replacement.
- Settings update/remove only while each current value matches its target-scoped
  ownership digest; unrelated values are never backed up or copied into state.
- Plugin/Hook and host scheduler registration require separate confirmation.
- `register` removes only Plugins/Hooks recorded by an earlier bridge
  registration. Before any product registration commands it verifies that the product's
  bridge-named marketplace is absent, still at the recorded source, or already
  at the desired source after a partial retry; unrelated sources are refused.
- Scheduler registration replaces or removes only a target-scoped crontab block
  or Windows task whose ownership marker and digest remain intact.
- On POSIX, new ownership state, Schedule snapshots/runtime files, and newly
  created Settings files are restricted to user-private modes. Windows mode
  bits are not an ACL guarantee; place `state_dir` and product homes in a
  user-private ACL-protected location.
- Product runtime state and secrets are out of scope by design.

Target `name` is the ownership-state identity. Before renaming or deleting a
target—or changing its product/home identity—keep the old identity, set
`components = []`, run both `apply` and `register` to reconcile its managed
Skills/Plugins/Hooks/Settings/Schedules/Instructions, and only then change or remove the
target. Otherwise the old `state_dir/targets/<name>` record becomes orphaned and
diagnostics report
that it cannot be reconciled automatically. `apply` and `register` then stop until
the old target identity is restored and reconciled; the bridge never adopts or
deletes orphan state by guesswork.

The same staged rule applies when replacing a target that refers to the same or
a nested physical Skill discovery root through normal spelling, a symlink alias,
or a Windows case variant. Reconcile the old target by itself, then remove or
disable it before enabling the replacement target. Discovery roots remain
exclusive even when a target does not select the `skills` component.

Plugin ownership also records the stable marketplace source. If the repository
or `state_dir` moves, `register` removes bridge-recorded installs and the old
marketplace entry before installing the same selection from the new path.

The generated `state_dir` is operational, non-secret bridge state: rendered
catalog content, small ownership records, and Skill backups. It contains no
auth/session/trust records by design. Keep secrets out of the canonical catalog,
because generated packages and backups reproduce catalog content.

Read [the security model](docs/security.md) before applying an untrusted catalog
or enabling unattended Schedules.

## Product limitations

- Plugin manifests are not interchangeable; keep both product overlays.
- Only the common command-hook schema should live in `common/hooks.json`. Use product additions for events or outputs with different semantics.
- Codex requires users to review and trust changed non-managed Hooks.
- Claude Code Desktop is available as a Linux beta as well as on Windows and
  macOS. Plugins work in Local and SSH sessions; Anthropic documents that they
  are unavailable in Remote (cloud) and WSL sessions.
- Large Skill catalogs can be shortened in initial model context; prefer repo-scoped Skills or Plugins to keep activation focused.
- Plugin caches are installation outputs. They are rendered or installed, never used as the canonical source.
- Host-managed Schedules do not appear in the Codex Desktop or Claude Code
  Desktop scheduler views. Codex scheduled tasks are web/Desktop-managed;
  Claude CLI loops are session-scoped, while Claude Desktop task cadence and
  Remote Routines are product-managed. The bridge does not copy or edit their
  private/native scheduler state.
- Scheduled prompts run fresh, non-persistent CLI invocations. They do not
  inherit a Desktop conversation, and missed minutes are not replayed.
- Windows Task Scheduler permits parallel minute heartbeats with no scheduler
  execution-time cap. A short minute-claim lock suppresses duplicate claims;
  a separate per-Schedule lock skips only a recurrence whose previous run is
  still active, while the Schedule's own timeout remains authoritative.
- Alpha releases do not provide an all-actions atomic transaction, an
  apply/register target lock shared with retention, automatic rollback,
  recovery logs, product
  capability inference, or full post-install validation. Doctor's selected-CLI
  `--version` probe is informational. Schedule ticks use a separate runtime
  lock.
- A process crash can occur after an external file, product registry, crontab,
  or Task Scheduler mutation succeeds but before its ownership-state write.
  The next run fails closed or reports a conflict; inspect the external state
  and reconcile explicitly rather than expecting automatic rollback.
- Symlink mode is live: editing canonical Skill content changes what the product
  sees without another `apply`.
- Product CLIs remain responsible for plugin caches, hook trust, permissions,
  and installation behavior. Running previewed commands manually does not update
  bridge ownership state.
- Only contained regular-file symlinks are valid catalog inputs. Directory
  symlinks, broken/escaping links, and nested Windows-incompatible names are
  rejected; managed Skill copy mode rejects all source symlinks.

See the full [compatibility matrix](docs/compatibility.md) and [architecture](docs/architecture.md).

## Development

```bash
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy
uv run --extra dev pytest --cov=agent_config_bridge --cov-report=term-missing
```

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and record significant design changes as an ADR under [docs/adr](docs/adr/README.md).

## License

[MIT](LICENSE)
