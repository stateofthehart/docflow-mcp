"""SQLite state store for drafts, revisions, and reviews.

State lives on disk so a crash or MCP restart does not lose in-flight drafts.
Concurrent access is serialized by SQLite's default transaction semantics;
the store is intended for single-process use but is safe against interleaved
tool calls from one agent.

Tables:
    drafts         one row per draft; holds kind/scope/path/state/iteration
    draft_content  one row per iteration of content
    reviews        one row per review attempt
    commits        one row per successful commit, pointing at the SHA
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

DraftState = Literal["drafting", "reviewed", "committed", "escalated", "abandoned"]
DraftKind = Literal["decision", "section", "stale"]
Verdict = Literal["approve", "revise", "escalate"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    scope           TEXT NOT NULL,
    path            TEXT,
    state           TEXT NOT NULL,
    iteration       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    author_model    TEXT,
    metadata_json   TEXT
);

CREATE TABLE IF NOT EXISTS draft_content (
    draft_id        TEXT NOT NULL,
    iteration       INTEGER NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (draft_id, iteration),
    FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reviews (
    draft_id            TEXT NOT NULL,
    iteration           INTEGER NOT NULL,
    verdict             TEXT NOT NULL,
    issues_json         TEXT NOT NULL,
    notes               TEXT,
    reviewer_model      TEXT,
    reviewer_prompt_hash TEXT,
    reviewed_at         TEXT NOT NULL,
    PRIMARY KEY (draft_id, iteration),
    FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commits (
    draft_id        TEXT PRIMARY KEY,
    target_path     TEXT NOT NULL,
    branch          TEXT,
    sha             TEXT,
    committed_at    TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drafts_state ON drafts(state);
CREATE INDEX IF NOT EXISTS idx_drafts_updated ON drafts(updated_at);
"""


@dataclass
class Draft:
    id: str
    kind: DraftKind
    scope: str
    path: str | None
    state: DraftState
    iteration: int
    created_at: str
    updated_at: str
    author_model: str | None
    metadata: dict


