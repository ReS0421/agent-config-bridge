# ADR-0005: Use host scheduler adapters for portable recurring tasks

## Status

Accepted — 2026-07-14

## Context

Codex and Claude expose several cron-like experiences, but they do not share a
portable local storage or management API. Codex scheduled tasks are managed in
ChatGPT web or Desktop, not Codex CLI. Claude CLI `/loop` tasks belong to a
session and recurring entries expire after seven days. Claude Desktop local
tasks persist, but only their prompt `SKILL.md` is public; cadence, working
folder, model, and enabled state remain UI-managed. Claude Remote Routines are
account-owned cloud resources and already follow the account across operating
systems.

Copying private Desktop databases would violate the runtime-state boundary and
would depend on undocumented schemas. Treating a session-scoped loop as a
durable cron job would also create false guarantees.

## Decision drivers

- One durable, reviewable definition for recurring local CLI work.
- Equivalent Windows and Linux behavior without private product state.
- Explicit unattended-execution and permission boundaries.
- No prompt interpolation into a shell command.
- Safe install, update, deselection, and drift handling.

## Considered options

### Synchronize Desktop task databases

Rejected because the schemas and locking contracts are private and the files
can contain runtime state.

### Drive each product UI automatically

Fragile, difficult to audit, and unsuitable for unattended reconciliation.

### Install bridge-owned host scheduler heartbeats

Does not appear in product-native scheduler UIs, but uses public CLI contracts
and can be implemented consistently on both supported operating systems.

## Decision

Add `schedules` as an independently selectable component. A canonical schedule
contains a strict, versioned `schedule.toml` and a `PROMPT.md`. The first schema
uses a five-field cron expression, an IANA timezone, a working directory
relative to the target user home, and a bounded timeout.

`apply` renders an immutable per-target schedule snapshot. `register` explicitly
installs one bridge-owned minute heartbeat per target:

- a marked user crontab entry on Linux;
- a namespaced Task Scheduler task on Windows.

The heartbeat invokes `agentbridge schedule tick` with an absolute bridge
configuration path, target identity, and validated absolute vendor CLI path.
The path comes from an optional per-target override or registration-time `PATH`
discovery, so later host scheduler runs do not depend on their ambient `PATH`.

The tick command briefly takes a target lock to claim and record one
snapshot/minute, then releases it before vendor work. Each vendor invocation
holds a separate target/Schedule lock; a recurrence is skipped if that same
Schedule is still active, while other Schedules and later minutes can proceed.
Missed minutes are not replayed. Prompt text is sent on standard input to either
`codex exec` or `claude --print`; it is never shell-interpolated and no
permission-bypass flag is added.

The Windows task permits parallel heartbeat instances and has no Task Scheduler
execution-time limit (`Parallel`, `PT0S`). Per-Schedule locks provide overlap
control and the canonical timeout bounds each vendor subprocess. POSIX Schedule
state and runtime files/directories use private `0600`/`0700` modes. Windows
deployments instead rely on the inherited ACL of a user-private `state_dir`.

The target operating system, scheduler service, product CLI, authentication,
and permissions remain local. Schedules do not carry tokens or arbitrary
environment variables. Working directories are resolved beneath each target's
`user_home` so one relative intent can map to native Windows and Linux paths.

Vendor-native scheduler adapters may be added only when a stable public
management contract exists. Until then, Codex Desktop schedules, Claude Desktop
local task metadata, Claude session loops, and Claude Remote Routines are
reported as distinct external capabilities rather than silently mutated.

## Consequences

### Positive

- A recurring workflow can be versioned once and run on native Windows/Linux.
- Private Desktop state remains isolated.
- Schedule installation and removal are explicit and ownership-aware.
- Prompt content never becomes shell syntax.

### Negative

- Host-managed jobs do not appear in Codex or Claude Desktop scheduler UIs.
- The machine and host scheduler must remain available.
- CLI runs do not inherit the conversational context of a Desktop thread.
- WSL and native Windows require separate targets and heartbeat registrations.

### Risks and mitigations

- **Unattended destructive work:** do not add bypass flags; use the narrowest
  product sandbox and tool policy, review prompts, and bound timeouts.
- **Duplicate or overlapping runs:** one short target lock and a recorded minute
  make claims idempotent; a per-Schedule lock skips only a still-active
  recurrence; missed runs are not replayed.
- **Scheduler takeover:** verify a bridge marker and digest before replacing or
  removing a crontab block or Windows task.
- **Path injection:** require relative working directories, resolve beneath the
  configured home, and pass vendor arguments as an argv array.
- **Crash after scheduler mutation:** a missing or stale ownership-state write
  becomes a fail-closed conflict; require explicit inspection rather than
  adopting the external entry or promising rollback.

## Related decisions

- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0004: Manage declarative settings by owned leaf](0004-manage-declarative-settings-by-owned-leaf.md)

## References

- [OpenAI: Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Anthropic: Claude Code scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks)
- [Anthropic: Desktop scheduled tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks)
- [Anthropic: Remote Routines](https://code.claude.com/docs/en/web-scheduled-tasks)
