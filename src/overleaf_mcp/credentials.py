"""Credential lookup for Overleaf MCP.

Credentials are supplied via environment variables by the host that
launches the server (e.g. an MCP client's config, a shell profile, or a
docker-compose file):

* ``OVERLEAF_SESSION``    — value of the ``overleaf_session2`` cookie
                            (required for list_projects / compile / pdf /
                            create_project).
* ``OVERLEAF_GIT_TOKEN``  — Overleaf Git Integration token (required for
                            read / edit / history / diff operations).
"""

from __future__ import annotations

import os


def get_session() -> str:
    """Return the Overleaf session cookie, or an empty string if unset."""
    return os.environ.get("OVERLEAF_SESSION", "")


def get_git_token() -> str:
    """Return the Overleaf git token, or an empty string if unset."""
    return os.environ.get("OVERLEAF_GIT_TOKEN", "")
