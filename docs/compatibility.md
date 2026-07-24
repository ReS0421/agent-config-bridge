# Compatibility

This document describes the implemented local projection targets and known
product boundaries as of 2026-07-23. Vendor behavior changes quickly. An alpha
bridge release validates its own catalog and generated-state invariants, but it
does not infer product capabilities or certify every vendor schema. `doctor`
does report the exact selected executable's `--version` output and, when Plugin
or Hook registration is relevant, performs a read-only marketplace ownership
preflight.

“Targeted” means the bridge can model the local filesystem home and generate
product CLI commands for that combination. It does not mean every component has
identical semantics or that the vendor product is generally available on that
operating system.

## Machine-readable plan contract

`agentbridge plan --json` emits a top-level JSON object with the integer
`schema_version` field. The current version is `1`; absence of that field is not
an implicit version. A downstream consumer must reject a missing, non-integer,
or unsupported version before interpreting any other field.

Version 1 contains these required top-level fields:

- `actions`: an array of read-only filesystem or rendering decisions. Each
  action includes `operation`, `disposition`, `component`, `target`, `name`,
  `source`, `destination`, `detail`, and the nullable provenance fields
  `source_id`, `source_digest`, and `link_mode`.
- `commands`: an array of structured product command hints. Each command
  contains `target`, an `environment` object, an `environment_unsets` array, an
  `argv` array, and a human-readable `reason`. Consumers must preserve both
  environment additions and removals when evaluating a command.
- `reviews` and `warnings`: arrays of human-readable strings.
- `has_changes` and `has_conflicts`: booleans summarizing the action set.

Exit status `0` means the plan has no conflicts; status `1` means the emitted
plan contains a conflict. Any other status, invalid JSON, a non-object payload,
a missing required field, or a required field with the wrong type is not a
usable plan. Consumers should fail closed in those cases and may ignore unknown
additive fields only after accepting a supported `schema_version`. Planning
remains read-only; command hints are data for a separately authorized workflow,
not commands executed by `plan`.

The 0.1.0 integration baseline exercised a complete isolated lifecycle on
native Linux with Codex CLI 0.144.3 and Claude Code 2.1.206: render, vendor
validation where available, marketplace registration, install, refresh,
idempotent retry, deselection, and removal. The bridge test suite also runs on
native Windows and Linux CI for Python 3.11 and 3.12. Native Windows product
registration is modeled and unit-tested, but is not yet an automated vendor
CLI integration job.

The 0.2.0 local host audit also exercised native Windows Skill projection with
official standalone Codex 0.144.4, Claude Code 2.1.20, Python 3.12, and a native
`agentbridge.exe`: 107 managed copies per product converged to a 215-action NOOP
plan. Codex Plugin add/list/remove and marketplace-list command surfaces were
probed successfully; no native Windows Plugin registration was performed
because the audited catalog contained no Plugin or Hook artifact.

Codex marketplace preflight accepts the two observed local-entry schemas:
0.144.4's absolute `root`-only record and 0.144.6's expanded record with the
same absolute `root` and `marketplaceSource.source` plus
`marketplaceSource.sourceType = "local"`. Vendor JSON stdout is decoded as
strict UTF-8. Duplicate entries, relative or malformed paths, mismatched
expanded paths, undecodable output, and unknown schemas fail closed.

As a historical baseline, version 0.2 introduced native Settings leaf
projection and host-managed recurring CLI Schedules. These features use public
Settings files and operating-system schedulers; they do not turn Codex or
Claude's product-native scheduled tasks into a common API.

The runtime requires Python 3.11+, PyYAML, and `tomlkit`. Native Windows
installations also include `tzdata` so regional IANA Schedule timezones remain
available when the operating system does not provide a Python-readable timezone
database.

## Surface matrix

| Product surface     | Windows                                                | Native Linux                              | WSL                                                       | Plugin/Hook boundary                                                |
| ------------------- | ------------------------------------------------------ | ----------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------- |
| Codex CLI           | Targeted                                               | Targeted                                  | Targeted as Linux                                         | Product-native package and marketplace                              |
| Codex Desktop       | Native PowerShell or WSL2 agent mode                   | No native Linux Desktop target documented | WSL2 agent mode uses a Linux target                       | Uses the active Codex environment                                   |
| Claude Code CLI     | Targeted                                               | Targeted                                  | Targeted as a separate Linux home                         | Custom `CLAUDE_CONFIG_DIR` is modeled through `config_home`         |
| Claude Code Desktop | Local sessions targeted; SSH is a separate remote home | Linux beta Local sessions targeted        | Desktop WSL sessions exist, but Plugins are not available | Plugins are available in Local/SSH, not Remote (cloud)/WSL sessions |

