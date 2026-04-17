"""Shared test fixtures: a throwaway docs repo with a few ADRs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docflow_mcp.state import StateStore


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Initialized git repo with a small docs/ tree."""
    repo = tmp_path / "docs-repo"
    repo.mkdir()
    (repo / "docs" / "decisions").mkdir(parents=True)
    (repo / "docs" / "contracts").mkdir(parents=True)

    (repo / "docs" / "decisions" / "0001-adopt-uv.md").write_text(
        "# ADR 0001: Adopt uv\n\n**Status: ACCEPTED**\n\n**Date**: 2026-01-15\n\n"
        "## Context\nPython tooling was slow.\n\n## Decision\nUse uv.\n\n"
        "## Alternatives Considered\n- pip: slower.\n- poetry: heavier.\n\n"
        "## Consequences\n### Positive\n- Faster installs.\n### Negative\n- New tool to learn.\n"
    )
    (repo / "docs" / "contracts" / "PROVIDER.md").write_text(
        "# Provider contract\n\n## Interface\nAll providers inherit BaseProvider.\n\n"
        "## Discovery\nProviders register via entry_points.\n"
    )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True
    )
    return repo


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")
