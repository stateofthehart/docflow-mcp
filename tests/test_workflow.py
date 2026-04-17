"""End-to-end state-machine tests: draft → review → commit gate."""

from __future__ import annotations

from pathlib import Path

from docflow_mcp.committer import Committer
from docflow_mcp.scope import extract_title, resolve_decision_path
from docflow_mcp.state import StateStore


def test_commit_gate_requires_approve_verdict(tmp_repo: Path, store: StateStore):
    """commit() must refuse unless the latest review is approve."""
    d = store.create_draft(
        kind="decision", scope="cross-repo", path=None,
        content="# ADR 0002: New Decision\n\n## Context\n...\n",
    )

    # Without review: commit gate is closed.
    assert store.review_for_iteration(d.id, d.iteration) is None

    # revise verdict: still closed.
    store.record_review(
        draft_id=d.id, iteration=0, verdict="revise",
        issues=[{"severity": "major", "message": "missing context"}],
        notes=None, reviewer_model="m", reviewer_prompt_hash="h",
    )
    store.mark_reviewed(d.id)
    latest = store.latest_review(d.id)
    assert latest is not None and latest.verdict == "revise"

    # After revise, new iteration, approve this time.
    store.revise_draft(d.id, "# ADR 0002: New Decision\n\n## Context\nBetter.\n")
    store.record_review(
        draft_id=d.id, iteration=1, verdict="approve",
        issues=[], notes="good", reviewer_model="m", reviewer_prompt_hash="h",
    )
    store.mark_reviewed(d.id)
    latest = store.latest_review(d.id)
    assert latest is not None and latest.verdict == "approve"

    # Now commit is permitted.
    title = extract_title(store.get_content(d.id))
    target = resolve_decision_path(tmp_repo, title)
    committer = Committer()
    result = committer.commit_direct(
        repo=tmp_repo, target_path=target,
        content=store.get_content(d.id), message=f"docs: add ADR — {title}",
    )
    store.mark_committed(d.id, result.target_path, result.branch, result.sha)

    final = store.get_draft(d.id)
    assert final is not None and final.state == "committed"


def test_max_iterations_escalates(store: StateStore):
    """Simulating a runaway revise loop — after N iterations, should escalate."""
    d = store.create_draft(kind="decision", scope="cross-repo", path=None, content="v0")
    for i in range(4):
        store.record_review(
            draft_id=d.id, iteration=i, verdict="revise",
            issues=[], notes=None, reviewer_model="m", reviewer_prompt_hash="h",
        )
        store.mark_reviewed(d.id)
        store.revise_draft(d.id, f"v{i + 1}")
    current = store.get_draft(d.id)
    assert current is not None
    assert current.iteration == 4
    # Simulate orchestrator auto-escalating
    store.mark_escalated(d.id, reason="Auto-escalated after 5 iterations")
    after = store.get_draft(d.id)
    assert after is not None
    assert after.state == "escalated"
    assert after.metadata["escalation_reason"].startswith("Auto-escalated")


def test_escalated_draft_cannot_revise(store: StateStore):
    d = store.create_draft(kind="decision", scope="cross-repo", path=None, content="x")
    store.mark_escalated(d.id, reason="test")
    try:
        store.revise_draft(d.id, "y")
    except ValueError as e:
        assert "Cannot revise" in str(e)
        return
    raise AssertionError("revise_draft should have rejected escalated state")
