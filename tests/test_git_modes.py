"""Git-mode-aware commit behavior + idempotent escalation + config validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docflow_mcp.committer import Committer, GitModeError
from docflow_mcp.config import CollectionConfig, Config
from docflow_mcp.state import StateStore


@pytest.fixture
def plain_dir(tmp_path: Path) -> Path:
    """A docs_root that's a plain directory — not a git repo."""
    root = tmp_path / "plain-docs"
    (root / "docs" / "decisions").mkdir(parents=True)
    return root


@pytest.fixture
def local_git_repo(tmp_path: Path) -> Path:
    """docs_root initialized with git but no remote."""
    root = tmp_path / "local-docs"
    (root / "docs" / "decisions").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


# ── git_mode=disabled — plain file write, no git calls ────────────


def test_commit_direct_disabled_mode_writes_file_no_git(plain_dir: Path):
    c = Committer()
    target = plain_dir / "docs" / "decisions" / "0001-test.md"
    result = c.commit_direct(
        repo=plain_dir, target_path=target,
        content="# Test\n", message="irrelevant", git_mode="disabled",
    )
    assert result.strategy == "file-only"
    assert result.branch is None
    assert result.sha is None
    assert target.read_text() == "# Test\n"
    # No .git directory should have been created
    assert not (plain_dir / ".git").exists()


def test_commit_on_branch_refuses_disabled_mode(plain_dir: Path):
    c = Committer()
    target = plain_dir / "docs" / "decisions" / "0001-x.md"
    with pytest.raises(GitModeError):
        c.commit_on_branch(
            repo=plain_dir, target_path=target,
            content="x", message="m", branch="test", git_mode="disabled",
        )


# ── git_mode=local — commit works but push must not be attempted ───


def test_commit_direct_local_mode_commits(local_git_repo: Path):
    c = Committer()
    target = local_git_repo / "docs" / "decisions" / "0001-local.md"
    result = c.commit_direct(
        repo=local_git_repo, target_path=target,
        content="# local\n", message="docs: local", git_mode="local",
    )
    assert result.sha is not None
    assert result.strategy == "direct"


def test_local_repo_has_no_origin(local_git_repo: Path):
    c = Committer()
    assert c.has_origin(local_git_repo) is False


# ── idempotent commit_on_branch — no-op on identical content ──────


def test_commit_on_branch_idempotent_when_nothing_changes(local_git_repo: Path):
    c = Committer()
    target = local_git_repo / "docs" / "decisions" / "0002-idem.md"
    # First call creates branch + commit
    r1 = c.commit_on_branch(
        repo=local_git_repo, target_path=target,
        content="# first\n", message="docs: first",
        branch="docs/agent-test", git_mode="local",
    )
    # Second call with same content: branch exists, commit should no-op
    r2 = c.commit_on_branch(
        repo=local_git_repo, target_path=target,
        content="# first\n", message="docs: first",
        branch="docs/agent-test", git_mode="local",
    )
    assert r1.sha == r2.sha  # no new commit on retry


# ── pre-flight probes ────────────────────────────────────────────


def test_gh_auth_ok_returns_tuple():
    c = Committer()
    ok, detail = c.gh_auth_ok()
    assert isinstance(ok, bool)
    assert isinstance(detail, str)


def test_branch_exists_on_origin_false_for_local_only(local_git_repo: Path):
    c = Committer()
    assert c.branch_exists_on_origin(local_git_repo, "does-not-exist") is False


# ── config validation ────────────────────────────────────────────


def test_config_validate_raises_on_missing_docs_root(tmp_path: Path):
    cfg = Config(
        collections={
            "bogus": CollectionConfig(
                name="bogus",
                docs_root=tmp_path / "does-not-exist",
                scope_map={},
                git_mode="remote",
            )
        },
        state_dir=tmp_path / "state",
        max_iterations=5,
        prompts_dir=tmp_path / "prompts",
        plane_stale_project=None,
    )
    with pytest.raises(RuntimeError, match="docs_root does not exist"):
        cfg.validate()


def test_config_validate_warns_on_non_git_when_mode_remote(plain_dir: Path, tmp_path: Path):
    cfg = Config(
        collections={
            "p": CollectionConfig(
                name="p", docs_root=plain_dir, scope_map={}, git_mode="remote",
            )
        },
        state_dir=tmp_path / "state",
        max_iterations=5,
        prompts_dir=tmp_path / "prompts",
        plane_stale_project=None,
    )
    warnings = cfg.validate()
    assert any("is not a git repo" in w for w in warnings)


def test_config_validate_clean_for_disabled_mode_with_plain_dir(plain_dir: Path, tmp_path: Path):
    """git_mode=disabled on a plain directory should pass validation cleanly."""
    cfg = Config(
        collections={
            "p": CollectionConfig(
                name="p", docs_root=plain_dir, scope_map={}, git_mode="disabled",
            )
        },
        state_dir=tmp_path / "state",
        max_iterations=5,
        prompts_dir=tmp_path / "prompts",
        plane_stale_project=None,
    )
    warnings = cfg.validate()
    assert warnings == []


# ── escalation stage recording ───────────────────────────────────


def test_escalation_stages_recorded_and_retrievable(tmp_path: Path):
    store = StateStore(tmp_path / "state")
    d = store.create_draft(
        collection="default", kind="decision", scope="cross-repo",
        path=None, content="x",
    )
    # Stage 1: commit
    store.record_escalation_commit(d.id, branch="docs/agent-x", sha="abc123def", reason="r")
    e = store.get_escalation(d.id)
    assert e is not None and e.branch == "docs/agent-x" and e.sha == "abc123def"
    assert not e.pushed and e.pr_url is None

    # Stage 2: pushed
    store.record_escalation_pushed(d.id)
    e = store.get_escalation(d.id)
    assert e is not None and e.pushed and e.pushed_at is not None

    # Stage 3: PR
    store.record_escalation_pr(d.id, "https://github.com/x/y/pull/42")
    e = store.get_escalation(d.id)
    assert e is not None and e.pr_url == "https://github.com/x/y/pull/42"


def test_escalation_commit_is_idempotent(tmp_path: Path):
    """Re-recording commit stage should update, not duplicate."""
    store = StateStore(tmp_path / "state")
    d = store.create_draft(
        collection="default", kind="decision", scope="cross-repo",
        path=None, content="x",
    )
    store.record_escalation_commit(d.id, branch="b1", sha="s1", reason="r1")
    store.record_escalation_commit(d.id, branch="b1", sha="s2", reason="r1")  # retry
    e = store.get_escalation(d.id)
    assert e is not None and e.sha == "s2"  # latest wins
