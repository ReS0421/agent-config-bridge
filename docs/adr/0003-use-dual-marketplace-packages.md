# ADR-0003: Use dual marketplace packages

## Status

Accepted — 2026-07-14

## Context

Codex and Claude both distribute reusable agent customizations as plugins, and a
plugin can contain similar payloads such as skills, hooks, and MCP configuration.
Their package entry points are different: Codex requires
`.codex-plugin/plugin.json`, while Claude uses `.claude-plugin/plugin.json`. Their
marketplace locations, catalog schemas, install flows, namespaces, metadata, and
cache behavior also differ.

Codex currently recognizes a legacy-compatible marketplace file at
`.claude-plugin/marketplace.json`, and it exports Claude-compatible plugin root
variables for hooks. These conveniences reduce migration cost but do not make the
two package contracts identical or guarantee future bidirectional compatibility.

## Decision drivers

- Native package structure for both vendors.
- One logical plugin release and shared payload where semantics match.
- No reliance on a legacy compatibility path as the primary contract.
- Independent evolution of discovery and install metadata.
- Deterministic local marketplace input for desktop and CLI surfaces.

## Considered options

### Publish only a Claude package and rely on Codex compatibility

Minimizes files today but leaves Codex-specific metadata and validation incomplete
and treats a compatibility affordance as a stable package standard.

### Publish only a Codex package and translate during Claude installation

Claude does not define the Codex manifest as an input. Installation-time mutation
also makes the installed artifact harder to reproduce and audit.

### Render two packages/catalogs from one canonical plugin

Duplicates a small amount of packaging metadata but gives each product a native,
testable contract.

## Decision

Each canonical plugin release produces two target packages or two target-specific
views of the same immutable payload:

- a Codex package with `.codex-plugin/plugin.json` and a Codex
  `.agents/plugins/marketplace.json` entry;
- a Claude package with `.claude-plugin/plugin.json` and a Claude
  `.claude-plugin/marketplace.json` entry.

Both artifacts carry the same canonical Plugin ID and release version. The
renderer filters packages according to per-product component selection and adds
the appropriate marketplace metadata. Shared files must remain within the
rendered Plugin root so product installation/cache behavior can resolve them.

Immutable content-addressed builds are copied to one stable published
marketplace path. Product registration uses the stable path, while build digests
preserve deterministic generated snapshots. Any package content change requires
a matching strict-SemVer precedence increase in both product manifests when that
package also exists in the current published snapshot. The generated Hook Plugin
follows the version in `catalog/hooks/.version`. This comparison is not a
permanent release ledger; deleting the published state or removing a package
also removes its immediate comparison baseline.

Registration and installation state are target-local. The bridge renders or
registers catalogs but does not share Claude's `~/.claude/plugins/cache`, Codex
installation state, connector tokens, or trust decisions.

## Consequences

### Positive

- Each package can be inspected or passed separately to a vendor validator; the
  bridge itself does not automatically validate arbitrary artifacts with vendor
  CLIs.
- Product-specific capabilities can evolve without contaminating the other
  manifest.
- One build digest and matching release version correlate the two packages.
- Deprecation of a legacy marketplace path does not break the architecture.

### Negative

- Two manifests and catalogs must be generated, tested, and documented.
- A product-only component can make package contents intentionally unequal.
- Users may see different install namespaces or availability on each surface.
- Marketplace refresh and plugin cache behavior must be tested separately.

### Risks and mitigations

- **Version skew:** validate matching strict SemVer before rendering and require
  higher precedence for changed packages overlapping the current published
  snapshot.
- **Payload drift:** generate both packages in one integrity-checked build and
  reject conflicting overlays.
- **Path escape after caching:** allow only regular-file symlinks contained in
  their artifact root, reject directory links, validate nested Windows names,
  and require authors to review textual references because the bridge does not
  validate manifest/`.mcp.json` path strings.

## Related decisions

- [ADR-0001: Render target-specific artifacts](0001-render-target-specific-artifacts.md)
- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)

## References

- [OpenAI: Build plugins and marketplace metadata](https://learn.chatgpt.com/docs/build-plugins)
- [Anthropic: Create plugins](https://code.claude.com/docs/en/plugins)
- [Anthropic: Create and distribute a plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
