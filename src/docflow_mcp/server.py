"""MCP tool surface for agent-authored documentation.

Eleven tools across read, write (staged), review, commit, and admin concerns.
State-machine workflow:
    draft -> (prepare_review -> submit_review -> revise -> ...) -> commit | escalate | abandon

docflow-mcp does not call any LLM. The review step is split in two:
    - `prepare_review(collection, draft_id)` returns a self-contained bundle
      the caller hands to its own sub-agent spawner (e.g. myllm.spawn_agent).
    - `submit_review(collection, draft_id, verdict, ...)` records the
      sub-agent's verdict so the commit gate can enforce it.

Multi-collection: every tool takes a `collection` parameter naming one of
the collections configured in DOCFLOW_CONFIG_FILE. The legacy single-collection
env (DOCS_ROOT) still works — it maps to a collection named "default".

Transports: the server runs over stdio by default. Pass `--http` to
run as a shared Streamable HTTP daemon (see docflow_mcp.__main__).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from .committer import Committer
from .config import CollectionConfig, Config
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
_committer: Committer | None = None
_readers: dict[str, DocReader] = {}


def _init() -> tuple[Config, StateStore, Committer]:
    global _cfg, _store, _committer
    if _cfg is None:
        _cfg = Config.from_env()
        # Validate on first use so daemon startup fails fast on bad paths.
        # Warnings are surfaced via logs and list_collections output, not errors.
        warnings = _cfg.validate()
        for w in warnings:
            print(f"[docflow config] WARN: {w}")
    if _store is None:
        _store = StateStore(_cfg.state_dir)
    if _committer is None:
        _committer = Committer()
    return _cfg, _store, _committer


def _reader_for(cfg: Config, collection: str) -> tuple[CollectionConfig, DocReader]:
    """Resolve a collection + cached reader for it, or raise ValueError."""
    coll = cfg.resolve_collection(collection)
    if collection not in _readers:
        _readers[collection] = DocReader(coll.docs_root)
    return coll, _readers[collection]


def _all_decisions_dirs(cfg: Config) -> list[Path]:
    """Every `docs/decisions/` dir across every collection.

    Used to enforce a global ADR counter — next ADR number is the max over
    every collection + every scope within each collection, guaranteeing
    uniqueness across the entire docflow install.
    """
    dirs: list[Path] = []
    for coll in cfg.collections.values():
        dirs.append(coll.docs_root / "docs" / "decisions")
        for repo in coll.scope_map.values():
            dirs.append(repo / "docs" / "decisions")
    return dirs


def _prompt_for_kind(prompts_dir: Path, kind: str) -> tuple[str, str]:
    path = prompts_dir / f"{kind}.md"
    if not path.is_file():
        raise FileNotFoundError(f"No reviewer prompt for kind '{kind}' at {path}")
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return text, digest


_COLLECTION_HEADER = "x-docflow-collection"


def _header_collection() -> str | None:
    """Return the X-Docflow-Collection header value, if any.

    Safe to call without an active HTTP request (returns None). stdio clients
    always see None here, which means they must pass `collection=` explicitly.
    """
    headers = get_http_headers()
    # HTTP header names are case-insensitive; fastmcp normalizes to lowercase.
    return headers.get(_COLLECTION_HEADER) or None


def _resolve_collection(collection: str | None) -> str:
    """Resolve the collection for a tool call.

    Precedence: explicit argument > X-Docflow-Collection header.
    Raises ValueError when neither source supplies a value.
    """
    if collection:
        return collection
    header_default = _header_collection()
    if header_default:
        return header_default
    raise ValueError(
        "collection is required — pass as a tool argument or configure the "
        "X-Docflow-Collection header on this client's MCP registration "
        "(see docflow README for setup)"
    )


# ─────────────────────────────────────────────────────────────────
# Read-side tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def search(
    query: str,
    collection: str | None = None,
    category: str | None = None,
    limit: int = 25,
) -> str:
    """Full-text search over one collection's docs.

    Args:
        query: search terms.
        collection: which collection to search. Falls back to the
            X-Docflow-Collection header if not passed.
        category: optional subdir under docs/ (e.g. "decisions").
        limit: max hits.
    """
    cfg, _, _ = _init()
    try:
        collection = _resolve_collection(collection)
        _, reader = _reader_for(cfg, collection)
    except ValueError as e:
        return f"ERROR: {e}"
    hits = reader.search(query, category=category, limit=limit)
    if not hits:
        return "No results."
    return "\n".join(
        f"{h.path}:{h.line}{f' — {h.heading}' if h.heading else ''}\n  {h.snippet}"
        for h in hits
    )


@mcp.tool
def read(
    path: str, collection: str | None = None, section: str | None = None
) -> str:
    """Read a doc file in a collection, optionally scoped to one heading.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, _, _ = _init()
    try:
        collection = _resolve_collection(collection)
        _, reader = _reader_for(cfg, collection)
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        return reader.read(path, section=section)
    except (FileNotFoundError, LookupError, ValueError) as e:
        return f"ERROR: {e}"


