"""Runtime configuration loaded from environment variables.

Variables:
    DOCS_ROOT            Required. Absolute path of the documentation root
                         repository. ADRs, contracts, etc. live under
                         DOCS_ROOT/docs/.
    DOCS_SCOPE_MAP       Optional. Colon-separated list of `scope=path` pairs
                         pointing at additional repositories that accept
                         scoped drafts.
                         Example: "qf-redis=/home/me/quant/qf/redis:qf-market=/home/me/quant/qf/market"
    DOCS_STATE_DIR       Optional. Directory for the SQLite state DB.
                         Default: $DOCS_ROOT/.docs-state
    DOCS_MAX_ITERATIONS  Optional. Max draft→revise loops before the commit
                         gate refuses new reviews and forces escalate.
                         Default: 5
    DOCS_PROMPTS_DIR     Optional. Directory containing reviewer prompts.
                         Default: the `prompts/` directory shipped with the package.
    DOCS_PLANE_STALE_PROJECT
                         Optional. Plane project id for staleness issues.
                         If unset, stale flags do not sync to Plane.

docflow does not talk to any LLM directly. Reviewer orchestration is the
caller's responsibility: `prepare_review` returns a self-contained bundle
the caller feeds to its own LLM gateway, and `submit_review` records the
verdict back.
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
    max_iterations: int
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
            max_iterations=int(os.environ.get("DOCS_MAX_ITERATIONS", "5")),
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
