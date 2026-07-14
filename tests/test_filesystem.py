"""Tests for fail-closed filesystem inspection primitives."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_config_bridge.filesystem import (
    MANAGED_MARKER,
    FilesystemError,
    read_managed_marker,
    tree_digest,
)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_tree_digest_rejects_nested_fifo(tmp_path: Path) -> None:
    """Nested special nodes cannot be represented by an incomplete digest."""

    root = tmp_path / "tree"
    root.mkdir()
    os.mkfifo(root / "input.pipe")

    with pytest.raises(FilesystemError, match="unsupported filesystem node"):
        tree_digest(root)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
def test_tree_digest_rejects_special_managed_marker(tmp_path: Path) -> None:
    """The excluded ownership marker must itself be a real regular file."""

    root = tmp_path / "tree"
    root.mkdir()
    os.mkfifo(root / MANAGED_MARKER)

    with pytest.raises(FilesystemError, match="marker is not a real regular file"):
        tree_digest(root)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 2, "source_id": "skills/hello", "installed_digest": "abc"},
        {"schema_version": True, "source_id": "skills/hello", "installed_digest": "abc"},
        {"schema_version": 1, "source_id": 42, "installed_digest": "abc"},
        {"schema_version": 1, "source_id": "skills/hello", "installed_digest": None},
    ],
)
def test_read_managed_marker_rejects_invalid_schema(tmp_path: Path, payload: object) -> None:
    """Ownership proof requires the complete versioned marker schema."""

    destination = tmp_path / "skill"
    destination.mkdir()
    (destination / MANAGED_MARKER).write_text(json.dumps(payload), encoding="utf-8")

    assert read_managed_marker(destination) is None


def test_read_managed_marker_rejects_symlink(tmp_path: Path) -> None:
    """A forged marker link cannot delegate ownership proof to another file."""

    destination = tmp_path / "skill"
    destination.mkdir()
    external = tmp_path / "external-marker.json"
    external.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": "skills/hello",
                "installed_digest": "abc",
            }
        ),
        encoding="utf-8",
    )
    try:
        (destination / MANAGED_MARKER).symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable in this test environment: {exc}")

    assert read_managed_marker(destination) is None
