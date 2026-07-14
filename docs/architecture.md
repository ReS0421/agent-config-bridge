# Architecture

Agent Config Bridge projects one canonical catalog of reusable agent
customizations into the native layouts expected by Codex and Claude Code. It
shares declarative content—Skills, Plugin source, and Hook source—but keeps
credentials, sessions, caches, trust decisions, and other runtime state local to
each product installation.

The core rule is:

> Author once, render per product, inspect the plan, then apply and register.

This is a projection system, not a home-directory synchronizer.

## Goals and non-goals

The bridge is designed to:

- maintain one reviewable catalog for Windows and Linux;
- target Codex CLI, Claude Code CLI, Codex Desktop, and Claude Code Desktop;
- enable `skills`, `plugins`, and `hooks` independently, globally or per target;
- produce separate Codex and Claude Code Plugin packages from shared source;
- reconcile only paths and installations that the bridge has recorded as owned;
- keep product-native trust, cache, and permission behavior in product control.

It does not synchronize login state, conversations, history, caches, approval
databases, product settings in general, or cloud-side installations. It does not
translate Hook semantics, probe installed product versions/capabilities, invoke
vendor validators for arbitrary catalog content, or assert that a Plugin is
fully active after the product CLI returns.

## System context

```text
                    +----------------------+
                    | canonical catalog    |
                    | skills/plugins/hooks |
                    +----------+-----------+
                               |
                      discover + validate
                               |
                    +----------v-----------+
                    | read-only plan       |
                    | actions/reviews/cmds |
                    +----+-------------+---+
                         |             |
                  apply --yes     register --yes
                         |             |
            +------------+--+       product CLIs
            |               |          + state
      standalone Skills     |
      + ownership state     |
                            v
                  immutable build
             <state_dir>/builds/<digest>
                            |
                    integrity-checked copy
                            v
                stable published marketplace
                  <state_dir>/marketplace
```

The catalog is the source of truth. Immutable builds, the published marketplace,
Skill links/copies, and product install caches are derived artifacts. The stable
marketplace path exists because product CLIs need a registration path that does
not change every time the content digest changes.

## Configuration model

Configuration schema version 1 has bridge-wide settings and explicit targets:

```toml
schema_version = 1

[bridge]
catalog = "/path/to/agent-catalog"
state_dir = "/path/to/bridge-state"
link_mode = "auto"
components = ["skills", "plugins", "hooks"]

[[targets]]
name = "local-codex"
product = "codex"
platform = "auto"
user_home = "~"
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "local-claude"
product = "claude-code"
platform = "auto"
user_home = "~"
components = ["skills", "hooks"]
surfaces = ["cli"]
enabled = true
```

Version 1 requires `schema_version`; all four `[bridge]` keys shown above; and
each target's `name`, `product`, `platform`, `user_home`, `surfaces`, and
`enabled`. Only target `config_home` and `components` may be omitted. The product
home is then derived from `user_home`, and target components inherit
`[bridge].components`. Use `platform = "auto"` for host detection.

Windows native, WSL, and a separate Linux host are distinct targets even when
they run on the same physical computer. Every enabled `user_home` must be
accessible using the path syntax of the process running the bridge. In practice,
host-specific TOML files can point to the same canonical catalog: use a native
Windows path when running on Windows and a Linux or `/mnt/...` path when running
under WSL. Registration still runs on the target platform. Two enabled targets
cannot use equal or nested `config_home` paths, even across products, or equal
or nested Skill discovery roots. Discovery roots are checked for every enabled
target even when `skills` is not selected, because the products still discover
content there. An enabled target's `config_home` also cannot overlap another
target's discovery root; Codex cannot overlap its own root. Claude Code's
intentional same-target `<config_home>/skills` relationship is the sole
exception. Target `surfaces` currently drives compatibility diagnostics; it
does not cause a separate surface-specific render.

Only the canonical `catalog` is cross-host source. Each native Windows, WSL, or
Linux host uses its own stable `state_dir`; ownership identities and registered
marketplace sources contain host-native physical paths and are not portable
runtime state. A shared operational state directory would make the other host's
records appear orphaned or mismatched.

Codex standalone Skills always target `<user_home>/.agents/skills`. Claude Code
standalone Skills target `<config_home>/skills`, including when `config_home`
models a custom `CLAUDE_CONFIG_DIR`.

## Canonical catalog

```text
catalog/
├── skills/<name>/
│   ├── SKILL.md
│   └── ...supporting files
├── plugins/<name>/
│   ├── common/                 # shared payload
│   ├── codex/
│   │   └── .codex-plugin/plugin.json
│   └── claude-code/
│       └── .claude-plugin/plugin.json
└── hooks/
    ├── .version                 # generated Hook Plugin SemVer
    └── <name>/
        ├── common/
        ├── codex/
        └── claude-code/
```

