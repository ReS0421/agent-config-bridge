# Releases and cross-host promotion

Package identity is immutable. Once a version is tagged or installed on another
host, do not rebuild different source under the same version. Any tracked change
or nonignored untracked file can affect the Hatch source distribution; wheel
identity can also change through metadata inputs such as `pyproject.toml`,
`README.md`, and `LICENSE`. A release-impacting change after `vX.Y.Z` therefore
receives a new version; patch fixes use the next `X.Y.(Z+1)` version. Keep
`pyproject.toml`, the editable project entry in `uv.lock`, and `CHANGELOG.md`
aligned.

## Prepare and tag

1. Fetch the complete tag history and start from the intended release commit.
2. Confirm the worktree is clean.
3. Run the release contract and the complete quality gate.
4. Create the exact `vX.Y.Z` tag only when its version equals package metadata.
5. On the exact clean tagged commit, run `uv build` once.

The release-contract check is read-only:

```console
uv run python scripts/check_release_contract.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

Do not promote artifacts rebuilt from a branch name, a dirty worktree, or a
commit merely adjacent to the tag.

The validator and CI enforce complete tag visibility, package/lock agreement,
post-tag version movement, populated changelog sections, exact-tag metadata,
and exact-tag worktree cleanliness. An exact stable tag requires its own
populated version-specific changelog section; Unreleased notes are accepted
only before tagging.

## Record artifact identity

Create a `SHA256SUMS` manifest that records the SHA-256 and filename of every
wheel and source distribution in `dist/`. Keep the manifest with the release
artifacts and release record. On Linux, for example:

```console
cd dist
sha256sum *.whl *.tar.gz > SHA256SUMS
sha256sum --check SHA256SUMS
```

On Windows, calculate each artifact with `Get-FileHash -Algorithm SHA256` and
record the same filename-to-digest mapping. A release operator must compare the
recorded digest on every destination host before installation.

Artifact hashing is not yet automated by a release workflow. Unlike the
metadata, tag, changelog, and cleanliness checks above, manifest creation,
retention, and destination verification remain operator-enforced gates.

## Promote to another host

Transfer the already-built artifact and `SHA256SUMS`; do not run `uv build` on
the destination. Verify the received file's SHA-256 against the release record,
then install that exact wheel. Record at least the version, Git commit, artifact
filename, and SHA-256 in the promotion log. A mismatched or missing digest stops
promotion, even when the embedded package version looks correct.
