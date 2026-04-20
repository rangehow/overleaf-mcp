"""Configuration management for Overleaf MCP Server."""

from __future__ import annotations

import logging
import os
import re

from pydantic import BaseModel

from .credentials import get_git_token

# Overleaf project IDs are 24-character lowercase hex strings (MongoDB ObjectIDs)
_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")

logger = logging.getLogger("overleaf-mcp")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
TEMP_DIR = os.environ.get("OVERLEAF_TEMP_DIR", "./overleaf_cache")
OVERLEAF_BASE_URL = os.environ.get("OVERLEAF_BASE_URL", "https://www.overleaf.com")
OVERLEAF_GIT_HOST = os.environ.get("OVERLEAF_GIT_HOST", "git.overleaf.com")

# History / diff limits
HISTORY_LIMIT_DEFAULT = int(os.environ.get("HISTORY_LIMIT_DEFAULT", "20"))
HISTORY_LIMIT_MAX = int(os.environ.get("HISTORY_LIMIT_MAX", "200"))
DIFF_CONTEXT_LINES = int(os.environ.get("DIFF_CONTEXT_LINES", "3"))
DIFF_MAX_OUTPUT_CHARS = int(os.environ.get("DIFF_MAX_OUTPUT_CHARS", "120000"))


class ProjectConfig(BaseModel):
    """Configuration for a single Overleaf project."""

    name: str
    project_id: str
    git_token: str


def _get_git_token() -> str:
    """Get the account-level git token from the OVERLEAF_GIT_TOKEN env var."""
    token = get_git_token()
    if not token:
        raise ValueError(
            "Git token not configured. Set the OVERLEAF_GIT_TOKEN environment "
            "variable.  Generate a token at: "
            "https://www.overleaf.com/user/settings → Git Integration → Create Token"
        )
    return token


def get_project(project_id: str) -> ProjectConfig:
    """Build a ProjectConfig for a given project ID.

    The git token is account-level — one token works for all projects.
    Just provide a project_id (from list_projects or the Overleaf URL).
    """
    if not project_id:
        raise ValueError(
            "project_id is required. "
            "Use list_projects to discover your projects, or copy it from the Overleaf URL."
        )
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid project_id {project_id!r}. Overleaf project IDs are "
            "24-character lowercase hex strings (e.g. '692a83fb82feceb233c4b0e7'). "
            "This tool operates on the REMOTE Overleaf repo, not your local filesystem — "
            "a filesystem path like '.' or 'overleaf-project/' is NOT a project_id. "
            "If the project is already downloaded locally, use the standard read_files / "
            "grep_search tools on the local path instead. "
            "Otherwise, call list_projects to discover the correct 24-hex ID."
        )
    token = _get_git_token()
    return ProjectConfig(
        name=f"Project {project_id[:8]}…",
        project_id=project_id,
        git_token=token,
    )
