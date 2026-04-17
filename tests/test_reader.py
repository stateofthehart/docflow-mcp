"""Reader — search, section-scoped read, list with recency, recent commits."""

from __future__ import annotations

from pathlib import Path

import pytest

from docflow_mcp.reader import DocReader


def test_search_finds_substring(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    hits = reader.search("entry_points")
    assert any("PROVIDER.md" in h.path for h in hits)


def test_search_scoped_by_category(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    hits = reader.search("Status", category="decisions")
    assert all("decisions" in h.path for h in hits)


def test_search_reports_heading_context(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    hits = reader.search("Faster installs")
    assert hits
    assert hits[0].heading is not None


def test_read_full_file(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    text = reader.read("docs/decisions/0001-adopt-uv.md")
    assert "ADR 0001" in text
    assert "Consequences" in text


def test_read_section_only(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    text = reader.read("docs/decisions/0001-adopt-uv.md", section="Decision")
    assert text.startswith("## Decision")
    assert "Use uv." in text
    assert "Alternatives" not in text  # section ended before this


def test_read_section_not_found(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    with pytest.raises(LookupError):
        reader.read("docs/decisions/0001-adopt-uv.md", section="NonexistentHeading")


def test_read_refuses_path_escape(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    with pytest.raises(ValueError, match="escapes"):
        reader.read("../../../etc/passwd")


def test_list_docs_returns_all_md(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    rows = reader.list()
    paths = {r["path"] for r in rows}
    assert "docs/decisions/0001-adopt-uv.md" in paths
    assert "docs/contracts/PROVIDER.md" in paths


def test_list_docs_filters_by_category(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    rows = reader.list(category="contracts")
    assert rows
    assert all("contracts" in r["path"] for r in rows)


def test_recent_returns_git_commits(tmp_repo: Path):
    reader = DocReader(tmp_repo)
    commits = reader.recent(limit=5)
    assert commits
    assert commits[0]["subject"] == "initial"
