"""Reviewer integration — spawns a sub-agent via MyLLM gateway.

The reviewer is another LLM call, deliberately running on a different model
family than the author (configured in MyLLM). The reviewer receives:
    - the draft content
    - the kind-specific prompt (decision / section / stale)
    - access to its own MCP tools (configured in the MyLLM profile)

Returns parsed verdict + issues from the reviewer's YAML output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class ReviewerResult:
    verdict: str  # "approve" | "revise" | "escalate"
    issues: list[dict]
    notes: str | None
    reviewer_model: str | None
    prompt_hash: str
    raw_response: str


class Reviewer:
    def __init__(
        self,
        gateway_url: str,
        profile: str,
        prompts_dir: Path,
        timeout: int = 600,
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.profile = profile
        self.prompts_dir = prompts_dir
        self.timeout = timeout

    def review(
        self,
        kind: str,
        content: str,
        context: dict[str, Any] | None = None,
    ) -> ReviewerResult:
        prompt_path = self.prompts_dir / f"{kind}.md"
        if not prompt_path.is_file():
            raise FileNotFoundError(f"No reviewer prompt for kind '{kind}' at {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]

        user_message = self._build_user_message(kind, content, context or {})

        payload = {
            "profile": self.profile,
            "task": user_message,
            "system": prompt,
            "background": False,
            "return_structured": True,
        }

        raw, model = self._call_gateway(payload)
        parsed = self._parse_yaml_block(raw)

        verdict = parsed.get("verdict", "escalate")
        if verdict not in ("approve", "revise", "escalate"):
            verdict = "escalate"
        issues = parsed.get("issues") or []
        if not isinstance(issues, list):
            issues = []
        notes = parsed.get("notes")

        return ReviewerResult(
            verdict=verdict,
            issues=issues,
            notes=str(notes) if notes else None,
            reviewer_model=model,
            prompt_hash=prompt_hash,
            raw_response=raw,
        )

    # ── Private ───────────────────────────────────────────────────

    def _build_user_message(
        self, kind: str, content: str, context: dict[str, Any]
    ) -> str:
        """Assemble the review request payload sent to the reviewer agent."""
        parts = [
            f"# Review request — kind: {kind}",
            "",
            "## Draft content",
            "",
            content,
            "",
        ]
        if context.get("path"):
            parts.extend(["## Target path", context["path"], ""])
        if context.get("scope"):
            parts.extend(["## Scope", context["scope"], ""])
        if context.get("old_section"):
            parts.extend(
                ["## Existing section content (for section kind)", context["old_section"], ""]
            )
        if context.get("reason"):
            parts.extend(["## Author's reason for change", context["reason"], ""])
        if context.get("docs_root"):
            parts.extend(
                [
                    "## Docs root",
                    context["docs_root"],
                    "",
                    "Use your MCP tools (axon, read, search, plane) to verify claims "
                    "against code and existing documentation before rendering a verdict.",
                    "",
                ]
            )
        parts.append(
            "Output your review strictly as the YAML block specified in your system prompt. "
            "No prose outside the YAML block."
        )
        return "\n".join(parts)

    def _call_gateway(self, payload: dict) -> tuple[str, str | None]:
        """POST to MyLLM. Returns (raw_text, model_name_if_reported)."""
        try:
            r = httpx.post(
                f"{self.gateway_url}/spawn_agent",
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise RuntimeError(f"MyLLM reviewer call failed: {e}") from e

        # MyLLM's spawn_agent response shape is flexible; accept common keys.
        for key in ("output", "response", "text", "result"):
            if key in data and isinstance(data[key], str):
                return data[key], data.get("model")
        if "messages" in data and isinstance(data["messages"], list) and data["messages"]:
            last = data["messages"][-1]
            if isinstance(last, dict) and "content" in last:
                return str(last["content"]), data.get("model")
        return json.dumps(data), data.get("model")

    @staticmethod
    def _parse_yaml_block(text: str) -> dict:
        """Extract the YAML block from the reviewer's response."""
        fence = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.DOTALL)
        body = fence.group(1) if fence else text

        # Hand-parse the subset of YAML we actually use, to avoid a yaml dependency.
        return _minimal_yaml_parse(body)


def _minimal_yaml_parse(text: str) -> dict:
    """Parse the narrow YAML dialect used by the reviewer output.

    Supports: top-level scalars, one list of dicts under `issues:`, and one
    list of strings under `suggestions:`. Enough for our schema; nothing more.
    """
    result: dict[str, Any] = {"verdict": "escalate", "issues": [], "suggestions": [], "notes": None}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith("issues:"):
            issues, i = _parse_list_of_dicts(lines, i + 1, indent_hint=2)
            result["issues"] = issues
            continue
        if line.startswith("suggestions:"):
            suggestions, i = _parse_list_of_scalars(lines, i + 1)
            result["suggestions"] = suggestions
            continue
        m = re.match(r"^(\w[\w_]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                i += 1
                continue
            val = val.strip('"').strip("'")
            result[key] = val
        i += 1
    return result


def _parse_list_of_dicts(lines: list[str], start: int, indent_hint: int = 2):
    items: list[dict] = []
    current: dict[str, Any] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0:
            break
        if stripped.startswith("- "):
            if current:
                items.append(current)
                current = {}
            rest = stripped[2:]
            m = re.match(r"^(\w[\w_]*):\s*(.*)$", rest)
            if m:
                current[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        elif ":" in stripped and indent >= indent_hint:
            m = re.match(r"^(\w[\w_]*):\s*(.*)$", stripped)
            if m:
                current[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        i += 1
    if current:
        items.append(current)
    return items, i


def _parse_list_of_scalars(lines: list[str], start: int):
    items: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0:
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().strip('"').strip("'"))
        i += 1
    return items, i
