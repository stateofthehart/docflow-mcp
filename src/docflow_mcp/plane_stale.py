"""Thin Plane API client for opening staleness issues.

Used by the `escalate` tool on stale-kind drafts and by approved stale reviews.
Intentionally minimal — just enough to create an issue with a title, body, and
label. Falls back to a no-op if DOCS_PLANE_STALE_PROJECT is unset.
"""

from __future__ import annotations

import os

import httpx


def open_stale_issue(
    title: str,
    body: str,
    project_id: str | None,
    api_key: str | None = None,
    workspace_slug: str | None = None,
    base_url: str | None = None,
    label_name: str = "docs-stale",
) -> str | None:
    """Create a Plane issue. Returns the issue URL, or None if Plane is not configured."""
    project_id = project_id or os.environ.get("DOCS_PLANE_STALE_PROJECT")
    if not project_id:
        return None

    api_key = api_key or os.environ.get("PLANE_API_KEY")
    workspace_slug = workspace_slug or os.environ.get("PLANE_WORKSPACE_SLUG")
    base_url = base_url or os.environ.get("PLANE_BASE_URL")

    if not all([api_key, workspace_slug, base_url]):
        return None

    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        r = httpx.post(
            f"{base_url}/api/v1/workspaces/{workspace_slug}/projects/{project_id}/issues/",
            headers=headers,
            json={"name": title, "description_html": f"<pre>{_escape_html(body)}</pre>"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        issue_id = data.get("id")
    except httpx.HTTPError as e:
        return f"ERROR creating Plane issue: {e}"

    if issue_id:
        try:
            labels_resp = httpx.get(
                f"{base_url}/api/v1/workspaces/{workspace_slug}/projects/{project_id}/labels/",
                headers=headers, timeout=15,
            )
            labels_resp.raise_for_status()
            label_id = None
            for lbl in labels_resp.json().get("results", labels_resp.json()) or []:
                if isinstance(lbl, dict) and lbl.get("name", "").lower() == label_name.lower():
                    label_id = lbl["id"]
                    break
            if label_id:
                httpx.patch(
                    f"{base_url}/api/v1/workspaces/{workspace_slug}/projects/"
                    f"{project_id}/issues/{issue_id}/",
                    headers=headers, json={"label_ids": [label_id]}, timeout=15,
                )
        except httpx.HTTPError:
            pass  # Label attach is best-effort.

    return f"{base_url}/{workspace_slug}/projects/{project_id}/issues/{issue_id}"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
