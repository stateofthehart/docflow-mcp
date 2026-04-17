"""Runtime configuration loaded from environment variables.

Configuration sources, in order of precedence:
1. Environment variables set at MCP launch time.
2. Defaults below.

Variables:
    DOCS_ROOT            Required. Absolute path of the documentation root repository.
                         ADRs, contracts, etc. live under DOCS_ROOT/docs/.
    DOCS_SCOPE_MAP       Optional. Colon-separated list of `scope=path` pairs pointing
                         at additional repositories that accept scoped drafts.
                         Example: "qf-redis=/home/ethan/.../qf/redis:qf-market=/home/ethan/.../qf/market"
    DOCS_STATE_DIR       Optional. Directory for the SQLite state DB. Default: $DOCS_ROOT/.docs-state
    DOCS_REVIEWER_URL    Optional. HTTP endpoint of the MyLLM gateway used for review calls.
                         Default: http://localhost:4000
    DOCS_REVIEWER_PROFILE Optional. MyLLM profile name. Default: docs-reviewer
    DOCS_MAX_ITERATIONS  Optional. Max draft→revise loops before auto-escalate. Default: 5
    DOCS_REVIEW_TIMEOUT  Optional. Seconds before review call times out. Default: 600
    DOCS_PROMPTS_DIR     Optional. Directory containing reviewer prompts. Default: the
                         `prompts/` directory shipped with the package.
    DOCS_PLANE_STALE_PROJECT Optional. Plane project id for staleness issues.
                             If unset, stale flags do not sync to Plane.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _package_prompts_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "prompts"


@dataclass(frozen=True)
class Config:
    docs_root: Path
    scope_map: dict[str, Path]
    state_dir: Path
    reviewer_url: str
    reviewer_profile: str
    max_iterations: int
    review_timeout: int
    prompts_dir: Path
    plane_stale_project: str | None

    @classmethod
    def from_env(cls) -> "Config":
        root_env = os.environ.get("DOCS_ROOT")
        if not root_env:
            raise RuntimeError(
                "DOCS_ROOT environment variable is required. "
                "Set it to the absolute path of your documentation root repository."
            )
        docs_root = Path(root_env).expanduser().resolve()

        scope_map: dict[str, Path] = {}
        raw = os.environ.get("DOCS_SCOPE_MAP", "").strip()
        if raw:
            for pair in raw.split(":"):
                if "=" not in pair:
                    continue
                name, path = pair.split("=", 1)
                scope_map[name.strip()] = Path(path.strip()).expanduser().resolve()

        state_dir = Path(
            os.environ.get("DOCS_STATE_DIR", str(docs_root / ".docs-state"))
        ).expanduser().resolve()

        prompts_dir = Path(
            os.environ.get("DOCS_PROMPTS_DIR", str(_package_prompts_dir()))
        ).expanduser().resolve()

        return cls(
            docs_root=docs_root,
            scope_map=scope_map,
            state_dir=state_dir,
            reviewer_url=os.environ.get("DOCS_REVIEWER_URL", "http://localhost:4000"),
            reviewer_profile=os.environ.get("DOCS_REVIEWER_PROFILE", "docs-reviewer"),
            max_iterations=int(os.environ.get("DOCS_MAX_ITERATIONS", "5")),
            review_timeout=int(os.environ.get("DOCS_REVIEW_TIMEOUT", "600")),
            prompts_dir=prompts_dir,
            plane_stale_project=os.environ.get("DOCS_PLANE_STALE_PROJECT") or None,
        )

    def resolve_scope(self, scope: str) -> Path:
        """Return the repository path for a given scope label.

        `cross-repo` and the default scope both resolve to docs_root.
        Custom scopes are looked up in scope_map.
        """
        if scope in ("cross-repo", "", "default"):
            return self.docs_root
        if scope in self.scope_map:
            return self.scope_map[scope]
        raise ValueError(
            f"Unknown scope '{scope}'. Known: cross-repo, {', '.join(sorted(self.scope_map))}"
        )

    def known_scopes(self) -> list[str]:
        return ["cross-repo", *sorted(self.scope_map)]
