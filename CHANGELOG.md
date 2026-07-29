# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.4] - 2026-07-29

### Added

- Instruction bundles may declare strict version-1 Codex profile projections
  from canonical direct `codex/model-instructions/*.md` sources. The new
  `agentbridge instructions generate` command writes deterministic,
  developer-instructions-only TOML profiles atomically, while
  `agentbridge instructions check` performs a strictly read-only byte drift
  check.

### Security

- Catalog discovery, validation, planning, and apply-time rediscovery now fail
  closed on missing, stale, malformed, symlinked, undeclared, escaping, or
  colliding profile outputs; the Codex base `config.toml`, extra TOML keys, and
  blank or non-string instructions remain forbidden. Generated profiles use the
  existing Instruction ownership and backup lifecycle, so an unmanaged runtime
  destination is never adopted or overwritten.

## [0.3.3] - 2026-07-28

### Added

- Instruction bundles may project direct Markdown files under
  `model-instructions/` for Codex and Claude Code. Nested paths and non-Markdown
  files remain rejected by the destination allowlist.

### Security

- `agentbridge state prune --yes` now fails closed before mutation whenever the
  reviewed plan contains deletion candidates. A filesystem candidate
  ABA/path-generation gap could otherwise allow a byte-identical replacement
  to reuse observable metadata and be deleted as the reviewed object. Read-only
  planning and locked no-change validation remain available; generation-bound
  atomic candidate capture is deferred.

## [0.3.2] - 2026-07-24

### Added

- Governance now validates the redistribution vocabulary and requires
  digest-pinned imported-Git provenance, concluded license and rights metadata,
  and contained regular evidence and attribution files before an artifact may
  declare `redistribution = "allowed"`.
- Rendered Hook plugins now include governed attribution files under
  `licenses/<hook>/`; renderer identity and marketplace digests include that
  governance input so older builds cannot be reused.

### Changed

- Active security, architecture, onboarding, compatibility, and release
  documentation now consistently describes all six component classes,
  Instruction ownership and backup behavior, and the build-once cross-host
  release workflow.
- `apply` and `register` command help now names every state class each command
  reconciles.

## [0.3.1] - 2026-07-23

### Added

- Machine-readable `agentbridge plan --json` output now declares
  `schema_version = 1` and documents its required fields and fail-closed
  consumer contract.
- A CI-enforced release contract now keeps project and lock metadata aligned,
  prevents post-tag distribution changes from retaining the released version,
  and verifies exact version tags against package metadata and a clean
  worktree.
- Governance provenance validation now uses a closed origin vocabulary,
  including `orca-runtime`, and rejects unknown or malformed provenance
  origins before registry generation.

### Changed

- Cross-host promotion now requires one build from a clean, exactly tagged
  commit, recorded wheel and source-distribution SHA-256 digests, and
  installation of those same artifacts instead of rebuilding an existing
  version on each host.

## [0.3.0] - 2026-07-23

### Added

- `agentbridge state prune`, with a read-only default plan and explicit
  `--yes` apply mode, bounds immutable marketplace builds and managed Skill
  backups through optional `[bridge.retention]` limits. It pins the published
  build, excludes Instruction backups, safely unlinks terminal symlink
  snapshots, and fails closed before every deletion when state shape,
  ownership, integrity, path ancestry, or identity is unsafe.
- `doctor` now performs the same read-only marketplace schema and ownership
  preflight used by registration and reports absent, owned, foreign, malformed,
  undecodable, timed-out, or cross-platform-skipped registry state.
- `agentbridge sync-skills` applies only reviewed standalone Skill changes,
  while failing closed on full-plan conflicts or pending non-Skill mutations,
  preserving plan TOCTOU verification, and never rendering or registering
  other components. Converged runs require no confirmation. Switching from a
  Bridge-owned symlink installation to `link_mode = "copy"` now performs a
  checked, staged migration with a managed marker and retained link backup;
  interrupted multi-Skill migrations can resume only from fully matching
  managed copies. Skill creates use a per-action durable ownership checkpoint,
  no-change runs reconcile stale Skill state after completed removals, and copy
  updates create and verify a non-destructive final backup snapshot before a
  same-filesystem destination swap installs the staged replacement. Managed
  copy removals use the same snapshot-first retention model.
  Both paths revalidate the live destination after snapshot creation, and a
  partially deleted removal swap is recovered only through a newly verified
  restore staged from the retained backup.

