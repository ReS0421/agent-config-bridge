# ADR-0001: Render target-specific artifacts

## Status

Accepted — 2026-07-14

## Context

Codex and Claude share useful concepts but not a single configuration contract.
Both can consume `SKILL.md`, yet their discovery paths, optional metadata,
collision behavior, plugin manifests, marketplace catalogs, hook events, command
fields, input payloads, and trust flows differ. Windows and Linux add another
axis of path and executable differences.

Directly linking one vendor's configuration tree into another makes those
differences implicit. It also encourages sharing product-owned files that are not
part of the desired catalog.

## Decision drivers

- One authoring source for portable intent.
- Native artifact structure and product-owned behavior on every target.
- Explicit handling of unsupported or lossy features.
- Previewable, ownership-aware filesystem changes.
- No dependency on undocumented cross-vendor compatibility.

## Considered options

### Link complete product homes

Simple initially, but it mixes credentials and runtime databases, creates
concurrent-writer risk, and still does not reconcile schemas or OS commands.

### Use only the lowest common denominator

Portable, but discards useful product capabilities and still leaves discovery and
packaging paths unresolved.

### Keep a canonical model and render per target

Adds renderer and test complexity, but makes packaging differences explicit and
allows each product to receive a native artifact.

## Decision

Maintain portable source and explicit product overlays in one canonical catalog.
Build a normalized catalog model, then render separate Plugin and Hook artifacts
for Codex and Claude Code. Platform affects standalone Skill link/copy policy and
command-preview syntax. Surface selection informs compatibility diagnostics; it
does not currently create a separate rendered package.

`SKILL.md` content and supporting files pass through unchanged when they use the
portable profile. Plugin manifests, marketplace entries, and Hook declarations
are product-specific output. `common/` and one explicit product overlay are
merged; the renderer does not infer Hook mappings or adapt command semantics.
Catalog authors keep uncertain or product-specific behavior out of `common/`.

Portable identity is based on paths, file/link type, link target, and file bytes,
not raw filesystem permission bits. Catalog validation permits only contained
regular-file symlinks and validates every nested path for Windows portability.
Directory symlinks are rejected. Authors use explicit interpreters and product
metadata rather than assuming executable mode survives every target filesystem.

Rendered artifacts are disposable. Immutable builds back one integrity-checked,
stable published marketplace path. Per-target ownership records track standalone
Skills applied by the bridge and Plugins/Hooks registered through the bridge.

## Consequences

### Positive

- Vendor updates are isolated to a renderer and its compatibility tests.
- Plans show Skill destinations, marketplace publication, product commands, and
  Hook/MCP review items for each environment.
- Product-native trust, cache, and permission behavior remains product-owned.
- Windows, WSL, and Linux can share intent without sharing invalid paths.

### Negative

- Metadata can be duplicated in overlays.
- Each supported product format needs fixtures and compatibility tests.
- Some hooks will be unavailable on one target.
- A catalog change is not live on copy-based targets until it is reapplied.
- Raw executable permission is not a cross-platform guarantee.

### Risks and mitigations

- **Semantic drift:** explicit overlays, rendered fixtures, plan review items,
  and product-side testing.
- **Unknown vendor capability:** Doctor reports the selected launcher's
  informational `--version` output but does not infer capabilities from it;
  require users to review product compatibility.
- **Renderer compromise:** content hashes, fresh-plan comparison, generated-tree
  integrity checks, and code review.

## Related decisions

- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0003: Use dual marketplace packages](0003-use-dual-marketplace-packages.md)

## References

- [OpenAI skill locations and symlink support](https://learn.chatgpt.com/docs/build-skills)
- [OpenAI plugin structure](https://learn.chatgpt.com/docs/build-plugins)
- [Anthropic skill locations](https://code.claude.com/docs/en/skills)
- [Anthropic plugin structure](https://code.claude.com/docs/en/plugins)
