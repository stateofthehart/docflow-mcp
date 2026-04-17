"""Commit drafts to disk + git, or escalate via draft PR + Plane issue."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommitResult:
    target_path: str
    branch: str | None
    sha: str | None
    strategy: str  # "direct" | "branch"


class Committer:
    def __init__(self, default_branch_prefix: str = "docs/agent-"):
        self.branch_prefix = default_branch_prefix

    def commit_direct(
        self,
        repo: Path,
        target_path: Path,
        content: str,
        message: str,
    ) -> CommitResult:
        """Write content to target_path and commit on the current branch."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        if not target_path.read_text().endswith("\n"):
            with target_path.open("a", encoding="utf-8") as f:
                f.write("\n")

        rel = str(target_path.relative_to(repo))
        self._git(repo, ["add", rel])
        self._git(repo, ["commit", "-m", message])
        sha = self._git(repo, ["rev-parse", "HEAD"]).strip()
        branch = self._current_branch(repo)
        return CommitResult(
            target_path=rel, branch=branch, sha=sha[:12], strategy="direct"
        )

    def commit_on_branch(
        self,
        repo: Path,
        target_path: Path,
        content: str,
        message: str,
        branch: str,
    ) -> CommitResult:
        """Create/checkout branch, write content, commit, return to original branch."""
        original = self._current_branch(repo)
        try:
            existing = self._git(repo, ["branch", "--list", branch]).strip()
            if existing:
                self._git(repo, ["checkout", branch])
            else:
                self._git(repo, ["checkout", "-b", branch])

            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            if not target_path.read_text().endswith("\n"):
                with target_path.open("a", encoding="utf-8") as f:
                    f.write("\n")

            rel = str(target_path.relative_to(repo))
            self._git(repo, ["add", rel])
            self._git(repo, ["commit", "-m", message])
            sha = self._git(repo, ["rev-parse", "HEAD"]).strip()
            return CommitResult(
                target_path=rel, branch=branch, sha=sha[:12], strategy="branch"
            )
        finally:
            if original and original != branch:
                try:
                    self._git(repo, ["checkout", original])
                except subprocess.CalledProcessError:
                    # Leave it on the branch if we can't switch back (e.g., uncommitted)
                    pass

    def open_draft_pr(
        self,
        repo: Path,
        branch: str,
        title: str,
        body: str,
    ) -> str | None:
        """Push branch and open a draft PR via gh. Returns PR URL or None."""
        try:
            self._git(repo, ["push", "-u", "origin", branch])
        except subprocess.CalledProcessError as e:
            return f"ERROR pushing branch: {e}"
        try:
            res = subprocess.run(
                ["gh", "pr", "create", "--draft", "--title", title, "--body", body],
                cwd=repo, capture_output=True, text=True, check=True,
            )
            return res.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return f"ERROR opening PR: {e}"

    # ── Private ───────────────────────────────────────────────────

    @staticmethod
    def _git(repo: Path, args: list[str]) -> str:
        res = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
        return res.stdout

    def _current_branch(self, repo: Path) -> str | None:
        try:
            return self._git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except subprocess.CalledProcessError:
            return None
