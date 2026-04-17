"""Scope-based target path resolution for commits.

A draft carries a `scope` label and optionally a user-supplied `path`. This
module turns those into an absolute filesystem path where the commit will land.

Rules:
 - `kind=decision`: commits to `<scope_repo>/docs/decisions/NNNN-<slug>.md`.
   NNNN is drawn from a single global counter (shared across all scopes)
   sourced from the docs_root decisions directory, so ADR numbering is
   unique across the entire ecosystem even when the file lands in a
   sub-repo. This prevents number collisions when multiple scopes draft
   ADRs concurrently.
 - `kind=section`: honors the caller-supplied `path` (required). Path must
   already exist in scope_repo; section updates do not create new files.
 - `kind=stale`: no file path — stale flags do not commit content, they
   produce Plane issues.
"""

from __future__ import annotations

import re
from pathlib import Path


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", title.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug[:60] or "untitled"


def _highest_adr_in_dir(decisions_dir: Path) -> int:
    """Highest NNNN seen among `NNNN-*.md` files in a single directory."""
    if not decisions_dir.is_dir():
        return 0
    highest = 0
    for f in decisions_dir.glob("*.md"):
        m = re.match(r"^(\d{4})-", f.name)
        if m:
            n = int(m.group(1))
            if n > highest:
                highest = n
    return highest


def next_adr_number(
    primary_dir: Path, additional_dirs: list[Path] | None = None
) -> int:
    """Return the next ADR number, taking the max across all supplied dirs.

    This supports a global ADR counter where NNNN is unique across every
    scope, even though files land in different repositories. Callers that
    want per-repo numbering can simply pass a single dir.
    """
    highest = _highest_adr_in_dir(primary_dir)
    for d in additional_dirs or []:
        highest = max(highest, _highest_adr_in_dir(d))
    return highest + 1


def resolve_decision_path(
    scope_repo: Path,
    title: str,
    decisions_subpath: str = "docs/decisions",
    number_sources: list[Path] | None = None,
) -> Path:
    """Compute the target path for a new ADR.

    Args:
        scope_repo: repo where the file will be written.
        title: ADR title — used to slugify the filename.
        decisions_subpath: relative path under scope_repo for ADRs.
        number_sources: additional decisions directories to consult when
            picking the next ADR number. Used to enforce a global counter
            shared across multiple repos.
    """
    decisions_dir = scope_repo / decisions_subpath
    num = next_adr_number(decisions_dir, additional_dirs=number_sources)
    return decisions_dir / f"{num:04d}-{slugify(title)}.md"


def resolve_section_path(scope_repo: Path, rel_path: str) -> Path:
    """Validate that a section-update target already exists."""
    full = (scope_repo / rel_path).resolve()
    if not str(full).startswith(str(scope_repo.resolve())):
        raise ValueError(f"Path '{rel_path}' escapes scope repository")
    if not full.is_file():
        raise FileNotFoundError(
            f"Section updates require an existing file. '{rel_path}' does not exist in {scope_repo}"
        )
    return full


def extract_title(content: str) -> str:
    """Extract an ADR title from `# ADR NNNN: Title` or `# Title` line."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            # Strip ADR numbering if present
            m = re.match(r"^ADR\s+\d+:\s*(.*)$", title, re.IGNORECASE)
            return m.group(1).strip() if m else title
    return "untitled"
