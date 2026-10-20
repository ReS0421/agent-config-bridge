"""Tests for fail-closed filesystem inspection primitives."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_config_bridge.filesystem import (
    MANAGED_MARKER,
    FilesystemError,
    _restore_copy_backup_to_destination,
    apply_copy,
    apply_remove,
    read_managed_marker,
    tree_digest,
)
from agent_config_bridge.models import LinkMode
from tests.conftest import require_directory_symlink_support


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


def test_apply_remove_accepts_windows_extended_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows readlink substitution prefixes do not create false drift."""

    require_directory_symlink_support(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "skill"
    destination.symlink_to(source, target_is_directory=True)
    extended = "\\\\?\\" + str(source.resolve()).replace("/", "\\")
    monkeypatch.setattr(os, "readlink", lambda _path: extended)

    apply_remove(
        destination,
        mode=LinkMode.SYMLINK,
        source_id="skills/hello",
        expected_link_target=source,
        installed_digest=None,
        state_dir=tmp_path / "state",
        target_name="target",
        windows_path_semantics=True,
    )

    assert not destination.is_symlink()


def _make_skill(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: hello\n---\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts/run.py").write_text("print('hi')\n", encoding="utf-8")
    return root


def _install(tmp_path: Path, source: Path, *, update: bool = False, name: str = "dest") -> Path:
    destination = tmp_path / name
    backup = apply_copy(
        source,
        destination,
        source_id="skills/hello",
        source_digest=tree_digest(source, ignore_bytecode_caches=True),
        state_dir=tmp_path / "state",
        target_name="target",
        update=update,
    )
    return backup if backup is not None else destination


def _plant_cache(root: Path) -> None:
    cache = root / "scripts/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "run.cpython-312.pyc").write_bytes(b"cache")
    (cache / "run.cpython-312.opt-1.PYO").write_bytes(b"cache")


def _plant_sh_in_pycache(root: Path) -> None:
    (root / "scripts/__pycache__").mkdir(exist_ok=True)
    (root / "scripts/__pycache__/x.sh").write_text("#!/bin/sh\n", encoding="utf-8")


def _plant_sourceless(root: Path) -> None:
    (root / "scripts/helper.pyc").write_bytes(b"code")


def _plant_nested(root: Path) -> None:
    (root / "scripts/__pycache__/sub").mkdir(parents=True)
    (root / "scripts/__pycache__/sub/x.pyc").write_bytes(b"cache")


def _plant_dir_pyc(root: Path) -> None:
    (root / "scripts/d.pyc").mkdir()
    (root / "scripts/d.pyc/payload").write_text("x", encoding="utf-8")


def _plant_symlink_pyc(root: Path) -> None:
    (root / "scripts/__pycache__").mkdir(exist_ok=True)
    try:
        (root / "scripts/__pycache__/x.pyc").symlink_to(root / "SKILL.md")
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")


def _plant_file_named_pycache(root: Path) -> None:
    (root / "scripts/__pycache__").write_bytes(b"not a directory")


@pytest.mark.parametrize(
    "plant",
    [
        _plant_sh_in_pycache,
        _plant_sourceless,
        _plant_nested,
        _plant_dir_pyc,
        _plant_symlink_pyc,
        _plant_file_named_pycache,
    ],
)
def test_ignoring_digest_still_covers_everything_but_cpython_caches(tmp_path: Path, plant) -> None:
    """Only regular .pyc/.pyo files directly in __pycache__ are excluded."""

    skill = _make_skill(tmp_path / "skill")
    clean = tree_digest(skill, ignore_bytecode_caches=True)

    plant(skill)

    assert tree_digest(skill, ignore_bytecode_caches=True) != clean


def test_digest_is_strict_by_default_and_ignores_caches_on_request(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path / "skill")
    clean = tree_digest(skill)

    _plant_cache(skill)

    assert tree_digest(skill) != clean
    assert tree_digest(skill, ignore_bytecode_caches=True) == clean


def test_copy_excludes_caches_and_backup_is_cache_free_and_strictly_verified(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "source")
    _plant_cache(source)
    destination = _install(tmp_path, source)
    assert not (destination / "scripts/__pycache__").exists()

    _plant_cache(destination)
    (source / "SKILL.md").write_text("---\nname: hello\n---\nupdated\n", encoding="utf-8")
    backup = _install(tmp_path, source, update=True)

    assert not (backup / "scripts/__pycache__").exists()
    marker = read_managed_marker(backup)
    assert marker is not None
    assert tree_digest(backup) == marker["installed_digest"]


def test_directory_named_like_bytecode_is_copied_not_dropped(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "source")
    _plant_dir_pyc(source)

    destination = _install(tmp_path, source)

    assert (destination / "scripts/d.pyc/payload").is_file()


@pytest.mark.parametrize("planted", ["shadow/json.pyc", "scripts/__pycache__/payload.sh"])
def test_update_refuses_live_copy_with_planted_importable_or_executable_file(tmp_path: Path, planted: str) -> None:
    source = _make_skill(tmp_path / "source")
    destination = _install(tmp_path, source)
    (destination / planted).parent.mkdir(parents=True, exist_ok=True)
    (destination / planted).write_bytes(b"planted")

    with pytest.raises(FilesystemError):
        _install(tmp_path, source, update=True)


def test_restore_refuses_backup_containing_planted_sourceless_pyc(tmp_path: Path) -> None:
    source = _make_skill(tmp_path / "source")
    backup = tmp_path / "backup"
    shutil.copytree(_install(tmp_path, source), backup)
    marker = read_managed_marker(backup)
    assert marker is not None
    (backup / "shadow").mkdir()
    (backup / "shadow/json.pyc").write_bytes(b"planted")

    with pytest.raises(FilesystemError):
        _restore_copy_backup_to_destination(
            backup,
            tmp_path / "restored",
            source_id="skills/hello",
            installed_digest=marker["installed_digest"],
        )
    assert not (tmp_path / "restored").exists()


def _legacy_copy_with_caches(tmp_path: Path) -> tuple[Path, str]:
    """A managed copy whose marker digest covers caches, as recorded before they were excluded."""

    source = _make_skill(tmp_path / "source")
    _plant_cache(source)
    destination = tmp_path / "legacy"
    shutil.copytree(source, destination)
    digest = tree_digest(destination)
    (destination / MANAGED_MARKER).write_text(
        json.dumps({"schema_version": 1, "source_id": "skills/hello", "installed_digest": digest}),
        encoding="utf-8",
    )
    return destination, digest


def test_legacy_marker_recorded_with_caches_still_verifies_and_backs_up_exactly(tmp_path: Path) -> None:
    destination, digest = _legacy_copy_with_caches(tmp_path)

    backup = apply_remove(
        destination,
        mode=LinkMode.COPY,
        source_id="skills/hello",
        expected_link_target=None,
        installed_digest=digest,
        state_dir=tmp_path / "state",
        target_name="target",
        windows_path_semantics=False,
    )

    assert backup is not None
    assert tree_digest(backup) == digest
    assert not destination.exists()


def test_legacy_marker_does_not_excuse_other_modifications(tmp_path: Path) -> None:
    destination, digest = _legacy_copy_with_caches(tmp_path)
    (destination / "scripts/run.py").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(FilesystemError):
        apply_remove(
            destination,
            mode=LinkMode.COPY,
            source_id="skills/hello",
            expected_link_target=None,
            installed_digest=digest,
            state_dir=tmp_path / "state",
            target_name="target",
            windows_path_semantics=False,
        )


def test_staged_copy_is_verified_strictly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bridge's own staged copy must contain no caches; a cache there is a failure."""

    source = _make_skill(tmp_path / "source")
    _plant_cache(source)
    real_copytree = shutil.copytree

    def unfiltered(src: Path, dst: Path, **kwargs):
        monkeypatch.setattr(shutil, "copytree", real_copytree)
        return real_copytree(src, dst, symlinks=True)

    monkeypatch.setattr(shutil, "copytree", unfiltered)

    with pytest.raises(FilesystemError, match="staged copy does not match"):
        _install(tmp_path, source)
