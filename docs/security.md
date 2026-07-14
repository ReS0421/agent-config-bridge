# Security model

Agent customizations are executable supply-chain inputs. A Skill can direct an
agent to run commands, a Plugin can start an MCP server, and a Hook can execute
at lifecycle boundaries. The bridge therefore treats its canonical catalog and
every rendered change as code, not as harmless preferences.

## Trust boundaries and assets

The main trust boundaries are:

1. the catalog source and its update channel;
2. the bridge process and renderer;
3. each Windows, WSL, or Linux target home;
4. Codex and Claude Code's trust, sandbox, cache, and permission systems;
5. external executables, MCP servers, connectors, and network services invoked
   by a Plugin or Hook.

Assets to protect include credentials, source code, user files, command approval
policy, conversation history, and target configuration integrity. The bridge is
not a sandbox against a malicious catalog the user chooses to install. Its role
is to constrain paths it manages, expose executable declarations for review, and
avoid importing unrelated product state.

## Allowlist, never home-directory sync

Only three component classes are eligible for projection:

- standalone `skills`;
- `plugins` and their declarative package payload;
- `hooks` and the handler files explicitly placed in Hook bundles.

Everything else is excluded. Never copy or link:

- OAuth tokens, API keys, credential stores, cookies, or connector secrets;
- session transcripts, conversation history, memories, checkpoints, or task
  databases;
- trust hashes, Hook approvals, workspace-trust decisions, or permission grants;
- telemetry, logs, caches, Plugin install caches, temporary product files, or
  update state;
- the entire `~/.codex`, `%USERPROFILE%\.codex`, or `~/.claude` directory.

This remains true even if vendor tools can be pointed at one home. Dedicated
credential-management or whole-home synchronization is outside this project's
scope. See [ADR-0002](adr/0002-never-share-runtime-state.md).

## Generated state is non-secret by design

The configured `state_dir` contains only:

- immutable rendered marketplace builds;
- the stable published marketplace snapshot;
- small per-target Skill and Plugin ownership records;
- retained managed Skill copies displaced during update or deselection.

Use a separate stable `state_dir` per native Windows, WSL, or Linux host. It is
operational ownership state with physical paths, not part of the portable
canonical catalog. Configuration loading rejects physical equal/nested overlap
between generated state, the canonical catalog, enabled product homes, and
Skill discovery roots, including symlink aliases and Windows case variants.

The bridge never writes product auth, session, conversation, trust, or cache
state there. Ownership records contain artifact identities, link/copy modes, and
source information, not credentials.

This design does not sanitize catalog content. Generated packages and backups
reproduce the canonical files, so a secret committed to a Skill, Plugin, Hook,
script, manifest, or `.mcp.json` will also appear in generated state. Keep the
catalog and `state_dir` in user-controlled locations and reference secrets
through product-supported environment/configuration mechanisms rather than
embedding values.

## Catalog validation and trust

The bridge accepts a local catalog path. Establish its provenance before use;
for a Git catalog, review and pin a trusted commit or release according to your
own workflow. The bridge does not fetch a remote catalog, verify signatures, or
attest publisher identity.

Implemented validation includes:

- lowercase kebab-case artifact names;
- rejection of Windows device names, invalid nested path characters, trailing
  dots/spaces, and case-insensitive sibling collisions;
- exact `SKILL.md` entry points and basic portable frontmatter requirements;
- required product Plugin manifests, matching names, and matching strict SemVer;
- Hook matcher/handler structure and a strict-SemVer `hooks/.version`;
- rejection of broken/escaping symlinks and all directory symlinks; only
  contained links to regular files are accepted;
- rejection of conflicting product overlay output;
- integrity hashes for immutable and published rendered marketplaces.

The bridge does not scan unexpected executables, run arbitrary vendor validators,
probe product versions, verify Hook semantics, or decide that a catalog is safe.
Validation is structural; code review remains necessary.

## Plan before mutation

`validate`, `plan`, and `doctor` are read-only. `plan` reports:

- Skill creates, updates, removals, no-ops, and conflicts;
- marketplace create/update state;
- Hook events, matchers, handler types, and command/URL/prompt values;
- Plugin `.mcp.json` or manifest MCP command/URL values;
- product CLI argv and environment needed for later registration;
- relevant warnings, including the Claude Code Desktop session boundary.

Review items intentionally display literal catalog command and URL fields. They
are not a redacted audit log. Do not embed tokens in those fields or publish plan
output without inspecting it. The bridge does not currently enumerate every
argument, working directory, environment variable, executable resolution, or
network behavior and does not report vendor validation results.

Before `apply` or `register` mutates state, it rediscovers the catalog and
rebuilds the plan. A difference from the reviewed plan aborts the operation.
Unmanaged Skill destinations and drifted bridge-managed copies/links are hard
conflicts. A corrupted generated artifact that does not change plan identity can
still fail a later integrity check rather than being classified as a stale plan.

## Filesystem mutation model

The apply engine acts sequentially and fails closed on detected ownership or
digest mismatches:

- new symlinks never replace existing paths;
- recorded symlinks are unlinked on deselection only if they still target the
  recorded source;