Artifact identities are lowercase kebab-case and portable to Windows. Discovery
validates every nested path component, rejecting Windows device names, invalid
characters, trailing dots/spaces, and case-insensitive sibling collisions. Group
or artifact roots may not escape the catalog. Nested symlinks must resolve to a
regular file inside the same artifact; broken, escaping, and directory symlinks
are rejected. Managed copy mode currently rejects standalone Skills containing
even an accepted file symlink.

Standalone `SKILL.md` files require YAML frontmatter whose `name` matches the
directory and whose `description` is non-empty. Plugins require both product
manifests; their names must match the artifact directory and their strict SemVer
versions must match each other. Product manifests belong in their product
overlay, not `common/`. The name `agent-config-bridge-hooks` is reserved for the
generated Hook Plugin.

## Product-specific rendering

For each selected product, the renderer copies `common/` and then that product's
overlay into a separate package tree:

```text
<state_dir>/builds/<digest>/plugins/codex/<name>/
<state_dir>/builds/<digest>/plugins/claude-code/<name>/
```

Files at the same output path must be byte-identical; otherwise rendering fails.
Raw filesystem permission bits do not participate in overlay identity, source or
rendered digests, or marketplace integrity. The renderer never combines Codex
and Claude Code manifests in one package, but it also does not inspect textual
absolute/`..` path references inside manifests or `.mcp.json`.

Because file mode is not a portable identity, the bridge does not guarantee that
an executable bit survives Git, Windows, WSL, or another filesystem. Hook and MCP
commands should name an explicit interpreter, such as `python script.py`, and
use product metadata where the product exposes execution intent.

Hook bundles are combined into a generated `agent-config-bridge-hooks` package
for each selected product. Hook event arrays from `common/hooks.json` and the
matching product overlay are appended. This is a structural merge only: the
bridge does not infer event equivalence, rewrite handlers, or verify blocking
semantics. Authors must keep non-portable declarations in product overlays.

Every immutable build contains both marketplace documents and an integrity
marker. Before reuse, the complete generated tree is rehashed and its Plugin
lists/manifests are checked. The bridge then copies the build to
`<state_dir>/marketplace`, validates the copy, and replaces the previous
published snapshot. Product registration always points to this stable path.

Package versions are part of cache correctness:

- both manifests for a canonical Plugin require the same strict SemVer;
- changing any rendered Plugin content requires increasing both matching
  manifest versions;
- a non-empty Hook catalog requires `hooks/.version` with strict SemVer;
- changing any generated Hook package content requires increasing
  `hooks/.version`.

For a package name present in both the current published snapshot and its
replacement, the renderer rejects changed content unless the new strict SemVer
has higher precedence. This protects the immediate cache transition only. The
bridge has no permanent release ledger, so deleting state or removing and later
re-adding a package removes that comparison history.

## Planning

`validate`, `plan`, and `doctor` are read-only. Planning reads configuration,
catalog content, destination paths, generated marketplace integrity metadata,
and bridge ownership records. It reports:

- Skill link/copy creates, updates, removals, no-ops, and conflicts;
- whether the stable marketplace must be created or refreshed;
- explicit product CLI commands for registration and reconciliation;
- Hook event, matcher, handler type, and command/URL/prompt review items;
- Plugin manifest or `.mcp.json` command/URL review items;
- compatibility warnings such as the Claude Code Desktop session boundary.

Text command previews use POSIX environment assignment/quoting for Linux targets
and PowerShell `$env:` assignment plus single-quoted arguments for Windows
targets. JSON plans expose the argv and environment separately. When the running
host and target platforms differ, planning omits registration commands and emits
a warning to rerun from the target platform with native paths.

Planning does not probe product versions or feature capabilities, run vendor
schema validators, resolve executable arguments, or validate an installed cache.
Review items are an inspection aid, not a safety certification, and may include
literal command or URL values from the catalog.

## Apply and Skill reconciliation

Before `apply`, the bridge rediscovers the catalog and rebuilds the complete
plan. If the newly derived plan differs from the reviewed plan, apply stops as
stale. Selected source or relevant destination/ownership changes normally alter
the plan; damage that does not change plan identity can instead fail a later
integrity check. Any planned conflict aborts before the action loop starts.

Actions then run sequentially:

- the marketplace build and stable published snapshot are rendered as needed;
- Linux `auto` mode creates standalone Skill directory symlinks;
- Windows `auto` mode creates managed standalone Skill copies;
- an unchanged managed copy is staged next to its destination before update;
- the displaced copy is retained under `state_dir/backups/<target>/...`;
- deselection unlinks a still-matching recorded symlink;
- deselection moves a still-matching managed copy into the backup tree;
- drift, changed ownership, or an unmanaged destination becomes a conflict;
- successful reconciliation writes `state_dir/targets/<target>/skills.json`.

