# Compatibility

This document describes the implemented local projection targets and known
product boundaries as of 2026-07-14. Vendor behavior changes quickly. An alpha
bridge release validates its own catalog and generated-state invariants, but it
does not probe installed product versions or certify every vendor schema.

“Targeted” means the bridge can model the local filesystem home and generate
product CLI commands for that combination. It does not mean every component has
identical semantics or that the vendor product is generally available on that
operating system.

The 0.1.0 integration baseline exercised a complete isolated lifecycle on
native Linux with Codex CLI 0.144.3 and Claude Code 2.1.206: render, vendor
validation where available, marketplace registration, install, refresh,
idempotent retry, deselection, and removal. The bridge test suite also runs on
native Windows and Linux CI for Python 3.11 and 3.12. Native Windows product
registration is modeled and unit-tested, but is not yet an automated vendor
CLI integration job.

## Surface matrix

| Product surface     | Windows                                                | Native Linux                              | WSL                                                       | Plugin/Hook boundary                                                |
| ------------------- | ------------------------------------------------------ | ----------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------- |
| Codex CLI           | Targeted                                               | Targeted                                  | Targeted as Linux                                         | Product-native package and marketplace                              |
| Codex Desktop       | Native PowerShell or WSL2 agent mode                   | No native Linux Desktop target documented | WSL2 agent mode uses a Linux target                       | Uses the active Codex environment                                   |
| Claude Code CLI     | Targeted                                               | Targeted                                  | Targeted as a separate Linux home                         | Custom `CLAUDE_CONFIG_DIR` is modeled through `config_home`         |
| Claude Code Desktop | Local sessions targeted; SSH is a separate remote home | Linux beta Local sessions targeted        | Desktop WSL sessions exist, but Plugins are not available | Plugins are available in Local/SSH, not Remote (cloud)/WSL sessions |

Codex Desktop for Windows can use native PowerShell or a WSL2 agent environment.
When WSL2 is selected, configure a Linux target: Linux paths, link behavior, and
command quoting apply. The bridge does not point native Windows and WSL at one
whole Codex home.

Claude Code Desktop's Code surface shares local configuration with Claude Code
CLI for supported Local sessions. The Desktop app is available on Windows,
macOS, and Linux beta. An SSH session uses the Claude home on its remote Linux or
macOS host. A WSL session is a distinct Linux environment exposed through the
Windows app, while Remote is Anthropic's cloud-session environment. Anthropic
documents Plugin support for Local and SSH sessions, but not Remote (cloud) or
WSL sessions. The bridge target schema does not encode those Desktop session
types; selecting the Desktop surface produces a compatibility warning, not a
capability probe or a way to enable Plugins in an unsupported session.

## Standalone Skills

Skills have the strongest common representation: a directory whose exact-case
entry point is `SKILL.md`, plus optional referenced files.

| Concern                   | Codex                              | Claude Code                                     |
| ------------------------- | ---------------------------------- | ----------------------------------------------- |
| Bridge-managed user root  | `<user_home>/.agents/skills`       | `<config_home>/skills`                          |
| Default `config_home`     | `<user_home>/.codex`               | `<user_home>/.claude`                           |
| Custom home behavior      | Registration receives `CODEX_HOME` | Skills and registration use `CLAUDE_CONFIG_DIR` |
| Linux `auto` mode         | Directory symlink                  | Directory symlink                               |
| Windows `auto` mode       | Managed directory copy             | Managed directory copy                          |
| Product-specific metadata | May be consumed by Codex           | May be consumed by Claude Code                  |

The bridge currently projects user-level standalone Skills only. It does not
create repository-scoped `.agents/skills` or `.claude/skills` trees.

The portable profile requires YAML frontmatter with a matching `name` and a
non-empty `description`. Other product-specific frontmatter remains untouched;
the bridge does not render separate Skill frontmatter variants. Catalog authors
must use the common subset or package product-specific Skills inside separate
Plugin overlays.

Linux symlink mode is live. A source edit is immediately visible through an
existing link and does not require `apply`. Copy mode requires a new plan/apply.
Catalog discovery accepts only contained symlinks to regular files. It rejects
directory symlinks, broken links, and escaping links. Managed copy mode currently
refuses a standalone Skill containing even an accepted file symlink.

Existing links and managed-copy markers are not adopted without matching
target-scoped ownership state. During a target handoff, the old target keeps its
physical Skill root reserved until an empty reconciliation completes; this also
applies to physical aliases and case variants on Windows.

## Plugins and marketplaces

The package concepts are similar, but the contracts are not interchangeable:

| Concern             | Codex                              | Claude Code                       |
| ------------------- | ---------------------------------- | --------------------------------- |
| Plugin manifest     | `.codex-plugin/plugin.json`        | `.claude-plugin/plugin.json`      |
| Marketplace         | `.agents/plugins/marketplace.json` | `.claude-plugin/marketplace.json` |
| Rendered package    | `plugins/codex/<name>`             | `plugins/claude-code/<name>`      |
| Registration home   | `CODEX_HOME=<config_home>`         | `CLAUDE_CONFIG_DIR=<config_home>` |
| Product cache/trust | Owned by Codex                     | Owned by Claude Code              |

OpenAI currently recognizes a legacy-compatible repo marketplace at
`.claude-plugin/marketplace.json`. The bridge does not rely on that as a shared
package contract. It writes separate product marketplace entries and packages,
each containing only its native manifest. See
[ADR-0003](adr/0003-use-dual-marketplace-packages.md).

The bridge performs structural validation—required manifests, matching artifact
names, strict matching SemVer, non-conflicting overlay files, source-symlink
containment, nested Windows path portability, and generated-tree integrity. It
does not inspect textual path references inside manifests or `.mcp.json`,
automatically run Codex or Claude validators against arbitrary source artifacts,
or validate the final product cache. Product CLI installation, refresh, cache,
trust, and policy results remain product-owned.

