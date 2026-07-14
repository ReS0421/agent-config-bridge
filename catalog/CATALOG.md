# Canonical example catalog

This directory contains source artifacts. It is the only content contributors
should edit:

- `skills/hello/` is an independently shareable skill.
- `plugins/hello-shared/common/` contains product-neutral plugin content.
- `plugins/hello-shared/codex/` and `claude-code/` contain source manifest
  overlays for their respective products.
- `hooks/audit-event/common/` contains a hook limited to events and behavior
  supported by both products.
- `hooks/.version` is the strict SemVer of the generated
  `agent-config-bridge-hooks` Plugin.

Agent Config Bridge merges source overlays into immutable, content-addressed
output below `<state_dir>/builds/<digest>/`. It publishes an integrity-checked
copy at the stable `<state_dir>/marketplace` path used for product registration.
Both locations are generated and must not be edited or committed. A product
cache populated from the marketplace is also generated, not a second source of
truth.

Plugin directories and both manifest `name` values must match. Both manifests
must use the same strict SemVer. Bump both versions whenever any rendered Plugin
content changes. Bump `hooks/.version` whenever any Hook declaration or bundled
Hook script changes. For package names overlapping the current published
snapshot, changed content requires strictly higher SemVer precedence. This check
uses only that snapshot; it is not a permanent release ledger.

Artifact names use lowercase kebab-case and must be portable to Windows. Catalog
validation checks every nested path component and rejects Windows device names,
invalid characters, trailing dots/spaces, case-insensitive sibling collisions,
broken/escaping links, and all directory symlinks. Only contained links to
regular files are valid catalog input. Standalone Skills containing even an
accepted file link cannot currently be installed in managed copy mode.

Raw filesystem permission bits are not part of catalog, overlay, copy, or
marketplace identity and are not a portable executable contract. Use explicit
interpreters in Hook/MCP commands and product metadata instead of relying on an
executable bit.

Author Plugin payloads so runtime files remain available inside the installed
Plugin tree. Marketplace installers cache a Plugin as a unit, so a
Plugin-bundled Skill is separate from a standalone catalog Skill even when both
are simple examples. The bridge enforces filesystem containment for source
symlinks; it does not inspect textual path references in manifests or
`.mcp.json`, so authors must review those references themselves.

Generated bridge state is designed to be non-secret, but it reproduces catalog
content and retained Skill copies. Never place credentials, authentication
state, session data, logs, caches, trust decisions, or embedded secret values in
this catalog.
