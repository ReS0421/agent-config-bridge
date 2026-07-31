# ADR-0007: Preserve Codex profile Hook trust state

## Status

Accepted — 2026-07-30

## Context

Codex can append provider-owned Hook trust decisions to the active named
profile as a `[hooks.state]` table. A Bridge-managed generated profile COPY
therefore stops matching its recorded Instruction digest after a normal Codex
approval. Treating that append as arbitrary drift makes every later plan
conflict and causes the conflict to recur after each profile update.

The generated Catalog profile must nevertheless remain a deterministic
`developer_instructions`-only projection. Hook trust is runtime authority
owned by Codex; the Bridge must not generate, grant, edit, infer, or synchronize
it.

## Decision drivers

- Keep the Catalog source and generator closed to runtime trust state.
- Let normal provider approvals converge instead of becoming permanent drift.
- Preserve provider-owned state exactly across managed profile updates and
  retained backups.
- Reject any broader runtime configuration or malformed trust representation.
- Avoid changing the generic Instruction or Settings ownership boundaries.

## Decision

Only a Bridge-managed regular-file COPY of a declared generated Codex root
`<name>.config.toml` profile may contain runtime bytes after its recorded
managed content. The managed prefix must still match the recorded Instruction
digest after newline normalization.

The additional bytes must be a suffix that parses to exactly one top-level
`hooks` table containing exactly `state`. Every `hooks.state` child must have a
non-empty identifier and exactly one `trusted_hash` string matching
`sha256:<64 lowercase hexadecimal digits>`. The literal `[hooks.state]` header
must start at column zero on its own line without a trailing comment. This
deliberately strict parser subset rejects alternate-but-equivalent TOML header
formatting. No other TOML data is accepted.

Planning excludes a valid suffix from managed-content drift. A no-op does not
rewrite the file. During a managed profile update, the Bridge copies the new
Catalog profile and appends the validated suffix byte-for-byte; the displaced
file, including that exact suffix, is retained as the normal Instruction
backup. Deselection likewise retains the complete file as its backup.

The Bridge never creates a suffix, trust identifier, or hash and never edits an
entry. An unmanaged destination still conflicts. A modified managed prefix,
malformed suffix, extra table/key/value, invalid hash, or the same suffix on
any other Instruction file conflicts. Symlink delivery has no exception.
On POSIX, generated Codex profile copies are installed with mode `0600`; a
reviewed apply repairs a legacy managed mode and tightens the displaced backup
to `0600`. Windows confidentiality is governed by inherited ACLs, not by a
claim that POSIX mode bits establish a private DACL.

For an update, the Bridge records the exact digest and file identity of the
fully staged replacement after appending the validated suffix. Managed-prefix
parsing, suffix extraction, the exact old-file digest, and path identity all
come from one descriptor-backed old-file observation whose path identity is
checked before and after the read. It revalidates that observation, atomically
displaces the old file to a private sibling, and verifies the exact displaced
file before retaining it. The Bridge keeps that local name while creating and
validating the backup. A same-filesystem backup prefers a no-replace hard link,
so later writes to the old inode remain visible through the backup.
Cross-filesystem or hard-link-unsupported retention uses an exclusively
created byte copy and revalidates the still-named old inode before continuing.

Only after retention succeeds does the Bridge publish the staged replacement.
Hard-link publication is atomic when supported; otherwise an exclusive-create
copy preserves the no-overwrite rule and is checked for exact bytes and path
identity. The fallback destination can be visible while its bytes are written,
so an ambiguous copy error fails closed and leaves the active path and recovery
candidates for inspection. The Bridge rechecks the old local name and backup
after replacement publication before releasing the local name.

Localized recovery never unlinks an occupied active destination, including one
that still identifies the staged replacement. If the destination is absent, it
restores the first regular-file candidate with a no-replace hard link or an
exclusive-create copy; a concurrent creator wins and is not overwritten or
deleted. If the destination is occupied, the active path and recoverable old
candidates are all preserved and the operation fails closed. Removal uses the
same retention and absent-only restoration rules.

These checks define a final validation and path-identity boundary; they are not
filesystem writer exclusion. Renaming a path cannot revoke an already-open
writable descriptor. On a same filesystem, a hard-linked backup continues to
name that inode. With a copied cross-filesystem backup, a write through another
same-user descriptor after the final old-inode check and local-name cleanup can
occur too late to enter the backup. The same-user actor can also rewrite or
delete Bridge private candidates directly. Those actions remain outside this
localized retention boundary and require manual recovery rather than a claim
that every late write is preserved.
Catalog generation, Catalog discovery, and `instructions check` continue to
require generated source profiles containing only `developer_instructions`.
The base Codex `config.toml` remains outside Instruction management.

## Consequences

### Positive

- Codex-owned Hook approvals no longer cause recurring Bridge drift.
- Trust material stays product-owned and survives reviewed profile updates.
- The exception is destination-only, typed, and limited to generated Codex
  profile copies.

### Negative

- Installed profile bytes can intentionally differ from their Catalog source.
- The Bridge must parse a small provider-owned TOML shape during copy
  planning, update, and removal.
- Provider schema changes fail closed until this decision is reviewed.

### Security consequences

- The Bridge does not grant authority: only Codex can create or alter trust
  entries through its approval flow.
- Lowercase SHA-256 shape validation establishes representation integrity, not
  that a Hook is safe or that an approval should have been granted.
- Byte preservation includes trust state in Instruction backups. Protect
  `state_dir` with the same local-user controls as the product home.
- Any data outside the closed suffix shape remains a conflict and is never
  silently preserved into a new managed profile.
- Update validation detects open-descriptor writes visible at its explicit
  checks; it does not serialize other same-user writers or preserve a
  cross-filesystem source write made after the final old-inode check.
- The localized copy/backup/install checks do not fsync file contents or parent
  directories and make no power-loss durability guarantee.

## Related decisions

- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0004: Manage declarative settings by owned leaf](0004-manage-declarative-settings-by-owned-leaf.md)
- [ADR-0006: Generate Codex instruction profiles](0006-generate-codex-instruction-profiles.md)
