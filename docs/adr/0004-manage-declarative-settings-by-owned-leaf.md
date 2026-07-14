# ADR-0004: Manage declarative settings by owned leaf

## Status

Accepted — 2026-07-14

## Context

The first bridge release deliberately supported only Skills, Plugins, and Hooks.
That boundary prevented accidental sharing of credentials and mutable product
state, but it also excluded useful public configuration such as Codex
`config.toml` values and Claude Code `settings.json` values.

The product files cannot be linked or replaced safely. They can contain
machine-local paths, user choices, plugin enablement, and settings written by
the product itself. Codex uses TOML while Claude Code uses JSON, and there is no
honest cross-product settings schema.

## Decision drivers

- Keep one reviewable source for selected durable settings.
- Preserve unrelated local values and Codex TOML comments.
- Never adopt or overwrite a pre-existing setting implicitly.
- Remove only values that the bridge can still prove it owns.
- Keep credentials and opaque runtime state outside bridge ownership.

## Considered options

### Link or replace the complete settings file

Simple, but it destroys local composition, shares machine-specific values, and
can race product writers.

### Deep-merge fragments without ownership records

Preserves unrelated keys initially, but later updates and deselection cannot
distinguish bridge values from user values safely.

### Merge product-specific fragments with leaf ownership

Requires format-aware patching and state, but makes conflicts, drift, and
cleanup deterministic.

## Decision

Add `settings` as an independently selectable component. Canonical bundles use
product-specific fragments:

```text
catalog/settings/<bundle>/codex/config.toml
catalog/settings/<bundle>/claude-code/settings.json
```

There is no `common` settings fragment. Each fragment is flattened to owned
leaf paths; mappings are containers and arrays are atomic leaf values. Two
bundles may not claim the same leaf or an ancestor/descendant pair.

The bridge patches only the user-level public settings file for the selected
product. Existing unowned leaves are conflicts even when their values happen to
match. Updates and removals proceed only while the current value digest matches
the target-scoped ownership record. Deselecting `settings` removes unchanged
owned leaves and prunes only empty containers created by the bridge.

Writes use a same-directory temporary file and atomic replacement. Codex TOML
is edited with a comment-preserving parser. Claude JSON is structurally merged;
unrelated values are preserved even if formatting is normalized. Ownership
state stores paths and value digests, not original values.

On POSIX, a newly created Settings file uses mode `0600`; updates preserve an
existing file's mode. Windows ACL inheritance remains authoritative because
POSIX mode bits cannot establish a Windows DACL.

The bridge never patches `~/.claude.json`, credential files, authentication,
session/history stores, trust decisions, caches, managed policy, registry
configuration, or an entire product home. Project-local settings require a
future project-target model and are not inferred from a user target.

## Consequences

### Positive

- Selected settings can follow Windows and Linux without sharing runtime state.
- Plans expose unmanaged-key and user-drift conflicts before mutation.
- Deselecting the component leaves unrelated local configuration intact.
- Product-specific schemas can evolve independently.

### Negative

- Settings cannot use one cross-product `common` document.
- Arrays are replaced atomically rather than merged element by element.
- Claude JSON formatting can change after a successful patch.
- A new runtime dependency is needed to preserve Codex TOML formatting.

### Risks and mitigations

- **Secret committed as a setting:** catalog review remains mandatory; prefer
  environment-variable references and never store secret values in ownership
  state.
- **Concurrent product write:** compare a fresh plan immediately before an
  atomic replacement and fail when owned values drift.
- **Crash after replacement:** the file may contain the new value before its
  ownership record is written; fail closed on the next run and require explicit
  inspection instead of adopting or rolling back the leaf automatically.
- **Vendor schema drift:** validate document syntax but leave semantic
  acceptance to the product; do not invent translations.

## Related decisions

- [ADR-0001: Render target-specific artifacts](0001-render-target-specific-artifacts.md)
- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0005: Use host scheduler adapters](0005-use-host-scheduler-adapters.md)

## References

- [OpenAI: Codex configuration](https://learn.chatgpt.com/docs/config-file/basic-config)
- [Anthropic: Claude Code configuration](https://code.claude.com/docs/en/configuration)
