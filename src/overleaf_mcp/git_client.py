"""Git-based client for Overleaf project operations.

Handles clone, pull, push, diff, history, and file I/O through the
Overleaf Git bridge (``git.overleaf.com``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from git import GitCommandError, Repo

from .config import (
    DIFF_CONTEXT_LINES,
    DIFF_MAX_OUTPUT_CHARS,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    OVERLEAF_GIT_HOST,
    TEMP_DIR,
    ProjectConfig,
)

logger = logging.getLogger("overleaf-mcp")

# Per-project lock to prevent concurrent git operations on the same repo
_project_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_project_lock(project_id: str) -> threading.Lock:
    """Get or create a threading lock for a specific project."""
    with _locks_lock:
        if project_id not in _project_locks:
            _project_locks[project_id] = threading.Lock()
        return _project_locks[project_id]


def _repo_path(project_id: str) -> Path:
    return Path(TEMP_DIR) / project_id


def _git_url(project: ProjectConfig) -> str:
    return f"https://git:{project.git_token}@{OVERLEAF_GIT_HOST}/{project.project_id}"


def _config_git_user(repo: Repo) -> None:
    """Ensure git user.name and user.email are configured."""
    try:
        repo.config_reader().get_value("user", "name")
    except Exception:
        name = os.environ.get("OVERLEAF_GIT_AUTHOR_NAME", "Overleaf MCP")
        email = os.environ.get("OVERLEAF_GIT_AUTHOR_EMAIL", "mcp@overleaf.local")
        with repo.config_writer() as cw:
            cw.set_value("user", "name", name)
            cw.set_value("user", "email", email)


def validate_path(base: Path, target: str) -> Path:
    """Ensure *target* doesn't escape the repo root."""
    resolved = (base / target).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise ValueError(f"Path '{target}' escapes repository root")
    return resolved


def ensure_repo(project: ProjectConfig) -> Repo:
    """Clone or pull the project repo.  Thread-safe per project."""
    lock = _get_project_lock(project.project_id)
    with lock:
        rp = _repo_path(project.project_id)
        git_url = _git_url(project)

        if rp.exists():
            repo = Repo(rp)
            try:
                repo.remotes.origin.pull()
            except GitCommandError as e:
                logger.warning("git pull failed for %s: %s", project.project_id, e)
            return repo

        rp.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning project %s …", project.project_id)
        return Repo.clone_from(git_url, rp)


# ---------------------------------------------------------------------------
# Async wrappers (run blocking git in a thread-pool)
# ---------------------------------------------------------------------------

async def async_ensure_repo(project: ProjectConfig) -> Repo:
    return await asyncio.to_thread(ensure_repo, project)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_files(project: ProjectConfig, extension: str = "") -> list[str]:
    """List files in the project, optionally filtered by extension."""
    ensure_repo(project)
    rp = _repo_path(project.project_id)
    files = []
    for p in sorted(rp.rglob("*")):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(rp).parts):
            if not extension or p.suffix == extension:
                files.append(str(p.relative_to(rp)))
    return files


def read_file(project: ProjectConfig, file_path: str) -> str:
    """Read a file from the project."""
    ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)
    if not target.exists():
        raise FileNotFoundError(f"File '{file_path}' not found in project")
    return target.read_text(encoding="utf-8")


