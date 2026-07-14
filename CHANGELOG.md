# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