- An `instructions` component (catalog ADR-5) bringing always-loaded policy
  files under the Bridge: `catalog/instructions/<bundle>/{claude-code,codex}/`
  overlays deploy file-by-file to overlay-relative paths below the target
  `config_home`, gated by a per-product destination allowlist (Claude Code:
  `CLAUDE.md`, `rules/**`, `agents/**`, `commands/**`; Codex: `AGENTS.md`,
  `agents/**`). Each destination file has exactly one owning bundle (two
  bundles shipping one relpath for one product is a validation error), the
  Bridge never merges or concatenates, and an existing unmanaged destination
  file conflicts even when its content matches. Sources must be non-empty
  UTF-8 without BOM; content identity normalizes CRLF/CR to LF. Delivery
  reuses the standalone link/copy machinery at file granularity with a
  per-target `instructions.json` ownership record, retained backups, safe
  deselection, `ResolvedInventory.instructions_for_target` governance gating
  in `required` mode, and `AGENTBRIDGE-MANAGED.json` provenance markers on
  managed instruction directories (root-level single files carry no marker).

- Governance core (`catalog/governance/*.toml` manifests as source of truth,
  ADR-1 capability axes, `GovernanceFinding` diagnostics) with `registry
  generate` writing a byte-deterministic `catalog/registry.json` and `registry
  check` gating drift. The active mode is the committed
  `catalog/governance/policy.toml` (`audit` and `required` implemented).
- `required` governance mode: a `ResolvedInventory` derived from the manifests
  gates which Skills deploy and which hooks render into each product's plugin,
  by deployable lifecycle and matching `[[targets]]`. Quarantining or removing
  a capability in the ledger retracts it on the next reconcile.
- `migrate-skills` dry-run/apply workflow for importing multiple existing Skill
  roots by priority, deduplicating portable text line endings, retaining
  divergent variants, repairing explicitly approved legacy frontmatter, and
  generating content-free HADS Markdown and JSON reports.
- Discovery-root redirect diagnostics for symlinks, junctions, and directory
  reparse points.
- Doctor validation and `--version` probing for the exact configured or
  PATH-selected product executable.
- Single-writer/multiple-passive-consumer support for installations sharing one
  physical Skill discovery root.
- End-to-end Windows, Linux, and WSL onboarding documentation.

### Changed

- Codex marketplace discovery supports both the absolute `root`-only local
  entry observed in CLI 0.144.4 and the expanded, matching
  `marketplaceSource` entry observed in 0.144.6. Product JSON and executable
  version output are decoded as strict UTF-8 rather than the host locale.
- Default Claude homes remove any inherited `CLAUDE_CONFIG_DIR`; command plans,
  JSON, copyable previews, and internal registration all model that removal,
  while custom homes still receive the reviewed value.
- A target `executable` now selects the reviewed product CLI for Plugin/Hook
  preflight and registration as well as Schedules.
- Skill validation accepts wrapped YAML `description` values commonly found in
  installed Skills.

### Security

- Retention rebuilds the complete reviewed plan under an exclusive lock and
  revalidates inode/device/timestamps, ownership markers, content digests, and
  real ancestors immediately before descriptor-anchored deletion of only
  Bridge-generated entries. Platforms without that safe primitive fail closed.
- Skill migration never mutates source roots; refuses overlapping or redirected
  catalog, conflict, and report outputs; verifies retained variants on rerun;
  bounds each Skill to 100 MiB before reading; excludes transient bytecode/cache
  artifacts; materializes only contained regular-file links; escapes untrusted
  report text; and reports secret rule/file matches without matched values.
- `migrate-skills --json` now emits one JSON document for dry runs, applies, and
  no-op report refreshes.

## [0.2.0] - 2026-07-14

### Added

- Independently selectable `settings` component with product-native canonical
  fragments for Codex `config.toml` and Claude Code `settings.json`.
- Conflict-aware Settings reconciliation by explicit leaf, including
  target-scoped value digests, drift detection, safe deselection, atomic writes,
  and preservation of unrelated local values.
- Comment-preserving Codex TOML patches and structural Claude JSON patches.
- Independently selectable `schedules` component with strict five-field cron,
  IANA timezone, target-relative working directory, bounded timeout, and prompt
  schema.
- Immutable per-target Schedule snapshots and direct manual execution through
  `agentbridge schedule run`.
- Idempotent minute ticks with a short target minute-claim lock, no
  missed-minute replay, and shell-free product CLI invocation with prompts on
  standard input.
- Ownership-aware current-user crontab heartbeat on Linux and namespaced
  Windows Task Scheduler heartbeat, reconciled through `agentbridge register`.
- Optional per-target Schedule vendor executable override, with validated
  absolute-path discovery at registration when omitted.
