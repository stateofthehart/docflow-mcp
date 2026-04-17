"""Read-side tools: search, read (with section scoping), list, recent."""

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
    """Filesystem-backed reader over docs_root."""

    def __init__(self, docs_root: Path, doc_subdirs: tuple[str, ...] = ("docs",)):
        self.docs_root = docs_root
        self.doc_subdirs = doc_subdirs

    # ── Search ────────────────────────────────────────────────────

    def search(self, query: str, category: str | None = None, limit: int = 25) -> list[SearchHit]:
        """Search across docs. Uses ripgrep if available on PATH, else pure Python.

        Returns ranked hits with path, line, enclosing heading, and snippet.
        """
        if not query.strip():
            return []

        targets: list[Path] = []
        if category:
            targets.extend(self.docs_root / sub / category for sub in self.doc_subdirs)
        else:
            targets.extend(self.docs_root / sub for sub in self.doc_subdirs)
        targets = [t for t in targets if t.exists()]
        if not targets:
            return []

        if _rg_available():
            return self._search_rg(query, targets, limit)
        return self._search_python(query, targets, limit)

    def _search_rg(self, query: str, targets: list[Path], limit: int) -> list[SearchHit]:
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
            return self._search_python(query, targets, limit)

        hits: list[SearchHit] = []
        for line in res.stdout.splitlines()[:limit]:
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            if not m:
                continue
            path_str, lineno_str, content = m.group(1), m.group(2), m.group(3)
            lineno = int(lineno_str)
            try:
                rel = str(Path(path_str).resolve().relative_to(self.docs_root))
            except ValueError:
                rel = path_str
            heading = self._heading_for_line(Path(path_str), lineno)
            hits.append(
                SearchHit(path=rel, line=lineno, heading=heading, snippet=content.strip()[:200])
            )
        return hits

    def _search_python(self, query: str, targets: list[Path], limit: int) -> list[SearchHit]:
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
                            rel = str(md.resolve().relative_to(self.docs_root))
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

    def read(self, rel_path: str, section: str | None = None) -> str:
        full = (self.docs_root / rel_path).resolve()
        if not str(full).startswith(str(self.docs_root)):
            raise ValueError(f"Path '{rel_path}' escapes docs_root")
        if not full.is_file():
            raise FileNotFoundError(f"No such doc: {rel_path}")
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
                    # Another heading at or above our level ends the section.
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
        self, category: str | None = None, changed_since_days: int | None = None
    ) -> list[dict]:
        """List doc files, optionally filtered by category and recency."""
        bases: list[Path] = []
        for sub in self.doc_subdirs:
            root = self.docs_root / sub
            if category:
                root = root / category
            if root.is_dir():
                bases.append(root)

        cutoff: datetime | None = None
        if changed_since_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=changed_since_days)

        results: list[dict] = []
        for base in bases:
            for p in sorted(base.rglob("*.md")):
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                if cutoff and mtime < cutoff:
                    continue
                try:
                    rel = str(p.relative_to(self.docs_root))
                except ValueError:
                    rel = str(p)
                results.append(
                    {
                        "path": rel,
                        "modified": mtime.isoformat(timespec="seconds"),
                        "size": p.stat().st_size,
                    }
                )
        return results

    # ── Recent ────────────────────────────────────────────────────

    def recent(self, limit: int = 10) -> list[dict]:
        """Return recent git commits that touched any docs file."""
        try:
            res = subprocess.run(
                [
                    "git",
                    "-C", str(self.docs_root),
                    "log",
                    f"-n{limit}",
                    "--pretty=format:%H|%aI|%an|%s",
                    "--",
                    *self.doc_subdirs,
                ],
                capture_output=True, text=True, timeout=10, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return []

        out: list[dict] = []
        for line in res.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            sha, date, author, subject = parts
            out.append(
                {"sha": sha[:12], "date": date, "author": author, "subject": subject}
            )
        return out
