# Architecture

Agent Config Bridge projects one canonical catalog of reusable agent
customizations into the native layouts expected by Codex and Claude Code. It
shares declarative content—Skills, Plugin source, Hook source, selected public
Settings, and recurring CLI workflows—but keeps credentials, sessions, caches,
trust decisions, and other runtime state local to each product installation.

The core rule is:

> Author once, render per product, inspect the plan, then apply and register.

This is a projection system, not a home-directory synchronizer.

## Goals and non-goals

The bridge is designed to:

- maintain one reviewable catalog for Windows and Linux;
- target Codex CLI, Claude Code CLI, Codex Desktop, and Claude Code Desktop;
- enable `skills`, `plugins`, `hooks`, `settings`, and `schedules`
  independently, globally or per target;
- produce separate Codex and Claude Code Plugin packages from shared source;
- patch explicit product Settings leaves without replacing unrelated local
  configuration;
- run portable recurring prompts through the native host scheduler and public
  product CLI;
- reconcile only paths and installations that the bridge has recorded as owned;
- keep product-native trust, cache, and permission behavior in product control.

It does not synchronize login state, conversations, history, caches, approval
databases, arbitrary or managed-policy settings, private Desktop scheduler
state, or cloud-side installations. It does not translate settings or Hook
semantics, infer installed product capabilities, invoke vendor validators for
arbitrary catalog content, or assert that a Plugin is fully active after the
product CLI returns. `doctor` reports the selected launcher's informational
`--version` output without turning it into a capability decision.

## System context

```text
                    +----------------------+
                    | canonical catalog    |
                    | skills/plugins/hooks |
                    | settings/schedules   |
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
            |               |       + host scheduler
      Skills + Settings     |
      + ownership state     |
                            v
                  immutable build
             marketplaces + target schedules
                       |                  |
           stable local marketplace   minute tick
```

The catalog is the source of truth. Immutable builds, the published marketplace,
Schedule snapshots, Skill links/copies, Settings patches, scheduler heartbeats,
and product install caches are derived artifacts. The stable marketplace and
per-target Schedule pointers let registered consumers use content without
depending on a changing build digest.

## Configuration model

Configuration schema version 1 has bridge-wide settings and explicit targets:

```toml
schema_version = 1

[bridge]
catalog = "/path/to/agent-catalog"
state_dir = "/path/to/bridge-state"
link_mode = "auto"
components = ["skills", "plugins", "hooks", "settings", "schedules"]

[[targets]]
name = "local-codex"
product = "codex"
platform = "auto"
user_home = "~"
executable = "/opt/codex/bin/codex"
surfaces = ["cli", "desktop"]
enabled = true

[[targets]]
name = "local-claude"
product = "claude-code"
platform = "auto"
user_home = "~"
components = ["skills", "hooks", "settings"]
surfaces = ["cli"]
enabled = true
```

Version 1 requires `schema_version`; all four `[bridge]` keys shown above; and
each target's `name`, `product`, `platform`, `user_home`, `surfaces`, and
`enabled`. Target `config_home`, `executable`, and `components` may be omitted.
The product home is then derived from `user_home`, and target components inherit
`[bridge].components`. Use `platform = "auto"` for host detection.

Windows native, WSL, and a separate Linux host are distinct targets even when
they run on the same physical computer. Every enabled `user_home` must be
accessible using the path syntax of the process running the bridge. In practice,
host-specific TOML files can point to the same canonical catalog: use a native
Windows path when running on Windows and a Linux or `/mnt/...` path when running
under WSL. Registration still runs on the target platform. Two enabled targets
cannot use equal or nested `config_home` paths, even across products. Equal or
nested physical Skill discovery roots are allowed only under a single-writer
rule: at most one target may select `skills`, while all other targets sharing
that root are passive consumers with `skills` excluded. Planning also counts a
non-empty previous Skill ownership record as a writer claim until cleanup has
completed, which keeps two-step target handoffs fail-closed. An enabled target's
`config_home` also cannot overlap another target's discovery root; Codex cannot
overlap its own root. Claude Code's
intentional same-target `<config_home>/skills` relationship is the sole
exception. Target `surfaces` currently drives compatibility diagnostics; it
does not cause a separate surface-specific render. A target selecting
`schedules` must include the `cli` surface because every host-managed run invokes
the product CLI.

