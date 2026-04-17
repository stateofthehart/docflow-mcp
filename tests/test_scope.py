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
