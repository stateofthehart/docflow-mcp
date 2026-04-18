"""Review-flow tests — the prepare_review / submit_review contract.

docflow does not spawn or call LLMs. These tests exercise the state
transitions and invariants around externally-produced verdicts, and
confirm the `collection` parameter is threaded correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def docs_root(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "docs-root"
    (root / "docs" / "decisions").mkdir(parents=True)
    yield root


@pytest.fixture(autouse=True)
def _reset_server_singletons(monkeypatch, docs_root: Path):
    """Point the server at the tmp docs_root and reset lazy singletons per test."""
    # Legacy env path — docflow creates a 'default' collection pointing at DOCS_ROOT.
    monkeypatch.setenv("DOCS_ROOT", str(docs_root))
    monkeypatch.setenv("DOCS_STATE_DIR", str(docs_root / ".docs-state"))
    monkeypatch.delenv("DOCS_SCOPE_MAP", raising=False)
    monkeypatch.delenv("DOCFLOW_CONFIG_FILE", raising=False)
    monkeypatch.setenv("DOCS_MAX_ITERATIONS", "3")

    from docflow_mcp import server
    server._cfg = None
    server._store = None
    server._committer = None
    server._readers.clear()
    yield


def _call(tool_fn, **kwargs) -> str:
    underlying = getattr(tool_fn, "fn", tool_fn)
    return underlying(**kwargs)


COLLECTION = "default"


def test_prepare_review_returns_self_contained_bundle(docs_root: Path):
    from docflow_mcp import server

    d = _call(
        server.draft,
        collection=COLLECTION, kind="decision", scope="cross-repo",
        content="# ADR: Test decision\n\n## Context\nx\n",
    )
    draft_id = json.loads(d)["draft_id"]

    bundle = json.loads(_call(server.prepare_review, collection=COLLECTION, draft_id=draft_id))
    assert bundle["collection"] == COLLECTION
    assert bundle["kind"] == "decision"
    assert bundle["iteration"] == 0
    assert bundle["working_dir"] == str(docs_root)
    assert "ADR: Test decision" in bundle["task"]
    assert bundle["system_prompt"].startswith("# Reviewer prompt")
    assert len(bundle["prompt_hash"]) == 12


def test_submit_review_records_verdict(docs_root: Path):
    from docflow_mcp import server

    d = _call(server.draft, collection=COLLECTION, kind="decision",
              scope="cross-repo", content="# ADR: X\n")
    draft_id = json.loads(d)["draft_id"]

    result = json.loads(
        _call(server.submit_review, collection=COLLECTION, draft_id=draft_id,
              verdict="approve", issues=[], notes="all clean",
              reviewer_model="gemini-2.5-pro", prompt_hash="abc123def456")
    )
    assert result["verdict"] == "approve"
    assert result["iteration"] == 0

    status = json.loads(_call(server.status, draft_id=draft_id))
    assert status["reviews"][0]["verdict"] == "approve"
    assert status["reviews"][0]["reviewer_model"] == "gemini-2.5-pro"


def test_submit_review_rejects_bad_verdict(docs_root: Path):
    from docflow_mcp import server

    d = _call(server.draft, collection=COLLECTION, kind="decision",
              scope="cross-repo", content="x")
    draft_id = json.loads(d)["draft_id"]
    err = _call(server.submit_review, collection=COLLECTION,
                draft_id=draft_id, verdict="looks_fine")
    assert "approve" in err and "revise" in err and "escalate" in err


def test_cross_collection_submit_rejected(docs_root: Path):
    """submit_review must refuse a draft from a different collection."""
    from docflow_mcp import server

    d = _call(server.draft, collection=COLLECTION, kind="decision",
              scope="cross-repo", content="x")
    draft_id = json.loads(d)["draft_id"]
    err = _call(server.submit_review, collection="other",
                draft_id=draft_id, verdict="approve")
    assert "belongs to collection" in err or "Unknown collection" in err


def test_commit_gate_requires_approve(docs_root: Path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=docs_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=docs_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=docs_root, check=True)
    (docs_root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=docs_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=docs_root, check=True)

    from docflow_mcp import server

    d = _call(server.draft, collection=COLLECTION, kind="decision",
              scope="cross-repo",
              content="# ADR: Gate\n\n## Context\nTest\n")
    draft_id = json.loads(d)["draft_id"]

    assert "ERROR" in _call(server.commit, collection=COLLECTION, draft_id=draft_id)

    _call(server.submit_review, collection=COLLECTION, draft_id=draft_id,
          verdict="revise", issues=[])
    err = _call(server.commit, collection=COLLECTION, draft_id=draft_id)
    assert "ERROR" in err

    _call(server.revise, collection=COLLECTION, draft_id=draft_id,
          content="# ADR: Gate\n\n## Context\nFixed\n")
    _call(server.submit_review, collection=COLLECTION, draft_id=draft_id,
          verdict="approve", issues=[])
    result = json.loads(_call(server.commit, collection=COLLECTION, draft_id=draft_id))
    assert result["state"] == "committed"
    assert result["sha"]


def test_prepare_review_refuses_when_max_iterations_hit(docs_root: Path):
    from docflow_mcp import server

    d = _call(server.draft, collection=COLLECTION, kind="decision",
              scope="cross-repo", content="v0")
    draft_id = json.loads(d)["draft_id"]

    for i in range(3):
        _call(server.submit_review, collection=COLLECTION, draft_id=draft_id,
              verdict="revise", issues=[])
        _call(server.revise, collection=COLLECTION, draft_id=draft_id,
              content=f"v{i + 1}")

    bundle = json.loads(_call(server.prepare_review,
                              collection=COLLECTION, draft_id=draft_id))
    assert bundle.get("error") == "max_iterations_exceeded"
    assert "escalate" in bundle["next_step"]


def test_list_collections_shows_configured(docs_root: Path):
    from docflow_mcp import server
    out = _call(server.list_collections)
    assert "default" in out
    assert str(docs_root) in out
