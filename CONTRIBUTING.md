# Contributing to Agent Config Bridge

Thank you for helping make cross-platform agent configuration safer and easier
to maintain. Bug reports, compatibility findings, documentation fixes, tests,
and focused implementation changes are welcome.

## Before you start

- Search existing issues before opening a new one.
- Use a private security advisory for a suspected vulnerability; see
  [SECURITY.md](SECURITY.md).
- Keep a pull request focused on one problem.
- Never include authentication files, session databases, logs, trust stores,
  private prompts, or other user data in an issue, fixture, or commit.

## Development setup

Agent Config Bridge requires Python 3.11 or later. Runtime dependencies are
`tomlkit` for comment-preserving Codex Settings edits and, on native Windows,
`tzdata` for IANA Schedule timezones. From a clone of the repository:

```console
python -m venv .venv
python -m pip install -e ".[dev]"
```

Activate the environment with `. .venv/bin/activate` on Linux or
`.venv\Scripts\Activate.ps1` in PowerShell on Windows.

Run the complete local quality gate before submitting a pull request:

```console
python scripts/check_release_contract.py
ruff check .
ruff format --check .
mypy
pytest
```

## Release contract

`pyproject.toml` is the package-version source of truth, and the editable
project entry in `uv.lock` must match it. Run
`python scripts/check_release_contract.py` before every pull request. The check
uses the latest reachable exact stable `vX.Y.Z` tag and fails when tracked
changes or nonignored untracked files retain that released version. Hatch
source distributions include nearly all tracked repository files, while wheel
metadata includes files such as `pyproject.toml`, `README.md`, and `LICENSE`;
release identity therefore is not limited to `src/`. A patch fix uses the next
patch version without skipping a patch; intentional minor or major releases may
move to the corresponding next release line. An untagged change may use either
a populated current-version changelog section or a populated Unreleased
section. An exact stable version tag on `HEAD` requires matching package
metadata, a populated version-specific changelog section, and a clean worktree.

CI checks out the complete tag history so these comparisons cannot silently
degrade in a shallow clone. See [Release and cross-host
promotion](docs/releases.md) for the build-once and digest-verification
procedure. The validator and CI enforce metadata, tag, changelog, and
cleanliness rules. Until an automated release workflow exists, creating and
verifying artifact SHA-256 records remains an operator-enforced release step.
Do not tag or promote a build until the complete quality gate passes from the
clean release commit.

## Safety invariants

Changes must preserve these project guarantees:

- `plan` is read-only.
- Apply operations never replace unmanaged files or silently overwrite drifted
  managed files.
- Only explicitly selected Skills, Plugins, Hooks, Settings, and Schedules are
  synchronized.
- Authentication, caches, logs, sessions, databases, and whole configuration
  homes are never synchronized.
- Source catalogs remain separate from generated, content-addressed output.
- The stable generated marketplace remains an integrity-checked copy of an
  immutable build.
- Codex and Claude Code overlays remain product-specific where their formats or
  behavior differ.
- Ownership reconciliation touches only standalone Skills and Settings leaves
  applied by the bridge, Plugins/Hooks registered through the bridge, and
  target scheduler heartbeats recorded by the bridge.
- Hooks are executable code. Tests and examples must be non-destructive, avoid
  network access, and never collect prompt or tool payloads.
- Schedule prompts are unattended executable intent. Tests must cover minute
  claiming, per-Schedule non-overlap, bounded execution, and absolute vendor
  executable validation without invoking a real user scheduler.

## Catalog contributions

Use lowercase kebab-case artifact directory names. Names must also be portable
to case-insensitive Windows filesystems and must not be Windows device names. A
standalone Skill belongs at
`catalog/skills/<name>/SKILL.md`. A plugin may share content through `common/`
and must keep its Codex and Claude Code manifests in the corresponding
`codex/` and `claude-code/` overlays. A hook uses `common/hooks.json` only for
the intersection supported by both products; product-specific events belong in
an overlay.

A Settings bundle uses only product-native
`codex/config.toml` and/or `claude-code/settings.json` fragments; there is no
cross-product common Settings schema. A Schedule directory contains exactly
`schedule.toml` and `PROMPT.md`, uses the strict portable schema documented in
[Host-managed Schedules](docs/schedules.md), and must not embed secrets.

Both Plugin manifests must have the same directory-matching `name` and strict
SemVer `version`. Bump both versions whenever rendered Plugin content changes.
A non-empty Hook catalog requires strict SemVer in `catalog/hooks/.version`;
bump it whenever any generated Hook declaration or script changes. For a package
also present in the current published snapshot, a changed replacement must use a
strictly higher SemVer precedence. Version-only increases are allowed; downgrade,
equal-precedence, and build-metadata-only changes do not satisfy that check. The
snapshot is not a permanent version ledger.

Every nested path must be portable to Windows. Broken/escaping links and all
directory symlinks are invalid. Only contained regular-file links are accepted
by catalog discovery, and managed copy mode does not yet install standalone
Skills containing even an accepted file link.

Raw filesystem modes are excluded from content and overlay identity. Do not use
the executable bit as cross-platform configuration; name an interpreter in
Hook/MCP commands and declare intent in product metadata.

Treat everything under the configured state directory as generated output. Do
not copy generated manifests back into the canonical catalog.

The current alpha has no all-actions transaction, apply/register target lock,
automatic rollback, or recovery log. External mutation can succeed before the
matching ownership-state write; tests that add mutation behavior must cover that
crash/partial-failure window and fail-closed ownership handling without
describing rollback or adoption as guarantees. Schedule runtime locks are a
separate mechanism, not an apply/register transaction.

POSIX tests for new ownership state, Schedule snapshot/runtime files, and new
Settings files should assert private `0600` files and `0700` managed
directories. Do not translate those assertions into a Windows ACL guarantee:
Windows inherits ACLs from the chosen product home or `state_dir`, and ACL
hardening/auditing is outside the current bridge implementation.

Before renaming/deleting a configured target or changing its product/home, keep
its old identity, set `components = []`, and run `apply` plus `register`. Removing
the target first leaves orphan ownership state; diagnostics, `apply`, and
`register` must fail until the old identity is restored and reconciled. Never
adopt or delete orphan ownership state automatically.

## Pull requests

Include:

- A concise description of the user-visible behavior and safety impact
- Tests covering success, conflict, and failure paths
- The platforms and product surfaces you exercised
- Documentation or changelog updates when behavior changes
- A package patch bump for release-impacting post-release changes, with matching
  `uv.lock` metadata and changelog entry

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
