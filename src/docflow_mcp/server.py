"""MCP tool surface for agent-authored documentation.

Eleven tools spanning read, write (staged), review, commit, and admin concerns.
The workflow is state-machine enforced:
    draft -> (review -> revise -> review -> ...) -> commit | escalate | abandon
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .committer import Committer
from .config import Config
from .plane_stale import open_stale_issue
from .reader import DocReader
from .reviewer import Reviewer
from .scope import (
    extract_title,
    resolve_decision_path,
    resolve_section_path,
    slugify,
)
from .state import StateStore

mcp = FastMCP("docflow-mcp")

# Lazy-initialized singletons (populated on first tool call).
_cfg: Config | None = None
_store: StateStore | None = None
_reader: DocReader | None = None
_reviewer: Reviewer | None = None
_committer: Committer | None = None


def _init() -> tuple[Config, StateStore, DocReader, Reviewer, Committer]:
    global _cfg, _store, _reader, _reviewer, _committer
    if _cfg is None:
        _cfg = Config.from_env()
    if _store is None:
        _store = StateStore(_cfg.state_dir)
    if _reader is None:
        _reader = DocReader(_cfg.docs_root)
    if _reviewer is None:
        _reviewer = Reviewer(
            gateway_url=_cfg.reviewer_url,
            profile=_cfg.reviewer_profile,
            prompts_dir=_cfg.prompts_dir,
            timeout=_cfg.review_timeout,
        )
    if _committer is None:
        _committer = Committer()
    return _cfg, _store, _reader, _reviewer, _committer


# ─────────────────────────────────────────────────────────────────
# Read-side tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def search(query: str, category: str | None = None, limit: int = 25) -> str:
    """Full-text search over docs. Returns ranked hits with path, line, heading, snippet.

    Args:
        query: search terms
        category: optional subdir under docs/ to scope to (e.g. "decisions", "contracts")
        limit: max hits to return (default 25)
    """
    _, _, reader, _, _ = _init()
    hits = reader.search(query, category=category, limit=limit)
    if not hits:
        return "No results."
    lines = []
    for h in hits:
        heading = f" — {h.heading}" if h.heading else ""
        lines.append(f"{h.path}:{h.line}{heading}\n  {h.snippet}")
    return "\n".join(lines)


@mcp.tool
def read(path: str, section: str | None = None) -> str:
    """Read a doc file, optionally scoped to one heading.

    Args:
        path: relative path under docs_root (e.g. "docs/decisions/0003-foo.md")
        section: optional heading text; returns only that section's body
    """
    _, _, reader, _, _ = _init()
    try:
        return reader.read(path, section=section)
    except (FileNotFoundError, LookupError, ValueError) as e:
        return f"ERROR: {e}"


@mcp.tool
def list_docs(category: str | None = None, changed_since_days: int | None = None) -> str:
    """List markdown docs, optionally filtered by subcategory and recency.

    Args:
        category: subdir under docs/ (e.g. "decisions", "contracts")
        changed_since_days: only include files modified within the last N days
    """
    _, _, reader, _, _ = _init()
    rows = reader.list(category=category, changed_since_days=changed_since_days)
    if not rows:
        return "No matching docs."
    return "\n".join(f"{r['path']}  ({r['modified']}, {r['size']}B)" for r in rows)


@mcp.tool
def recent(limit: int = 10) -> str:
    """Recent commits that touched any docs file. Useful for "what changed lately?"."""
    _, _, reader, _, _ = _init()
    rows = reader.recent(limit=limit)
    if not rows:
        return "No recent doc commits."
    return "\n".join(f"{r['sha']}  {r['date']}  {r['author']}: {r['subject']}" for r in rows)


# ─────────────────────────────────────────────────────────────────
# Write-side tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def draft(
    kind: str,
    scope: str,
    content: str,
    path: str | None = None,
    reason: str | None = None,
) -> str:
    """Stage a new draft. Returns a draft_id for subsequent review/revise/commit calls.

    Args:
        kind: one of "decision", "section", "stale"
        scope: target repo scope ("cross-repo" or a name from DOCS_SCOPE_MAP)
        content: draft content (full ADR text, new section body, or stale-flag report)
        path: for kind="section", the relative path under the scope repo. Required
              for "section", ignored for "decision" (auto-routed) and "stale".
        reason: free-text justification, recommended for "section" and "stale".
    """
    cfg, store, _, _, _ = _init()
    if kind not in ("decision", "section", "stale"):
        return f"ERROR: unknown kind '{kind}'. Use 'decision', 'section', or 'stale'."
    try:
        cfg.resolve_scope(scope)
    except ValueError as e:
        return f"ERROR: {e}"
    if kind == "section" and not path:
        return "ERROR: kind='section' requires a `path` argument (file to update)."

    metadata: dict[str, Any] = {}
    if reason:
        metadata["reason"] = reason

    d = store.create_draft(
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        path=path,
        content=content,
        metadata=metadata,
    )
    return json.dumps(
        {
            "draft_id": d.id,
            "kind": d.kind,
            "scope": d.scope,
            "path": d.path,
            "iteration": d.iteration,
            "state": d.state,
            "next_step": "Call `review(draft_id='" + d.id + "')` before committing.",
        },
        indent=2,
    )


@mcp.tool
def review(draft_id: str) -> str:
    """Run the reviewer sub-agent against the draft's current iteration.

    The reviewer uses a separate model (configured in MyLLM) with its own MCP
    stack so it can verify claims against code and existing docs. Returns the
    parsed verdict plus structured issue list.
    """
    cfg, store, reader, reviewer, _ = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state in ("committed", "abandoned", "escalated"):
        return f"ERROR: draft is '{d.state}' and cannot be reviewed again."

    if d.iteration >= cfg.max_iterations:
        store.mark_escalated(
            draft_id,
            reason=f"Auto-escalated after {cfg.max_iterations} iterations.",
        )
        return json.dumps(
            {
                "verdict": "escalate",
                "reason": "max_iterations_exceeded",
                "iteration": d.iteration,
                "max": cfg.max_iterations,
            },
            indent=2,
        )

    content = store.get_content(draft_id)

    context: dict[str, Any] = {
        "scope": d.scope,
        "path": d.path,
        "docs_root": str(cfg.docs_root),
    }
    if d.kind == "section" and d.path:
        try:
            context["old_section"] = reader.read(d.path)
        except (FileNotFoundError, ValueError):
            pass
    reason = (d.metadata or {}).get("reason")
    if reason:
        context["reason"] = reason

    try:
        result = reviewer.review(kind=d.kind, content=content, context=context)
    except Exception as e:
        return f"ERROR: reviewer call failed: {e}"

    store.record_review(
        draft_id=draft_id,
        iteration=d.iteration,
        verdict=result.verdict,  # type: ignore[arg-type]
        issues=result.issues,
        notes=result.notes,
        reviewer_model=result.reviewer_model,
        reviewer_prompt_hash=result.prompt_hash,
    )
    store.mark_reviewed(draft_id)

    return json.dumps(
        {
            "draft_id": draft_id,
            "iteration": d.iteration,
            "verdict": result.verdict,
            "issues": result.issues,
            "notes": result.notes,
            "reviewer_model": result.reviewer_model,
            "prompt_hash": result.prompt_hash,
        },
        indent=2,
    )


@mcp.tool
def revise(draft_id: str, content: str) -> str:
    """Submit a revised version of a draft. Requires a prior review.

    Increments iteration, resets state to 'drafting'. Caller should then
    call `review(draft_id)` again.
    """
    _, store, _, _, _ = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    try:
        new_d = store.revise_draft(draft_id, content)
    except (LookupError, ValueError) as e:
        return f"ERROR: {e}"
    return json.dumps(
        {
            "draft_id": new_d.id,
            "iteration": new_d.iteration,
            "state": new_d.state,
            "next_step": f"Call `review(draft_id='{new_d.id}')` again.",
        },
        indent=2,
    )


@mcp.tool
def commit(draft_id: str) -> str:
    """Commit a draft to its scope repository. Machine-checked: the most
    recent review for this iteration must have verdict='approve'.

    `decision` drafts write to `docs/decisions/NNNN-<slug>.md`, auto-numbered.
    `section` drafts rewrite the whole target file with the new content.
    `stale` drafts do not commit — use `escalate` to produce a Plane issue.
    """
    cfg, store, _, _, committer = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state == "committed":
        return f"ERROR: draft '{draft_id}' is already committed."
    if d.kind == "stale":
        return (
            "ERROR: stale-kind drafts are not committed to disk. "
            "Use `escalate` to file the Plane issue instead."
        )

    latest = store.review_for_iteration(draft_id, d.iteration)
    if latest is None:
        return (
            f"ERROR: draft '{draft_id}' iteration {d.iteration} has no review. "
            "Call `review(draft_id)` first."
        )
    if latest.verdict != "approve":
        return (
            f"ERROR: most recent review returned verdict='{latest.verdict}'. "
            "Only 'approve' verdicts can commit. Revise and re-review, or escalate."
        )

    try:
        scope_repo = cfg.resolve_scope(d.scope)
    except ValueError as e:
        return f"ERROR: {e}"

    content = store.get_content(draft_id)
    if d.kind == "decision":
        title = extract_title(content)
        target = resolve_decision_path(scope_repo, title)
        msg = f"docs: add ADR — {title}"
    elif d.kind == "section":
        if not d.path:
            return "ERROR: section draft has no target path."
        try:
            target = resolve_section_path(scope_repo, d.path)
        except (FileNotFoundError, ValueError) as e:
            return f"ERROR: {e}"
        reason = (d.metadata or {}).get("reason", "section update")
        msg = f"docs: update {d.path} — {reason}"
    else:
        return f"ERROR: unsupported kind for commit: {d.kind}"

    try:
        result = committer.commit_direct(
            repo=scope_repo, target_path=target, content=content, message=msg
        )
    except Exception as e:
        return f"ERROR committing: {e}"

    store.mark_committed(
        draft_id=draft_id,
        target_path=result.target_path,
        branch=result.branch,
        sha=result.sha,
    )

    return json.dumps(
        {
            "draft_id": draft_id,
            "state": "committed",
            "scope": d.scope,
            "target": result.target_path,
            "sha": result.sha,
            "branch": result.branch,
        },
        indent=2,
    )


@mcp.tool
def escalate(draft_id: str, reason: str) -> str:
    """Mark a draft as requiring human review.

    For `decision`/`section` drafts: opens a draft PR on a new branch
    (`docs/agent-<draft_id>`) with the draft content. Humans review the PR.

    For `stale` drafts: opens a Plane issue (if DOCS_PLANE_STALE_PROJECT
    is configured) with the flag report. Otherwise returns the report text.
    """
    cfg, store, _, _, committer = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state in ("committed", "abandoned"):
        return f"ERROR: draft is '{d.state}' and cannot be escalated."

    content = store.get_content(draft_id)
    response: dict[str, Any] = {"draft_id": draft_id, "reason": reason}

    if d.kind == "stale":
        title_line = next(
            (line.strip() for line in content.splitlines() if line.strip()), "stale-flag"
        )
        title = f"[docs-stale] {title_line[:80]}"
        body = (
            f"**Agent-flagged staleness**\n\nReason: {reason}\n\n"
            f"Path: {d.path or 'n/a'}\nScope: {d.scope}\n\n---\n\n{content}"
        )
        url = open_stale_issue(title=title, body=body, project_id=cfg.plane_stale_project)
        response["plane_issue"] = url or "Plane not configured; no issue created."
    else:
        try:
            scope_repo = cfg.resolve_scope(d.scope)
        except ValueError as e:
            return f"ERROR: {e}"

        branch = f"docs/agent-{draft_id}"
        if d.kind == "decision":
            title = extract_title(content)
            target = resolve_decision_path(scope_repo, title)
            msg = f"docs: escalated ADR draft — {title}"
            pr_title = f"[docs-escalated] ADR: {title}"
        else:  # section
            if not d.path:
                return "ERROR: section draft has no target path."
            try:
                target = resolve_section_path(scope_repo, d.path)
            except (FileNotFoundError, ValueError) as e:
                return f"ERROR: {e}"
            msg = f"docs: escalated section update — {d.path}"
            pr_title = f"[docs-escalated] section: {d.path}"

        try:
            result = committer.commit_on_branch(
                repo=scope_repo, target_path=target, content=content,
                message=msg, branch=branch,
            )
        except Exception as e:
            return f"ERROR preparing branch: {e}"

        pr_body = (
            f"Agent-authored draft requiring human review.\n\n"
            f"**Reason for escalation:** {reason}\n\n"
            f"Draft id: `{draft_id}`  \nIterations: {d.iteration + 1}  \n"
            f"Scope: {d.scope}\n"
        )
        pr_url = committer.open_draft_pr(
            repo=scope_repo, branch=branch, title=pr_title, body=pr_body
        )
        response["branch"] = result.branch
        response["sha"] = result.sha
        response["pr"] = pr_url or "gh CLI unavailable; branch pushed locally."

    store.mark_escalated(draft_id, reason=reason)
    response["state"] = "escalated"
    return json.dumps(response, indent=2)


# ─────────────────────────────────────────────────────────────────
# Admin tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def status(draft_id: str | None = None, state: str | None = None) -> str:
    """Inspect one draft or list drafts.

    With draft_id: returns full state + review history.
    With state filter: returns list of drafts in that state.
    With neither: returns the 20 most recent drafts across all states.
    """
    _, store, _, _, _ = _init()
    if draft_id:
        d = store.get_draft(draft_id)
        if d is None:
            return f"ERROR: no draft '{draft_id}'"
        reviews = store.all_reviews(draft_id)
        return json.dumps(
            {
                "draft": asdict(d),
                "reviews": [asdict(r) for r in reviews],
            },
            indent=2,
            default=str,
        )
    drafts = store.list_drafts(state=state)  # type: ignore[arg-type]
    return json.dumps(
        [
            {
                "id": d.id,
                "kind": d.kind,
                "scope": d.scope,
                "state": d.state,
                "iteration": d.iteration,
                "updated_at": d.updated_at,
            }
            for d in drafts
        ],
        indent=2,
    )


@mcp.tool
def abandon(draft_id: str, reason: str) -> str:
    """Abandon a draft. It stays in the DB for audit until GC after 7 days."""
    _, store, _, _, _ = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state in ("committed", "escalated"):
        return f"ERROR: cannot abandon a '{d.state}' draft."
    store.abandon(draft_id, reason=reason)
    return json.dumps({"draft_id": draft_id, "state": "abandoned", "reason": reason})


# ─────────────────────────────────────────────────────────────────
# Unused slug import guard (kept for IDE integration tests)
# ─────────────────────────────────────────────────────────────────
_ = slugify  # re-export for tests


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
