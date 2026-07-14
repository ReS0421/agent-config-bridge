"""Tests for cross-platform path safety helpers."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from agent_config_bridge.path_safety import is_directory_reparse_point, path_comparison_key, paths_overlap


class _FakePath:
    def __init__(self, attributes: int) -> None:
        self.attributes = attributes

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_file_attributes=self.attributes)


def test_directory_reparse_detection_works_with_python_311_stat_fields() -> None:
    """Windows directory attributes identify junctions without Path.is_junction."""

    directory_junction = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY
    path = cast(Any, _FakePath(directory_junction))

    assert is_directory_reparse_point(path)
    assert not is_directory_reparse_point(cast(Any, _FakePath(stat.FILE_ATTRIBUTE_REPARSE_POINT)))
    assert not is_directory_reparse_point(cast(Any, _FakePath(0)))


def test_windows_path_comparison_normalizes_case_and_separators() -> None:
    """Windows target identity is independent of host path flavor."""

    left = Path("/MNT/C/Users/Res/.CODEX")
    right = Path("/mnt/c/users/res/.codex")

    assert path_comparison_key(left, windows=True) == path_comparison_key(right, windows=True)
    assert path_comparison_key(left, windows=False) != path_comparison_key(right, windows=False)


def test_paths_overlap_is_bidirectional_and_segment_aware() -> None:
    """Parents overlap descendants, while path-prefix siblings remain separate."""

    parent = Path("/srv/agent")
    child = parent / "state/builds"

    assert paths_overlap(parent, child, windows=False)
    assert paths_overlap(child, parent, windows=False)
    assert not paths_overlap(parent, Path("/srv/agent-other"), windows=False)


def test_paths_overlap_uses_windows_case_semantics() -> None:
    """Case variants of a Windows ancestor cannot evade containment checks."""

    parent = Path("/MNT/C/Users/Res/.CODEX")
    child = Path("/mnt/c/users/res/.codex/plugins/cache")

    assert paths_overlap(parent, child, windows=True)
    assert not paths_overlap(parent, child, windows=False)