Codex Desktop for Windows can use native PowerShell or a WSL2 agent environment.
When WSL2 is selected, configure a Linux target: Linux paths, link behavior, and
command quoting apply. The bridge does not point native Windows and WSL at one
whole Codex home.

Claude Code Desktop's Code surface shares local configuration with Claude Code
CLI for supported Local sessions. The Desktop app is available on Windows,
macOS, and Linux beta. An SSH session uses the Claude home on its remote Linux or
macOS host. A WSL session is a distinct Linux environment exposed through the
Windows app, while Remote is Anthropic's cloud-session environment. Anthropic
documents Plugin support for Local and SSH sessions, but not Remote (cloud) or
WSL sessions. The bridge target schema does not encode those Desktop session
types; selecting the Desktop surface produces a compatibility warning, not a
capability probe or a way to enable Plugins in an unsupported session.

Settings compatibility follows the configured product home, but individual
keys remain product-defined; the bridge does not assert that every public key is
consumed by every Desktop surface. Host-managed Schedules require `cli` in the
target surfaces and invoke that CLI even when `desktop` is also selected.

## Standalone Skills

Skills have the strongest common representation: a directory whose exact-case
entry point is `SKILL.md`, plus optional referenced files.

| Concern                   | Codex                              | Claude Code                                     |
| ------------------------- | ---------------------------------- | ----------------------------------------------- |
| Bridge-managed user root  | `<user_home>/.agents/skills`       | `<config_home>/skills`                          |
| Default `config_home`     | `<user_home>/.codex`               | `<user_home>/.claude`                           |
| Custom home behavior      | Registration receives `CODEX_HOME` | Custom homes use `CLAUDE_CONFIG_DIR`; the default profile removes inherited overrides |
| Linux `auto` mode         | Directory symlink                  | Directory symlink                               |
| Windows `auto` mode       | Managed directory copy             | Managed directory copy                          |
| Product-specific metadata | May be consumed by Codex           | May be consumed by Claude Code                  |

The bridge currently projects user-level standalone Skills only. It does not
create repository-scoped `.agents/skills` or `.claude/skills` trees.

The portable profile requires YAML frontmatter with a matching `name` and a
non-empty `description`. Other product-specific frontmatter remains untouched;
the bridge does not render separate Skill frontmatter variants. Catalog authors
must use the common subset or package product-specific Skills inside separate
Plugin overlays.

Linux symlink mode is live. A source edit is immediately visible through an
existing link and does not require `apply`. Copy mode requires a new plan/apply.
Catalog discovery accepts only contained symlinks to regular files. It rejects
directory symlinks, broken links, and escaping links. Managed copy mode currently
refuses a standalone Skill containing even an accepted file symlink.

Existing links and managed-copy markers are not adopted without matching
target-scoped ownership state. During a target handoff, the old target keeps its
physical Skill root reserved until an empty reconciliation completes; this also
applies to physical aliases and case variants on Windows.

Separate installations may discover the same physical Skill root. Configure
exactly one of those targets with `skills`; targets that only consume the shared
root must exclude that component. A retained ownership record counts as the one
writer claim, so moving ownership requires cleaning the old target before
selecting `skills` on the replacement.

## Settings

Settings share a lifecycle, not a schema:

| Concern | Codex | Claude Code |
| --- | --- | --- |
| Canonical fragment | `settings/<bundle>/codex/config.toml` | `settings/<bundle>/claude-code/settings.json` |
| User destination | `<config_home>/config.toml` | `<config_home>/settings.json` |
| Merge unit | Explicit scalar/array leaf | Explicit scalar/array leaf |
| Unrelated values | Preserved | Preserved |
| Formatting | Existing TOML comments/formatting preserved | JSON formatting may be normalized |
| New-file POSIX mode | `0600` | `0600` |

There is no `common` Settings fragment and no Codex-to-Claude key translation.
Catalog authors must express each product's supported schema separately. The
bridge validates syntax and ownership structure, not whether an installed
product version recognizes a key.

