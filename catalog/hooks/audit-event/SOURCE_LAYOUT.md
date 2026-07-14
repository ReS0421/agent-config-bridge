# Source layout, not generated output

`common/hooks.json` contains only the lifecycle events and handler fields used
by both Codex and Claude Code. Its script is copied into the generated synthetic
hook plugin at `scripts/audit-event/audit_event.py`.

If a hook needs a product-only event or different semantics, add it to
`codex/hooks.json` or `claude-code/hooks.json` instead of widening the common
document. Product documents are additive.

The generated product-specific hook plugins live under
`<state_dir>/builds/<digest>/plugins/{codex,claude-code}/` and must not be edited
or committed. An integrity-checked copy is published below the stable
`<state_dir>/marketplace` path for product registration.

`catalog/hooks/.version` supplies the strict SemVer used by both generated Hook
Plugin manifests. Bump it whenever any Hook declaration or bundled script
changes. If the generated Hook package also exists in the current published
snapshot, rendering requires strictly higher SemVer precedence for changed
content. No permanent version ledger is kept outside that snapshot.

Do not rely on a script's executable bit crossing Git, Windows, WSL, or another
filesystem. Hook commands should name the required interpreter explicitly.
