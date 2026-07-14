# Source layout, not generated output

The `common/` directory is copied first into each target package. The `codex/`
or `claude-code/` overlay is then added to its matching package:

```text
common/skills/hello/SKILL.md
codex/.codex-plugin/plugin.json
claude-code/.claude-plugin/plugin.json
```

Both JSON files in this directory tree are canonical source overlays. Separate
rendered packages are generated under
`<state_dir>/builds/<digest>/plugins/{codex,claude-code}/hello-shared/`. Never
edit that output; change an overlay and render again.

The two manifests must retain the same `name` and strict SemVer `version`. If
anything in `common/`, `codex/`, or `claude-code/` changes the rendered package,
bump the version in both manifests before publishing the next stable marketplace
snapshot. A changed package overlapping the current published snapshot must have
strictly higher SemVer precedence; the bridge does not retain version history
after that snapshot is removed.

Overlay collision identity compares path/type/bytes, not raw filesystem mode.
Use explicit interpreters and product metadata for execution rather than relying
on executable permission portability.