Only the canonical `catalog` is cross-host source. Each native Windows, WSL, or
Linux host uses its own stable `state_dir`; ownership identities and registered
marketplace sources contain host-native physical paths and are not portable
runtime state. A shared operational state directory would make the other host's
records appear orphaned or mismatched.

Codex standalone Skills always target `<user_home>/.agents/skills`. Claude Code
standalone Skills target `<config_home>/skills`, including when `config_home`
models a custom `CLAUDE_CONFIG_DIR`. Selected Settings target
`<config_home>/config.toml` for Codex and `<config_home>/settings.json` for
Claude Code. Schedule working directories are portable relative paths resolved
beneath each target's `user_home` during rendering.

The optional target `executable` controls Plugin/Hook registration, its
marketplace ownership preflight, and Scheduled product CLI runs. The
loader normalizes it to an absolute path, using `user_home` as the base for a
relative spelling. At registration the path must resolve to a real executable
file (and to a native `.exe` or `.com` launcher for Windows). When it is omitted,
Plugin/Hook commands retain the product's bare PATH command. Schedule
registration uses the stricter absolute, non-CWD PATH search. Its validated
absolute vendor path becomes part of the reviewed heartbeat command and
ownership digest, so the host scheduler never has to rediscover it. Windows Task
Scheduler operations use the absolute System32 path returned by WinAPI, not an
environment-derived or bare `schtasks.exe` command.

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
├── hooks/
│   ├── .version                 # generated Hook Plugin SemVer
│   └── <name>/
│       ├── common/
│       ├── codex/
│       └── claude-code/
├── settings/<bundle>/
│   ├── codex/config.toml        # optional native fragment
│   └── claude-code/settings.json
└── schedules/<name>/
    ├── schedule.toml
    └── PROMPT.md
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
generated Hook Plugin. Settings bundles and Schedules use the same portable
lowercase kebab-case identity. A Settings bundle must contain at least one
non-empty product fragment. A Schedule contains exactly its two declared files.

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

## Settings projection

Settings are native fragments rather than a translated common schema:

```text
catalog/settings/<bundle>/codex/config.toml
catalog/settings/<bundle>/claude-code/settings.json
```

For a target selecting `settings`, every fragment for that target's product is
flattened into explicit leaves. Mappings are containers; arrays and scalar
values are atomic leaves. Duplicate paths and ancestor/descendant claims across
bundles are rejected. The destination is the product's user-level public
Settings file under `config_home`.

Planning compares the desired leaves with the destination and target-scoped
digest-only ownership state. A new leaf can claim only an absent path. An
existing unowned leaf is a conflict even when its value equals the desired
value. A previously owned leaf can be updated or removed only while its current
value matches the recorded digest. Cleanup prunes only empty mapping containers
that the bridge recorded as created.

Apply reparses and rehashes the destination immediately before an atomic
same-directory replacement. Codex TOML editing preserves unrelated comments and
formatting. Claude JSON preserves unrelated values but may normalize formatting.
Ownership state does not retain the original or displaced values, so unrelated
product configuration is not copied into bridge state.

## Host-managed Schedules

A Schedule is a product-neutral recurring CLI intent:

```toml
schema_version = 1
cron = "0 9 * * 1-5"
timezone = "Asia/Seoul"
working_directory = "workspace/project"
timeout_seconds = 1800
```

The five-field cron grammar is numeric and deliberately excludes vendor
extensions. The IANA timezone is evaluated for each real UTC minute, including
DST transitions. `working_directory` is a portable POSIX-style relative path
that must resolve to a real directory beneath the target `user_home` when
`apply` renders the immutable target snapshot. `PROMPT.md` is passed on standard
input; catalog Schedules cannot define environment variables.

`register` installs one target-scoped, once-per-minute heartbeat, not one host
job per Schedule:

- Linux: a marked block in the current user's crontab;
- Windows: `AgentConfigBridge-Heartbeat-<target>` in Task Scheduler, using the
  current user's interactive token and least privilege.

Both invoke an absolute `agentbridge schedule tick` command with an absolute
configuration path, target name, and validated absolute product CLI path. The
tick briefly acquires a non-blocking target lock to claim one snapshot/minute,
records that claim, then releases the lock before vendor work starts. A duplicate
claim is skipped and missed minutes are not replayed.

