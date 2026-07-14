# Security Policy

Agent Config Bridge projects executable agent configuration and unattended
Schedule prompts into product and host-managed locations. Security reports are
taken seriously.

## Supported versions

Until the project reaches a stable release, security fixes are provided on the
latest version of the `main` branch. Older snapshots may not receive fixes.

## Reporting a vulnerability

Please report vulnerabilities through a [private GitHub security
advisory](https://github.com/ReS0421/agent-config-bridge/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability.

Include only the information needed to reproduce and assess the problem:

- Affected version or commit
- Operating system, product, and surface
- Minimal reproduction steps
- Expected and observed behavior
- Security impact and any known mitigations

Remove tokens, credentials, personal prompts, filesystem inventories, and other
sensitive data. You should receive an acknowledgment through GitHub. The
maintainers will coordinate validation, remediation, and disclosure there.

## Security boundaries

Useful reports include unintended writes outside managed paths, path traversal,
unsafe symlink handling, overwrite of unmanaged content or Settings leaves,
command injection, secret leakage, generated-marketplace or Schedule-snapshot
integrity failures, scheduler ownership confusion, and cases where a read-only
plan operation changes state.

Catalog hooks are executable code. Running a hook that a user deliberately
installed from an untrusted catalog is not, by itself, a vulnerability in Agent
Config Bridge. Executing a hook that was not selected, bypassing a conflict or
trust check, or altering the hook during synchronization may be a vulnerability.

Agent Config Bridge is not intended to synchronize authentication, session
databases, caches, logs, trust decisions, private Desktop scheduler state, or
complete Codex or Claude Code configuration homes.

The generated `state_dir` is non-secret operational state by design, but
rendered packages, retained Skill backups, Settings digests, and Schedule
snapshots reproduce or describe canonical catalog content. A catalog containing
embedded secrets can therefore copy them into generated state. Do not attach
catalog or state files to a report without reviewing and redacting them.

The current alpha does not claim an all-actions atomic transaction, target lock,
automatic rollback, recovery log, product capability/version probe, arbitrary
vendor artifact validation, or full installed-state validation. A report should
distinguish an advertised ownership/integrity guarantee from one of these known
limitations.

Raw filesystem permission bits are excluded from portable content identity; the
bridge does not guarantee executable-mode preservation. Target names also key
ownership state. Rename/delete targets only after reconciling the old identity
with `components = []`, `apply`, and `register`; otherwise diagnostics report an
orphan record and state-changing commands stop until it is restored and
reconciled. Orphan state is never adopted or removed automatically.
