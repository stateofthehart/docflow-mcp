"""Committer — direct commits, branch commits, git correctness."""

from __future__ import annotations

import subprocess
from pathlib import Path

from docflow_mcp.committer import Committer


def test_commit_direct_writes_and_commits(tmp_repo: Path):
    c = Committer()
    target = tmp_repo / "docs" / "decisions" / "0002-new.md"
    result = c.commit_direct(
        repo=tmp_repo, target_path=target,
        content="# ADR 0002: New\n\ncontent\n",
        message="docs: add ADR — New",
    )
    assert target.read_text().startswith("# ADR 0002")
    assert result.sha is not None
    assert result.strategy == "direct"
    # Verify the commit landed on the current branch
    log = subprocess.run(
        ["git", "-C", str(tmp_repo), "log", "-1", "--pretty=format:%s"],
        capture_output=True, text=True, check=True,
    )
    assert log.stdout == "docs: add ADR — New"


def test_commit_on_branch_creates_branch(tmp_repo: Path):
    c = Committer()
    target = tmp_repo / "docs" / "decisions" / "0003-branched.md"
    result = c.commit_on_branch(
        repo=tmp_repo, target_path=target,
        content="# ADR 0003\n",
        message="docs: branched draft",
        branch="docs/agent-xyz",
    )
    assert result.branch == "docs/agent-xyz"
    assert result.strategy == "branch"
    # Current branch should have been restored
    current = subprocess.run(
        ["git", "-C", str(tmp_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current != "docs/agent-xyz"
    # Branch should exist
    branches = subprocess.run(
        ["git", "-C", str(tmp_repo), "branch", "--list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "docs/agent-xyz" in branches