Each product invocation holds a separate lock keyed by target and Schedule name.
If the same Schedule's previous invocation is still active, only that recurrence
is skipped; other Schedules and later minute claims can continue. Codex receives
an ephemeral `codex exec` invocation; Claude Code receives a non-persistent
`claude --print` invocation. Prompt text stays on standard input, vendor
invocation does not use a shell, and the bridge adds no permission-bypass
option.

Windows Task Scheduler uses `MultipleInstancesPolicy=Parallel` and
`ExecutionTimeLimit=PT0S`. This lets the next minute heartbeat start and avoids
Task Scheduler terminating a legitimate long vendor invocation. Bridge minute
claims and per-Schedule locks provide overlap control, while each canonical
Schedule's `timeout_seconds` bounds its product subprocess.

Scheduler ownership is separate from the rendered snapshot. A marked crontab
block or Windows task is updated or removed only while both its embedded digest
and target ownership record agree. `apply` publishes Schedule content;
`register` owns the host heartbeat. Deselecting `schedules` therefore requires
both commands to remove the snapshot and heartbeat safely.

This design does not integrate with product-native scheduler storage. Codex
scheduled tasks are web/Desktop-managed. Claude CLI loop tasks are
session-scoped, Claude Desktop exposes only the prompt file as public local task
storage, and Remote Routines are account-owned cloud resources. The bridge does
not read or write any of those private or product-managed lifecycle records.

## Planning

`validate`, `plan`, and `doctor` are read-only. Planning reads configuration,
catalog content, destination paths, generated marketplace integrity metadata,
and bridge ownership records. It reports:

- Skill link/copy creates, updates, removals, no-ops, and conflicts;
- aggregate Settings leaf create/update/remove/no-op/conflict counts;
- whether the stable marketplace must be created or refreshed;
- whether each per-target Schedule snapshot must be published or removed;
- explicit product CLI commands for registration and reconciliation;
- Hook event, matcher, handler type, and command/URL/prompt review items;
- Plugin manifest or `.mcp.json` command/URL review items;
- compatibility warnings such as the Claude Code Desktop session boundary and
  the host-managed/native-Desktop Schedule boundary.

Text command previews use POSIX environment assignment/quoting for Linux targets
and PowerShell `$env:` assignment plus single-quoted arguments for Windows
targets. A default Claude profile is represented as `env -u
CLAUDE_CONFIG_DIR` on POSIX and `Remove-Item Env:CLAUDE_CONFIG_DIR` in
PowerShell. JSON plans expose argv, environment assignments, and
`environment_unsets` separately. When the running host and target platforms
differ, planning omits registration commands and emits a warning to rerun from
the target platform with native paths.

Planning does not probe product versions or feature capabilities, run vendor
schema validators, resolve executable arguments, or validate an installed cache.
Review items are an inspection aid, not a safety certification, and may include
literal command or URL values from the catalog.

`doctor` separately validates the exact explicit or PATH-selected product
launcher and invokes it once with `--version`. A missing or invalid configured
launcher is an error; ambient PATH discovery and version-probe failures are
warnings unless an explicit launcher caused them. This reports identity and
version only; it does not certify Plugin, Hook, Settings, or Schedule support.

## Apply and filesystem reconciliation

Before `apply`, the bridge rediscovers the catalog and rebuilds the complete
plan. If the newly derived plan differs from the reviewed plan, apply stops as
stale. Selected source or relevant destination/ownership changes normally alter
the plan; damage that does not change plan identity can instead fail a later
integrity check. Any planned conflict aborts before the action loop starts.

Actions then run sequentially:

- the marketplace build and stable published snapshot are rendered as needed;
- selected product Settings leaves are patched and their digest-only ownership
  state is updated;
- immutable per-target Schedule snapshots are published or deselected pointers
  are removed;
- Linux `auto` mode creates standalone Skill directory symlinks;
- Windows `auto` mode creates managed standalone Skill copies;
- an unchanged managed copy is staged next to its destination before update;
- the displaced copy is retained under `state_dir/backups/<target>/...`;
- deselection unlinks a still-matching recorded symlink;
- deselection moves a still-matching managed copy into the backup tree;
- drift, changed ownership, or an unmanaged destination becomes a conflict;
- successful reconciliation writes target-scoped Skill and Settings ownership
  state.

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

## Plugin, Hook, and Schedule registration