@mcp.tool
def list_docs(
    collection: str | None = None,
    category: str | None = None,
    changed_since_days: int | None = None,
) -> str:
    """List markdown docs in a collection, optionally filtered.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, _, _ = _init()
    try:
        collection = _resolve_collection(collection)
        _, reader = _reader_for(cfg, collection)
    except ValueError as e:
        return f"ERROR: {e}"
    rows = reader.list(category=category, changed_since_days=changed_since_days)
    if not rows:
        return "No matching docs."
    return "\n".join(f"{r['path']}  ({r['modified']}, {r['size']}B)" for r in rows)


@mcp.tool
def recent(collection: str | None = None, limit: int = 10) -> str:
    """Recent commits that touched any docs file in this collection.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, _, _ = _init()
    try:
        collection = _resolve_collection(collection)
        _, reader = _reader_for(cfg, collection)
    except ValueError as e:
        return f"ERROR: {e}"
    rows = reader.recent(limit=limit)
    if not rows:
        return "No recent doc commits."
    return "\n".join(
        f"{r['sha']}  {r['date']}  {r['author']}: {r['subject']}" for r in rows
    )


@mcp.tool
def list_collections() -> str:
    """List every configured collection with its docs_root and scope map.

    Marks the current client's default collection (from the
    X-Docflow-Collection header) with an asterisk.
    """
    cfg, _, _ = _init()
    if not cfg.collections:
        return "No collections configured."
    default = _header_collection()
    lines = []
    for name in sorted(cfg.collections):
        c = cfg.collections[name]
        star = " *" if name == default else ""
        lines.append(f"{name}  (git: {c.git_mode}){star}")
        lines.append(f"  docs_root: {c.docs_root}")
        if c.scope_map:
            lines.append("  scopes:")
            for s, p in sorted(c.scope_map.items()):
                lines.append(f"    {s} -> {p}")
    if default:
        lines.append("")
        lines.append(
            f"* = default collection for this client (via X-Docflow-Collection "
            f"header = '{default}'). Pass `collection=` explicitly to override."
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# Write-side tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def draft(
    kind: str,
    scope: str,
    content: str,
    collection: str | None = None,
    path: str | None = None,
    reason: str | None = None,
) -> str:
    """Stage a new draft in a collection.

    Args:
        kind: "decision", "section", or "stale".
        scope: "cross-repo" or a name from the collection's scope_map.
        content: the draft body.
        collection: which collection to draft in. Falls back to the
            X-Docflow-Collection header.
        path: required for kind="section".
        reason: recommended for "section" and "stale".
    """
    cfg, store, _ = _init()
    if kind not in ("decision", "section", "stale"):
        return f"ERROR: unknown kind '{kind}'. Use 'decision', 'section', or 'stale'."
    try:
        collection = _resolve_collection(collection)
        coll = cfg.resolve_collection(collection)
        coll.resolve_scope(scope)
    except ValueError as e:
        return f"ERROR: {e}"
    if kind == "section" and not path:
        return "ERROR: kind='section' requires a `path` argument."

    metadata: dict[str, Any] = {}
    if reason:
        metadata["reason"] = reason

    d = store.create_draft(
        collection=collection,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        path=path,
        content=content,
        metadata=metadata,
    )
    return json.dumps(
        {
            "draft_id": d.id,
            "collection": d.collection,
            "kind": d.kind,
            "scope": d.scope,
            "path": d.path,
            "iteration": d.iteration,
            "state": d.state,
            "next_step": (
                f"Call `prepare_review(collection='{collection}', draft_id='{d.id}')`, "
                f"spawn your reviewer sub-agent with the returned bundle, then "
                f"`submit_review(...)` with the verdict."
            ),
        },
        indent=2,
    )


