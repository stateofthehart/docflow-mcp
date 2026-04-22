"""Cross-scope read/search/list/recent on a collection with scope_map."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docflow_mcp.reader import DocReader


@pytest.fixture
def multi_root(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Build a docs_root with one ADR plus two 'sub-repo' scopes each with one ADR."""
    docs_root = tmp_path / "central-docs"
    (docs_root / "docs" / "decisions").mkdir(parents=True)
    (docs_root / "docs" / "decisions" / "0001-central-a.md").write_text(
        "# ADR 0001: Central A\n\n## Context\nShared decision.\n"
    )

    sports = tmp_path / "sports-repo"
    (sports / "docs" / "decisions").mkdir(parents=True)
    (sports / "docs" / "decisions" / "0002-sports-local.md").write_text(
        "# ADR 0002: Sports Local\n\n## Context\nSports-specific decision.\n"
    )

    news = tmp_path / "news-repo"
    (news / "docs" / "decisions").mkdir(parents=True)
    (news / "docs" / "decisions" / "0003-news-local.md").write_text(
        "# ADR 0003: News Local\n\n## Context\nNews-specific decision.\n"
    )

    return docs_root, {"qf-sports": sports, "qf-news": news}


# ── Backward compatibility: no scope / empty scope = docs_root only ──


def test_search_default_scope_only_sees_docs_root(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    hits = r.search("Context")
    # Only hits from central-docs, not sub-repos
    paths = [h.path for h in hits]
    assert any("0001-central-a.md" in p for p in paths)
    assert not any("0002-sports-local.md" in p for p in paths)
    assert not any("0003-news-local.md" in p for p in paths)


def test_list_default_scope_only_sees_docs_root(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    rows = r.list(category="decisions")
    paths = [row["path"] for row in rows]
    assert len(paths) == 1
    assert "0001-central-a.md" in paths[0]


# ── scope='*' searches across all roots with prefix labels ──


def test_search_star_scope_hits_every_root(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    hits = r.search("Context", scope="*")
    paths = [h.path for h in hits]
    # central hit has no prefix
    assert any(p == "docs/decisions/0001-central-a.md" for p in paths)
    # sub-repo hits are prefixed
    assert any(p.startswith("qf-sports:") for p in paths)
    assert any(p.startswith("qf-news:") for p in paths)


def test_list_star_scope_prefixes_sub_repo_entries(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    rows = r.list(category="decisions", scope="*")
    paths = [row["path"] for row in rows]
    assert any(p == "docs/decisions/0001-central-a.md" for p in paths)
    assert any(p.startswith("qf-sports:") for p in paths)
    assert any(p.startswith("qf-news:") for p in paths)


# ── Named scope restricts to one sub-repo ──


def test_search_named_scope_restricts(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    hits = r.search("Context", scope="qf-sports")
    paths = [h.path for h in hits]
    assert len(paths) == 1
    # Single-scope results get the prefix so caller can round-trip to read()
    assert paths[0].startswith("qf-sports:")
    assert "0002-sports-local.md" in paths[0]


def test_search_unknown_scope_errors(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    with pytest.raises(ValueError, match="Unknown scope"):
        r.search("Context", scope="bogus-scope")


# ── read with scope + round-trip from prefixed paths ──


def test_read_with_named_scope(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    text = r.read("docs/decisions/0002-sports-local.md", scope="qf-sports")
    assert "Sports-specific decision" in text


def test_read_prefixed_path_round_trip(multi_root):
    """search returns 'qf-sports:...'; read should accept that directly."""
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    text = r.read("qf-sports:docs/decisions/0002-sports-local.md")
    assert "Sports-specific decision" in text


def test_read_explicit_scope_wins_over_prefix(multi_root):
    """If both scope= and a prefix are given, explicit scope wins."""
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    # Ask for sports prefix but pass scope=qf-news → reads from news
    with pytest.raises(FileNotFoundError):
        r.read(
            "qf-sports:docs/decisions/0002-sports-local.md", scope="qf-news"
        )


def test_read_default_scope_unchanged(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    text = r.read("docs/decisions/0001-central-a.md")
    assert "Shared decision" in text


def test_read_rejects_path_traversal_in_scope(multi_root):
    docs_root, scopes = multi_root
    r = DocReader(docs_root, scope_paths=scopes)
    with pytest.raises(ValueError, match="escapes"):
        r.read("../../etc/passwd", scope="qf-sports")


# ── recent across scopes ──


def test_recent_across_scopes_merges_and_sorts(tmp_path: Path):
    """recent(scope='*') merges git logs from every root, sorted by date."""
    docs_root = tmp_path / "central"
    (docs_root / "docs").mkdir(parents=True)
    sports = tmp_path / "sports"
    (sports / "docs").mkdir(parents=True)

    def init_repo(path: Path, msg: str):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "t@e"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
        (path / "docs" / "x.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", msg], cwd=path, check=True
        )

    init_repo(docs_root, "docs: central change")
    init_repo(sports, "docs: sports change")

    r = DocReader(docs_root, scope_paths={"qf-sports": sports})
    rows = r.recent(limit=10, scope="*")
    subjects = [row["subject"] for row in rows]
    assert any("central" in s for s in subjects)
    assert any("sports" in s for s in subjects)
    # Sports commit entries carry scope tag
    sport_rows = [row for row in rows if row.get("scope") == "qf-sports"]
    assert len(sport_rows) == 1
