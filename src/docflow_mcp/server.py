"""MCP tool surface for agent-authored documentation.

Eleven tools across read, write (staged), review, commit, and admin concerns.
State-machine workflow:
    draft -> (prepare_review -> submit_review -> revise -> ...) -> commit | escalate | abandon

docflow-mcp does not call any LLM. The review step is split in two:
    - `prepare_review(draft_id)` returns a self-contained bundle the caller
      hands to its own sub-agent spawner (e.g. myllm.spawn_agent).
    - `submit_review(draft_id, verdict, ...)` records the sub-agent's verdict
      so the commit gate can enforce it.

This keeps docflow a pure state/storage layer with no HTTP client, no LLM
profile assumptions, and no circular tool dependencies with the reviewer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .committer import Committer
from .config import Config
from .plane_stale import open_stale_issue
from .reader import DocReader
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
_committer: Committer | None = None


def _all_decisions_dirs(cfg: Config) -> list[Path]:
    """All known `docs/decisions/` dirs — docs_root plus every mapped scope.

    Used to enforce a global ADR counter: the next ADR number is the max
    over every scope's decisions directory, not just the target one.
    """
    dirs: list[Path] = [cfg.docs_root / "docs" / "decisions"]
    for repo in cfg.scope_map.values():
        dirs.append(repo / "docs" / "decisions")
    return dirs


def _init() -> tuple[Config, StateStore, DocReader, Committer]:
    global _cfg, _store, _reader, _committer
    if _cfg is None:
        _cfg = Config.from_env()
    if _store is None:
        _store = StateStore(_cfg.state_dir)
    if _reader is None:
        _reader = DocReader(_cfg.docs_root)
    if _committer is None:
        _committer = Committer()
    return _cfg, _store, _reader, _committer


def _prompt_for_kind(prompts_dir: Path, kind: str) -> tuple[str, str]:
    """Return (prompt_text, prompt_hash) for a given draft kind."""
    path = prompts_dir / f"{kind}.md"
    if not path.is_file():
        raise FileNotFoundError(f"No reviewer prompt for kind '{kind}' at {path}")
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return text, digest


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
    _, _, reader, _ = _init()
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
    _, _, reader, _ = _init()
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
    _, _, reader, _ = _init()
    rows = reader.list(category=category, changed_since_days=changed_since_days)
    if not rows:
        return "No matching docs."
    return "\n".join(f"{r['path']}  ({r['modified']}, {r['size']}B)" for r in rows)


@mcp.tool
def recent(limit: int = 10) -> str:
    """Recent commits that touched any docs file. Useful for "what changed lately?"."""
    _, _, reader, _ = _init()
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
    cfg, store, _, _ = _init()
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
            "next_step": (
                "Call `prepare_review(draft_id='" + d.id + "')` to get a "
                "reviewer bundle, spawn your reviewer sub-agent with it, "
                "then call `submit_review(...)` with the verdict."
            ),
        },
        indent=2,
    )


@mcp.tool
def prepare_review(draft_id: str) -> str:
    """Return a self-contained review bundle for this draft's current iteration.

    The caller (author agent) passes the returned `system_prompt` and `task`
    to its own sub-agent spawner (e.g. `myllm.spawn_agent` with the returned
    `working_dir`). The sub-agent emits a YAML verdict. The caller then
    submits that verdict via `submit_review`.

    docflow does NOT call any LLM — it just assembles the review package.

    Returns JSON with:
        kind               draft kind (decision / section / stale)
        iteration          iteration being reviewed (0-indexed)
        system_prompt      prompt text the reviewer's system role should get
        prompt_hash        sha256 prefix of the prompt — pass to submit_review
                           so we can detect reviews against stale prompts later
        working_dir        absolute path the reviewer's file tools should be
                           sandboxed to (docs_root; reviewer can read any doc)
        task               formatted user-role message for the reviewer, with
                           draft content + context inline
        suggested_profile  suggestion string only — reviewer profile is
                           caller's choice
    """
    cfg, store, reader, _ = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state in ("committed", "abandoned", "escalated"):
        return f"ERROR: draft is '{d.state}' and cannot be reviewed."
    if d.iteration >= cfg.max_iterations:
        return json.dumps(
            {
                "error": "max_iterations_exceeded",
                "iteration": d.iteration,
                "max": cfg.max_iterations,
                "next_step": (
                    f"Call `escalate(draft_id='{draft_id}', reason=...)` — this"
                    " draft has consumed its revise budget."
                ),
            },
            indent=2,
        )

    content = store.get_content(draft_id)
    try:
        prompt_text, prompt_hash = _prompt_for_kind(cfg.prompts_dir, d.kind)
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    task_parts = [
        f"# Review request — kind: {d.kind}",
        "",
        f"Draft id: `{draft_id}`  ·  iteration: {d.iteration}  ·  scope: {d.scope}",
        "",
        "## Draft content",
        "",
        content,
        "",
    ]
    if d.path:
        task_parts.extend(["## Target path (relative to scope repo)", d.path, ""])
    reason = (d.metadata or {}).get("reason")
    if reason:
        task_parts.extend(["## Author's reason", reason, ""])
    if d.kind == "section" and d.path:
        try:
            existing = reader.read(d.path)
            task_parts.extend(
                [
                    "## Existing section content (the doc as it stands on disk)",
                    existing,
                    "",
                ]
            )
        except (FileNotFoundError, ValueError):
            pass
    task_parts.extend(
        [
            "## Reviewer working directory",
            f"{cfg.docs_root}",
            "",
            "Use your read_file / list_files / grep tools to explore related",
            "docs if needed. Your working directory is sandboxed to this path;",
            "code-graph claims should be treated with skepticism unless the",
            "author embedded verification results above.",
            "",
            "Output your review strictly as the YAML block specified in your",
            "system prompt. No prose outside the YAML block.",
        ]
    )

    return json.dumps(
        {
            "draft_id": draft_id,
            "iteration": d.iteration,
            "kind": d.kind,
            "system_prompt": prompt_text,
            "prompt_hash": prompt_hash,
            "working_dir": str(cfg.docs_root),
            "task": "\n".join(task_parts),
            "suggested_profile": "docs-reviewer",
            "next_step": (
                f"Pass system_prompt + task to your sub-agent spawner "
                f"(e.g. myllm.spawn_agent). Then call "
                f"`submit_review(draft_id='{draft_id}', verdict=..., issues=..., "
                f"notes=..., reviewer_model=..., prompt_hash='{prompt_hash}')`."
            ),
        },
        indent=2,
    )


@mcp.tool
def submit_review(
    draft_id: str,
    verdict: str,
    issues: list[dict] | None = None,
    notes: str | None = None,
    reviewer_model: str | None = None,
    prompt_hash: str | None = None,
) -> str:
    """Record an externally-produced review verdict for this draft's iteration.

    Machine-checked: only `approve`, `revise`, or `escalate` are accepted.
    The commit gate consults the latest review for the current iteration;
    `approve` unlocks commit, anything else does not.

    Args:
        draft_id: the draft being reviewed.
        verdict: one of "approve", "revise", "escalate".
        issues: structured issue list from the reviewer (optional for approve).
        notes: freeform notes from the reviewer.
        reviewer_model: the model name that produced this review, for audit.
        prompt_hash: the prompt_hash returned by prepare_review; recorded so
                     future reruns can detect prompts that have since changed.
    """
    cfg, store, _, _ = _init()
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.state in ("committed", "abandoned", "escalated"):
        return f"ERROR: draft is '{d.state}'; cannot record a new review."
    if verdict not in ("approve", "revise", "escalate"):
        return (
            f"ERROR: verdict must be one of approve / revise / escalate, "
            f"got '{verdict}'."
        )
    if d.iteration >= cfg.max_iterations and verdict != "escalate":
        return (
            f"ERROR: draft has reached max_iterations ({cfg.max_iterations}); "
            "only an escalate verdict is accepted from here."
        )

    clean_issues = list(issues or [])
    store.record_review(
        draft_id=draft_id,
        iteration=d.iteration,
        verdict=verdict,  # type: ignore[arg-type]
        issues=clean_issues,
        notes=notes,
        reviewer_model=reviewer_model,
        reviewer_prompt_hash=prompt_hash,
    )
    store.mark_reviewed(draft_id)
    return json.dumps(
        {
            "draft_id": draft_id,
            "iteration": d.iteration,
            "verdict": verdict,
            "issues_count": len(clean_issues),
            "next_step": {
                "approve": f"Call `commit(draft_id='{draft_id}')`.",
                "revise": f"Call `revise(draft_id='{draft_id}', content=...)` and re-review.",
                "escalate": f"Call `escalate(draft_id='{draft_id}', reason=...)`.",
            }[verdict],
        },
        indent=2,
    )


@mcp.tool
def revise(draft_id: str, content: str) -> str:
    """Submit a revised version of a draft. Requires a prior review.

    Increments iteration, resets state to 'drafting'. Caller should then
    call `review(draft_id)` again.
    """
    _, store, _, _ = _init()
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
    cfg, store, _, committer = _init()
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
        target = resolve_decision_path(
            scope_repo, title, number_sources=_all_decisions_dirs(cfg)
        )
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
    cfg, store, _, committer = _init()
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
            target = resolve_decision_path(
                scope_repo, title, number_sources=_all_decisions_dirs(cfg)
            )
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
    _, store, _, _ = _init()
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
    _, store, _, _ = _init()
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
