# Agent Config Bridge contributor guidance

- Keep the runtime dependency-free; use the Python standard library unless an ADR accepts a new dependency.
- Treat `plan` as strictly read-only.
- Never overwrite an unmanaged destination. Existing content is a conflict unless
  bridge ownership state (and, for managed copies, its marker) proves ownership
  and the installed content has not drifted.
- Never synchronize authentication, session databases, logs, caches, trust stores, or entire product config homes.
- Preserve target-specific plugin manifests and hook semantics; do not claim that Codex and Claude Code formats are identical.
- Add or update tests for every behavior change. Run `ruff check .`, `ruff format --check .`, `mypy`, and `pytest` before publishing.
- Use `apply_patch` for hand-written file changes and keep commits focused.