An existing canonical-pointing symlink or valid managed-copy marker is not, by
itself, ownership proof. It can be a no-op or update only when that same target
has a matching prior Skill ownership entry. A target with non-empty prior Skill
state also reserves its physical Skill root during `components = []` cleanup;
path aliases and Windows case variants cannot transfer the root until the old
target completes an empty reconciliation and is then removed or disabled.

The current alpha does not hold a target lock and does not wrap all actions in
one atomic transaction. Individual managed-copy and marketplace replacements use
temporary paths and local replacement checks, but a later action can fail after
an earlier one succeeded. There is no automatic rollback or recovery log. Run a
fresh `plan` after an interrupted apply; retained managed-copy backups are for
manual recovery.

## Plugin and Hook registration

`register` is deliberately separate from `apply`. It requires confirmation,
refuses targets whose platform differs from the current host, rechecks the plan,
publishes the marketplace, and executes product commands sequentially with the
target's `CODEX_HOME` or `CLAUDE_CONFIG_DIR` environment.

The desired installation set is the selected canonical Plugins plus the
generated Hook Plugin when Hooks are selected. Registration:

- removes only names recorded by an earlier successful bridge `register` run;
- registers or refreshes the stable marketplace;
- installs or refreshes the desired product packages;
- explicitly runs Claude's marketplace and Plugin update commands;
- writes `state_dir/targets/<target>/plugins.json` after that target's commands
  succeed.

Unrelated product installations are never removed. Commands copied from a plan
and run manually bypass bridge ownership recording, so later deselection cannot
reconcile those manual actions. Product CLIs remain authoritative for cache
contents, trust, permission prompts, and whether an installation is usable.

Target `name` is also the key below `state_dir/targets`. Before changing a
target's name, product, or home—or deleting it—retain the old identity, set its
`components = []`, then run `apply` and `register` to reconcile standalone Skills
and registered Plugins/Hooks. After the empty ownership records are cleared, the
target can be changed or removed. Otherwise its state directory is orphaned;
diagnostics fail and state-changing commands stop rather than guessing a
replacement identity or deleting it. Restore the old target identity and
complete the empty reconciliation before continuing.

Plugin ownership records also bind the product home and stable marketplace
source. If only the repository or `state_dir` path moves, registration performs
an explicit uninstall/remove/add/install migration so a product never retains
the same marketplace name at a stale absolute source. Before the first
product command, a read-only registry preflight accepts only an absent entry,
the recorded old source, or the desired new source from a partial retry. Without
prior ownership state, only absence or the desired source is accepted, so a
Claude marketplace add cannot silently replace another checkout's same-name
entry.

With no `--target` arguments, `register` selects enabled targets for the current
host platform only. An explicitly selected target for another platform remains
an error.

## State and ownership

`state_dir` contains generated, bridge-owned operational data:

```text
state_dir/
├── builds/<digest>/              # immutable marketplace builds
├── marketplace/                 # stable published snapshot
├── backups/<target>/...         # retained managed Skill copies
└── targets/<target>/
    ├── skills.json              # standalone Skill ownership
    └── plugins.json             # registration ownership
```

This state is designed to be non-secret: the bridge never writes product auth,
session, trust, cache, or conversation state there. The ownership files contain
target IDs, artifact names, link modes, and source identity—not credentials.
Rendered output and backups reproduce canonical catalog content, so secrets must
not be placed in the catalog.

The schema physically resolves existing symlink/junction ancestors and rejects
equal, ancestor, or descendant overlap between `catalog` and `state_dir`, and
between either bridge path and every enabled target's `config_home` or Skill
discovery root. Windows-target comparisons are case-insensitive, including in
mixed-platform configurations. Keep these paths in isolated sibling trees;
merely sharing a `user_home` ancestor is allowed. See
[ADR-0002](adr/0002-never-share-runtime-state.md) and [security.md](security.md).

## Alpha limitations

- No all-actions atomic transaction, target lock, automatic rollback, or
  recovery log.
- Symlink mode is live; canonical Skill edits are visible immediately without
  another `apply`.
- No automatic product capability/version probing, arbitrary vendor artifact
  validation, or full installed-state validation.
- Product CLIs own trust, permissions, plugin caches, and refresh behavior.
- Manual product commands do not update bridge ownership records.
- Raw filesystem permission bits are not preserved as content identity; use an
  explicit interpreter instead of relying on executable mode.
- Plugin and Hook changes are compared only with overlapping packages in the
  current published snapshot and require a strict SemVer precedence increase.
- Renamed/deleted targets can leave orphan ownership state unless reconciled
  empty first.
- Claude Code Desktop is available on Linux beta. Plugins load only in Local or
  SSH sessions; Remote (cloud) and WSL sessions do not load them.

## References

- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [OpenAI: Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [OpenAI: Hooks](https://learn.chatgpt.com/docs/hooks)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Anthropic: Create plugins](https://code.claude.com/docs/en/plugins)
- [Anthropic: Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Hooks reference](https://code.claude.com/docs/en/hooks)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
