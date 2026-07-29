# ADR-0006: Generate Codex instruction profiles

## Status

Accepted — 2026-07-29

## Context

Codex can layer a named profile above its normal configuration. A profile with
`developer_instructions` lets an operator activate a reviewed team instruction
without replacing the product's base system instructions. The canonical
instruction is already maintained as Markdown so it can be reviewed, diffed,
and shared with other agent surfaces.

Hand-copying that Markdown into a separate TOML profile creates two editable
sources. The copies can drift silently, and an apparently convenient TOML file
can also acquire unrelated settings such as approval, sandbox, model, MCP, or
tool configuration. Editing the target runtime home directly would additionally
bypass the Bridge's Instruction ownership and unmanaged-content protections.

## Decision drivers

- Keep canonical prose in one reviewed Markdown source.
- Preserve the exact prompt after newline normalization and TOML parsing.
- Keep the generated profile incapable of changing unrelated Codex settings.
- Detect generated-artifact drift during normal validation and planning.
- Keep `plan` and drift checking strictly read-only.
- Reuse existing Instruction ownership, conflict, backup, and removal behavior.
- Never adopt or replace an unmanaged runtime destination.

## Considered options

### Maintain Markdown and profile TOML by hand

This requires no generator, but makes drift a review convention rather than an
enforced invariant. It also leaves the profile schema open to unrelated keys.

### Patch the active Codex base `config.toml`

This avoids a named profile but mixes portable instruction content with
machine-local and product-managed settings. Safe leaf ownership would require
the Settings lifecycle and would not solve activation for a distinct profile.

### Generate a closed profile and deploy it as an Instruction

This adds a small product-specific projection boundary while preserving the
existing per-file deployment lifecycle. The Catalog can enforce exact source,
schema, and byte identity before any runtime plan is built.

## Decision

An Instruction bundle may contain a top-level `projections.toml` with exactly
this versioned schema:

```toml
schema_version = 1

[[codex_profiles]]
name = "team-lead"
source = "codex/model-instructions/team-lead.md"
```

The top-level document has only `schema_version` and `codex_profiles`. It must
declare at least one profile. Each entry has only `name` and `source`.
`name` is a portable lowercase kebab-case identifier, unique under
case-insensitive comparison. `source` is a contained, real, non-symlink,
regular direct `codex/model-instructions/*.md` file.

Each entry derives `codex/<name>.config.toml`. The generated TOML has comments
and exactly one data key, `developer_instructions`. Its parsed value equals the
LF-normalized Markdown source exactly, including a trailing newline. Rendering
is deterministic UTF-8 with LF endings and includes a source SHA-256 comment.
The encoding must safely round-trip Unicode, quotes, backslashes, control
characters, and sequences that resemble multiline TOML delimiters.

`agentbridge instructions generate` validates every descriptor and source
before writing, refuses symlink or non-regular output destinations, and
atomically creates or updates each declared Catalog output.
`agentbridge instructions check` is strictly read-only and byte-compares each
output with an in-memory render. It reports missing or stale output as drift.

Catalog discovery fails closed on missing, stale, malformed, symlinked, or
undeclared profile output; path escape, duplicate or case-folded name collision,
the base `codex/config.toml`, extra TOML data keys, and blank or non-string
instructions are errors. Consequently `validate`, `plan`, apply-time
rediscovery, and `apply` cannot proceed from a drifted projection. Apply never
runs the generator.

`projections.toml` is metadata and is never enumerated for deployment. The
generated profile is an ordinary Codex `InstructionFile` with runtime
destination `<config_home>/<name>.config.toml`. It uses the existing
target-scoped Instruction state, symlink/copy choice, content digest, backup,
update, deselection, and conflict behavior. An existing runtime destination is
unmanaged and conflicts even when its bytes already equal the desired profile.
The base `<config_home>/config.toml` remains outside this Instruction path.

## Consequences

### Positive

- Markdown remains the single hand-authored prompt source.
- Generated profile drift is deterministic and CI-checkable.
- A profile cannot silently grow model, permission, sandbox, MCP, or tool keys.
- Runtime deployment inherits the established fail-closed ownership lifecycle.
- Existing Codex base instructions and base configuration are not replaced.

### Negative

- The generated TOML is committed alongside its source and descriptor.
- Catalog authors must run generation before validation after prompt changes.
- Selecting the named profile at Codex launch remains an operator or runtime
  integration responsibility; the Bridge only generates and deploys the file.
- There is no Claude Code equivalent because the products do not share a
  profile schema.

### Risks and mitigations

- **Malicious prompt content:** the generator preserves source meaning rather
  than sanitizing it. Treat the Catalog as executable supply-chain input and
  review the Markdown.
- **Authority confusion:** developer instructions express intent but grant no
  authority. Codex approval, sandbox, managed policy, and tool controls remain
  product-owned and are not bypassed.
- **Descriptor or path substitution:** closed schemas, fixed direct source
  paths, containment checks, regular-file checks, and symlink refusal fail
  closed.
- **Generated output edited by hand:** byte comparison makes comments and data
  drift visible; normal discovery refuses to plan or apply until regenerated.
- **Unmanaged runtime collision:** existing Instruction conflict rules prohibit
  implicit adoption or replacement.

## Related decisions

- [ADR-0001: Render target-specific artifacts](0001-render-target-specific-artifacts.md)
- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0004: Manage declarative settings by owned leaf](0004-manage-declarative-settings-by-owned-leaf.md)

## References

- [OpenAI: Codex configuration profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles)