Every product fragment from every Settings bundle contributes to a target that
selects `settings`. Mappings are containers; arrays are atomic leaves. Bundles
cannot define duplicate or ancestor/descendant paths. A pre-existing unowned
leaf is a conflict even when it has the requested value. Later updates and
removals require the installed value to match its target ownership digest.

Only the user-level public Settings files above are eligible. The bridge does
not treat project-local settings, `~/.claude.json`, organization-managed policy,
authentication, trust, or other product-home files as Settings fragments.
Plugin marketplace registration remains a separate explicit workflow.

## Instructions

Instructions share a file lifecycle, not a cross-product syntax. Canonical
sources live below `instructions/<bundle>/<product>/`. Claude Code destinations
are restricted to `CLAUDE.md`, `rules/**`, `agents/**`, and `commands/**`;
Codex destinations are restricted to `AGENTS.md` and `agents/**`. Two bundles
cannot claim the same destination for one product. Files must be non-empty
UTF-8 without BOM; line-ending identity is normalized, but content is not
merged, translated, or concatenated.

An unmanaged destination conflicts even when its bytes match. Bridge-managed
files are attributed by target-scoped `instructions.json` ownership state and,
for managed directories, `AGENTBRIDGE-MANAGED.json`. Root-level `AGENTS.md` and
`CLAUDE.md` cannot use a directory marker and therefore rely on ownership state
plus content or link identity. Copy-mode update or deselection creates a
verified, non-destructive backup below
`state_dir/backups/<target>/instructions/`; those backups are excluded from
bounded generated-state pruning and do not authorize replacement of live
unmanaged content.

## Plugins and marketplaces

The package concepts are similar, but the contracts are not interchangeable:

| Concern             | Codex                              | Claude Code                       |
| ------------------- | ---------------------------------- | --------------------------------- |
| Plugin manifest     | `.codex-plugin/plugin.json`        | `.claude-plugin/plugin.json`      |
| Marketplace         | `.agents/plugins/marketplace.json` | `.claude-plugin/marketplace.json` |
| Rendered package    | `plugins/codex/<name>`             | `plugins/claude-code/<name>`      |
| Registration home   | `CODEX_HOME=<config_home>`         | custom Claude homes set `CLAUDE_CONFIG_DIR`; the default removes it |
| Product cache/trust | Owned by Codex                     | Owned by Claude Code              |

OpenAI currently recognizes a legacy-compatible repo marketplace at
`.claude-plugin/marketplace.json`. The bridge does not rely on that as a shared
package contract. It writes separate product marketplace entries and packages,
each containing only its native manifest. See
[ADR-0003](adr/0003-use-dual-marketplace-packages.md).

The bridge performs structural validation—required manifests, matching artifact
names, strict matching SemVer, non-conflicting overlay files, source-symlink
containment, nested Windows path portability, and generated-tree integrity. It
does not inspect textual path references inside manifests or `.mcp.json`,
automatically run Codex or Claude validators against arbitrary source artifacts,
or validate the final product cache. Product CLI installation, refresh, cache,
trust, and policy results remain product-owned.

Do not link a product Plugin cache back to the catalog. Rendered packages and the
stable marketplace are generated inputs; product caches are installation outputs
and never a source of truth.

## Hooks

Hook handler programs may be portable; Hook declarations are portable only when
the author has verified that both products interpret them equivalently. Codex
and Claude Code differ in event sets, matcher behavior, input JSON,
decision/exit behavior, trust, and Windows command handling.

The implemented merge policy is intentionally small:

- `common/hooks.json` and `<product>/hooks.json` use a top-level `hooks` object;
- matcher-group arrays for the same event are appended in source order;
- scripts from `common/scripts` and the selected product's `scripts` are copied
  into that Hook bundle's generated script directory;
- the bridge does not map event names, adapt payloads, rewrite commands, or
  prove equivalent blocking behavior;
- plan output surfaces event, matcher, handler type, and command/URL/prompt for
  human review.

Use `common/` only for declarations that are already correct on both products.
Use `codex/` and `claude-code/` for any semantic or command difference. Keep
PowerShell and POSIX handler commands in product-specific content when one
command string cannot run correctly in both environments.

