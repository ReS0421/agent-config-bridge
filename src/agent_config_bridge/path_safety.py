"""Cross-platform helpers for path identity and Windows reparse points."""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["is_directory_reparse_point", "path_comparison_key", "paths_overlap"]


def is_directory_reparse_point(path: Path) -> bool:
    """Return whether ``path`` is a Windows directory reparse point.

    This uses fields available on Windows in Python 3.11, where
    :meth:`pathlib.Path.is_junction` is not yet available. On other platforms,
    ``st_file_attributes`` is absent and the result is always ``False``.
    """

    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and attributes & stat.FILE_ATTRIBUTE_DIRECTORY)


def path_comparison_key(path: Path, *, windows: bool) -> str:
    """Return a physical, host-independent comparison key for a target path.

    ``strict=False`` resolves every existing symlink or junction ancestor while
    preserving a missing suffix, so aliases to a not-yet-created destination are
    still recognized as the same location.
    """

    try:
        physical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        physical = path
    return _normalized_path_key(physical, windows=windows)


def paths_overlap(left: Path, right: Path, *, windows: bool) -> bool:
    """Return whether two physical paths are equal or contain one another.

    Unlike :func:`path_comparison_key`, resolution failures are not hidden. A
    caller enforcing isolation can therefore fail closed instead of comparing
    unresolved spellings.
    """

    left_key = _normalized_path_key(left.resolve(strict=False), windows=windows)
    right_key = _normalized_path_key(right.resolve(strict=False), windows=windows)
    if left_key == right_key:
        return True

    separator = "/" if windows else os.sep
    left_prefix = left_key if left_key.endswith(separator) else left_key + separator
    right_prefix = right_key if right_key.endswith(separator) else right_key + separator
    return left_key.startswith(right_prefix) or right_key.startswith(left_prefix)


def _normalized_path_key(path: Path, *, windows: bool) -> str:
    normalized = os.path.normpath(os.fspath(path))
    if windows:
        return normalized.replace("\\", "/").casefold()
    return normalized
