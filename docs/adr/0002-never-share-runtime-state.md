# ADR-0002: Never share runtime state

## Status

Accepted — 2026-07-14

## Context

Codex and Claude product homes contain both declarative customization and mutable
runtime state. Depending on product and version, that state can include cached
authentication, sessions, conversation history, task databases, plugin caches,
logs, trust approvals, and update metadata. Windows native applications, WSL
CLIs, and Linux processes can access the same physical disk but have different
locking, permissions, path, and credential-store assumptions.

OpenAI documents that a user can point WSL `CODEX_HOME` at a Windows Codex home to
share configuration, cached auth, and sessions. That is a valid vendor-supported
user choice, but it is broader than this project's goal and expands both the
security and concurrency blast radius.

## Decision drivers

- Minimize credential exposure.
- Avoid corruption from multiple products or OSes writing one state store.
- Preserve independent logout, trust, retention, and enterprise policy behavior.
- Make bridge ownership an allowlist rather than an exclusion list.

## Considered options

### Share each product home wholesale

Convenient, but couples credentials and mutable databases to cross-OS filesystem
semantics and allows unrelated vendor changes into bridge scope.

### Maintain an expanding runtime exclusion list

Better, but unsafe by default: a newly introduced vendor file would be shared
until the bridge learned to exclude it.

### Share only declared component artifacts

Requires explicit renderers but fails closed when vendors add new runtime state.

## Decision

The bridge will never synchronize or link a complete product home. It projects
only the allowlisted `skills`, `plugins`, and `hooks` artifacts selected in bridge
configuration.

Authentication, sessions, histories, caches, trust/approval records, telemetry,
logs, and product databases are unconditionally out of scope. They cannot be
enabled through a component-selection option. Immutable builds, the stable
published marketplace, ownership records, and managed Skill backups live in a
separate `state_dir` and are never presented as vendor runtime state.

The generated state is designed to be non-secret. It reproduces catalog content,
so catalog authors must not embed secrets in Skills, Plugins, Hooks, scripts, or
manifests.

Installation does not confer trust. Each Codex or Claude target performs its own
workspace trust, hook review, connector authentication, and permission decisions.

## Consequences

### Positive

- A compromised or lost catalog does not automatically contain login tokens.
- Windows, WSL, and Linux sessions can be revoked and retained independently.
- Product database writers never contend through the bridge.
- Newly added vendor runtime files remain excluded automatically.

### Negative

- Users authenticate connectors and products separately per environment.
- Conversation history and install state do not follow the catalog.
- Supported Local/SSH environments need their own registration. Claude Code
  Desktop is available on Linux beta, but its Remote (cloud) and WSL sessions
  cannot load Plugins.
- Users wanting full-home synchronization must use a separate tool and accept its
  risks.

### Risks and mitigations

- **A declarative file embeds a secret:** plan review may expose literal
  command/URL values and generated state will reproduce the file; prohibit
  embedded secrets and prefer environment-variable references.
- **Backups reproduce private catalog content:** place `state_dir` in a
  user-controlled location and review backups before sharing them.
- **Path misclassification:** resolve targets explicitly, manage only named
  component destinations, and document that `state_dir` belongs outside product
  homes.

## Related decisions

- [ADR-0001: Render target-specific artifacts](0001-render-target-specific-artifacts.md)

## References

- [OpenAI: Windows app and WSL Codex homes](https://learn.chatgpt.com/docs/windows/windows-app#share-config-auth-and-sessions-with-wsl)
- [OpenAI: Hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
- [Anthropic: Claude settings scopes](https://code.claude.com/docs/en/settings)
