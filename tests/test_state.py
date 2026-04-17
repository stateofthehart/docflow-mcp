"""State store — lifecycle, concurrency within a process, invariants."""

from __future__ import annotations

import pytest

from docflow_mcp.state import StateStore


def test_create_and_read_draft(store: StateStore):
    d = store.create_draft(
        kind="decision", scope="cross-repo", path=None, content="# ADR 1: Test\n"
    )
    assert d.state == "drafting"
    assert d.iteration == 0
    assert store.get_content(d.id) == "# ADR 1: Test\n"

    fetched = store.get_draft(d.id)
    assert fetched is not None
    assert fetched.id == d.id
    assert fetched.scope == "cross-repo"


def test_revise_increments_iteration_and_resets_state(store: StateStore):
    d = store.create_draft(
        kind="decision", scope="cross-repo", path=None, content="v1"
    )
    store.record_review(
        draft_id=d.id, iteration=0, verdict="revise",
        issues=[{"severity": "major", "message": "missing consequences"}],
        notes=None, reviewer_model="gemini-2.5-pro", reviewer_prompt_hash="abc123",
    )
    store.mark_reviewed(d.id)

    revised = store.revise_draft(d.id, "v2")
    assert revised.iteration == 1
    assert revised.state == "drafting"
    assert store.get_content(d.id) == "v2"
    assert store.get_content(d.id, iteration=0) == "v1"


def test_commit_gate_state_transitions(store: StateStore):
    d = store.create_draft(
        kind="decision", scope="cross-repo", path=None, content="x"
    )
    store.mark_committed(d.id, target_path="docs/decisions/0001-x.md", branch="main", sha="abc")
    updated = store.get_draft(d.id)
    assert updated is not None
    assert updated.state == "committed"


def test_revise_rejects_committed_draft(store: StateStore):
    d = store.create_draft(kind="decision", scope="cross-repo", path=None, content="x")
    store.mark_committed(d.id, "docs/decisions/0001-x.md", "main", "abc")
    with pytest.raises(ValueError, match="Cannot revise"):
        store.revise_draft(d.id, "y")


def test_latest_review_returns_most_recent(store: StateStore):
    d = store.create_draft(kind="decision", scope="cross-repo", path=None, content="x")
    store.record_review(d.id, 0, "revise", [], None, "m1", "h1")
    store.revise_draft(d.id, "x2")
    store.record_review(d.id, 1, "approve", [], "all good", "m2", "h2")
    latest = store.latest_review(d.id)
    assert latest is not None
    assert latest.verdict == "approve"
    assert latest.iteration == 1


def test_abandon_keeps_row_for_audit(store: StateStore):
    d = store.create_draft(kind="decision", scope="cross-repo", path=None, content="x")
    store.abandon(d.id, "author reconsidered")
    got = store.get_draft(d.id)
    assert got is not None
    assert got.state == "abandoned"
    assert got.metadata["abandon_reason"] == "author reconsidered"


def test_list_drafts_filters_by_state(store: StateStore):
    a = store.create_draft(kind="decision", scope="cross-repo", path=None, content="a")
    b = store.create_draft(kind="decision", scope="cross-repo", path=None, content="b")
    store.abandon(a.id, "x")
    remaining = store.list_drafts(state="drafting")
    ids = {d.id for d in remaining}
    assert b.id in ids
    assert a.id not in ids