All Hook bundles selected for a product are packaged into the synthetic
`agent-config-bridge-hooks` Plugin. A non-empty Hook catalog requires
`catalog/hooks/.version`; bump that strict SemVer whenever the generated Hook
package changes.

## Schedules

Bridge Schedules are host-managed CLI jobs:

| Concern | Linux | Windows |
| --- | --- | --- |
| Heartbeat | Marked current-user crontab block | `AgentConfigBridge-Heartbeat-<target>` Task Scheduler task |
| Frequency | Once per minute | Once per minute |
| Schedule evaluation | Five-field cron plus IANA timezone | Same |
| Registration | Must run on Linux target host | Must run on native Windows target host |
| Product CLI | Validated absolute path pinned at registration | Validated absolute native `.exe`/`.com` path pinned at registration |
| Host overlap policy | Cron may start each minute; bridge locks decide claims/runs | Task Scheduler `Parallel`, `PT0S`; bridge locks decide claims/runs |
| Ownership check | Marker, content digest, target state | Task description digest, task content, target state |

One heartbeat evaluates every published Schedule for its target. `apply`
publishes the target snapshot; `register` installs the heartbeat. A target must
include the `cli` surface. Codex runs `codex exec --ephemeral`; Claude Code runs
`claude --print --no-session-persistence`. Prompts are sent on standard input,
no permission-bypass flags are added, and missed minutes are not replayed.

An optional target `executable` overrides product CLI selection for Plugin/Hook
registration, marketplace preflight, and Schedules. Without it, Plugin/Hook
commands use the normal bare product command; Schedule registration resolves
`codex` or `claude` only from absolute non-CWD entries in its own `PATH`. The
resulting absolute Schedule path is stored in the heartbeat, so the later
scheduler process does not depend on its usually smaller `PATH`. A short target
lock makes
snapshot/minute claiming idempotent and is released before vendor work. A
separate target/Schedule lock skips only a recurrence whose previous run is
still active. On Windows, `Parallel` allows later heartbeats to make new claims,
and `PT0S` leaves duration enforcement to each Schedule's bounded timeout.
Task Scheduler management itself uses the genuine System32 executable resolved
through WinAPI rather than Windows current-directory command search.

These jobs are intentionally separate from vendor-native automation:

| Vendor feature | Product ownership/lifetime | Bridge behavior |
| --- | --- | --- |
| Codex scheduled tasks | Managed in ChatGPT web or Codex Desktop | Not imported, listed, or mutated |
| Claude Code CLI `/loop` | Session-scoped; recurring tasks expire after seven days | Not used as durable scheduling |
| Claude Desktop local tasks | Prompt file is public; cadence/folder/model/enabled state is UI-managed | Native task state is not synchronized |
| Claude Remote Routines | Account-owned cloud resource | Already follows the account; not synchronized |

Consequently, a bridge Schedule never appears in a Codex or Claude Desktop
scheduler view. It starts a fresh CLI process and does not inherit a Desktop
thread or session context. Native Windows and WSL are separate hosts and need
separate targets and heartbeat registrations.

## Filesystem modes

| Mode      | Current behavior                                                           | Limitations                                                 |
| --------- | -------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `copy`    | Managed standalone Skill directory and allowlisted Instruction file copies | Re-apply required; source Skill symlinks are rejected       |
| `symlink` | Live standalone Skill directory and allowlisted Instruction file symlinks  | Windows privilege/policy and persistent source availability |
| `auto`    | `copy` for Windows targets; `symlink` for Linux targets                    | A simple platform rule, not product capability detection    |

The selected operation appears in `plan`. Windows command previews use
PowerShell syntax; Linux previews use POSIX syntax. Registration itself must run
on the configured target platform. When host and target differ, registration
commands are omitted from the plan. WSL filesystem visibility does not make a
Windows executable, path, permission model, or command string valid on Linux.

Share the catalog, not `state_dir`. Native Windows, WSL, and Linux need separate
stable operational state directories because ownership identities and local
marketplace registrations contain host-native physical paths.

Raw filesystem mode bits are deliberately excluded from Skill digests,
marketplace digests, rendered integrity, and common/product overlay identity.
This avoids host-umask and checkout noise, but it also means the bridge does not
preserve or validate executable permission portability. Invoke scripts through
an explicit interpreter and use product metadata for execution intent.

