"""Read-side tools: search, read (with section scoping), list, recent.

Scope-aware across a collection. A reader owns the collection's docs_root
plus any scope_map paths. The `scope` parameter on read-side tools selects
which root(s) to operate on:

    ""               docs_root only (cross-repo scope; default; backward compat)
    "cross-repo"     same as ""
    "*"              docs_root + every scope_map path
    "<scope-name>"   just that sub-repo

When operating across multiple roots, results are prefixed with the scope
name they came from (e.g. "qf-sports:docs/decisions/0008-foo.md"). The
prefix is round-trippable: `read(path="qf-sports:docs/decisions/0008-foo.md")`
is equivalent to `read(path="docs/decisions/0008-foo.md", scope="qf-sports")`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _rg_available() -> bool:
    """Check whether a real ripgrep binary is on PATH (not a shell function)."""
    return shutil.which("rg") is not None


@dataclass
class SearchHit:
    path: str
    line: int
    heading: str | None
    snippet: str


class DocReader:
    """Filesystem-backed reader over a collection (docs_root + scope_map)."""

    def __init__(
        self,
        docs_root: Path,
        scope_paths: dict[str, Path] | None = None,
        doc_subdirs: tuple[str, ...] = ("docs",),
    ):
        self.docs_root = docs_root
        self.scope_paths = dict(scope_paths or {})
        self.doc_subdirs = doc_subdirs

    # ── Scope resolution ──────────────────────────────────────────

    def _search_roots(self, scope: str) -> list[tuple[str, Path]]:
        """Return a list of (scope_label, root_path) to operate on.

        scope_label is empty for the collection's docs_root; non-empty for
        scope_map entries (used to prefix result paths for disambiguation).
        """
        if scope in ("", "cross-repo", "default"):
            return [("", self.docs_root)]
        if scope == "*":
            out: list[tuple[str, Path]] = [("", self.docs_root)]
            for name in sorted(self.scope_paths):
                out.append((name, self.scope_paths[name]))
            return out
        if scope in self.scope_paths:
            return [(scope, self.scope_paths[scope])]
        valid = ", ".join(["*", "cross-repo", *sorted(self.scope_paths)])
        raise ValueError(
            f"Unknown scope '{scope}' for read operation. Valid: {valid}"
        )

    def _parse_path_prefix(self, rel_path: str, scope: str) -> tuple[str, str]:
        """Split a possibly-prefixed path into (scope, path).

        Handles round-tripped paths like "qf-sports:docs/decisions/0008.md".
        Explicit scope argument wins when both are supplied.
        """
        if scope:
            return scope, rel_path
        if ":" in rel_path and not rel_path.startswith("/"):
            candidate_scope, _, rest = rel_path.partition(":")
            if candidate_scope in self.scope_paths or candidate_scope in (
                "cross-repo",
                "default",
            ):
                return candidate_scope, rest
        return "", rel_path

    def _root_for(self, scope: str) -> Path:
        if scope in ("", "cross-repo", "default"):
            return self.docs_root
        if scope in self.scope_paths:
            return self.scope_paths[scope]
        raise ValueError(f"Unknown scope '{scope}'")

    # ── Search ────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 25,
        scope: str = "",
    ) -> list[SearchHit]:
        """Search across one or more roots per the scope argument.

        Returns ranked hits with path, line, enclosing heading, and snippet.
        Hits from scope_map roots are prefixed `<scope>:` so agents can
        round-trip via read(path=...).
        """
        if not query.strip():
            return []

        roots = self._search_roots(scope)
        all_hits: list[SearchHit] = []
        for scope_label, root in roots:
            targets: list[Path] = []
            if category:
                targets.extend(root / sub / category for sub in self.doc_subdirs)
            else:
                targets.extend(root / sub for sub in self.doc_subdirs)
            targets = [t for t in targets if t.exists()]
            if not targets:
                continue

            remaining = limit - len(all_hits)
            if remaining <= 0:
                break

            if _rg_available():
                hits = self._search_rg(query, targets, remaining, root)
            else:
                hits = self._search_python(query, targets, remaining, root)

            for h in hits:
                if scope_label:
                    h.path = f"{scope_label}:{h.path}"
                all_hits.append(h)
                if len(all_hits) >= limit:
                    break

        return all_hits[:limit]

    def _search_rg(
        self, query: str, targets: list[Path], limit: int, root: Path
    ) -> list[SearchHit]:
        cmd = [
            "rg",
            "--max-count", "5",
            "--with-filename",
            "--line-number",
            "--no-heading",
            "--color", "never",
            "--glob", "*.md",
            "-i",
            query,
            *[str(t) for t in targets],
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, check=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return self._search_python(query, targets, limit, root)

        hits: list[SearchHit] = []
        for line in res.stdout.splitlines()[:limit]:
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            if not m:
                continue
            path_str, lineno_str, content = m.group(1), m.group(2), m.group(3)
            lineno = int(lineno_str)
            try:
                rel = str(Path(path_str).resolve().relative_to(root))
            except ValueError:
                rel = path_str
            heading = self._heading_for_line(Path(path_str), lineno)
            hits.append(
                SearchHit(path=rel, line=lineno, heading=heading, snippet=content.strip()[:200])
            )
        return hits

    def _search_python(
        self, query: str, targets: list[Path], limit: int, root: Path
    ) -> list[SearchHit]:
        """Fallback search: pure Python regex over markdown files."""
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits: list[SearchHit] = []
        for base in targets:
            for md in sorted(base.rglob("*.md")):
                try:
                    lines = md.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                per_file = 0
                current_heading: str | None = None
                for i, line in enumerate(lines, start=1):
                    hm = re.match(r"^(#{1,6})\s+(.*)$", line)
                    if hm:
                        current_heading = hm.group(2).strip()
                    if pattern.search(line):
                        try:
                            rel = str(md.resolve().relative_to(root))
                        except ValueError:
                            rel = str(md)
                        hits.append(
                            SearchHit(
                                path=rel, line=i, heading=current_heading,
                                snippet=line.strip()[:200],
                            )
                        )
                        per_file += 1
                        if per_file >= 5 or len(hits) >= limit:
                            break
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        return hits[:limit]

    # ── Read ──────────────────────────────────────────────────────

    def read(
        self, rel_path: str, section: str | None = None, scope: str = ""
    ) -> str:
        """Read a doc file, resolving against scope's root.

        Accepts both explicit `scope=` and the `scope:path` prefix format
        returned by search(). Explicit scope wins when both are supplied.
        """
        effective_scope, effective_path = self._parse_path_prefix(rel_path, scope)
        root = self._root_for(effective_scope)
        full = (root / effective_path).resolve()
        if not str(full).startswith(str(root)):
            raise ValueError(f"Path '{effective_path}' escapes scope root")
        if not full.is_file():
            raise FileNotFoundError(
                f"No such doc: {effective_scope + ':' if effective_scope else ''}{effective_path}"
            )
        text = full.read_text(encoding="utf-8")
        if section is None:
            return text
        return self._extract_section(text, section)

    def _extract_section(self, text: str, section: str) -> str:
        """Return content under a heading that matches `section` (case-insensitive)."""
        target = section.strip().lower()
        lines = text.splitlines()
        capture: list[str] = []
        in_section = False
        section_level = 0
        for line in lines:
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                level = len(m.group(1))
                heading = m.group(2).strip()
                if in_section and level <= section_level:
                    break
                if not in_section and heading.lower() == target:
                    in_section = True
                    section_level = level
                    capture.append(line)
                    continue
            if in_section:
                capture.append(line)
        if not capture:
            raise LookupError(f"Section '{section}' not found")
        return "\n".join(capture).rstrip() + "\n"

    def _heading_for_line(self, path: Path, lineno: int) -> str | None:
        """Walk backwards to find the enclosing heading for a line."""
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return None
        for i in range(min(lineno, len(lines)) - 1, -1, -1):
            m = re.match(r"^(#{1,6})\s+(.*)$", lines[i])
            if m:
                return m.group(2).strip()
        return None

    # ── List ──────────────────────────────────────────────────────

    def list(
        self,
        category: str | None = None,
        changed_since_days: int | None = None,
        scope: str = "",
    ) -> list[dict]:
        """List doc files across one or more roots per the scope argument."""
        roots = self._search_roots(scope)
        cutoff: datetime | None = None
        if changed_since_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=changed_since_days)

        results: list[dict] = []
        for scope_label, root in roots:
            bases: list[Path] = []
            for sub in self.doc_subdirs:
                base = root / sub
                if category:
                    base = base / category
                if base.is_dir():
                    bases.append(base)
            for base in bases:
                for p in sorted(base.rglob("*.md")):
                    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                    if cutoff and mtime < cutoff:
                        continue
                    try:
                        rel = str(p.relative_to(root))
                    except ValueError:
                        rel = str(p)
                    prefixed = f"{scope_label}:{rel}" if scope_label else rel
                    results.append(
                        {
                            "path": prefixed,
                            "modified": mtime.isoformat(timespec="seconds"),
                            "size": p.stat().st_size,
                        }
                    )
        return results

    # ── Recent ────────────────────────────────────────────────────

    def recent(self, limit: int = 10, scope: str = "") -> list[dict]:
        """Return recent git commits touching docs files across the scope.

        With scope='*', commits from every root are merged by date.
        """
        roots = self._search_roots(scope)
        merged: list[dict] = []
        for scope_label, root in roots:
            try:
                res = subprocess.run(
                    [
                        "git",
                        "-C", str(root),
                        "log",
                        f"-n{limit}",
                        "--pretty=format:%H|%aI|%an|%s",
                        "--",
                        *self.doc_subdirs,
                    ],
                    capture_output=True, text=True, timeout=10, check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                continue

            for line in res.stdout.splitlines():
                parts = line.split("|", 3)
                if len(parts) != 4:
                    continue
                sha, date, author, subject = parts
                entry = {"sha": sha[:12], "date": date, "author": author, "subject": subject}
                if scope_label:
                    entry["scope"] = scope_label
                merged.append(entry)

        merged.sort(key=lambda c: c["date"], reverse=True)
        return merged[:limit]
