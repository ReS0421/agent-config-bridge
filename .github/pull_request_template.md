## Summary

Describe the user-visible behavior and the products/platforms affected.

## Safety and compatibility

- [ ] I did not add credentials, sessions, caches, trust state, or private Hook payloads.
- [ ] Existing unmanaged destinations remain protected.
- [ ] Product-specific behavior is isolated to the correct overlay.
- [ ] Plugin manifests or `hooks/.version` were increased when package content changed.

## Validation

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
