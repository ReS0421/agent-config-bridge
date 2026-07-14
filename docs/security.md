# Security model

Agent customizations are executable supply-chain inputs. A Skill can direct an
agent to run commands, a Plugin can start an MCP server, and a Hook can execute
at lifecycle boundaries. A Settings fragment can change permissions or tool
behavior, and a Schedule can trigger a prompt without a human present. The
bridge therefore treats its canonical catalog and every rendered change as
code, not as harmless preferences.

## Trust boundaries and assets

The main trust boundaries are:

1. the catalog source and its update channel;
2. the bridge process and renderer;
3. each Windows, WSL, or Linux target home;
4. Codex and Claude Code's trust, sandbox, cache, and permission systems;
5. host schedulers, product CLIs, external executables, MCP servers, connectors,
   and network services invoked by projected content.

Assets to protect include credentials, source code, user files, command approval
policy, conversation history, and target configuration integrity. The bridge is
not a sandbox against a malicious catalog the user chooses to install. Its role
is to constrain paths it manages, expose executable declarations for review, and
avoid importing unrelated product state.

## Allowlist, never home-directory sync

Only five component classes are eligible for projection:

- standalone `skills`;
- `plugins` and their declarative package payload;
- `hooks` and the handler files explicitly placed in Hook bundles;
- explicit product-native `settings` leaves in reviewed fragments;
- strict `schedules` containing a definition and prompt.

Everything else is excluded. Never copy or link:

- OAuth tokens, API keys, credential stores, cookies, or connector secrets;
- session transcripts, conversation history, memories, checkpoints, or task
  databases;
- trust hashes, Hook approvals, workspace-trust decisions, or permission grants;
- private Codex/Claude Desktop scheduler databases, Claude loop session state,
  or Remote Routine account state;
- telemetry, logs, caches, Plugin install caches, temporary product files, or
  update state;
- the entire `~/.codex`, `%USERPROFILE%\.codex`, or `~/.claude` directory.

Settings eligibility is deliberately narrow: Codex `config.toml` and Claude
Code `settings.json` at the configured user home. It does not include
credentials, `~/.claude.json`, project-local configuration, managed policy, or
any other file merely because it is beneath a product home.

This remains true even if vendor tools can be pointed at one home. Dedicated
credential-management or whole-home synchronization is outside this project's
scope. See [ADR-0002](adr/0002-never-share-runtime-state.md).

## Generated state is non-secret by design

The configured `state_dir` contains only:

- immutable rendered marketplace builds;
- the stable published marketplace snapshot;
- immutable Schedule snapshots and their stable target pointers;
- non-sensitive Schedule locks and last-processed minute markers;
- small per-target Skill, Plugin, Settings, and scheduler ownership records;
- retained managed Skill copies displaced during update or deselection.

Use a separate stable `state_dir` per native Windows, WSL, or Linux host. It is
operational ownership state with physical paths, not part of the portable
canonical catalog. Configuration loading rejects physical equal/nested overlap
between generated state, the canonical catalog, enabled product homes, and
Skill discovery roots, including symlink aliases and Windows case variants.

The bridge never writes product auth, session, conversation, trust, or cache
state there. Ownership records contain artifact identities, paths, link/copy
modes, and content/value digests, not credentials or displaced Settings values.

On POSIX, newly created ownership files, Schedule snapshots/runtime files, and
new vendor Settings files use mode `0600`; bridge-managed state/runtime
directories use `0700`, and an existing Settings file retains its prior mode.
This is defense in depth, not a substitute for keeping the catalog secret-free.
Windows `chmod` behavior does not establish or audit a private DACL. Inherited
Windows ACLs remain authoritative, so use user-private ACL-protected product
homes and `state_dir` locations and review their ACLs separately.

This design does not sanitize catalog content. Generated packages and backups
reproduce the canonical files, so a secret committed to a Skill, Plugin, Hook,
script, manifest, or `.mcp.json` will also appear in generated state. Keep the
catalog and `state_dir` in user-controlled locations and reference secrets
through product-supported environment/configuration mechanisms rather than
embedding values.