@mcp.tool
def prepare_review(draft_id: str, collection: str | None = None) -> str:
    """Return a self-contained review bundle for a draft.

    Output is JSON with: system_prompt, prompt_hash, working_dir, task,
    suggested_profile. Caller hands system_prompt + task to its own
    sub-agent spawner (e.g. myllm.spawn_agent) with working_dir pointing
    at the collection's docs_root.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, store, _ = _init()
    try:
        collection = _resolve_collection(collection)
        coll, reader = _reader_for(cfg, collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return (
            f"ERROR: draft '{draft_id}' belongs to collection '{d.collection}', "
            f"not '{collection}'."
        )
    if d.state in ("committed", "abandoned", "escalated"):
        return f"ERROR: draft is '{d.state}' and cannot be reviewed."
    if d.iteration >= cfg.max_iterations:
        return json.dumps(
            {
                "error": "max_iterations_exceeded",
                "iteration": d.iteration,
                "max": cfg.max_iterations,
                "next_step": (
                    f"Call `escalate(collection='{collection}', "
                    f"draft_id='{draft_id}', reason=...)` — revise budget spent."
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
        f"Collection: `{collection}`  ·  draft_id: `{draft_id}`  ·  "
        f"iteration: {d.iteration}  ·  scope: {d.scope}",
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
            f"{coll.docs_root}",
            "",
            "Use your read_file / list_files / grep tools to explore related",
            "docs if needed. Your working directory is sandboxed to this path.",
            "Code-graph claims should be treated with skepticism unless the",
            "author embedded verification results above.",
            "",
            "Output your review strictly as the YAML block specified in your",
            "system prompt. No prose outside the YAML block.",
        ]
    )

    return json.dumps(
        {
            "draft_id": draft_id,
            "collection": collection,
            "iteration": d.iteration,
            "kind": d.kind,
            "system_prompt": prompt_text,
            "prompt_hash": prompt_hash,
            "working_dir": str(coll.docs_root),
            "task": "\n".join(task_parts),
            "suggested_profile": "docs-reviewer",
            "next_step": (
                f"Pass system_prompt + task to your sub-agent spawner with "
                f"working_dir='{coll.docs_root}'. Then call "
                f"`submit_review(collection='{collection}', draft_id='{draft_id}', "
                f"verdict=..., issues=..., notes=..., reviewer_model=..., "
                f"prompt_hash='{prompt_hash}')`."
            ),
        },
        indent=2,
    )


@mcp.tool
def submit_review(
    draft_id: str,
    verdict: str,
    collection: str | None = None,
    issues: list[dict] | None = None,
    notes: str | None = None,
    reviewer_model: str | None = None,
    prompt_hash: str | None = None,
) -> str:
    """Record an externally-produced review verdict.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, store, _ = _init()
    try:
        collection = _resolve_collection(collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return (
            f"ERROR: draft '{draft_id}' belongs to collection '{d.collection}', "
            f"not '{collection}'."
        )
    if d.state in ("committed", "abandoned", "escalated"):
        return f"ERROR: draft is '{d.state}'; cannot record a new review."
    if verdict not in ("approve", "revise", "escalate"):
        return (
            f"ERROR: verdict must be approve / revise / escalate, got '{verdict}'."
        )
    if d.iteration >= cfg.max_iterations and verdict != "escalate":
        return (
            f"ERROR: draft has reached max_iterations ({cfg.max_iterations}); "
            "only escalate is accepted."
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
            "collection": collection,
            "iteration": d.iteration,
            "verdict": verdict,
            "issues_count": len(clean_issues),
            "next_step": {
                "approve": f"Call `commit(collection='{collection}', draft_id='{draft_id}')`.",
                "revise": (
                    f"Call `revise(collection='{collection}', draft_id='{draft_id}', "
                    f"content=...)` and re-review."
                ),
                "escalate": (
                    f"Call `escalate(collection='{collection}', draft_id='{draft_id}', "
                    f"reason=...)`."
                ),
            }[verdict],
        },
        indent=2,
    )


