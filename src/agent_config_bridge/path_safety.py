"""Cross-platform helpers for path identity and Windows reparse points."""

from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path

__all__ = ["is_directory_reparse_point", "path_comparison_key", "paths_overlap", "read_symlink_target"]


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

    _, has_windows_prefix = _strip_windows_extended_prefix(os.fspath(path))
    if has_windows_prefix:
        physical = path
    else:
        try:
            physical = path.resolve(strict=False)
        except (OSError, RuntimeError):
            physical = path
    return _normalized_path_key(physical, windows=windows)


def read_symlink_target(path: Path) -> Path:
    """Return a symlink's lexical target as an absolute path when necessary."""

    raw_target = os.readlink(path)
    target = Path(raw_target)
    if target.is_absolute() or ntpath.isabs(raw_target):
        return target
    return Path(os.path.abspath(path.parent / target))


def paths_overlap(left: Path, right: Path, *, windows: bool) -> bool:
    """Return whether two physical paths are equal or contain one another.

    Unlike :func:`path_comparison_key`, resolution failures are not hidden. A
    caller enforcing isolation can therefore fail closed instead of comparing
    unresolved spellings.
    """

    left_key = _normalized_path_key(_resolve_with_missing_suffix(left), windows=windows)
    right_key = _normalized_path_key(_resolve_with_missing_suffix(right), windows=windows)
    if left_key == right_key:
        return True

    separator = "/" if windows else os.sep
    left_prefix = left_key if left_key.endswith(separator) else left_key + separator
    right_prefix = right_key if right_key.endswith(separator) else right_key + separator
    return left_key.startswith(right_prefix) or right_key.startswith(left_prefix)


def _normalized_path_key(path: Path, *, windows: bool) -> str:
    raw_path, had_windows_prefix = _strip_windows_extended_prefix(os.fspath(path))
    if windows:
        return ntpath.normpath(raw_path).replace("\\", "/").casefold()
    if had_windows_prefix:
        return ntpath.normpath(raw_path)
    return os.path.normpath(raw_path)


def _resolve_with_missing_suffix(path: Path) -> Path:
    """Strictly resolve the deepest lexically existing ancestor of ``path``."""

    candidate = Path(os.path.abspath(path))
    missing: list[str] = []
    while True:
        try:
            candidate.lstat()
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                break
            missing.append(candidate.name)
            candidate = parent
        else:
            break

    resolved = candidate.resolve(strict=True)
    for name in reversed(missing):
        resolved /= name
    return resolved


def _strip_windows_extended_prefix(value: str) -> tuple[str, bool]:
    """Remove Windows kernel namespace prefixes returned by ``os.readlink``."""

    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    for prefix in ("\\\\?\\unc\\", "\\??\\unc\\"):
        if folded.startswith(prefix):
            return "\\\\" + normalized[len(prefix) :], True
    for prefix in ("\\\\?\\", "\\??\\"):
        if folded.startswith(prefix):
            return normalized[len(prefix) :], True
    return value, False