This portable content rule is distinct from local state confidentiality. On
POSIX, the bridge restricts new ownership files, Schedule snapshots/runtime
files, and new Settings files to `0600`, with managed state/runtime directories
at `0700`; existing Settings files retain their mode. Windows POSIX-style mode
bits do not prove a private DACL, so Windows deployments rely on inherited ACLs
and should use user-private locations for product homes and `state_dir`.

## Ownership and reconciliation

`apply` records standalone Skills, Instruction files, and Settings leaves, and
publishes Schedule snapshots. On a later plan it can:

- remove a recorded symlink only if it still targets the recorded source;
- update or remove a managed copy only if its marker matches and it has not
  drifted;
- retain the displaced managed copy in the backup tree;
- report replacement, marker mismatch, content drift, or unmanaged content as a
  conflict;
- update/remove only Settings leaves whose current value still matches the
  target ownership digest;
- remove a deselected published Schedule pointer without touching a host
  scheduler entry.

`register` separately records the Plugin names and host heartbeat it
successfully reconciles for a target. Later deselection removes only those
recorded items. Claude registration includes marketplace update and Plugin
update commands so a bumped local release is refreshed. Unrelated Plugins and
scheduler entries remain untouched.

If a user copies a preview command and runs it manually, the product may change
but bridge ownership state does not. Use `agentbridge register` when automatic
future reconciliation is desired.

Target names identify ownership records. To rename/delete a target or change its
product/home identity safely, first keep the old target identity, set
`components = []`, run `apply` and `register`, and confirm the empty
reconciliation of all six component classes. Only then change or remove it.
Skipping this sequence leaves an orphan `state_dir/targets/<old-name>` record;
diagnostics fail and `apply` plus `register` stop because the bridge cannot infer
which new target, if any, owns the old state. Restore the old identity and
reconcile it to empty.

## Known alpha limitations

- No all-actions atomic transaction, apply/register target lock, automatic
  rollback, or recovery log. Schedule ticks use a separate runtime lock.
- A crash between a successful external mutation and the matching ownership
  write can leave a fail-closed conflict; the bridge has no automatic rollback
  or adoption path for that window.
- A sequential apply/register can stop after earlier actions succeeded; inspect
  a fresh plan before retrying.
- Symlink mode is live and bypasses copy-mode update checkpoints.
- No product capability inference, automatic vendor validation of arbitrary
  artifacts, or full post-install/cache validation. Doctor's executable
  `--version` probe is informational rather than a capability certification.
- Hook event parity is not inferred or tested by the bridge.
- Product CLIs own trust approvals, permission policy, caches, and authentication.
- Manual product commands bypass bridge ownership recording.
- Settings remain product-specific, project-local and policy-managed Settings
  are out of scope, and arrays are atomic merge leaves.
- Host Schedules do not appear in native product scheduler UIs, do not inherit
  conversation context, and do not replay missed minutes.
- Host scheduler availability, product CLI authentication, and unattended
  permission policy remain machine-local responsibilities.
- Plugin content changes require the same new strict SemVer in both product
  manifests; Hook content changes require a new `hooks/.version`. For packages
  present in both the current published snapshot and its replacement, precedence
  must increase; the bridge does not maintain a permanent release ledger.
- Claude Code Desktop Plugins are available in Local and SSH sessions, including
  Linux beta Local sessions, but unavailable in Remote (cloud) and WSL sessions.
- Product cloud state, organization-managed Plugins, and managed policy layers
  are not writable local bridge targets.

## Official references

- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [OpenAI: Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [OpenAI: Hooks](https://learn.chatgpt.com/docs/hooks)
- [OpenAI: Codex configuration](https://learn.chatgpt.com/docs/config-file/basic-config)
- [OpenAI: Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [OpenAI: ChatGPT desktop app for Windows](https://learn.chatgpt.com/docs/windows/windows-app)
- [OpenAI: WSL](https://learn.chatgpt.com/docs/windows/wsl)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Anthropic: Create plugins](https://code.claude.com/docs/en/plugins)
- [Anthropic: Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Hooks reference](https://code.claude.com/docs/en/hooks)
- [Anthropic: Claude Code configuration](https://code.claude.com/docs/en/configuration)
- [Anthropic: Claude Code scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
- [Anthropic: Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Anthropic: Remote Routines](https://code.claude.com/docs/en/web-scheduled-tasks)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