The same warning applies to Settings values and Schedule prompts. Rendered
Schedule snapshots reproduce the prompt, and product or host scheduler logs may
capture execution metadata or output outside bridge control. Catalog Schedules
cannot define arbitrary environment variables, but the invoked CLI still uses
the target host's local environment, authentication, and product configuration.

## Catalog validation and trust

The bridge accepts a local catalog path. Establish its provenance before use;
for a Git catalog, review and pin a trusted commit or release according to your
own workflow. The bridge does not fetch a remote catalog, verify signatures, or
attest publisher identity.

Implemented validation includes:

- lowercase kebab-case artifact names;
- rejection of Windows device names, invalid nested path characters, trailing
  dots/spaces, and case-insensitive sibling collisions;
- exact `SKILL.md` entry points and basic portable frontmatter requirements;
- required product Plugin manifests, matching names, and matching strict SemVer;
- Hook matcher/handler structure and a strict-SemVer `hooks/.version`;
- strict native Settings documents, unique owned leaf paths, and rejection of
  symlinked or special fragment files;
- exact Schedule file sets, bounded source sizes, numeric five-field cron,
  IANA timezone, bounded timeout, and a portable relative worktree path;
- rejection of broken/escaping symlinks and all directory symlinks; only
  contained links to regular files are accepted;
- rejection of conflicting product overlay output;
- integrity hashes for immutable and published rendered marketplaces.

The bridge does not scan unexpected executables, run arbitrary vendor validators,
probe product versions, verify Hook semantics, or decide that a catalog is safe.
Validation is structural; code review remains necessary.

## Plan before mutation

`validate`, `plan`, and `doctor` are read-only. `plan` reports:

- Skill creates, updates, removals, no-ops, and conflicts;
- aggregate Settings leaf dispositions, fragment paths, and destinations;
- Schedule snapshot creates/updates/removals and the host-scheduler boundary;
- marketplace create/update state;
- Hook events, matchers, handler types, and command/URL/prompt values;
- Plugin `.mcp.json` or manifest MCP command/URL values;
- product CLI argv and environment needed for later registration;
- relevant warnings, including the Claude Code Desktop session boundary.

Review items intentionally display literal catalog command and URL fields. They
are not a redacted audit log. Do not embed tokens in those fields or publish plan
output without inspecting it. The bridge does not currently enumerate every
argument, working directory, environment variable, executable resolution, or
network behavior and does not report vendor validation results.

Settings values and Schedule prompt text are source-review inputs rather than
expanded plan output. Review the referenced fragment files and every
`PROMPT.md` directly before approving `apply` and `register`; counts and digests
are not a substitute for code review.

Before `apply` or `register` mutates state, it rediscovers the catalog and
rebuilds the plan. A difference from the reviewed plan aborts the operation.
Unmanaged Skill destinations, unmanaged Settings leaves, and drifted
bridge-managed content are hard conflicts. A corrupted generated artifact that
does not change plan identity can still fail a later integrity check rather than
being classified as a stale plan.

External mutation and ownership persistence cannot be one filesystem-atomic
operation. A process can crash after replacing a Settings file, changing a
product marketplace/install registry, or changing crontab/Task Scheduler, but
before writing the matching target ownership record. The next plan or register
run fails closed when it cannot prove ownership; inspect the external system and
recover explicitly. The bridge does not silently adopt the result or promise
automatic rollback.

## Filesystem mutation model

The apply engine acts sequentially and fails closed on detected ownership or
digest mismatches:

- new symlinks never replace existing paths;
- recorded symlinks are unlinked on deselection only if they still target the
  recorded source;
- new managed copies are staged next to the destination and checked against the
  planned source digest;
- managed copies update/remove only when their marker and installed digest still
  match;
- displaced unchanged managed copies are retained below `state_dir/backups`;
- marketplace builds are immutable and content-addressed;
- the published marketplace is rehashed before reuse/replacement, copied through
  a temporary sibling, and checked before publication;