@mcp.tool
def revise(draft_id: str, content: str, collection: str | None = None) -> str:
    """Submit a revised version. Increments iteration, resets to drafting.

    `collection` falls back to the X-Docflow-Collection header.
    """
    _, store, _ = _init()
    try:
        collection = _resolve_collection(collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return f"ERROR: draft is in collection '{d.collection}', not '{collection}'."
    try:
        new_d = store.revise_draft(draft_id, content)
    except (LookupError, ValueError) as e:
        return f"ERROR: {e}"
    return json.dumps(
        {
            "draft_id": new_d.id,
            "collection": new_d.collection,
            "iteration": new_d.iteration,
            "state": new_d.state,
            "next_step": (
                f"Call `prepare_review(collection='{collection}', "
                f"draft_id='{new_d.id}')` again."
            ),
        },
        indent=2,
    )


@mcp.tool
def commit(draft_id: str, collection: str | None = None) -> str:
    """Commit a draft to its scope repository. Gated: latest review must be approve.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, store, committer = _init()
    try:
        collection = _resolve_collection(collection)
        coll = cfg.resolve_collection(collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return f"ERROR: draft is in collection '{d.collection}', not '{collection}'."
    if d.state == "committed":
        return f"ERROR: draft '{draft_id}' is already committed."
    if d.kind == "stale":
        return (
            "ERROR: stale-kind drafts are not committed. Use `escalate` to file "
            "a Plane issue."
        )

    latest = store.review_for_iteration(draft_id, d.iteration)
    if latest is None:
        return (
            f"ERROR: iteration {d.iteration} has no review. Call "
            f"`prepare_review` / `submit_review` first."
        )
    if latest.verdict != "approve":
        return (
            f"ERROR: most recent review verdict is '{latest.verdict}'. "
            "Only 'approve' unlocks commit."
        )

    try:
        scope_repo = coll.resolve_scope(d.scope)
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
            repo=scope_repo, target_path=target, content=content, message=msg,
            git_mode=coll.git_mode,
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
            "collection": collection,
            "state": "committed",
            "scope": d.scope,
            "target": result.target_path,
            "sha": result.sha,
            "branch": result.branch,
        },
        indent=2,
    )


@mcp.tool
def escalate(draft_id: str, reason: str, collection: str | None = None) -> str:
    """Mark a draft as requiring human review.

    `collection` falls back to the X-Docflow-Collection header.
    """
    cfg, store, committer = _init()
    try:
        collection = _resolve_collection(collection)
        coll = cfg.resolve_collection(collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return f"ERROR: draft is in collection '{d.collection}', not '{collection}'."
    if d.state in ("committed", "abandoned"):
        return f"ERROR: draft is '{d.state}' and cannot be escalated."

    content = store.get_content(draft_id)
    response: dict[str, Any] = {
        "draft_id": draft_id, "collection": collection, "reason": reason
    }

    # Stale-kind drafts go to Plane, not git. No git_mode dependency.
    if d.kind == "stale":
        title_line = next(
            (line.strip() for line in content.splitlines() if line.strip()),
            "stale-flag",
        )
        title = f"[docs-stale] {title_line[:80]}"
        body = (
            f"**Agent-flagged staleness**\n\nCollection: {collection}\n"
            f"Path: {d.path or 'n/a'}\nScope: {d.scope}\n\nReason: {reason}\n\n"
            f"---\n\n{content}"
        )
        url = open_stale_issue(title=title, body=body, project_id=cfg.plane_stale_project)
        response["plane_issue"] = url or "Plane not configured; no issue created."
        store.mark_escalated(draft_id, reason=reason)
        response["state"] = "escalated"
        return json.dumps(response, indent=2)

    # Decision / section escalations create a branch + commit, then push + PR.
    # The three stages are recorded in the escalations table so retries skip
    # work that's already done.

    if coll.git_mode == "disabled":
        return (
            f"ERROR: collection '{collection}' has git_mode='disabled'; "
            "escalate requires a git-tracked collection so the draft can "
            "be preserved on a branch for human review."
        )

    try:
        scope_repo = coll.resolve_scope(d.scope)
    except ValueError as e:
        return f"ERROR: {e}"

    if not (scope_repo / ".git").exists():
        return (
            f"ERROR: scope repo {scope_repo} is not a git repository; "
            "escalate requires git."
        )

    existing = store.get_escalation(draft_id)
    branch = existing.branch if existing else f"docs/agent-{draft_id}"

    if d.kind == "decision":
        title = extract_title(content)
        target = resolve_decision_path(
            scope_repo, title, number_sources=_all_decisions_dirs(cfg)
        )
        msg = f"docs: escalated ADR draft — {title}"
        pr_title = f"[docs-escalated] ADR: {title}"
    else:
        if not d.path:
            return "ERROR: section draft has no target path."
        try:
            target = resolve_section_path(scope_repo, d.path)
        except (FileNotFoundError, ValueError) as e:
            return f"ERROR: {e}"
        msg = f"docs: escalated section update — {d.path}"
        pr_title = f"[docs-escalated] section: {d.path}"

    # Stage 1: commit on branch (skip if already done and content unchanged)
    if existing is None or not existing.sha:
        try:
            result = committer.commit_on_branch(
                repo=scope_repo, target_path=target, content=content,
                message=msg, branch=branch, git_mode=coll.git_mode,
            )
        except Exception as e:
            return f"ERROR preparing branch: {e}"
        store.record_escalation_commit(
            draft_id=draft_id, branch=result.branch or branch,
            sha=result.sha, reason=reason,
        )
        response["branch"] = result.branch
        response["sha"] = result.sha
    else:
        response["branch"] = existing.branch
        response["sha"] = existing.sha
        response["note"] = "branch + commit already recorded from prior attempt"

    # Stage 2: push (skip if already pushed OR git_mode=local)
    if coll.git_mode != "remote":
        response["pr"] = "git_mode is 'local'; branch kept locally, no push/PR attempted"
        store.mark_escalated(draft_id, reason=reason)
        response["state"] = "escalated"
        return json.dumps(response, indent=2)

    # git_mode == remote from here on.
    ok_auth, auth_detail = committer.gh_auth_ok()
    if not ok_auth:
        response["push"] = "skipped"
        response["pr"] = f"skipped — gh not authenticated ({auth_detail}). Run `gh auth login`."
        store.mark_escalated(draft_id, reason=reason)
        response["state"] = "escalated"
        return json.dumps(response, indent=2)

    current = store.get_escalation(draft_id)
    if not current or not current.pushed:
        ok_push, push_detail = committer.push_branch(scope_repo, branch)
        if not ok_push:
            response["push"] = push_detail
            store.mark_escalated(draft_id, reason=reason)
            response["state"] = "escalated"
            return json.dumps(response, indent=2)
        store.record_escalation_pushed(draft_id)
        response["push"] = "pushed to origin"
    else:
        response["push"] = "already pushed from prior attempt"

    # Stage 3: open PR (skip if URL already recorded)
    current = store.get_escalation(draft_id)
    if current and current.pr_url:
        response["pr"] = current.pr_url
        response["pr_note"] = "already opened from prior attempt"
    else:
        pr_body = (
            f"Agent-authored draft requiring human review.\n\n"
            f"**Reason:** {reason}\n\n"
            f"Collection: `{collection}`  \nDraft id: `{draft_id}`  \n"
            f"Iterations: {d.iteration + 1}  \nScope: {d.scope}\n"
        )
        ok_pr, pr_detail = committer.open_pr(
            repo=scope_repo, branch=branch, title=pr_title, body=pr_body
        )
        if ok_pr:
            store.record_escalation_pr(draft_id, pr_detail)
            response["pr"] = pr_detail
        else:
            response["pr"] = pr_detail

    store.mark_escalated(draft_id, reason=reason)
    response["state"] = "escalated"
    return json.dumps(response, indent=2)


# ─────────────────────────────────────────────────────────────────
# Admin tools
# ─────────────────────────────────────────────────────────────────


@mcp.tool
def status(
    collection: str | None = None,
    draft_id: str | None = None,
    state: str | None = None,
) -> str:
    """Inspect one draft or list drafts. Filter by collection and/or state."""
    _, store, _ = _init()
    if draft_id:
        d = store.get_draft(draft_id)
        if d is None:
            return f"ERROR: no draft '{draft_id}'"
        reviews = store.all_reviews(draft_id)
        return json.dumps(
            {"draft": asdict(d), "reviews": [asdict(r) for r in reviews]},
            indent=2, default=str,
        )
    drafts = store.list_drafts(collection=collection, state=state)  # type: ignore[arg-type]
    return json.dumps(
        [
            {
                "id": d.id, "collection": d.collection, "kind": d.kind,
                "scope": d.scope, "state": d.state, "iteration": d.iteration,
                "updated_at": d.updated_at,
            }
            for d in drafts
        ],
        indent=2,
    )


@mcp.tool
def abandon(draft_id: str, reason: str, collection: str | None = None) -> str:
    """Abandon a draft.

    `collection` falls back to the X-Docflow-Collection header.
    """
    _, store, _ = _init()
    try:
        collection = _resolve_collection(collection)
    except ValueError as e:
        return f"ERROR: {e}"
    d = store.get_draft(draft_id)
    if d is None:
        return f"ERROR: no draft '{draft_id}'"
    if d.collection != collection:
        return f"ERROR: draft is in collection '{d.collection}', not '{collection}'."
    if d.state in ("committed", "escalated"):
        return f"ERROR: cannot abandon a '{d.state}' draft."
    store.abandon(draft_id, reason=reason)
    return json.dumps(
        {"draft_id": draft_id, "collection": collection, "state": "abandoned", "reason": reason}
    )


# Re-export for tests
_ = slugify


def main() -> None:
    import sys
    if "--http" in sys.argv:
        # Streamable HTTP daemon mode
        port = 8422
        host = "127.0.0.1"
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        if "--host" in sys.argv:
            host = sys.argv[sys.argv.index("--host") + 1]
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run()  # stdio (default)


if __name__ == "__main__":
    main()
