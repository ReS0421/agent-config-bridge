# ADR-0009: Register MCP servers by owned entry

## Status

Accepted — 2026-11-02

## Context

User-scope MCP servers were configured outside the Bridge: a launcher script
and hand edits wrote Codex `[mcp_servers.<name>]` tables and Claude Code
`mcpServers` entries separately on each host. The two products drifted apart
(one Claude entry pointed at a stale repository argument), package pins were
not reviewed in one place, and nothing reported a changed or missing entry.

The products store these entries differently:

- Codex reads `mcp_servers.<name>` tables from the public
  `<config_home>/config.toml`, the same file that ADR-0004 already patches by
  owned leaf. The tables can also carry user approval state such as
  `mcp_servers.<name>.tools.<tool>.approval_mode`.
- Claude Code keeps user-scope servers in the `mcpServers` object of its user
  config, `<user_home>/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json` for
  a custom profile). That file also holds authentication, project history, and
  other product-written state. ADR-0004 forbids the Bridge from patching it.
  The supported writer is `claude mcp add-json/remove --scope user`.

The live entries already exist, so a first deployment must take them over
without removing and recreating working servers.

## Decision drivers

- Keep one reviewed, product-neutral declaration per server, including its
  package pin and declared access scope.
- Never write `~/.claude.json`, never copy secret values, and keep product
  approval and trust state out of Bridge ownership.
- Reuse the existing owned-leaf engine and registration flow instead of
  inventing a second mutation model.
- Detect drift, missing entries, and unowned same-name entries before mutation.
- Hand over existing entries only when the Bridge can prove that their values
  are exactly the declared ones.

## Considered options

### Use the product CLI for both products

`codex mcp add` and `claude mcp add-json` would give one code path. The Codex
CLI rewrites the table and cannot express ownership of individual leaves, so
deselection could not distinguish Bridge values from user approval state, and
comments in `config.toml` are not preserved.

### Patch `~/.claude.json` directly

A structural JSON merge would mirror the Codex path. It would make the Bridge a
second writer of a product-owned file that contains credentials and session
state, race the running product, and contradict ADR-0004.

### Bundle `.mcp.json` in the existing dual-marketplace plugin

ADR-0003 already renders Codex and Claude Code plugin packages, and both
products can start MCP servers declared in a plugin's `.mcp.json`. Plugin
servers get plugin-scoped tool IDs (for example `mcp__plugin_<plugin>_<server>__…`),
which would break every existing permission rule and tool approval written for
the user-scope IDs. Claude Code Desktop also loads plugins only in Local and
SSH sessions, not in Remote or WSL sessions, so servers would disappear there.

### Owned leaves for Codex, product CLI for Claude Code (chosen)

Codex entries become owned leaves applied by `apply`. Claude entries become
explicit `register` commands whose results are verified by reading the user
config. Each product keeps its native writer, at the cost of two delivery
paths.

## Decision

Add `mcp` as an independently selectable component. Each server has one
manifest, `catalog/mcp/<name>/server.toml` (schema version 1), with an
optional `README.md` and no other files. A manifest declares the transport
(`stdio` or `http`), the command and arguments or the URL, forwarded
environment-variable names, an optional bearer-token environment-variable
name, a required `[package]` pin, and a required `[access]` declaration.
Unknown keys are errors, so the schema has no field for environment values,
headers, tokens, or product tuning keys. `command`, `args`, and `url` are free
text: common token shapes, credential flags with a value, environment
assignments in `command`, and URL fragments or path parameters are rejected,
but that is a heuristic for accidents, and catalog review remains the control.
A bearer token requires an `https` URL or a loopback `http` host.
`{user_home}` is the only placeholder and expands to the target's native home
path. Governance gates each server like other artifacts, with
`capability_kind = "tool"` and `delivery = "mcp-registration"`.

**Ownership record.** All MCP ownership for a target lives in
`state_dir/targets/<target>/mcp.json`: owned Codex leaves with the containers
the Bridge created or adopted, and Claude `name → spec digest` pairs as a
separate field. It is independent of Settings ownership and of Plugin
registration state (`plugins.json`), which never holds MCP data.

**Codex.** Desired values are rendered as leaves below
`mcp_servers.<name>`: `command`, `args` and `env_vars` for stdio (empty lists
are omitted), `url` and `bearer_token_env_var` for http. `apply` merges them
into `<config_home>/config.toml` with the ADR-0004 engine, and a Settings
fragment may not declare `mcp_servers`. A desired server may create its table
but never merges into an existing one it does not own: a same-name table with
any child, such as `env`, `http_headers`, `bearer_token`, `tools` approvals, or
the other transport's key, is an unowned conflict until it is adopted, so
pre-staged credentials or approvals are never attached to the catalog endpoint.
While a server stays declared, its unowned `tools` subtable (user approvals) is
never owned, compared, or removed.

