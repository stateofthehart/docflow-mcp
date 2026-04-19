"""Commit drafts to disk + git, with git_mode-aware behavior.

Three git modes per collection:
    remote   — full flow: local commit, push to origin, open draft PR.
    local    — local commit only; push + PR skipped (no origin assumed).
    disabled — plain file write, no git calls.

File-locking protects git operations from concurrent writers so two
agents committing to the same docs_root can't race on .git/index.lock.

Pre-flight helpers (gh_auth_ok, has_origin) let callers surface auth /
remote issues cleanly before starting a multi-step operation.
"""

from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class CommitResult:
    target_path: str
    branch: str | None
    sha: str | None
    strategy: str  # "direct" | "branch" | "file-only"


class GitModeError(Exception):
    """Raised when an operation is incompatible with the declared git_mode."""


class Committer:
    def __init__(self, default_branch_prefix: str = "docs/agent-"):
        self.branch_prefix = default_branch_prefix

    # ── Pre-flight probes ─────────────────────────────────────────

    @staticmethod
    def gh_auth_ok() -> tuple[bool, str]:
        """Return (ok, detail) for gh CLI authentication status."""
        try:
            res = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if res.returncode == 0:
                return True, "authenticated"
            return False, res.stderr.strip() or res.stdout.strip() or "not authenticated"
        except FileNotFoundError:
            return False, "gh CLI not installed"
        except subprocess.TimeoutExpired:
            return False, "gh auth status timed out"

    @staticmethod
    def has_origin(repo: Path) -> bool:
        """Check whether this repo has an 'origin' remote configured."""
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "remote"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return res.returncode == 0 and "origin" in res.stdout.split()
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    @staticmethod
    def branch_exists_on_origin(repo: Path, branch: str) -> bool:
        """Whether origin already has the branch."""
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "ls-remote", "--heads", "origin", branch],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return res.returncode == 0 and bool(res.stdout.strip())
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    # ── File lock ─────────────────────────────────────────────────

    @contextmanager
    def _locked(self, repo: Path) -> Iterator[None]:
        """Exclusive lock on a per-repo sentinel file to serialize git ops."""
        lock_dir = repo / ".git"
        lock_dir.mkdir(exist_ok=True)
        lock_file = lock_dir / "docflow.lock"
        with lock_file.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # ── Commit operations ────────────────────────────────────────

    def commit_direct(
        self,
        repo: Path,
        target_path: Path,
        content: str,
        message: str,
        git_mode: str = "remote",
    ) -> CommitResult:
        """Write content to target_path. Git-commit unless git_mode=disabled."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        if not target_path.read_text().endswith("\n"):
            with target_path.open("a", encoding="utf-8") as f:
                f.write("\n")

        rel = str(target_path.relative_to(repo))

        if git_mode == "disabled":
            return CommitResult(
                target_path=rel, branch=None, sha=None, strategy="file-only"
            )

        with self._locked(repo):
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
        git_mode: str = "remote",
    ) -> CommitResult:
        """Create/checkout branch, write content, commit. Requires git.

        Raises GitModeError when git_mode='disabled' — escalate can't branch
        without git.
        """
        if git_mode == "disabled":
            raise GitModeError(
                "commit_on_branch requires a git-tracked collection; "
                "git_mode='disabled' has no branches."
            )

        with self._locked(repo):
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
                # Only commit if there's something staged — makes this idempotent
                # on retry when the same content is already on the branch.
                staged = self._git(repo, ["diff", "--cached", "--name-only"]).strip()
                if staged:
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
                        pass

    def push_branch(self, repo: Path, branch: str) -> tuple[bool, str]:
        """Push a branch to origin. Returns (ok, detail)."""
        try:
            self._git(repo, ["push", "-u", "origin", branch])
            return True, "pushed"
        except subprocess.CalledProcessError as e:
            return False, f"push failed: {e.stderr or e}"

    def open_pr(
        self, repo: Path, branch: str, title: str, body: str
    ) -> tuple[bool, str]:
        """Open a draft PR via gh. Returns (ok, url-or-detail)."""
        try:
            res = subprocess.run(
                [
                    "gh", "pr", "create", "--draft",
                    "--head", branch,
                    "--title", title,
                    "--body", body,
                ],
                cwd=repo, capture_output=True, text=True, check=True,
            )
            return True, res.stdout.strip()
        except subprocess.CalledProcessError as e:
            return False, f"gh pr create failed: {e.stderr.strip() or e}"
        except FileNotFoundError:
            return False, "gh CLI not installed"

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
