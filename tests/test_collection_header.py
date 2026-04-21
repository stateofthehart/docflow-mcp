"""Header-based collection default — precedence rules for _resolve_collection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from docflow_mcp.server import _resolve_collection


def test_explicit_arg_wins_even_when_header_absent():
    with patch("docflow_mcp.server.get_http_headers", return_value={}):
        assert _resolve_collection("qf-docs") == "qf-docs"


def test_explicit_arg_overrides_header():
    with patch(
        "docflow_mcp.server.get_http_headers",
        return_value={"x-docflow-collection": "homelab-docs"},
    ):
        assert _resolve_collection("qf-docs") == "qf-docs"


def test_header_fallback_when_no_arg():
    with patch(
        "docflow_mcp.server.get_http_headers",
        return_value={"x-docflow-collection": "qf-docs"},
    ):
        assert _resolve_collection(None) == "qf-docs"


def test_empty_arg_treated_as_missing():
    """Empty string should fall through to header, not be used as the collection."""
    with patch(
        "docflow_mcp.server.get_http_headers",
        return_value={"x-docflow-collection": "qf-docs"},
    ):
        assert _resolve_collection("") == "qf-docs"


def test_error_when_neither_arg_nor_header():
    with patch("docflow_mcp.server.get_http_headers", return_value={}):
        with pytest.raises(ValueError, match="collection is required"):
            _resolve_collection(None)


def test_empty_header_value_falls_through_to_error():
    """A header set to empty string should not satisfy the resolver."""
    with patch(
        "docflow_mcp.server.get_http_headers",
        return_value={"x-docflow-collection": ""},
    ):
        with pytest.raises(ValueError):
            _resolve_collection(None)


def test_stdio_client_no_http_context():
    """For stdio clients there's no HTTP request so headers are empty."""
    # get_http_headers returns {} when no HTTP request is active
    with patch("docflow_mcp.server.get_http_headers", return_value={}):
        with pytest.raises(ValueError):
            _resolve_collection(None)
        # Explicit arg still works for stdio
        assert _resolve_collection("qf-docs") == "qf-docs"