- ownership state paths are target-scoped and reject a parent symlink that would
  escape `state_dir`;
- Settings writes recheck the destination, stage a same-directory regular file,
  and replace only when the reviewed byte digest still matches;
- Schedule snapshots are immutable and content-addressed, while stable pointers
  and runtime markers remain beneath validated `state_dir` paths.

Raw filesystem permission bits are excluded from Skill, source, overlay, and
rendered marketplace identity. This avoids false drift across Git checkouts,
Windows, WSL, and different umasks, but the bridge does not preserve or validate
executable-mode portability. Hook and MCP commands should invoke a named
interpreter and declare execution intent through product metadata where possible.

Current alpha limitations matter for threat modeling:

- there is no apply/register target lock or one transaction covering all
  actions (Schedule ticks have a separate runtime lock);
- there is no automatic rollback or recovery log;
- a later action can fail after an earlier action succeeded;
- symlink mode is live, so canonical Skill changes become visible immediately;
- retained backups have no automatic retention/restore command;
- the implementation does not provide comprehensive no-follow/reparse-point
  protection against every concurrent filesystem race on POSIX, Windows, or WSL.

After an interrupted operation, inspect a fresh `plan`. Restore a retained
managed-copy backup manually only after reviewing both the destination and
ownership state.

## Package versions and caches

Product caches may otherwise retain old Plugin content under an unchanged
release identity. The bridge requires matching strict SemVer in a canonical
Plugin's Codex and Claude Code manifests. When a package name exists in both the
current published snapshot and its replacement, changed content requires a new
version with strictly higher SemVer precedence. Generated Hook content is
versioned by `catalog/hooks/.version` and follows the same rule.

This check is not a permanent version ledger: deleting bridge state or removing
and later re-adding a package removes the comparison baseline. It is also not a
signature, supply-chain attestation, or guarantee that every external product
cache refreshed. `register` asks Claude Code to update its marketplace and each
selected Plugin; product CLIs still own the result.

## Registration and product-owned trust

`register` requires confirmation, runs only on the configured target platform,
rechecks the plan, and passes `CODEX_HOME` or `CLAUDE_CONFIG_DIR` explicitly. It
records desired Plugin names only after all planned commands for that target
succeed, and separately records a successfully reconciled host heartbeat. Later
deselection removes only names and scheduler entries in those bridge records;
unrelated product installations and host jobs are out of scope.

Running preview commands manually bypasses ownership recording. It may create an
installation the bridge cannot later reconcile, so use `agentbridge register`
when managed lifecycle behavior is desired.

Changing/deleting a target identity has the same ownership risk. Before changing
its `name`, product, or home, keep the old target, set `components = []`, and run
both `apply` and `register`. If the target disappears first, its
`state_dir/targets/<name>` record is orphaned; diagnostics fail and `apply` plus
`register` stop until the old identity is restored and reconciled. The bridge
does not guess a new owner or delete it automatically.

Before any product registration commands, `register` queries the product's
current marketplace registry. The `agent-config-bridge` entry must be absent,
point to the source recorded by the bridge, or already point to the desired
source after a partial retry. On an initial registration with no ownership
record, only an absent entry or the desired source is accepted. A duplicate
entry, an unknown vendor JSON shape, or a third-party source fails closed before
marketplace add/update, Plugin installation, or removal starts.

Installation does not confer trust. Codex and Claude Code remain responsible for
Hook review, workspace trust, organization policy, permission prompts, Plugin
caches, connector authentication, and disabled-feature settings. The bridge does
not copy or bypass those controls and does not claim a package is active merely
because registration commands completed.

## Settings and Schedule hardening

Settings ownership is per leaf and per target. New values can claim only absent
paths; equal pre-existing values are not adopted. Updates and cleanup require
the current value digest to match the ownership record. The bridge retains no
backup of the full vendor file because that would copy unrelated local values
into operational state. Review and back up local configuration using the
product's normal mechanism when appropriate.