Do not link a product Plugin cache back to the catalog. Rendered packages and the
stable marketplace are generated inputs; product caches are installation outputs
and never a source of truth.

## Hooks

Hook handler programs may be portable; Hook declarations are portable only when
the author has verified that both products interpret them equivalently. Codex
and Claude Code differ in event sets, matcher behavior, input JSON,
decision/exit behavior, trust, and Windows command handling.

The implemented merge policy is intentionally small:

- `common/hooks.json` and `<product>/hooks.json` use a top-level `hooks` object;
- matcher-group arrays for the same event are appended in source order;
- scripts from `common/scripts` and the selected product's `scripts` are copied
  into that Hook bundle's generated script directory;
- the bridge does not map event names, adapt payloads, rewrite commands, or
  prove equivalent blocking behavior;
- plan output surfaces event, matcher, handler type, and command/URL/prompt for
  human review.

Use `common/` only for declarations that are already correct on both products.
Use `codex/` and `claude-code/` for any semantic or command difference. Keep
PowerShell and POSIX handler commands in product-specific content when one
command string cannot run correctly in both environments.

All Hook bundles selected for a product are packaged into the synthetic
`agent-config-bridge-hooks` Plugin. A non-empty Hook catalog requires
`catalog/hooks/.version`; bump that strict SemVer whenever the generated Hook
package changes.

## Filesystem modes

| Mode      | Current behavior                                        | Limitations                                                 |
| --------- | ------------------------------------------------------- | ----------------------------------------------------------- |
| `copy`    | Managed standalone Skill copy with ownership marker     | Re-apply required; source Skill symlinks are rejected       |
| `symlink` | Live standalone Skill directory symlink                 | Windows privilege/policy and persistent source availability |
| `auto`    | `copy` for Windows targets; `symlink` for Linux targets | A simple platform rule, not product capability detection    |

The selected operation appears in `plan`. Windows command previews use
PowerShell syntax; Linux previews use POSIX syntax. Registration itself must run
on the configured target platform. When host and target differ, registration
commands are omitted from the plan. WSL filesystem visibility does not make a
Windows executable, path, permission model, or command string valid on Linux.

Share the catalog, not `state_dir`. Native Windows, WSL, and Linux need separate
stable operational state directories because ownership identities and local
marketplace registrations contain host-native physical paths.

Raw filesystem mode bits are deliberately excluded from Skill digests,
marketplace digests, rendered integrity, and common/product overlay identity.
This avoids host-umask and checkout noise, but it also means the bridge does not
preserve or validate executable permission portability. Invoke scripts through
an explicit interpreter and use product metadata for execution intent.

## Ownership and reconciliation

`apply` records only standalone Skills. On a later plan it can:

- remove a recorded symlink only if it still targets the recorded source;
- update or remove a managed copy only if its marker matches and it has not
  drifted;
- retain the displaced managed copy in the backup tree;
- report replacement, marker mismatch, content drift, or unmanaged content as a
  conflict.

`register` separately records only the Plugin names it successfully reconciles
for a target. Later deselection removes only those recorded names. Claude
registration includes marketplace update and Plugin update commands so a bumped
local release is refreshed. Unrelated Plugins remain untouched.

If a user copies a preview command and runs it manually, the product may change
but bridge ownership state does not. Use `agentbridge register` when automatic
future reconciliation is desired.

Target names identify ownership records. To rename/delete a target or change its
product/home identity safely, first keep the old target identity, set
`components = []`, run `apply` and `register`, and confirm the empty
reconciliation. Only then change or remove it. Skipping this sequence leaves an
orphan `state_dir/targets/<old-name>` record; diagnostics fail and `apply` plus
`register` stop because the bridge cannot infer which new target, if any, owns
the old state. Restore the old identity and reconcile it to empty.

## Known alpha limitations

- No all-actions atomic transaction, target lock, automatic rollback, or
  recovery log.
- A sequential apply/register can stop after earlier actions succeeded; inspect
  a fresh plan before retrying.
- Symlink mode is live and bypasses copy-mode update checkpoints.
- No product capability/version probing, automatic vendor validation of
  arbitrary artifacts, or full post-install/cache validation.
- Hook event parity is not inferred or tested by the bridge.
- Product CLIs own trust approvals, permission policy, caches, and authentication.
- Manual product commands bypass bridge ownership recording.
- Plugin content changes require the same new strict SemVer in both product
  manifests; Hook content changes require a new `hooks/.version`. For packages
  present in both the current published snapshot and its replacement, precedence
  must increase; the bridge does not maintain a permanent release ledger.
- Claude Code Desktop Plugins are available in Local and SSH sessions, including
  Linux beta Local sessions, but unavailable in Remote (cloud) and WSL sessions.
- Product cloud state, organization-managed Plugins, and managed policy layers
  are not writable local bridge targets.

## Official references

- [OpenAI: Build skills](https://learn.chatgpt.com/docs/build-skills)
- [OpenAI: Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [OpenAI: Hooks](https://learn.chatgpt.com/docs/hooks)
- [OpenAI: ChatGPT desktop app for Windows](https://learn.chatgpt.com/docs/windows/windows-app)
- [OpenAI: WSL](https://learn.chatgpt.com/docs/windows/wsl)
- [Anthropic: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Anthropic: Create plugins](https://code.claude.com/docs/en/plugins)
- [Anthropic: Plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)
- [Anthropic: Hooks reference](https://code.claude.com/docs/en/hooks)
- [Anthropic: Claude Code on desktop](https://code.claude.com/docs/en/desktop)
