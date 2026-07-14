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

Agent Config Bridge requires Python 3.11 or later and has no runtime
dependencies. From a clone of the repository:

```console
python -m venv .venv
python -m pip install -e ".[dev]"
```

Activate the environment with `. .venv/bin/activate` on Linux or
`.venv\Scripts\Activate.ps1` in PowerShell on Windows.

Run the complete local quality gate before submitting a pull request:

```console
ruff check .
ruff format --check .
mypy
pytest
```

## Safety invariants

Changes must preserve these project guarantees:

- `plan` is read-only.
- Apply operations never replace unmanaged files or silently overwrite drifted
  managed files.
- Only explicitly selected skills, plugins, and hooks are synchronized.
- Authentication, caches, logs, sessions, databases, and whole configuration
  homes are never synchronized.
- Source catalogs remain separate from generated, content-addressed output.
- The stable generated marketplace remains an integrity-checked copy of an
  immutable build.
- Codex and Claude Code overlays remain product-specific where their formats or
  behavior differ.
- Ownership reconciliation touches only standalone Skills applied by the bridge
  and Plugins/Hooks registered through the bridge.
- Hooks are executable code. Tests and examples must be non-destructive, avoid
  network access, and never collect prompt or tool payloads.

## Catalog contributions

Use lowercase kebab-case artifact directory names. Names must also be portable
to case-insensitive Windows filesystems and must not be Windows device names. A
standalone Skill belongs at
`catalog/skills/<name>/SKILL.md`. A plugin may share content through `common/`
and must keep its Codex and Claude Code manifests in the corresponding
`codex/` and `claude-code/` overlays. A hook uses `common/hooks.json` only for
the intersection supported by both products; product-specific events belong in
an overlay.

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

The current alpha has no all-actions transaction, target lock, automatic
rollback, or recovery log. Tests that add mutation behavior must cover partial
failure and ownership/drift handling without describing those missing features
as guarantees.

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

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
