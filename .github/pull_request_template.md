## Summary

Describe the user-visible behavior and the products/platforms affected.

## Safety and compatibility

- [ ] I did not add credentials, sessions, caches, trust state, private Hook payloads, Settings values, or Schedule prompts.
- [ ] Existing unmanaged destinations remain protected.
- [ ] Product-specific behavior is isolated to the correct overlay.
- [ ] Host scheduler changes preserve ownership, drift, and unattended-execution boundaries.
- [ ] Plugin manifests or `hooks/.version` were increased when package content changed.
- [ ] Release-impacting post-release changes use a new package version,
      matching `uv.lock` and the changelog.

## Validation

- [ ] `uv run python scripts/check_release_contract.py`
- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