Removal is the one deliberate exception to "unowned children are never
removed". Codex refuses to load a server table without `command` or `url`, so
leaving `{"tools": …}` behind would break the whole file (and launchers that
copy it to other homes). When a plan removes all remaining owned leaves of a
server and their digests still match, the whole `mcp_servers.<name>` table is
removed if its only other children are `tools` or empty `args`/`env_vars`
arrays. Approvals are meaningless without their server, and the plan warns
how many approvals a removal deletes. If any other unowned key remains, the
removal is a conflict that names the leftover keys. An owned table that gains
such keys while still declared gets a warning, not a conflict. tomlkit stores
the comment block above the next table header inside the preceding table, so
the comment lines that end a removed table are put back in place; only
comments between the removed table's own keys go with it.

**Claude Code.** `plan` emits `claude mcp add-json --scope user <name> <json>`
for a new server, `remove` then `add-json` for an update, and `remove` for a
deselected server. The JSON carries only `${NAME}` references for environment
variables and bearer tokens. Claude Code documents `${NAME}` expansion for
`.mcp.json`; expansion in user-scope entries is not verified, and no catalog
manifest uses `env_vars` today. `register` runs the commands with the existing
`CLAUDE_CONFIG_DIR` scoping, re-reads the user config before each command,
checkpoints ownership after each successful command, and verifies after
`add-json` that the product stored the requested entry. The Bridge reads the
user config only for these checks and never writes it.

An unowned same-name entry, an owned entry whose value changed, a missing owned
entry, a blocked Codex table removal, and an unreadable Claude user config are
plan conflicts. Conflicts block `apply`, `register`, and `sync-skills`; pending
Claude registration does not, and `doctor` reports it as a warning.

**Adoption and release.** `agentbridge mcp adopt` records ownership of existing
unowned entries, without changing product configuration, only when they
exactly equal the rendered declaration: every declared Codex leaf must be
equal, an empty `args`/`env_vars` array counts as absent, and the only other
key allowed in the table is `tools`; the normalized Claude JSON must be equal.
Mismatches are listed and never adopted. `agentbridge mcp release` is the
inverse: it forgets ownership of selected servers, again without touching
product configuration, so a missing or drifted owned entry can be re-created,
adopted, or removed by hand. Both run only for targets on the current platform,
preview before writing, re-check before recording, and fail closed on orphaned
state. This is a narrow extension of ADR-0004, which never adopts an existing
setting implicitly: adoption is explicit, per entry, and value-proven.

## Consequences

### Positive

- One reviewed manifest declares each server for both products, including its
  package pin, access scope, and environment-variable names.
- Plans and `doctor` report drift, missing entries, unowned conflicts, blocked
  removals, pending registration, and an unreadable Claude user config by
  server name, without values.
- Deselection removes only unchanged owned entries and never leaves a server
  table Codex cannot load; unrelated servers stay in place.
- Existing servers can be handed over without downtime, and ownership can be
  handed back with `mcp release`.

### Negative

- Two delivery paths: Codex changes land with `apply`, Claude changes with
  `register`.
- Removing a Codex server also removes its tool approvals.
- Claude registration depends on the product CLI's argument handling and on
  its stored representation matching the Bridge normalization; a mismatch is
  reported after the command rather than prevented.
- Product tuning keys such as tool allowlists or startup timeouts cannot be
  declared in schema version 1.

### Security

- The schema offers no place for secret values, and ownership state and
  diagnostics hold only names, key names, paths, and digests. Free-text
  `command`, `args`, and `url` values appear in plan reviews and rendered
  commands; the credential heuristics catch common accidents, not every secret.
- Error messages for manifests and the Claude user config name fields, argument
  indexes, and line and column positions, never values; server names are shown
  only when the catalog or Bridge ownership declares them.
- The Claude user config is read with a size cap, without blocking on special
  files, and as strict JSON.
- The declared access scope is a review aid, not a sandbox; the server process
  runs with the product's privileges.

### Risks and mitigations

- **Concurrent product write:** `apply` re-plans before the atomic replacement,
  and `register` re-reads the user config before each command. Claude Code
  still removes and adds by name, so an edit made between that check and the
  product command can be lost; close sessions that edit MCP servers while
  `register` runs.
- **Partial registration:** ownership is checkpointed after each successful
  command, so a failed run leaves state that the next plan reconciles.
- **Concurrent Bridge commands:** there is no state lock. `register`,
  `mcp adopt`, and `mcp release` re-check before writing but must not run at
  the same time for one target; a lost update surfaces on the next plan as an
  unowned entry, which `mcp adopt` can take over again.
- **Older Bridge versions:** 0.3.7 cannot load a configuration that selects
  `mcp` or a catalog with `mcp/` governance refs, and it never reads or writes
  `mcp.json`, so a downgrade cannot erase MCP ownership.

## Related decisions

- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md) — amended: `mcp`
  joins the allowlisted components; tool approvals stay unowned and are removed only with
  their server table.
- [ADR-0003: Use dual marketplace packages](0003-use-dual-marketplace-packages.md) — not used for MCP
  servers; see the considered options.
- [ADR-0004: Manage declarative settings by owned leaf](0004-manage-declarative-settings-by-owned-leaf.md) —
  amended by explicit adoption and release and by whole-table removal of MCP servers.

## References

- [OpenAI: Codex configuration](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Anthropic: Claude Code MCP](https://code.claude.com/docs/en/mcp)
