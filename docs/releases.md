# Releases and cross-host promotion

Package identity is immutable. Once a version is tagged or installed on another
host, do not rebuild different source under the same version. Any tracked change
or nonignored untracked file can affect the Hatch source distribution; wheel
identity can also change through metadata inputs such as `pyproject.toml`,
`README.md`, and `LICENSE`. A release-impacting change after `vX.Y.Z` therefore
receives a new version; patch fixes use the next `X.Y.(Z+1)` version. Keep
`pyproject.toml`, the editable project entry in `uv.lock`, and `CHANGELOG.md`
aligned.

## Pre-tag gates

1. Fetch the complete tag history and start from the intended release commit.
2. Confirm the worktree is clean.
3. Confirm `pyproject.toml` and `uv.lock` contain the intended version.
   Development commits may keep populated notes under `Unreleased`; the final
   release commit must move them into a populated version-specific section
   before the tag is created.
4. Run the release contract and complete quality gate on that final release
   commit. These commands inspect source; they do not create release artifacts.

The pre-tag gates are read-only:

```console
uv run python scripts/check_release_contract.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

The validator and CI enforce complete tag visibility, package/lock agreement,
post-tag version movement, populated changelog sections, exact-tag metadata,
and exact-tag worktree cleanliness. An exact stable tag requires its own
populated version-specific changelog section; Unreleased notes are accepted
only before tagging.

## Tag, build once, and publish

1. Create an annotated `vX.Y.Z` tag on that exact commit and push the tag.
2. Verify `HEAD` is the exact tag, the worktree is still clean, and rerun the
   release-contract check.
3. Create a fresh empty output directory and run `uv build` exactly once from
   that clean tagged checkout.
4. Validate the wheel and source distribution, then create `SHA256SUMS`.
5. Upload that wheel, source distribution, and `SHA256SUMS` to the GitHub
   Release for the already-pushed tag. Never rebuild between validation and
   upload.
6. Download all three published assets into a second fresh directory and run
   `sha256sum --check SHA256SUMS` there. Publication is incomplete until this
   independent download check succeeds.

Do not promote artifacts built from a branch name, a dirty worktree, or a
commit merely adjacent to the tag. A failed artifact or upload gate is fixed
under a new version; it is not replaced by a second build with the same tag.

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

Artifact hashing and GitHub Release publication are not yet automated by a
release workflow. Unlike the metadata, tag, changelog, and cleanliness checks
above, manifest creation, upload, re-download verification, retention, and
destination verification remain operator-enforced gates.

## Promote to another host

Transfer the already-built artifact and `SHA256SUMS`; do not run `uv build` on
the destination. Verify the received file's SHA-256 against the release record,
then install that exact wheel. Record at least the version, Git commit, artifact
filename, SHA-256, destination runtime, installation command, and verification
result in the promotion log. Multiple runtimes must cite the same released wheel
digest. A mismatched or missing digest stops promotion, even when the embedded
package version looks correct.
