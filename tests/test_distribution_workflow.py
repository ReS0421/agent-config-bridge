"""Offline wiring guards; CI checks compatibility against the actual distributions."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_ci_pins_twine_with_core_metadata_25_support() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["quality"]["steps"]
    step = next(step for step in steps if step["name"] == "Validate distribution metadata")

    # 6.2.0 rejects valid Metadata-Version: 2.5; 7.0.0 includes upstream fix #1317.
    assert shlex.split(step["run"]) == ["uvx", "--from", "twine==7.0.0", "twine", "check", "dist/*"]


def test_distribution_gate_remains_between_build_and_wheel_smoke() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["quality"]
    steps = job["steps"]
    names = [step["name"] for step in steps]
    selected = ("Build distribution", "Validate distribution metadata", "Smoke test wheel entrypoint")
    indices = [names.index(name) for name in selected]

    assert indices == sorted(indices)
    assert not job.get("continue-on-error", False)
    for index in indices:
        assert steps[index]["if"] == "matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'"
        assert not steps[index].get("continue-on-error", False)
    assert steps[indices[0]]["run"] == "uv build"


def test_release_guide_uses_the_same_distribution_checker() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["quality"]["steps"]
    step = next(step for step in steps if step["name"] == "Validate distribution metadata")
    guide = (ROOT / "docs/releases.md").read_text(encoding="utf-8")

    assert f"\n{step['run']}\n" in guide
