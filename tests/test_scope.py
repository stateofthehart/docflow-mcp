"""Scope routing and ADR numbering."""

from __future__ import annotations

from pathlib import Path

import pytest

from docflow_mcp.scope import (
    extract_title,
    next_adr_number,
    resolve_decision_path,
    resolve_section_path,
    slugify,
)


def test_slugify_basic():
    assert slugify("Plugin Contract") == "plugin-contract"
    assert slugify("Use HTTPX (not requests!)") == "use-httpx-not-requests"


def test_slugify_empty_fallback():
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"


def test_next_adr_number_empty_dir(tmp_path: Path):
    d = tmp_path / "decisions"
    d.mkdir()
    assert next_adr_number(d) == 1


def test_next_adr_number_skips_non_numbered(tmp_path: Path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "0001-foo.md").touch()
    (d / "0003-bar.md").touch()
    (d / "README.md").touch()
    assert next_adr_number(d) == 4


def test_next_adr_number_global_across_dirs(tmp_path: Path):
    """Global counter: max ADR number is taken across all supplied dirs."""
    primary = tmp_path / "primary" / "decisions"
    primary.mkdir(parents=True)
    (primary / "0001-a.md").touch()
    (primary / "0002-b.md").touch()

    scoped = tmp_path / "scoped" / "decisions"
    scoped.mkdir(parents=True)
    (scoped / "0005-c.md").touch()

    assert next_adr_number(primary) == 3
    assert next_adr_number(primary, additional_dirs=[scoped]) == 6


def test_resolve_decision_path_with_global_counter(tmp_path: Path):
    """resolve_decision_path threads number_sources through to the counter."""
    scope_repo = tmp_path / "qf-market"
    (scope_repo / "docs" / "decisions").mkdir(parents=True)
    (scope_repo / "docs" / "decisions" / "0001-local.md").touch()

    other = tmp_path / "qf-docs"
    (other / "docs" / "decisions").mkdir(parents=True)
    for n in range(1, 7):
        (other / "docs" / "decisions" / f"{n:04d}-x.md").touch()

    target = resolve_decision_path(
        scope_repo, "New Decision", number_sources=[other / "docs" / "decisions"]
    )
    assert target.name == "0007-new-decision.md"
    assert target.parent == scope_repo / "docs" / "decisions"


def test_resolve_decision_path(tmp_repo: Path):
    target = resolve_decision_path(tmp_repo, "New Redis Schema")
    assert target.name == "0002-new-redis-schema.md"
    assert target.parent == tmp_repo / "docs" / "decisions"


def test_resolve_section_path_requires_existing_file(tmp_repo: Path):
    with pytest.raises(FileNotFoundError):
        resolve_section_path(tmp_repo, "docs/contracts/NONEXISTENT.md")


def test_resolve_section_path_refuses_escape(tmp_repo: Path):
    with pytest.raises(ValueError, match="escapes"):
        resolve_section_path(tmp_repo, "../../../etc/passwd")


def test_extract_title_adr_format():
    assert extract_title("# ADR 0003: Arrow IPC\n\n...") == "Arrow IPC"


def test_extract_title_plain_format():
    assert extract_title("# A generic doc title\n\n...") == "A generic doc title"


def test_extract_title_missing():
    assert extract_title("no heading here\njust body") == "untitled"
