# docflow-mcp

MCP server for agent-authored documentation.

## What it does

Exposes a state-machine-enforced tool surface that lets any MCP-capable LLM
draft, review, revise, commit, and escalate documentation changes against
one or more markdown repositories. Reviews are delegated to a sub-agent
running through an LLM gateway (MyLLM by default) so the author and the
reviewer can be different models with different tool stacks. All draft
state lives in SQLite so nothing is lost on restart.

Not tied to any particular project — point it at any docs tree.

## Tools

Read-side: `search`, `read`, `list_docs`, `recent`.

Write-side: `draft`, `review`, `revise`, `commit`, `escalate`.

Admin: `status`, `abandon`.

The workflow: `draft → review → (revise → review)* → commit | escalate`.
`commit` is machine-checked: it refuses unless the most recent review for
the current iteration returned `approve`.

## Configuration

Required:

- `DOCS_ROOT` — absolute path of the documentation root repository.

Optional:

- `DOCS_SCOPE_MAP` — `scope=path` pairs separated by `:`, pointing at
  additional repositories. Example:
  `qf-redis=/home/me/quant/qf/redis:qf-market=/home/me/quant/qf/market`.
- `DOCS_STATE_DIR` — where SQLite state lives. Default `$DOCS_ROOT/.docs-state`.
- `DOCS_REVIEWER_URL` — MyLLM gateway. Default `http://localhost:4000`.
- `DOCS_REVIEWER_PROFILE` — MyLLM profile. Default `docs-reviewer`.
- `DOCS_MAX_ITERATIONS` — max revise loops before auto-escalate. Default `5`.
- `DOCS_REVIEW_TIMEOUT` — seconds. Default `600`.
- `DOCS_PROMPTS_DIR` — override the shipped reviewer prompts.
- `DOCS_PLANE_STALE_PROJECT` — Plane project id for staleness issues.
  If unset, stale flags return their report text instead of filing an issue.

## Install

```bash
git clone https://github.com/stateofthehart/docflow-mcp ~/.mcps/docflow
cd ~/.mcps/docflow
uv venv .venv && uv pip install --python .venv/bin/python -e .
```

Register with Claude Code / OpenCode:

```bash
claude mcp add -s project docflow \
  -e "DOCS_ROOT=$(pwd)" \
  -- ~/.mcps/docflow/.venv/bin/python -m docflow_mcp
```

## Reviewer setup

The reviewer is an LLM sub-agent invoked via MyLLM's `spawn_agent`. Configure
a `docs-reviewer` profile in your MyLLM config with:

- A different model family from your author (e.g. if authors run on Claude,
  point the reviewer at Gemini — enforces perspective diversity).
- Access to: code graph (axon), docs read-side tools, Plane read-side tools.
- No write tools. The reviewer never edits documents.

## Prompts

Three kind-specific reviewer prompts live in `prompts/`:

- `decision.md` — ADR reviewer (structural + ground-truth + consistency)
- `section.md` — section-update reviewer (reason + loss-of-info + contradiction)
- `stale.md` — stale-flag reviewer (evidence quality + classification)

Each prompt is hashed and the hash is stored with every review. When the
prompt changes, old reviews remain traceable to the prompt that produced them.