- new managed copies are staged next to the destination and checked against the
  planned source digest;
- managed copies update/remove only when their marker and installed digest still
  match;
- displaced unchanged managed copies are retained below `state_dir/backups`;
- marketplace builds are immutable and content-addressed;
- the published marketplace is rehashed before reuse/replacement, copied through
  a temporary sibling, and checked before publication;
- ownership state paths are target-scoped and reject a parent symlink that would
  escape `state_dir`.

Raw filesystem permission bits are excluded from Skill, source, overlay, and
rendered marketplace identity. This avoids false drift across Git checkouts,
Windows, WSL, and different umasks, but the bridge does not preserve or validate
executable-mode portability. Hook and MCP commands should invoke a named
interpreter and declare execution intent through product metadata where possible.

Current alpha limitations matter for threat modeling:

- there is no target lock or one transaction covering all actions;
- there is no automatic rollback or recovery log;
- a later action can fail after an earlier action succeeded;
- symlink mode is live, so canonical Skill changes become visible immediately;
- retained backups have no automatic retention/restore command;
- the implementation does not provide comprehensive no-follow/reparse-point
  protection against every concurrent filesystem race on POSIX, Windows, or WSL.

After an interrupted operation, inspect a fresh `plan`. Restore a retained
managed-copy backup manually only after reviewing both the destination and
ownership state.

## Package versions and caches

Product caches may otherwise retain old Plugin content under an unchanged
release identity. The bridge requires matching strict SemVer in a canonical
Plugin's Codex and Claude Code manifests. When a package name exists in both the
current published snapshot and its replacement, changed content requires a new
version with strictly higher SemVer precedence. Generated Hook content is
versioned by `catalog/hooks/.version` and follows the same rule.

This check is not a permanent version ledger: deleting bridge state or removing
and later re-adding a package removes the comparison baseline. It is also not a
signature, supply-chain attestation, or guarantee that every external product
cache refreshed. `register` asks Claude Code to update its marketplace and each
selected Plugin; product CLIs still own the result.

## Registration and product-owned trust

`register` requires confirmation, runs only on the configured target platform,
rechecks the plan, and passes `CODEX_HOME` or `CLAUDE_CONFIG_DIR` explicitly. It
records desired Plugin names only after all planned commands for that target
succeed. Later deselection removes only names in that bridge record; unrelated
product installations are out of scope.

Running preview commands manually bypasses ownership recording. It may create an
installation the bridge cannot later reconcile, so use `agentbridge register`
when managed lifecycle behavior is desired.

Changing/deleting a target identity has the same ownership risk. Before changing
its `name`, product, or home, keep the old target, set `components = []`, and run
both `apply` and `register`. If the target disappears first, its
`state_dir/targets/<name>` record is orphaned; diagnostics fail and `apply` plus
`register` stop until the old identity is restored and reconciled. The bridge
does not guess a new owner or delete it automatically.

Before any product registration commands, `register` queries the product's
current marketplace registry. The `agent-config-bridge` entry must be absent,
point to the source recorded by the bridge, or already point to the desired
source after a partial retry. On an initial registration with no ownership
record, only an absent entry or the desired source is accepted. A duplicate
entry, an unknown vendor JSON shape, or a third-party source fails closed before
marketplace add/update, Plugin installation, or removal starts.

Installation does not confer trust. Codex and Claude Code remain responsible for
Hook review, workspace trust, organization policy, permission prompts, Plugin
caches, connector authentication, and disabled-feature settings. The bridge does
not copy or bypass those controls and does not claim a package is active merely
because registration commands completed.

## Hook and MCP hardening

Catalog authors should:

- validate all JSON input from the product;
- reject `..`, unexpected absolute paths, and paths outside allowed roots;
- use product root variables such as `PLUGIN_ROOT` or `CLAUDE_PLUGIN_ROOT` rather
  than catalog-time absolute paths inside cached packages;
- avoid inherited secrets unless the Hook or MCP server explicitly requires
  named variables;
- set finite timeouts and predictable exit behavior;
- avoid network access by default;
- keep Windows PowerShell and POSIX commands in product-specific overlays when
  they differ;
- quote command strings for the shell that actually receives them;
- treat prompt, file, tool, Hook, and MCP output as untrusted input.

The bridge structurally appends common and product-specific Hook matcher groups;
it does not prove that event timing or blocking semantics match. If equivalence
is uncertain, do not put the declaration in `common/hooks.json`.

## Reporting vulnerabilities

Security reports should include the bridge version or commit, target type, a
carefully redacted plan, and the smallest catalog fragment that reproduces the
issue. Never attach auth files, session databases, `state_dir` contents that may
reproduce private catalog files, or unredacted environment dumps to a public
issue.

## Official references

- [OpenAI: Hook review and trust](https://learn.chatgpt.com/docs/hooks)
- [OpenAI: Plugin-bundled Hooks](https://learn.chatgpt.com/docs/build-plugins)
- [Anthropic: Hook security best practices](https://code.claude.com/docs/en/hooks#security-best-practices)
- [Anthropic: Plugin marketplace caching](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