@dataclass
class Review:
    draft_id: str
    iteration: int
    verdict: Verdict
    issues: list[dict]
    notes: str | None
    reviewer_model: str | None
    reviewer_prompt_hash: str | None
    reviewed_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """SQLite-backed persistence for all draft workflow state."""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = state_dir / "state.db"
        self._conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # autocommit; explicit txns via _tx()
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    # ── Transactions ──────────────────────────────────────────────

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── Draft lifecycle ───────────────────────────────────────────

    def create_draft(
        self,
        kind: DraftKind,
        scope: str,
        path: str | None,
        content: str,
        author_model: str | None = None,
        metadata: dict | None = None,
    ) -> Draft:
        draft_id = uuid.uuid4().hex[:12]
        now = _now()
        meta_json = json.dumps(metadata or {})
        with self._tx() as c:
            c.execute(
                "INSERT INTO drafts (id, kind, scope, path, state, iteration, "
                "created_at, updated_at, author_model, metadata_json) "
                "VALUES (?, ?, ?, ?, 'drafting', 0, ?, ?, ?, ?)",
                (draft_id, kind, scope, path, now, now, author_model, meta_json),
            )
            c.execute(
                "INSERT INTO draft_content (draft_id, iteration, content, created_at) "
                "VALUES (?, 0, ?, ?)",
                (draft_id, content, now),
            )
        return self.get_draft(draft_id)  # type: ignore[return-value]

    def revise_draft(self, draft_id: str, new_content: str) -> Draft:
        draft = self.get_draft(draft_id)
        if draft is None:
            raise LookupError(f"No draft with id '{draft_id}'")
        if draft.state not in ("drafting", "reviewed"):
            raise ValueError(
                f"Cannot revise draft in state '{draft.state}'. "
                f"Only 'drafting' or 'reviewed' drafts accept revisions."
            )
        next_iter = draft.iteration + 1
        now = _now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO draft_content (draft_id, iteration, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (draft_id, next_iter, new_content, now),
            )
            c.execute(
                "UPDATE drafts SET state='drafting', iteration=?, updated_at=? WHERE id=?",
                (next_iter, now, draft_id),
            )
        return self.get_draft(draft_id)  # type: ignore[return-value]

    def mark_reviewed(self, draft_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE drafts SET state='reviewed', updated_at=? WHERE id=?",
                (_now(), draft_id),
            )

    def mark_committed(
        self, draft_id: str, target_path: str, branch: str | None, sha: str | None
    ) -> None:
        now = _now()
        with self._tx() as c:
            c.execute(
                "UPDATE drafts SET state='committed', updated_at=? WHERE id=?",
                (now, draft_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO commits "
                "(draft_id, target_path, branch, sha, committed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, target_path, branch, sha, now),
            )

    def mark_escalated(self, draft_id: str, reason: str) -> None:
        draft = self.get_draft(draft_id)
        meta = draft.metadata if draft else {}
        meta["escalation_reason"] = reason
        with self._tx() as c:
            c.execute(
                "UPDATE drafts SET state='escalated', updated_at=?, metadata_json=? WHERE id=?",
                (_now(), json.dumps(meta), draft_id),
            )

    def abandon(self, draft_id: str, reason: str) -> None:
        draft = self.get_draft(draft_id)
        meta = draft.metadata if draft else {}
        meta["abandon_reason"] = reason
        with self._tx() as c:
            c.execute(
                "UPDATE drafts SET state='abandoned', updated_at=?, metadata_json=? WHERE id=?",
                (_now(), json.dumps(meta), draft_id),
            )

    # ── Reviews ───────────────────────────────────────────────────

    def record_review(
        self,
        draft_id: str,
        iteration: int,
        verdict: Verdict,
        issues: list[dict],
        notes: str | None,
        reviewer_model: str | None,
        reviewer_prompt_hash: str | None,
    ) -> Review:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO reviews "
                "(draft_id, iteration, verdict, issues_json, notes, reviewer_model, "
                " reviewer_prompt_hash, reviewed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id,
                    iteration,
                    verdict,
                    json.dumps(issues),
                    notes,
                    reviewer_model,
                    reviewer_prompt_hash,
                    _now(),
                ),
            )
        return Review(
            draft_id=draft_id,
            iteration=iteration,
            verdict=verdict,
            issues=issues,
            notes=notes,
            reviewer_model=reviewer_model,
            reviewer_prompt_hash=reviewer_prompt_hash,
            reviewed_at=_now(),
        )

    def latest_review(self, draft_id: str) -> Review | None:
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE draft_id=? ORDER BY iteration DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
        return _review_from_row(row) if row else None

    def review_for_iteration(self, draft_id: str, iteration: int) -> Review | None:
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE draft_id=? AND iteration=?",
            (draft_id, iteration),
        ).fetchone()
        return _review_from_row(row) if row else None

    # ── Reads ─────────────────────────────────────────────────────

    def get_draft(self, draft_id: str) -> Draft | None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        return _draft_from_row(row) if row else None

    def get_content(self, draft_id: str, iteration: int | None = None) -> str:
        draft = self.get_draft(draft_id)
        if draft is None:
            raise LookupError(f"No draft with id '{draft_id}'")
        it = draft.iteration if iteration is None else iteration
        row = self._conn.execute(
            "SELECT content FROM draft_content WHERE draft_id=? AND iteration=?",
            (draft_id, it),
        ).fetchone()
        if row is None:
            raise LookupError(f"No content for draft '{draft_id}' iteration {it}")
        return row["content"]

    def list_drafts(
        self, state: DraftState | None = None, limit: int = 50
    ) -> list[Draft]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM drafts WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM drafts ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_draft_from_row(r) for r in rows]

    def all_reviews(self, draft_id: str) -> list[Review]:
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE draft_id=? ORDER BY iteration ASC",
            (draft_id,),
        ).fetchall()
        return [_review_from_row(r) for r in rows]

    # ── Maintenance ───────────────────────────────────────────────

    def gc_abandoned(self, days: int) -> int:
        """Delete drafts that have been abandoned for more than `days`. Returns count."""
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM drafts WHERE state='abandoned' AND updated_at < ?",
                (cutoff_iso,),
            )
            return cur.rowcount


def _draft_from_row(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        kind=row["kind"],
        scope=row["scope"],
        path=row["path"],
        state=row["state"],
        iteration=row["iteration"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        author_model=row["author_model"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _review_from_row(row: sqlite3.Row) -> Review:
    return Review(
        draft_id=row["draft_id"],
        iteration=row["iteration"],
        verdict=row["verdict"],
        issues=json.loads(row["issues_json"]),
        notes=row["notes"],
        reviewer_model=row["reviewer_model"],
        reviewer_prompt_hash=row["reviewer_prompt_hash"],
        reviewed_at=row["reviewed_at"],
    )
