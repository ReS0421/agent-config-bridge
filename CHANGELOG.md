# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