def list_history(
    project: ProjectConfig,
    limit: int | None = None,
    file_path: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Return git commit history."""
    repo = ensure_repo(project)
    n = min(limit or HISTORY_LIMIT_DEFAULT, HISTORY_LIMIT_MAX)

    kwargs: dict[str, Any] = {"max_count": n}
    if file_path:
        kwargs["paths"] = file_path
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until

    commits = list(repo.iter_commits(**kwargs))
    results = []
    for c in commits:
        results.append(
            {
                "hash": c.hexsha,
                "short": c.hexsha[:8],
                "date": c.committed_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "author": f"{c.author.name} <{c.author.email}>",
                "message": c.message.strip()[:200],
            }
        )
    return results


def get_diff(
    project: ProjectConfig,
    from_ref: str | None = None,
    to_ref: str | None = None,
    file_path: str | None = None,
    context_lines: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Return a git diff."""
    repo = ensure_repo(project)
    ctx = max(0, min(context_lines or DIFF_CONTEXT_LINES, 10))
    limit = max(2000, max_chars or DIFF_MAX_OUTPUT_CHARS)

    args: list[str] = []
    fr = from_ref or "HEAD"
    if to_ref:
        args = [fr, to_ref]
    else:
        args = [fr]

    try:
        if file_path:
            diff = repo.git.diff(*args, "--", file_path, unified=ctx, no_color=True)
        else:
            diff = repo.git.diff(*args, unified=ctx, no_color=True)
    except GitCommandError as e:
        return {"diff": f"Error: {e}", "truncated": False}

    truncated = len(diff) > limit
    return {"diff": diff[:limit] if truncated else diff, "truncated": truncated}


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def create_file(
    project: ProjectConfig,
    file_path: str,
    content: str,
    commit_message: str | None = None,
) -> str:
    """Create a new file, commit and push."""
    repo = ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)

    if target.exists():
        raise FileExistsError(f"File '{file_path}' already exists. Use edit_file or rewrite_file.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    _config_git_user(repo)
    repo.index.add([file_path])
    repo.index.commit(commit_message or f"Add {file_path}")
    repo.remotes.origin.push()
    return f"Created and pushed '{file_path}'"


def edit_file(
    project: ProjectConfig,
    file_path: str,
    old_string: str,
    new_string: str,
    commit_message: str | None = None,
) -> str:
    """Surgical search-and-replace edit, commit and push."""
    repo = ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)

    if not target.exists():
        raise FileNotFoundError(f"File '{file_path}' not found")

    content = target.read_text(encoding="utf-8")
    if old_string not in content:
        preview = content[:500] + ("…" if len(content) > 500 else "")
        raise ValueError(f"old_string not found in '{file_path}'. Preview:\n{preview}")

    count = content.count(old_string)
    if count > 1:
        raise ValueError(
            f"old_string appears {count} times in '{file_path}'. "
            "Make it more specific to match exactly once."
        )

    new_content = content.replace(old_string, new_string, 1)
    target.write_text(new_content, encoding="utf-8")

    _config_git_user(repo)
    repo.index.add([file_path])
    repo.index.commit(commit_message or f"Edit {file_path}")
    repo.remotes.origin.push()
    return f"Edited and pushed '{file_path}'"


def rewrite_file(
    project: ProjectConfig,
    file_path: str,
    content: str,
    commit_message: str | None = None,
) -> str:
    """Replace entire file contents, commit and push."""
    repo = ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)

    if not target.exists():
        raise FileNotFoundError(f"File '{file_path}' not found. Use create_file instead.")

    target.write_text(content, encoding="utf-8")

    _config_git_user(repo)
    repo.index.add([file_path])
    repo.index.commit(commit_message or f"Rewrite {file_path}")
    repo.remotes.origin.push()
    return f"Rewrote and pushed '{file_path}'"


def delete_file(
    project: ProjectConfig,
    file_path: str,
    commit_message: str | None = None,
) -> str:
    """Delete a file, commit and push."""
    repo = ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)

    if not target.exists():
        raise FileNotFoundError(f"File '{file_path}' not found")

    _config_git_user(repo)
    repo.index.remove([file_path])
    target.unlink()
    repo.index.commit(commit_message or f"Delete {file_path}")
    repo.remotes.origin.push()
    return f"Deleted and pushed '{file_path}'"


def sync_project(project: ProjectConfig) -> str:
    """Pull latest changes from Overleaf."""
    rp = _repo_path(project.project_id)
    if not rp.exists():
        ensure_repo(project)
        return f"Cloned project '{project.name}'"

    repo = Repo(rp)
    if repo.is_dirty():
        return "Warning: uncommitted local changes. Commit or discard them before syncing."

    try:
        repo.remotes.origin.pull()
        return f"Synced project '{project.name}' with Overleaf"
    except GitCommandError as e:
        return f"Error syncing: {e}"
