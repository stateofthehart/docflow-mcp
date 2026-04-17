"""Review-flow tests — the prepare_review/submit_review contract.

docflow does not spawn or call LLMs. These tests exercise the state
transitions and invariants around externally-produced verdicts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

# prepare_review and submit_review are FastMCP-decorated functions on a
# singleton; importing the module is enough to register them, but we test
# the underlying state-store contracts plus the bundle assembly via the
# helper that server.py uses (_prompt_for_kind).


@pytest.fixture
def docs_root(tmp_path: Path) -> Iterator[Path]:
    """Minimal docs_root with the packaged prompts/ dir copied in."""
    root = tmp_path / "docs-root"
    (root / "docs" / "decisions").mkdir(parents=True)
    yield root


@pytest.fixture(autouse=True)
def _reset_server_singletons(monkeypatch, docs_root: Path):
    """Point the server at the tmp docs_root and reset lazy singletons per test."""
    monkeypatch.setenv("DOCS_ROOT", str(docs_root))
    monkeypatch.setenv("DOCS_STATE_DIR", str(docs_root / ".docs-state"))
    monkeypatch.delenv("DOCS_SCOPE_MAP", raising=False)
    monkeypatch.setenv("DOCS_MAX_ITERATIONS", "3")

    from docflow_mcp import server
    server._cfg = None
    server._store = None
    server._reader = None
    server._committer = None
    yield


def _call_tool(tool_fn, **kwargs) -> str:
    """Unwrap FastMCP's decorator so we can call the tool as a plain fn."""
    underlying = getattr(tool_fn, "fn", tool_fn)
    return underlying(**kwargs)


def test_prepare_review_returns_self_contained_bundle(docs_root: Path):
    from docflow_mcp import server

    d = _call_tool(
        server.draft,
        kind="decision",
        scope="cross-repo",
        content="# ADR: Test decision\n\n## Context\nx\n",
    )
    draft_id = json.loads(d)["draft_id"]

    bundle = json.loads(_call_tool(server.prepare_review, draft_id=draft_id))
    assert bundle["kind"] == "decision"
    assert bundle["iteration"] == 0
    assert bundle["working_dir"] == str(docs_root)
    assert "ADR: Test decision" in bundle["task"]
    assert bundle["system_prompt"].startswith("# Reviewer prompt")
    assert len(bundle["prompt_hash"]) == 12


def test_submit_review_records_verdict(docs_root: Path):
    from docflow_mcp import server

    d = _call_tool(
        server.draft,
        kind="decision",
        scope="cross-repo",
        content="# ADR: X\n",
    )
    draft_id = json.loads(d)["draft_id"]

    result = json.loads(
        _call_tool(
            server.submit_review,
            draft_id=draft_id,
            verdict="approve",
            issues=[],
            notes="all clean",
            reviewer_model="gemini-2.5-pro",
            prompt_hash="abc123def456",
        )
    )
    assert result["verdict"] == "approve"
    assert result["iteration"] == 0

    status = json.loads(_call_tool(server.status, draft_id=draft_id))
    assert status["reviews"][0]["verdict"] == "approve"
    assert status["reviews"][0]["reviewer_model"] == "gemini-2.5-pro"


def test_submit_review_rejects_bad_verdict(docs_root: Path):
    from docflow_mcp import server

    d = _call_tool(server.draft, kind="decision", scope="cross-repo", content="x")
    draft_id = json.loads(d)["draft_id"]
    err = _call_tool(server.submit_review, draft_id=draft_id, verdict="looks_fine")
    assert "must be one of" in err


def test_commit_gate_requires_approve(docs_root: Path, tmp_path: Path):
    """End-to-end: commit refuses without an approve verdict."""
    import subprocess

    # Make docs_root a git repo so commit_direct can write there.
    subprocess.run(["git", "init", "-q"], cwd=docs_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=docs_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=docs_root, check=True)
    (docs_root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=docs_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=docs_root, check=True)

    from docflow_mcp import server

    d = _call_tool(
        server.draft,
        kind="decision",
        scope="cross-repo",
        content="# ADR: Gate\n\n## Context\nTest\n",
    )
    draft_id = json.loads(d)["draft_id"]

    # Without any review, commit is refused.
    assert "ERROR" in _call_tool(server.commit, draft_id=draft_id)

    # With a revise verdict, commit is still refused.
    _call_tool(server.submit_review, draft_id=draft_id, verdict="revise", issues=[])
    err = _call_tool(server.commit, draft_id=draft_id)
    assert "ERROR" in err
    assert "revise" in err or "approve" in err

    # After revise + approve on the new iteration, commit succeeds.
    rev = json.loads(
        _call_tool(server.revise, draft_id=draft_id, content="# ADR: Gate\n\n## Context\nFixed\n")
    )
    assert rev["iteration"] == 1
    _call_tool(server.submit_review, draft_id=draft_id, verdict="approve", issues=[])
    result = json.loads(_call_tool(server.commit, draft_id=draft_id))
    assert result["state"] == "committed"
    assert result["sha"]


def test_prepare_review_refuses_when_max_iterations_hit(docs_root: Path):
    """After iteration == max_iterations, prepare_review returns an escalate signal."""
    from docflow_mcp import server

    d = _call_tool(
        server.draft, kind="decision", scope="cross-repo", content="v0"
    )
    draft_id = json.loads(d)["draft_id"]

    # Burn through iterations via revise (DOCS_MAX_ITERATIONS=3 in fixture).
    for i in range(3):
        _call_tool(server.submit_review, draft_id=draft_id, verdict="revise", issues=[])
        _call_tool(server.revise, draft_id=draft_id, content=f"v{i + 1}")

    bundle = json.loads(_call_tool(server.prepare_review, draft_id=draft_id))
    assert bundle.get("error") == "max_iterations_exceeded"
    assert "escalate" in bundle["next_step"]