- Runtime dependencies on `tomlkit` for Codex Settings preservation and native
  Windows `tzdata` for IANA Schedule timezones.
- Architecture decisions for owned-leaf Settings and host scheduler adapters.

### Changed

- The canonical catalog and component selection model now cover Skills,
  Plugins, Hooks, Settings, and Schedules.
- `apply` also reconciles Settings and publishes Schedule snapshots;
  `register` also reconciles host scheduler heartbeats.
- Minute claiming now releases its target lock before vendor execution and uses
  a separate per-Schedule run lock, allowing later minutes and other Schedules
  to proceed without overlapping the same Schedule name.
- Windows heartbeat tasks use `MultipleInstancesPolicy=Parallel` and
  `ExecutionTimeLimit=PT0S`; canonical Schedule timeouts bound vendor processes.
- Target identity cleanup with `components = []` now reconciles all five
  component classes before a target is renamed, moved, disabled, or deleted.
- Schedule prompt discovery normalizes CRLF and lone CR line endings to LF so
  equivalent Windows and POSIX checkouts produce the same immutable snapshot.

### Security

- Reject unmanaged or drifted Settings leaves instead of adopting or
  overwriting them, even when an unowned current value equals the desired value.
- Store Settings ownership as paths and digests without retaining unrelated
  local values or full product-file backups.
- Constrain Schedule worktrees beneath target user homes and prohibit
  catalog-defined environment variables or permission-bypass flags.
- Protect scheduler create/update/removal with target-scoped markers, content
  digests, ownership state, and stale-plan checks.
- Resolve Windows Task Scheduler through the WinAPI-reported System32 path and
  exclude the current directory and relative entries from unattended product
  CLI discovery; show all resolved heartbeat paths before confirmation.
- Restrict new ownership state, Schedule snapshot/runtime state, and new
  Settings files to private POSIX modes; document that Windows confidentiality
  depends on inherited ACLs rather than POSIX mode bits.
- Document the fail-closed crash window between successful external mutation
  and its ownership-state write; automatic adoption and rollback remain out of
  scope.
- Keep Codex and Claude Desktop scheduler databases, Claude loop sessions, and
  Remote Routine cloud state outside the synchronization boundary.

## [0.1.0] - 2026-07-14

### Added

- Initial cross-platform bridge for selectively sharing skills, plugins, and
  hooks with Codex and Claude Code.
- Canonical catalog layout with common, Codex, and Claude Code plugin overlays.
- Safe greeting skill and event-name-only audit hook examples.
- Immutable content-addressed marketplace builds published through a stable,
  integrity-checked local marketplace path.
- Target-scoped ownership records for standalone Skills and bridge-registered
  Plugins/Hooks, bound to product, platform, and managed roots.
- Skill deselection reconciliation, managed-copy drift detection, retained copy
  backups, and safe recorded-symlink removal.
- Plugin/Hook registration reconciliation with Claude marketplace and Plugin
  refresh commands.
- Custom Claude `config_home` Skill destinations and target-specific POSIX or
  PowerShell command previews.
- Plan review items for Hook handlers and Plugin MCP command/URL declarations.
- Linux beta Claude Code Desktop compatibility, with Local/SSH Plugin support
  distinguished from unsupported Remote (cloud)/WSL sessions.
- Orphan target-state diagnostics and a documented empty-component reconciliation
  workflow before target identity changes.
- Marketplace-source relocation reconciliation and cross-host registration-command
  suppression.
- Fail-closed marketplace-source ownership preflight before all product
  registration commands, including initial add/refresh and safe partial-retry
  handling.
- Staged Skill-root handoff and target-scoped ownership proof, preventing
  physical-path aliases, Windows case variants, canonical links, or copy markers
  from being adopted by another target.

### Security

- Reject catalog path escapes, broken symlinks, special filesystem nodes,
  directory symlinks, Windows device names, nested Windows-incompatible names,
  reserved managed-copy/foreign Plugin metadata, portable cross-overlay output
  collisions, conflicting overlays, and corrupted published marketplace snapshots.
- Block `apply` and `register` when enabled-target ownership records become
  orphaned instead of guessing a new owner.
- Require matching strict SemVer in both product Plugin manifests and
  `hooks/.version` for generated Hook packages; changed packages overlapping the
  current published snapshot require strictly higher precedence.
- Exclude raw filesystem mode bits from portable content identity and document
  that executable permissions are not preserved across targets.
- Normalize Windows extended-path symlink identities and fail closed when
  physical overlap checks encounter looped, broken, or unreadable ancestors.