`register` is deliberately separate from `apply`. It requires confirmation,
refuses targets whose platform differs from the current host, rechecks the plan
and scheduler inspection, publishes the marketplace, and executes product
commands sequentially with the target's product-home environment. Codex always
receives `CODEX_HOME`; Claude removes an inherited `CLAUDE_CONFIG_DIR` for the
default profile and sets it only for a custom `config_home`, because default
profile metadata lives beside `<user_home>/.claude`. It then reconciles any selected or previously owned
scheduler heartbeat.

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
Likewise, a manually created or edited scheduler entry is never adopted by
name; an unrecorded or digest-mismatched heartbeat is a conflict.

Target `name` is also the key below `state_dir/targets`. Before changing a
target's name, product, or home—or deleting it—retain the old identity, set its
`components = []`, then run `apply` and `register` to reconcile Skills,
Settings, Schedule snapshots, registered Plugins/Hooks, and the host heartbeat.
After the empty ownership records are cleared, the target can be changed or
removed. Otherwise its state directory is orphaned; diagnostics fail and
state-changing commands stop rather than guessing a
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
├── builds/<digest>/                 # immutable marketplace builds
├── marketplace/                    # stable marketplace snapshot
├── schedule-builds/<digest>/<target>/snapshot.json
├── schedules/<target>.json          # stable Schedule pointer
├── schedule-runtime/                # claim/run locks + minute marker
├── backups/<target>/...             # retained managed Skill copies
└── targets/<target>/
    ├── skills.json                  # standalone Skill ownership
    ├── plugins.json                 # registration ownership
    ├── settings.json                # owned paths/value digests
    └── scheduler.json               # heartbeat ownership
```

This state is designed to be non-secret: the bridge never writes product auth,
session, trust, cache, or conversation state there. The ownership files contain
target IDs, artifact names, paths, link modes, and content/value digests—not
credentials or displaced Settings values. Rendered output and backups reproduce
canonical catalog content, so secrets must not be placed in the catalog.

On POSIX, newly written target ownership files and Schedule snapshot/runtime
files use mode `0600`, and their managed directories use `0700`; a newly created
vendor Settings file also starts at `0600`, while an existing file retains its
mode. These numeric modes do not establish a Windows DACL. On Windows, inherited
ACLs remain authoritative, so operators must choose user-private ACL-protected
locations for `state_dir` and product homes.

The schema physically resolves existing symlink/junction ancestors and rejects
equal, ancestor, or descendant overlap between `catalog` and `state_dir`, and
between either bridge path and every enabled target's `config_home` or Skill
discovery root. Windows-target comparisons are case-insensitive, including in
mixed-platform configurations. Keep these paths in isolated sibling trees;
merely sharing a `user_home` ancestor is allowed. See
[ADR-0002](adr/0002-never-share-runtime-state.md) and [security.md](security.md).

## Alpha limitations

- No all-actions atomic transaction, apply/register target lock, automatic
  rollback, or recovery log. Schedule ticks use a separate runtime lock.
- External mutation and ownership-state persistence are separate steps. A crash
  after a Settings/product-registry/crontab/Task-Scheduler change but before its
  state write can leave a fail-closed conflict that requires inspection and
  explicit reconciliation.
- Symlink mode is live; canonical Skill edits are visible immediately without
  another `apply`.
- No automatic product capability inference, arbitrary vendor artifact
  validation, or full installed-state validation. Doctor's selected-CLI
  `--version` probe is informational.
- Product CLIs own trust, permissions, plugin caches, and refresh behavior.
- Manual product commands do not update bridge ownership records.
- Settings are schema-preserving native fragments, not a cross-product
  translation; arrays are owned and replaced as atomic leaf values.
- Host Schedules run fresh CLI processes, do not replay missed minutes, and do
  not appear in product-native scheduler views.
- Codex scheduled tasks, Claude Desktop scheduled task metadata, Claude CLI
  loops, and Claude Remote Routines remain product-owned and unsynchronized.
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
- [OpenAI: Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [OpenAI: Codex configuration](https://learn.chatgpt.com/docs/config-file/basic-config)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Anthropic: Create plugins](https://code.claude.com/docs/en/plugins)
- [Anthropic: Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Hooks reference](https://code.claude.com/docs/en/hooks)
- [Anthropic: Claude Code configuration](https://code.claude.com/docs/en/configuration)
- [Anthropic: Claude Code scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
- [Anthropic: Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Anthropic: Remote Routines](https://code.claude.com/docs/en/web-scheduled-tasks)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