Host scheduler registration is similarly fail-closed. Linux reconciliation
touches only one marked current-user crontab block. Windows reconciliation
touches only the target-namespaced Task Scheduler task. An unexpected marker,
duplicate block, malformed task, changed command, or digest mismatch is a
conflict; the bridge does not adopt by name.

At runtime, each target uses a short non-blocking claim lock and a
snapshot/minute marker. The marker is written before due work begins, then the
claim lock is released so long-running work cannot hide later minutes. A second
lock keyed by target and Schedule name is held for each vendor invocation. A
recurrence is skipped when that same Schedule is still active; it is not queued
or replayed. The bridge therefore favors at-most-once attempts for a minute and
non-overlap for each Schedule name rather than automatic retry.

Prompts are passed on standard input to a fixed, shell-free vendor argv. The
bridge adds neither Codex nor Claude permission-bypass flags. The host scheduler
itself invokes only the absolute `agentbridge` entry point, absolute config
path, and a validated absolute vendor executable pinned during registration.
Explicit target overrides are resolved as real files; automatic discovery uses
only absolute entries from the registering process `PATH` and excludes the
current directory and relative entries. Linux requires an executable file,
while Windows requires a native `.exe` or `.com` launcher. Registration exposes
the resolved paths before confirmation. On Linux the fixed heartbeat command is
shell-quoted for crontab and cron percent handling. Working directories must
physically resolve beneath the target `user_home`.

The owned Windows task uses `MultipleInstancesPolicy=Parallel` and
`ExecutionTimeLimit=PT0S`: Task Scheduler can start later heartbeats and does
not terminate long work at one minute. The bridge's claim/per-Schedule locks and
the canonical `timeout_seconds` are the intended concurrency and duration
controls. Task inspection and mutation invoke only the genuine System32
`schtasks.exe` path reported by WinAPI, never a bare current-directory-resolved
command. The task still uses the current user's interactive token and least
privilege.

This is not a sandbox. A permitted CLI can still modify files, invoke tools, or
reach networks according to its local configuration and authentication. Use
least privilege, narrowly scoped prompts, finite timeouts, and product sandbox
or allowlist policies suitable for unattended execution. Do not assume a
Desktop prompt or approval dialog will be available to a host-scheduled process.

## Hook and MCP hardening

Catalog authors should:

- validate all JSON input from the product;
- reject `..`, unexpected absolute paths, and paths outside allowed roots;
- use product root variables such as `PLUGIN_ROOT` or `CLAUDE_PLUGIN_ROOT` rather
  than catalog-time absolute paths inside cached packages;
- avoid inherited secrets unless the Hook or MCP server explicitly requires
  named variables;
- set finite timeouts and predictable exit behavior;
- avoid network access by default;
- keep Windows PowerShell and POSIX commands in product-specific overlays when
  they differ;
- quote command strings for the shell that actually receives them;
- treat prompt, file, tool, Hook, and MCP output as untrusted input.

The bridge structurally appends common and product-specific Hook matcher groups;
it does not prove that event timing or blocking semantics match. If equivalence
is uncertain, do not put the declaration in `common/hooks.json`.

## Reporting vulnerabilities

Security reports should include the bridge version or commit, target type, a
carefully redacted plan, and the smallest catalog fragment that reproduces the
issue. Never attach auth files, session databases, `state_dir` contents that may
reproduce private catalog files, or unredacted environment dumps to a public
issue.

## Official references

- [OpenAI: Hook review and trust](https://learn.chatgpt.com/docs/hooks)
- [OpenAI: Plugin-bundled Hooks](https://learn.chatgpt.com/docs/build-plugins)
- [OpenAI: Codex configuration](https://learn.chatgpt.com/docs/config-file/basic-config)
- [OpenAI: Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Anthropic: Hook security best practices](https://code.claude.com/docs/en/hooks#security-best-practices)
- [Anthropic: Plugin marketplace caching](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Claude Code configuration](https://code.claude.com/docs/en/configuration)
- [Anthropic: Claude Code scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
- [Anthropic: Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Anthropic: Remote Routines](https://code.claude.com/docs/en/web-scheduled-tasks)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
