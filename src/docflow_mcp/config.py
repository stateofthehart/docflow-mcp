"""Runtime configuration for docflow-mcp.

Configuration sources, in order of precedence:
    1. DOCFLOW_CONFIG_FILE — a YAML/JSON file defining collections.
    2. Legacy single-collection env (DOCS_ROOT + DOCS_SCOPE_MAP) —
       maps to one collection named "default".
    3. Defaults below.

YAML format:

    collections:
      qf-docs:
        docs_root: /home/me/.mcps/qf-docs
        scope_map:
          qf-core:   /home/me/quant/qf/core
          qf-market: /home/me/quant/qf/market
      homelab-docs:
        docs_root: /home/me/.mcps/homelab-docs

    state_dir: /home/me/.mcps/docflow-state
    max_iterations: 5
    prompts_dir: /home/me/.mcps/docflow-mcp/prompts   # optional
    plane_stale_project: some-uuid                     # optional

Every tool call must supply a `collection` argument naming one of the
keys under `collections:`. Scope map lookups resolve within that
collection.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _package_prompts_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "prompts"


@dataclass(frozen=True)
class CollectionConfig:
    name: str
    docs_root: Path
    scope_map: dict[str, Path] = field(default_factory=dict)

    def resolve_scope(self, scope: str) -> Path:
        """Return the repository path for a scope label within this collection.

        `cross-repo` and the default scope both resolve to docs_root.
        Other scopes are looked up in scope_map.
        """
        if scope in ("cross-repo", "", "default"):
            return self.docs_root
        if scope in self.scope_map:
            return self.scope_map[scope]
        raise ValueError(
            f"Unknown scope '{scope}' in collection '{self.name}'. "
            f"Known: cross-repo, {', '.join(sorted(self.scope_map))}"
        )

    def known_scopes(self) -> list[str]:
        return ["cross-repo", *sorted(self.scope_map)]


@dataclass(frozen=True)
class Config:
    collections: dict[str, CollectionConfig]
    state_dir: Path
    max_iterations: int
    prompts_dir: Path
    plane_stale_project: str | None

    @classmethod
    def from_env(cls) -> "Config":
        config_path = os.environ.get("DOCFLOW_CONFIG_FILE")
        if config_path:
            return cls._from_file(Path(config_path).expanduser().resolve())
        return cls._from_legacy_env()

    @classmethod
    def _from_file(cls, path: Path) -> "Config":
        if not path.is_file():
            raise RuntimeError(f"DOCFLOW_CONFIG_FILE not found: {path}")
        text = path.read_text(encoding="utf-8")
        data = _parse_config_text(text, path)

        raw_collections = data.get("collections") or {}
        if not raw_collections:
            raise RuntimeError(
                f"{path}: no collections defined. At least one collection is required."
            )

        collections: dict[str, CollectionConfig] = {}
        for name, entry in raw_collections.items():
            if "docs_root" not in entry:
                raise RuntimeError(f"{path}: collection '{name}' is missing docs_root.")
            scope_map = {
                k: Path(v).expanduser().resolve()
                for k, v in (entry.get("scope_map") or {}).items()
            }
            collections[name] = CollectionConfig(
                name=name,
                docs_root=Path(entry["docs_root"]).expanduser().resolve(),
                scope_map=scope_map,
            )

        state_dir = Path(
            data.get("state_dir")
            or os.environ.get("DOCFLOW_STATE_DIR")
            or f"{Path.home()}/.mcps/docflow-state"
        ).expanduser().resolve()

        prompts_dir = Path(
            data.get("prompts_dir")
            or os.environ.get("DOCFLOW_PROMPTS_DIR")
            or str(_package_prompts_dir())
        ).expanduser().resolve()

        return cls(
            collections=collections,
            state_dir=state_dir,
            max_iterations=int(data.get("max_iterations", 5)),
            prompts_dir=prompts_dir,
            plane_stale_project=data.get("plane_stale_project")
            or os.environ.get("DOCFLOW_PLANE_STALE_PROJECT"),
        )

    @classmethod
    def _from_legacy_env(cls) -> "Config":
        docs_root_env = os.environ.get("DOCS_ROOT")
        if not docs_root_env:
            raise RuntimeError(
                "No configuration found. Either set DOCFLOW_CONFIG_FILE to a "
                "collections YAML, or set DOCS_ROOT (legacy single-collection)."
            )
        docs_root = Path(docs_root_env).expanduser().resolve()

        scope_map: dict[str, Path] = {}
        raw = os.environ.get("DOCS_SCOPE_MAP", "").strip()
        if raw:
            for pair in raw.split(":"):
                if "=" not in pair:
                    continue
                name, path = pair.split("=", 1)
                scope_map[name.strip()] = Path(path.strip()).expanduser().resolve()

        default_collection = CollectionConfig(
            name="default",
            docs_root=docs_root,
            scope_map=scope_map,
        )

        state_dir = Path(
            os.environ.get("DOCS_STATE_DIR", str(docs_root / ".docs-state"))
        ).expanduser().resolve()

        prompts_dir = Path(
            os.environ.get("DOCS_PROMPTS_DIR", str(_package_prompts_dir()))
        ).expanduser().resolve()

        return cls(
            collections={"default": default_collection},
            state_dir=state_dir,
            max_iterations=int(os.environ.get("DOCS_MAX_ITERATIONS", "5")),
            prompts_dir=prompts_dir,
            plane_stale_project=os.environ.get("DOCS_PLANE_STALE_PROJECT") or None,
        )

    def resolve_collection(self, name: str) -> CollectionConfig:
        if name in self.collections:
            return self.collections[name]
        raise ValueError(
            f"Unknown collection '{name}'. Known: {', '.join(sorted(self.collections))}"
        )

    def known_collections(self) -> list[str]:
        return sorted(self.collections)


def _parse_config_text(text: str, path: Path) -> dict:
    """Parse YAML or JSON, depending on file extension. No yaml dep when unused."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    # YAML path — hand-rolled for the simple shape we actually use, so we
    # don't drag in PyYAML for a ~30-line schema.
    return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """Parse the narrow YAML dialect used for docflow's config.

    Supports: nested mappings (2-space indent), scalar values, strings
    (bare or quoted). No lists, no flow-style, no multi-doc. Enough for
    the config format and tiny enough to not need PyYAML.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.lstrip(" ")
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            new_map: dict = {}
            parent[key] = new_map
            stack.append((indent, new_map))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _parse_scalar(v: str):
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "none", "~"):
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v
