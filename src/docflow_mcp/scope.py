"""Scope-based target path resolution for commits.

A draft carries a `scope` label and optionally a user-supplied `path`. This
module turns those into an absolute filesystem path where the commit will land.

Rules:
 - `kind=decision`: always goes to `<scope_repo>/docs/decisions/NNNN-<slug>.md`.
   NNNN is the next available number within the scope_repo's decisions dir.
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


def next_adr_number(decisions_dir: Path) -> int:
    """Return the next ADR number (1-indexed) based on existing files."""
    if not decisions_dir.is_dir():
        return 1
    highest = 0
    for f in decisions_dir.glob("*.md"):
        m = re.match(r"^(\d{4})-", f.name)
        if m:
            n = int(m.group(1))
            if n > highest:
                highest = n
    return highest + 1


def resolve_decision_path(
    scope_repo: Path, title: str, decisions_subpath: str = "docs/decisions"
) -> Path:
    """Compute the target path for a new ADR."""
    decisions_dir = scope_repo / decisions_subpath
    num = next_adr_number(decisions_dir)
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
