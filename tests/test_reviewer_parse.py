"""Reviewer YAML parsing — the narrow dialect the reviewer prompt enforces."""

from __future__ import annotations

from docflow_mcp.reviewer import _minimal_yaml_parse


def test_parse_approve_with_no_issues():
    text = """verdict: approve
issues: []
notes: all clean
"""
    r = _minimal_yaml_parse(text)
    assert r["verdict"] == "approve"
    assert r["issues"] == []
    assert r["notes"] == "all clean"


def test_parse_revise_with_issues():
    text = """verdict: revise
issues:
  - severity: major
    pass: structural
    location: Consequences
    message: Negative section is empty
    evidence: Line 12 shows blank list
  - severity: minor
    pass: style
    location: Context
    message: Inconsistent tense
suggestions:
  - Add three negative consequences
  - Normalize voice
notes: retry recommended
"""
    r = _minimal_yaml_parse(text)
    assert r["verdict"] == "revise"
    assert len(r["issues"]) == 2
    assert r["issues"][0]["severity"] == "major"
    assert r["issues"][0]["pass"] == "structural"
    assert r["issues"][0]["location"] == "Consequences"
    assert r["issues"][1]["pass"] == "style"
    assert r["suggestions"] == ["Add three negative consequences", "Normalize voice"]


def test_parse_escalate():
    text = """verdict: escalate
issues:
  - severity: major
    pass: ground_truth
    location: Decision
    message: Claim about BaseProvider contradicts code
    evidence: axon_query returned no match for claimed method
notes: human should adjudicate
"""
    r = _minimal_yaml_parse(text)
    assert r["verdict"] == "escalate"
    assert r["issues"][0]["pass"] == "ground_truth"


def test_parse_ignores_unknown_keys():
    text = """verdict: approve
issues: []
some_future_field: xyz
"""
    r = _minimal_yaml_parse(text)
    assert r["verdict"] == "approve"
    assert r.get("some_future_field") == "xyz"
