#!/usr/bin/env python3
"""Overleaf MCP Server — the most comprehensive MCP server for Overleaf.

Provides tools for full CRUD, LaTeX structure analysis, git history,
diff, compilation, and PDF download.

Setup — set these environment variables before launching the server:

  OVERLEAF_SESSION    (required)
      Value of the `overleaf_session2` browser cookie. Needed for
      list_projects, compile_project, download_pdf, download_log,
      download_source_zip, download_source,
      and create_project. Get it from DevTools → Application → Cookies
      → https://www.overleaf.com → `overleaf_session2` → Value
      (starts with ``s%3A…``).

  OVERLEAF_GIT_TOKEN  (optional, required for read/edit operations)
      Token from https://www.overleaf.com/user/settings → "Git Integration"
      → "Create Token". Starts with ``olp_``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from . import __version__, git_client, verify as _verify_mod
from .config import get_project, project_url
from .credentials import get_git_token, get_session
from .latex import get_section_content, parse_sections, update_section

logger = logging.getLogger("overleaf-mcp")

# Optional compile module (gracefully missing)
try:
    from . import compile as _compile_mod

    _HAS_COMPILE = True
except ImportError:
    _HAS_COMPILE = False

# Layout perception (page count + source→page locator via SyncTeX). Depends on
# the same compile seam + session cookie, so it shares the _HAS_COMPILE guard.
try:
    from . import layout as _layout_mod

    _HAS_LAYOUT = _HAS_COMPILE
except ImportError:
    _HAS_LAYOUT = False

# Free-tier web write (session-cookie only). Needs the `websockets` package
# (for the one-shot realtime ID-discovery round trip) on top of the
# `compile` extras, so it shares the _HAS_COMPILE guard but has its own
# ImportError fallback for the extra dependency.
try:
    from . import web_write as _web_write_mod

    _HAS_WEB_WRITE = _HAS_COMPILE
except ImportError:
    _HAS_WEB_WRITE = False

# ---------------------------------------------------------------------------
# Shared schema fragment
# ---------------------------------------------------------------------------
_PROJECT_ID_PROP = {
    "type": "string",
    "pattern": "^[0-9a-f]{24}$",
    "description": (
        "Overleaf project ID — a 24-character lowercase hex string "
        "(e.g. '692a83fb82feceb233c4b0e7'), obtained from list_projects "
        "or the Overleaf project URL. "
        "NOT a local filesystem path, NOT '.', NOT a project name or title. "
        "These tools operate on the REMOTE Overleaf repo. "
        "Only call these overleaf_* tools when the user explicitly asks to "
        "work with an Overleaf project — never for general local file I/O. "
        "If you already have a copy of THIS Overleaf project checked out on "
        "the local filesystem, prefer the standard read_files / grep_search "
        "tools against that local path. "
        "Always call list_projects first when unsure."
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════════════

_TOOLS: list[Tool] = [
    # ── CREATE ────────────────────────────────────────────────────────────
    Tool(
        name="create_file",
        description=(
            "Create a new file in an Overleaf project. "
            "Auto-creates parent folders. Commits and pushes immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {
                    "type": "string",
                    "description": "Path for the new file (e.g. 'chapters/intro.tex')",
                },
                "content": {"type": "string", "description": "File content"},
                "commit_message": {"type": "string", "description": "Git commit message"},
            },
            "required": ["project_id", "file_path", "content"],
        },
    ),
    Tool(
        name="create_project",
        description=(
            "Create a new blank Overleaf project via the web API. "
            "Requires OVERLEAF_SESSION env var (session cookie)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
            },
            "required": ["name"],
        },
    ),
    # ── READ ──────────────────────────────────────────────────────────────
    Tool(
        name="list_projects",
        description=(
            "List all Overleaf projects in your account. "
            "Requires OVERLEAF_SESSION env var. "
            "Returns project names and IDs — use any ID with other tools."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_files",
        description="List files in an Overleaf project, optionally filtered by extension.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "extension": {
                    "type": "string",
                    "description": "Filter by extension (e.g. '.tex', '.bib'). Empty = all.",
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="read_file",
        description="Read the contents of a file from an Overleaf project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string", "description": "Path to the file"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    Tool(
        name="verify_citations",
        description=(
            "Detect likely-hallucinated references in the project's "
            "bibliography. Reads the project's .bib file(s), then verifies "
            "each entry's DOI / arXiv id against free authoritative catalogues "
            "(CrossRef, arXiv) with ZERO LLM calls. Returns three buckets: "
            "verified (catalogue match), suspicious (a concrete identifier "
            "that definitively does NOT resolve — high-confidence), and "
            "unverifiable (no identifier / coverage gap / book / rate-limit — "
            "reported separately and NEVER as fabrication). Use to sanity-check "
            "an AI-assisted draft's citations before submission."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "bib_path": {
                    "type": "string",
                    "description": (
                        "Optional explicit path to a .bib file. Omit to "
                        "auto-discover every .bib in the project."
                    ),
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="get_sections",
        description=(
            "Parse a LaTeX file and extract its section/subsection structure. "
            "Returns types, titles, hierarchy levels, and content previews."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string", "description": "Path to the LaTeX file"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    Tool(
        name="get_section_content",
        description="Get the full content of a specific LaTeX section by its title.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "section_title": {"type": "string", "description": "Section title to retrieve"},
            },
            "required": ["project_id", "file_path", "section_title"],
        },
    ),
    Tool(
        name="list_history",
        description="Show git commit history for the project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "limit": {
                    "type": "integer",
                    "description": "Max commits (default 20, max 200)",
                },
                "file_path": {"type": "string", "description": "Filter to a specific file"},
                "since": {
                    "type": "string",
                    "description": "Git --since filter (e.g. '2.weeks', '2025-01-01')",
                },
                "until": {"type": "string", "description": "Git --until filter"},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="get_diff",
        description=(
            "Get a git diff between refs or the working tree. "
            "Useful for reviewing recent changes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "from_ref": {
                    "type": "string",
                    "description": "Start ref (e.g. 'HEAD~3', commit hash). Default: HEAD",
                },
                "to_ref": {
                    "type": "string",
                    "description": "End ref. Omit for working tree.",
                },
                "file_path": {"type": "string", "description": "Filter to a specific file"},
                "context_lines": {
                    "type": "integer",
                    "description": "Diff context lines (0-10, default 3)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate diff to N chars (default 120000)",
                },
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="status_summary",
        description="Get a quick overview: file count, structure of main .tex file, project status.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
            },
            "required": ["project_id"],
        },
    ),
    # ── UPDATE ────────────────────────────────────────────────────────────
    Tool(
        name="edit_file",
        description=(
            "Surgical search-and-replace edit in a file. "
            "old_string must match exactly once. Commits and pushes immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "commit_message": {"type": "string"},
            },
            "required": ["project_id", "file_path", "old_string", "new_string"],
        },
    ),
    Tool(
        name="rewrite_file",
        description="Replace entire file contents. Commits and pushes immediately.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "content": {"type": "string", "description": "New full file content"},
                "commit_message": {"type": "string"},
            },
            "required": ["project_id", "file_path", "content"],
        },
    ),
    Tool(
        name="update_section",
        description=(
            "Update a specific LaTeX section by title, preserving the header. "
            "Commits and pushes immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "section_title": {"type": "string"},
                "new_content": {
                    "type": "string",
                    "description": "New section body (excluding \\section{} header)",
                },
                "commit_message": {"type": "string"},
            },
            "required": ["project_id", "file_path", "section_title", "new_content"],
        },
    ),
    Tool(
        name="sync_project",
        description="Pull the latest changes from Overleaf (git pull).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="upload_file",
        description=(
            "Upload a local (possibly BINARY) file into an Overleaf project. "
            "Use this for images (PNG/JPG), PDFs, and other non-text assets "
            "that must not be UTF-8 decoded. Commits and pushes immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {
                    "type": "string",
                    "description": "Destination path inside the project (e.g. 'figures/cat.png').",
                },
                "source_path": {
                    "type": "string",
                    "description": "Local filesystem path of the file to upload.",
                },
                "commit_message": {"type": "string", "description": "Git commit message"},
                "overwrite": {
                    # Accept boolean OR string ("true"/"1"/"yes"/etc.) because
                    # LLM clients often emit JSON strings even when the schema
                    # asks for a bool. _coerce_bool() normalises both forms.
                    "type": ["boolean", "string"],
                    "description": (
                        "If true, replace an existing file at file_path. "
                        "Default false. Accepts boolean or case-insensitive "
                        "string ('true'/'false'/'1'/'0'/'yes'/'no')."
                    ),
                },
            },
            "required": ["project_id", "file_path", "source_path"],
        },
    ),
    # ── DELETE ────────────────────────────────────────────────────────────
    Tool(
        name="delete_file",
        description="Delete a file from the project. Commits and pushes immediately.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "commit_message": {"type": "string"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    # ── COMPILE / PDF ────────────────────────────────────────────────────
    Tool(
        name="compile_project",
        description=(
            "Trigger PDF compilation on Overleaf. "
            "Returns compilation status and output file list. "
            "Requires OVERLEAF_SESSION env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="download_pdf",
        description=(
            "Download the compiled PDF to a local path. "
            "Call compile_project first. Requires OVERLEAF_SESSION env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "output_path": {
                    "type": "string",
                    "description": "Local file path to save the PDF",
                },
            },
            "required": ["project_id", "output_path"],
        },
    ),
    Tool(
        name="download_log",
        description=(
            "Download the LaTeX compilation log (.log file) from Overleaf. "
            "Useful for debugging compilation errors. "
            "Requires OVERLEAF_SESSION env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
            },
            "required": ["project_id"],
        },
    ),
    # ── LAYOUT / SYNCTEX (page positions) ────────────────────────────────
    Tool(
        name="get_page_count",
        description=(
            "Compile the project and report the TOTAL number of pages in the "
            "PDF (parsed from the LaTeX log, with a PDF-parse fallback). Use "
            "this to answer 'how many pages is it?' or to drive a "
            "fill-exactly-N-pages editing loop. Requires OVERLEAF_SESSION."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project_id": _PROJECT_ID_PROP},
            "required": ["project_id"],
        },
    ),
    Tool(
        name="locate_in_pdf",
        description=(
            "Find WHERE a given source line lands in the compiled PDF, using "
            "SyncTeX. Returns the page number and bounding rectangle(s) "
            "{page,h,v,width,height} in PostScript points (v measured from the "
            "page top). Omit `file` to use the compile root. This is the "
            "element→page-position capability the source parser cannot "
            "provide. Requires OVERLEAF_SESSION."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "line": {
                    "type": "integer",
                    "description": "1-based source line number to locate.",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Project-relative path of the source file (e.g. "
                        "'main.tex' or 'latex/paper.tex'). Omit to use the "
                        "compile root (auto-detected from SyncTeX)."
                    ),
                },
                "column": {
                    "type": "integer",
                    "description": "1-based column (default 0). Usually leave unset.",
                },
            },
            "required": ["project_id", "line"],
        },
    ),
    Tool(
        name="section_page_map",
        description=(
            "Compile once, then map EVERY section/subsection heading (and "
            "\\end{document}) to the PDF page it starts on, plus the total "
            "page count. This is the 'perceive the position of each element on "
            "the page' overview — ideal for judging how content is distributed "
            "across pages and for a fill-exactly-N-pages workflow. Omit `file` "
            "to use the compile root. Requires OVERLEAF_SESSION."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file": {
                    "type": "string",
                    "description": (
                        "Project-relative path of the root .tex file. Omit to "
                        "auto-detect the compile root from SyncTeX."
                    ),
                },
            },
            "required": ["project_id"],
        },
    ),
    # ── SOURCE DOWNLOAD ──────────────────────────────────────────────────
    Tool(
        name="download_source_zip",
        description=(
            "Download the full project source as a ZIP file to a local path. "
            "Requires OVERLEAF_SESSION env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "output_path": {
                    "type": "string",
                    "description": "Local file path to save the .zip (e.g. '/tmp/project.zip').",
                },
            },
            "required": ["project_id", "output_path"],
        },
    ),
    Tool(
        name="download_source",
        description=(
            "Download the project source and extract it into a local directory. "
            "Creates the directory if missing. Fails if the directory is not empty "
            "unless overwrite=true. Requires OVERLEAF_SESSION env var."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "output_dir": {
                    "type": "string",
                    "description": "Local directory to extract the project source into.",
                },
                "overwrite": {
                    "type": ["boolean", "string"],
                    "description": (
                        "If true, extract even if output_dir is non-empty. "
                        "Default false. Accepts boolean or case-insensitive "
                        "string ('true'/'false'/'1'/'0'/'yes'/'no')."
                    ),
                },
            },
            "required": ["project_id", "output_dir"],
        },
    ),
    # ── FREE-TIER WEB WRITE (session-cookie only, no git token) ──────────
    Tool(
        name="web_list_files",
        description=(
            "List file paths in an Overleaf project WITHOUT requiring "
            "OVERLEAF_GIT_TOKEN. Uses a single short-lived realtime "
            "'joinProject' round trip to discover the project's file tree "
            "(this is not exposed via any REST endpoint). Use this instead "
            "of list_files when only OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project_id": _PROJECT_ID_PROP},
            "required": ["project_id"],
        },
    ),
    Tool(
        name="web_read_file",
        description=(
            "Read a file's contents from an Overleaf project WITHOUT "
            "requiring OVERLEAF_GIT_TOKEN. Downloads the project source ZIP "
            "with the session cookie and extracts one file. Use this "
            "instead of read_file when only OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string", "description": "Path to the file"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    Tool(
        name="web_rewrite_file",
        description=(
            "Create or fully overwrite a file's content in an Overleaf "
            "project WITHOUT requiring OVERLEAF_GIT_TOKEN. Uses Overleaf's "
            "web upload endpoint (the same one the drag-and-drop uploader "
            "calls): if a file already exists at file_path it is replaced "
            "in place, otherwise a new one is created. NOTE: this does NOT "
            "create missing parent folders \u2014 they must already exist. "
            "Use this instead of rewrite_file/create_file when only "
            "OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "content": {"type": "string", "description": "New full file content"},
            },
            "required": ["project_id", "file_path", "content"],
        },
    ),
    Tool(
        name="web_edit_file",
        description=(
            "Surgical search-and-replace edit in a file WITHOUT requiring "
            "OVERLEAF_GIT_TOKEN. old_string must match exactly once. "
            "Downloads the current content, replaces locally, then "
            "re-uploads the full file via the web upload endpoint. Use this "
            "instead of edit_file when only OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["project_id", "file_path", "old_string", "new_string"],
        },
    ),
    Tool(
        name="web_upload_file",
        description=(
            "Upload a local BINARY-safe file (images, PDFs, or any "
            "non-text asset) into an Overleaf project from base64-encoded "
            "content, WITHOUT requiring OVERLEAF_GIT_TOKEN. Accepts either "
            "a raw base64 string or a data: URL (e.g. "
            "'data:image/png;base64,...') \u2014 the data: prefix is "
            "stripped automatically. If a file already exists at file_path "
            "it is replaced in place, otherwise it is created. NOTE: this "
            "does NOT create missing parent folders \u2014 they must "
            "already exist. Use this instead of upload_file (which reads "
            "from a local filesystem path) when only OVERLEAF_SESSION is "
            "configured, or when the caller has no shared filesystem with "
            "the MCP server (e.g. a chat client attaching an image "
            "directly)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {
                    "type": "string",
                    "description": "Destination path inside the project (e.g. 'figures/cat.png').",
                },
                "content_base64": {
                    "type": "string",
                    "description": (
                        "Base64-encoded file content, or a data: URL "
                        "containing it. Decoded size is capped at 20 MB."
                    ),
                },
            },
            "required": ["project_id", "file_path", "content_base64"],
        },
    ),
    Tool(
        name="web_delete_file",
        description=(
            "Delete a file/doc from an Overleaf project WITHOUT requiring "
            "OVERLEAF_GIT_TOKEN. Use this instead of delete_file when only "
            "OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    Tool(
        name="web_get_sections",
        description=(
            "Parse a LaTeX file's section/subsection structure WITHOUT "
            "requiring OVERLEAF_GIT_TOKEN. Use this instead of get_sections "
            "when only OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string", "description": "Path to the LaTeX file"},
            },
            "required": ["project_id", "file_path"],
        },
    ),
    Tool(
        name="web_get_section_content",
        description=(
            "Get the full content of a specific LaTeX section by title "
            "WITHOUT requiring OVERLEAF_GIT_TOKEN. Use this instead of "
            "get_section_content when only OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "section_title": {"type": "string", "description": "Section title to retrieve"},
            },
            "required": ["project_id", "file_path", "section_title"],
        },
    ),
    Tool(
        name="web_update_section",
        description=(
            "Update a specific LaTeX section body by title, preserving the "
            "header, WITHOUT requiring OVERLEAF_GIT_TOKEN. Downloads the "
            "file, patches the section locally, then re-uploads the whole "
            "file. Use this instead of update_section when only "
            "OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "file_path": {"type": "string"},
                "section_title": {"type": "string"},
                "new_content": {
                    "type": "string",
                    "description": "New section body (excluding \\section{} header)",
                },
            },
            "required": ["project_id", "file_path", "section_title", "new_content"],
        },
    ),
    Tool(
        name="web_status_summary",
        description=(
            "Get a quick overview (file count, structure of the main .tex "
            "file, project URL) WITHOUT requiring OVERLEAF_GIT_TOKEN. Use "
            "this instead of status_summary when only OVERLEAF_SESSION is "
            "configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project_id": _PROJECT_ID_PROP},
            "required": ["project_id"],
        },
    ),
    Tool(
        name="web_verify_citations",
        description=(
            "Detect likely-hallucinated references in the project's "
            "bibliography WITHOUT requiring OVERLEAF_GIT_TOKEN (verifies "
            "each .bib entry's DOI/arXiv id against CrossRef & arXiv, zero "
            "LLM calls). Use this instead of verify_citations when only "
            "OVERLEAF_SESSION is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_PROP,
                "bib_path": {
                    "type": "string",
                    "description": (
                        "Optional explicit path to a .bib file. Omit to "
                        "auto-discover every .bib in the project."
                    ),
                },
            },
            "required": ["project_id"],
        },
    ),
]



# ═══════════════════════════════════════════════════════════════════════════
# Tool safety annotations
# ═══════════════════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
# ---------------
# ``annotations.readOnlyHint`` is not documentation — it is a CONTROL SIGNAL.
# An MCP host cannot know whether a tool mutates anything, so a careful host
# must assume the worst: it treats every un-annotated tool as a write, which
# means SERIAL dispatch and (in manual/approval modes) a confirmation prompt
# per call. Leaving all 25 tools un-annotated therefore forced a purely
# read-only workflow — open a project, list files, read a few, diff two
# revisions — down a one-at-a-time path with a prompt on every step.
#
# The hints are declared here as ONE table rather than inline on each ``Tool``
# so the whole safety partition can be read (and reviewed) at a glance; the
# completeness check below makes it impossible to add a tool and forget it.
#
# WHY WRITES ARE DECLARED EXPLICITLY RATHER THAN LEFT TO THE DEFAULT
# -------------------------------------------------------------------
# ``readOnlyHint`` defaults to false, so a write tool works fine unannotated.
# But "absent" and "false" mean different things to a reviewer: absent says
# nobody classified this tool, false says someone did and it mutates. Only the
# second is auditable, and only the second survives a new tool being added by
# someone who never read this comment.
#
# HOW EACH VERDICT WAS REACHED (derived from the dispatch branch, NOT the name)
# -----------------------------------------------------------------------------
# The spec's wording is "the tool does not modify its ENVIRONMENT" — not "does
# not modify the remote project". Reading the implementations against that
# wider test moves seven tools that *sound* read-only out of the read set:
#
#   * compile_project / download_log / get_page_count / locate_in_pdf /
#     section_page_map — every one of these RUNS A COMPILE on Overleaf's
#     servers (``layout._compile_with_editor_id`` mirrors
#     ``compile.compile_project``). That consumes remote compute, mutates the
#     project's build state, and is exactly the kind of expensive side effect
#     a host serializes deliberately. "It returns information" does not make
#     an operation read-only; what matters is what it does to get it.
#   * download_pdf / download_source_zip / download_source — these WRITE TO
#     THE USER'S FILESYSTEM at a caller-supplied path, and ``download_source``
#     can overwrite a non-empty directory when ``overwrite=true``. The local
#     disk is part of the environment.
#   * list_projects is a genuine read of the account's project list. It is
#     kept read-only: it touches the web dashboard only, runs no compile and
#     writes nothing.
#
# ``destructiveHint`` and ``idempotentHint`` are meaningful ONLY when
# ``readOnlyHint`` is false (per the spec), so they appear only on writes.

#: Tools that observe without changing anything: no remote mutation, no
#: compile, no local file writes.
_READ_ONLY_TOOLS = frozenset({
    "list_projects",
    "list_files",
    "read_file",
    "verify_citations",
    "get_sections",
    "get_section_content",
    "list_history",
    "get_diff",
    "status_summary",
    "web_list_files",
    "web_read_file",
    "web_get_sections",
    "web_get_section_content",
    "web_status_summary",
    "web_verify_citations",
})

#: Tools that change something — remote project state, remote compute, or the
#: local filesystem. Value is ``(destructive, idempotent)``; ``None`` leaves a
#: hint unstated rather than guessing.
#:
#: "destructive" follows the spec's own split: true when a call may DESTROY or
#: overwrite existing state, false when the update is purely additive.
_WRITE_TOOLS = {
    # ── Remote project mutations ──
    "create_file":         (False, False),  # additive; fails if it exists
    "create_project":      (False, False),  # additive: makes a new project
    "upload_file":         (True,  False),  # overwrite=true replaces a file
    "edit_file":           (True,  False),  # rewrites a region of a file
    "rewrite_file":        (True,  False),  # replaces entire file contents
    "update_section":      (True,  False),  # replaces a section body
    "delete_file":         (True,  False),  # removes a file outright
    # git pull: converges the local clone onto the remote; running it twice
    # changes nothing further, and it destroys no user-authored state.
    "sync_project":        (False, True),
    # ── Remote compute (a compile is a real side effect) ──
    "compile_project":     (False, False),
    "download_log":        (False, False),
    "get_page_count":      (False, False),
    "locate_in_pdf":       (False, False),
    "section_page_map":    (False, False),
    # ── Local filesystem writes ──
    "download_pdf":        (True,  True),   # overwrites output_path
    "download_source_zip": (True,  True),   # overwrites output_path
    # ── Free-tier web write (session-cookie only, no git token) ──
    "web_rewrite_file":    (True,  False),  # replaces or creates a file
    "web_edit_file":       (True,  False),  # rewrites a region of a file
    "web_upload_file":     (True,  False),  # replaces or creates a (binary) file
    "web_delete_file":     (True,  False),  # removes a file/doc outright
    "web_update_section":  (True,  False),  # replaces a section body
    "download_source":     (True,  True),   # overwrite=true clobbers a dir
}


def _apply_tool_annotations(tools: list[Tool]) -> None:
    """Stamp every tool with its safety hints, refusing to leave one unclassified.

    Raises:
        RuntimeError: a tool is missing from both tables, appears in both, or a
            table names a tool that does not exist. Failing at import time is
            deliberate: a silently unclassified tool would default to "write"
            and quietly lose its parallelism, which is precisely the regression
            this table exists to prevent — and a missing WRITE classification
            would be far worse, since it decides whether a destructive call is
            allowed to skip an approval prompt.
    """
    names = {t.name for t in tools}
    classified = _READ_ONLY_TOOLS | set(_WRITE_TOOLS)

    both = _READ_ONLY_TOOLS & set(_WRITE_TOOLS)
    if both:
        raise RuntimeError(
            f"tool(s) declared BOTH read-only and write: {sorted(both)}")
    unclassified = names - classified
    if unclassified:
        raise RuntimeError(
            f"tool(s) missing a safety classification: {sorted(unclassified)}. "
            f"Add each to _READ_ONLY_TOOLS or _WRITE_TOOLS in server.py — a "
            f"tool left unclassified silently becomes a serial, "
            f"approval-gated write."
        )
    phantom = classified - names
    if phantom:
        raise RuntimeError(
            f"safety table names non-existent tool(s): {sorted(phantom)}")

    for tool in tools:
        if tool.name in _READ_ONLY_TOOLS:
            tool.annotations = ToolAnnotations(readOnlyHint=True)
        else:
            destructive, idempotent = _WRITE_TOOLS[tool.name]
            tool.annotations = ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=destructive,
                idempotentHint=idempotent,
            )


_apply_tool_annotations(_TOOLS)


# ═══════════════════════════════════════════════════════════════════════════
# Tool dispatch
# ═══════════════════════════════════════════════════════════════════════════

_SERVER_INSTRUCTIONS = """\
This server provides tools for the Overleaf LaTeX editor.

Credentials are read from environment variables set by the host
(e.g. the chatui MCP install dialog):

  OVERLEAF_SESSION    (required)
      Session cookie `overleaf_session2` — needed for list_projects,
      compile_project, download_pdf, download_log, create_project.
      Copy from browser DevTools → Application → Cookies →
      https://www.overleaf.com → overleaf_session2 → Value.
      (The cookie is HttpOnly, so it cannot be read from the JS console.)
      Sessions expire in ~30 days — when that happens, tools will fail
      and the user needs to update OVERLEAF_SESSION with a fresh cookie.

  OVERLEAF_GIT_TOKEN  (optional, required for read/edit/write tools)
      Git Integration token from https://www.overleaf.com/user/settings.
      Starts with `olp_`.

If a tool fails with an auth error, tell the user which env var to
update in their MCP server configuration, and how to obtain a fresh value.
"""

# ═══════════════════════════════════════════════════════════════════════════
# Handler registration — MCP SDK v2 (``on_*`` constructor parameters)
# ═══════════════════════════════════════════════════════════════════════════
#
# v1 registered handlers with decorators::
#
#     server = Server("overleaf-mcp", ...)
#
#     @server.list_tools()
#     async def handle_list_tools() -> list[Tool]: ...
#
#     @server.call_tool()
#     async def handle_call_tool(name, arguments) -> list[TextContent]: ...
#
# SDK 2.0.0 REMOVED that API outright — the low-level ``Server`` has no
# ``list_tools`` / ``call_tool`` attribute and no ``__getattr__`` fallback, so
# the decorators raised ``AttributeError`` AT IMPORT TIME (module scope), which
# is why 0.2.1 could not even start against mcp 2.x.
#
# Three things changed together, and all three matter:
#   1. Handlers are passed to the CONSTRUCTOR, not attached afterwards — so
#      they must be defined BEFORE the ``Server(...)`` call below.
#   2. Each handler receives ``(ctx, params)``. The request context is the
#      first argument; the old bare ``(name, arguments)`` shape is gone.
#   3. Handlers return the FULL result type (``ListToolsResult`` /
#      ``CallToolResult``). v2 removed the automatic wrapping that used to turn
#      a bare ``list[Tool]`` into a ``ListToolsResult``.
#
# One BEHAVIOURAL subtlety worth stating, because it is invisible in the diff:
# v2 no longer converts an escaping exception into ``CallToolResult(is_error=
# True)`` — a raised exception becomes a top-level JSON-RPC error instead, which
# the model never sees as tool output. The ``try/except`` in the call handler is
# therefore LOAD-BEARING under v2 in a way it was not under v1: it is the only
# thing that keeps a failing tool visible to the LLM as text it can react to.


async def handle_list_tools(
    ctx: ServerRequestContext,
    params: PaginatedRequestParams | None,
) -> ListToolsResult:
    """Return the full tool catalogue.

    ``_TOOLS`` is a module-level constant, so the order is already stable
    across calls and reconnects — which the 2026-07-28 revision explicitly
    asks for, since a shifting tool list invalidates the client's prompt cache.
    """
    return ListToolsResult(tools=_TOOLS)


async def handle_call_tool(
    ctx: ServerRequestContext,
    params: CallToolRequestParams,
) -> CallToolResult:
    """Dispatch one tool call and return its text result.

    Errors are deliberately returned as a NORMAL result whose text starts with
    ``Error:`` rather than raised. Two reasons, both load-bearing:
      * under v2 a raised exception becomes a transport-level JSON-RPC error the
        model cannot see or recover from;
      * Tofu's MCP credential health-probe classifies this server by matching
        phrases in the RESULT TEXT of a successful call (see the ``health_probe``
        entry for 'overleaf' in Tofu's lib/mcp/registry.py). Flipping these to
        ``is_error=True`` or to exceptions would silently break expiry detection.
    """
    try:
        result = await _dispatch(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=result)])
    except Exception as e:
        logger.error("Tool %s failed: %s", params.name, e, exc_info=True)
        return CallToolResult(content=[TextContent(type="text", text=f"Error: {e}")])


server = Server(
    "overleaf-mcp",
    version=__version__,
    instructions=_SERVER_INSTRUCTIONS,
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# Tools that only need a session cookie (not a git token)
_COOKIE_ONLY_TOOLS = {
    "list_projects",
    "compile_project",
    "download_pdf",
    "download_log",
    "get_page_count",
    "locate_in_pdf",
    "section_page_map",
    "download_source_zip",
    "download_source",
    "create_project",
    "web_list_files",
    "web_read_file",
    "web_rewrite_file",
    "web_edit_file",
    "web_upload_file",
    "web_delete_file",
    "web_get_sections",
    "web_get_section_content",
    "web_update_section",
    "web_status_summary",
    "web_verify_citations",
}


_MISSING_CREDENTIALS_HINT = (
    "How to obtain them:\n"
    "  • OVERLEAF_SESSION: log into https://www.overleaf.com, press F12 →\n"
    "    Application tab → Cookies → https://www.overleaf.com → copy the\n"
    "    Value of `overleaf_session2` (starts with `s%3A…`). It is HttpOnly,\n"
    "    so you must copy it from this DevTools panel — the JS console cannot\n"
    "    read it.\n"
    "  • OVERLEAF_GIT_TOKEN: https://www.overleaf.com/user/settings →\n"
    "    \"Git Integration\" → \"Create Token\" (starts with `olp_`).\n"
    "Set them in your MCP server configuration and restart the server."
)


_TRUTHY_STRINGS = {"true", "1", "yes", "y", "on"}
_FALSY_STRINGS = {"false", "0", "no", "n", "off", ""}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Normalise a JSON Schema boolean argument to a Python bool.

    LLM clients (Claude, GPT, Gemini) routinely emit JSON strings like
    ``"true"`` or ``"True"`` even when the tool schema declares ``boolean``.
    The MCP framework's schema validator then rejects the call before
    we can coerce. We work around this by declaring such fields as
    ``["boolean", "string"]`` in the schema and routing every value
    through this helper so the dispatch code can stay typed.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUTHY_STRINGS:
            return True
        if v in _FALSY_STRINGS:
            return False
    raise ValueError(
        f"Cannot interpret {value!r} as a boolean — expected true/false "
        "or one of 'true','false','1','0','yes','no'."
    )


def _auto_setup_guard(name: str) -> str | None:
    """If a tool is called with no credentials, return a clear error listing
    the missing environment variables instead of failing cryptically.

    Returns None to pass through normally.
    """
    has_session = bool(get_session())
    has_token = bool(get_git_token())

    # Cookie-only tools: need just the session cookie
    if name in _COOKIE_ONLY_TOOLS:
        if has_session:
            return None
        return (
            f"❌ `{name}` requires the OVERLEAF_SESSION environment variable, "
            "but it is not set.\n\n" + _MISSING_CREDENTIALS_HINT
        )

    # Git-based tools: need both session and git token
    if has_session and has_token:
        return None
    missing = []
    if not has_session:
        missing.append("OVERLEAF_SESSION")
    if not has_token:
        missing.append("OVERLEAF_GIT_TOKEN")
    return (
        f"❌ `{name}` requires the following environment variable(s) which "
        f"are not set: {', '.join(missing)}.\n\n" + _MISSING_CREDENTIALS_HINT
    )


async def _dispatch(name: str, args: dict[str, Any]) -> str:
    """Route a tool call to the appropriate handler."""

    # Short-circuit with a clear error if required credentials are missing
    guard = _auto_setup_guard(name)
    if guard is not None:
        return guard

    # ── CREATE ────────────────────────────────────────────────────────────

    if name == "create_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.create_file,
            project,
            args["file_path"],
            args["content"],
            args.get("commit_message"),
        )

    if name == "create_project":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        result = await asyncio.to_thread(_compile_mod.create_project_web, args["name"])
        # Emit a human-readable confirmation that shows BOTH the natural
        # name the user just typed AND the full 24-hex project_id that
        # every other tool needs. Historically we dumped the bare JSON
        # dict here, which made the UI show only "🔌 overleaf/create_project"
        # with no clue *which* project was created — see chatui issue
        # "improve overleaf-mcp UX" (2026-04-29).
        pid = result.get("id", "?")
        pname = result.get("name", args.get("name", "?"))
        short = f"{pid[:5]}…{pid[-4:]}" if pid and len(pid) >= 10 else pid
        url = project_url(pid)
        open_line = f"   Open: {url}\n" if url else ""
        return (
            f"✅ Created Overleaf project [{pname}]\n"
            f"   project_id: {pid}  (short: {short})\n"
            f"{open_line}"
            f"   Pass this project_id to other overleaf tools "
            f"(create_file, edit_file, compile_project, …)."
        )

    # ── READ ──────────────────────────────────────────────────────────────

    if name == "list_projects":
        if not _HAS_COMPILE:
            return (
                "Error: compile extras required for list_projects.\n"
                "Run: pip install overleaf-mcp[compile]"
            )
        if not get_session():
            return (
                "❌ OVERLEAF_SESSION env var is not set.\n\n"
                + _MISSING_CREDENTIALS_HINT
            )
        try:
            web_projects = await asyncio.to_thread(_compile_mod.list_projects_web)
        except Exception as e:
            return (
                f"Error fetching projects: {e}\n\n"
                "If your session cookie has expired (~30 days), update the "
                "OVERLEAF_SESSION environment variable with a fresh value.\n\n"
                + _MISSING_CREDENTIALS_HINT
            )

        if not web_projects:
            return "No projects found in your Overleaf account."

        lines = [f"Your Overleaf projects ({len(web_projects)}):"]
        lines.append("")
        for wp in web_projects:
            url = project_url(wp["id"])
            tail = f"  {url}" if url else ""
            lines.append(f"  • {wp['name']}  [{wp['id']}]{tail}")
        lines.append("")
        lines.append("Pass any project ID to other tools (e.g. read_file, edit_file, compile_project).")
        return "\n".join(lines)

    if name == "list_files":
        project = get_project(args["project_id"])
        files = await asyncio.to_thread(
            git_client.list_files, project, args.get("extension", "")
        )
        if not files:
            ext = args.get("extension", "")
            return f"No files found{' with extension ' + ext if ext else ''}"
        return f"Files ({len(files)}):\n" + "\n".join(f"  • {f}" for f in files)

    if name == "read_file":
        project = get_project(args["project_id"])
        content = await asyncio.to_thread(git_client.read_file, project, args["file_path"])
        return f"── {args['file_path']} ({len(content)} chars) ──\n\n{content}"

    if name == "verify_citations":
        ok, why = _verify_mod.verify_available()
        if not ok:
            return f"Error: {why}"
        project = get_project(args["project_id"])
        explicit = (args.get("bib_path") or "").strip()
        if explicit:
            bib_paths = [explicit]
        else:
            bib_paths = await asyncio.to_thread(
                git_client.list_files, project, _verify_mod._BIB_EXT)
        if not bib_paths:
            return ("No .bib file found in the project. Pass bib_path explicitly "
                    "if your bibliography lives elsewhere.")

        def _read(p):
            return git_client.read_file(project, p)

        combined, read_paths = await asyncio.to_thread(
            _verify_mod.collect_bibtex, _read, bib_paths)
        if not combined.strip():
            return f"Bibliography file(s) {', '.join(bib_paths)} were empty or unreadable."
        result = await asyncio.to_thread(_verify_mod.run_verification, combined)
        return _verify_mod.format_report(result, read_paths)

    if name == "get_sections":
        project = get_project(args["project_id"])
        content = await asyncio.to_thread(git_client.read_file, project, args["file_path"])
        sections = parse_sections(content)
        if not sections:
            return f"No sections found in '{args['file_path']}'"
        lines = [f"Sections in '{args['file_path']}' ({len(sections)} total):"]
        for s in sections:
            indent = "  " * s["level"]
            lines.append(f"\n{indent}[{s['type']}] {s['title']}")
            lines.append(f"{indent}  {s['preview'][:120]}…")
        return "\n".join(lines)

    if name == "get_section_content":
        project = get_project(args["project_id"])
        content = await asyncio.to_thread(git_client.read_file, project, args["file_path"])
        section = get_section_content(content, args["section_title"])
        if section is None:
            available = [s["title"] for s in parse_sections(content)]
            return (
                f"Section '{args['section_title']}' not found.\n"
                f"Available: {', '.join(available)}"
            )
        return section

    if name == "list_history":
        project = get_project(args["project_id"])
        commits = await asyncio.to_thread(
            git_client.list_history,
            project,
            limit=args.get("limit"),
            file_path=args.get("file_path"),
            since=args.get("since"),
            until=args.get("until"),
        )
        if not commits:
            return "No commits found"
        lines = ["Commit history:"]
        for c in commits:
            lines.append(f"  {c['short']} | {c['date']} | {c['author']}")
            lines.append(f"           {c['message']}")
        return "\n".join(lines)

    if name == "get_diff":
        project = get_project(args["project_id"])
        result = await asyncio.to_thread(
            git_client.get_diff,
            project,
            from_ref=args.get("from_ref"),
            to_ref=args.get("to_ref"),
            file_path=args.get("file_path"),
            context_lines=args.get("context_lines"),
            max_chars=args.get("max_chars"),
        )
        diff = result["diff"]
        if not diff:
            return "No differences found"
        suffix = "\n\n[diff truncated]" if result["truncated"] else ""
        return f"Diff:\n\n{diff}{suffix}"

    if name == "status_summary":
        project = get_project(args["project_id"])

        # Resolve the project's natural (human) name via the web dashboard,
        # since ProjectConfig.name is only a placeholder derived from the ID.
        natural_name: str | None = None
        if _HAS_COMPILE and get_session():
            try:
                web_projects = await asyncio.to_thread(_compile_mod.list_projects_web)
                for wp in web_projects:
                    if wp.get("id") == project.project_id:
                        natural_name = wp.get("name")
                        break
            except Exception as e:
                logger.warning("status_summary: could not resolve natural name: %s", e)

        files = await asyncio.to_thread(git_client.list_files, project, ".tex")
        all_files = await asyncio.to_thread(git_client.list_files, project)

        title = natural_name if natural_name else project.name
        url = project_url(project.project_id)
        summary = [
            f"📄 Project: {title}  [{project.project_id}]",
        ]
        if url:
            summary.append(f"   URL: {url}")
        summary += [
            f"   Total files: {len(all_files)}",
            f"   .tex files: {len(files)}",
        ]

        if files:
            main_file = next((f for f in files if "main" in f.lower()), files[0])
            content = await asyncio.to_thread(git_client.read_file, project, main_file)
            sections = parse_sections(content)
            summary.append(f"\n📋 Structure of {main_file} ({len(sections)} sections):")
            # Show every section in full — no truncation. Callers rely on
            # status_summary for a complete bird's-eye view of the project.
            for i, s in enumerate(sections):
                indent = "  " * s["level"]
                summary.append(f"   {indent}{i + 1}. [{s['type']}] {s['title']}")

        return "\n".join(summary)

    # ── UPDATE ────────────────────────────────────────────────────────────

    if name == "edit_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.edit_file,
            project,
            args["file_path"],
            args["old_string"],
            args["new_string"],
            args.get("commit_message"),
        )

    if name == "rewrite_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.rewrite_file,
            project,
            args["file_path"],
            args["content"],
            args.get("commit_message"),
        )

    if name == "update_section":
        project = get_project(args["project_id"])
        content = await asyncio.to_thread(git_client.read_file, project, args["file_path"])
        new_content = update_section(content, args["section_title"], args["new_content"])
        if new_content is None:
            available = [s["title"] for s in parse_sections(content)]
            return f"Section '{args['section_title']}' not found. Available: {', '.join(available)}"
        return await asyncio.to_thread(
            git_client.rewrite_file,
            project,
            args["file_path"],
            new_content,
            args.get("commit_message", f"Update section '{args['section_title']}'"),
        )

    if name == "sync_project":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(git_client.sync_project, project)

    if name == "upload_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.upload_file,
            project,
            args["file_path"],
            args["source_path"],
            args.get("commit_message"),
            _coerce_bool(args.get("overwrite"), default=False),
        )

    # ── DELETE ────────────────────────────────────────────────────────────

    if name == "delete_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.delete_file,
            project,
            args["file_path"],
            args.get("commit_message"),
        )

    # ── COMPILE / PDF ────────────────────────────────────────────────────

    if name == "compile_project":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        result = await asyncio.to_thread(_compile_mod.compile_project, args["project_id"])
        return json.dumps(result, indent=2)

    if name == "download_pdf":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        return await asyncio.to_thread(
            _compile_mod.download_pdf, args["project_id"], args["output_path"]
        )

    if name == "download_log":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        import httpx
        from .compile import _headers, _build_output_url  # noqa: F811

        pid = args["project_id"]
        compile_result = await asyncio.to_thread(_compile_mod.compile_project, pid)
        output_files = compile_result.get("output_files", [])
        log_file = next((f for f in output_files if f.get("path", "").endswith(".log")), None)
        if not log_file:
            return f"No .log file in compile output. Status: {compile_result.get('status')}"
        # Use the per-build URL from compile_project output_files and append
        # ?clsiserverid=<id> (required by the CLSI CDN as of 2026-05; without
        # it every per-build URL returns HTTP 404). The legacy shortcut
        # /project/<id>/output/output.log also returns 404 on current Overleaf.
        log_path = log_file.get("url") or f"/project/{pid}/output/{log_file['path']}"
        log_url = _build_output_url(log_path, compile_result.get("clsi_server_id"))
        r = httpx.get(log_url, headers=_headers(), follow_redirects=True, timeout=30)
        r.raise_for_status()
        text = r.text
    # ── LAYOUT / SYNCTEX ─────────────────────────────────────────────────

    if name == "get_page_count":
        if not _HAS_LAYOUT:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        result = await asyncio.to_thread(_layout_mod.get_page_count, args["project_id"])
        pages = result.get("pages")
        if pages is None:
            return (
                f"Could not determine page count (compile status="
                f"{result.get('status')}). Check compilation with download_log."
            )
        return (
            f"{pages} page(s)  (status={result.get('status')}, "
            f"source={result.get('source')})"
        )

    if name == "locate_in_pdf":
        if not _HAS_LAYOUT:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        result = await asyncio.to_thread(
            _layout_mod.locate_in_pdf,
            args["project_id"],
            int(args["line"]),
            args.get("file"),
            int(args.get("column", 0)),
        )
        if result.get("error"):
            return f"Error: {result['error']}"
        if result.get("page") is None:
            return (
                f"No SyncTeX mapping for {result.get('file')!r} line "
                f"{result.get('line')} (blank line/comment?). Try a nearby "
                "non-empty line."
            )
        return json.dumps(result, indent=2)

    if name == "section_page_map":
        if not _HAS_LAYOUT:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        result = await asyncio.to_thread(
            _layout_mod.section_page_map, args["project_id"], args.get("file")
        )
        return _layout_mod.format_section_page_map(result)

        if len(text) > 50000:
            text = text[-50000:]
            return f"[… truncated to last 50000 chars]\n\n{text}"
        return text

    if name == "download_source_zip":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        return await asyncio.to_thread(
            _compile_mod.download_source_zip, args["project_id"], args["output_path"]
        )

    if name == "download_source":
        if not _HAS_COMPILE:
            return "Error: compile extras required. pip install overleaf-mcp[compile]"
        return await asyncio.to_thread(
            _compile_mod.download_source,
            args["project_id"],
            args["output_dir"],
            _coerce_bool(args.get("overwrite"), default=False),
        )

    # ── FREE-TIER WEB WRITE (session-cookie only, no git token) ──────────

    if name == "web_list_files":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        files = await asyncio.to_thread(_web_write_mod.list_files_web, args["project_id"])
        if not files:
            return "No files found"
        return f"Files ({len(files)}):\n" + "\n".join(f" • {f}" for f in files)

    if name == "web_read_file":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        content = await asyncio.to_thread(
            _compile_mod.read_file_web, args["project_id"], args["file_path"]
        )
        return f"── {args['file_path']} ({len(content)} chars) ──\n\n{content}"

    if name == "web_rewrite_file":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        return await asyncio.to_thread(
            _web_write_mod.upload_file_content,
            args["project_id"],
            args["file_path"],
            args["content"],
        )

    if name == "web_edit_file":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        return await asyncio.to_thread(
            _web_write_mod.edit_file_content,
            args["project_id"],
            args["file_path"],
            args["old_string"],
            args["new_string"],
        )

    if name == "web_upload_file":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        return await asyncio.to_thread(
            _web_write_mod.upload_file_base64,
            args["project_id"],
            args["file_path"],
            args["content_base64"],
        )

    if name == "web_delete_file":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        return await asyncio.to_thread(
            _web_write_mod.delete_file_web,
            args["project_id"],
            args["file_path"],
        )

    if name == "web_get_sections":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        pid = args["project_id"]
        content = await asyncio.to_thread(_compile_mod.read_file_web, pid, args["file_path"])
        sections = parse_sections(content)
        if not sections:
            return f"No sections found in '{args['file_path']}'"
        lines = [f"Sections in '{args['file_path']}' ({len(sections)} total):"]
        for s in sections:
            indent = "  " * s["level"]
            lines.append(f"\n{indent}[{s['type']}] {s['title']}")
            lines.append(f"{indent}  {s['preview'][:120]}…")
        return "\n".join(lines)

    if name == "web_get_section_content":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        pid = args["project_id"]
        content = await asyncio.to_thread(_compile_mod.read_file_web, pid, args["file_path"])
        section = get_section_content(content, args["section_title"])
        if section is None:
            available = [s["title"] for s in parse_sections(content)]
            return (
                f"Section '{args['section_title']}' not found.\n"
                f"Available: {', '.join(available)}"
            )
        return section

    if name == "web_update_section":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        pid = args["project_id"]
        content = await asyncio.to_thread(_compile_mod.read_file_web, pid, args["file_path"])
        new_content = update_section(content, args["section_title"], args["new_content"])
        if new_content is None:
            available = [s["title"] for s in parse_sections(content)]
            return f"Section '{args['section_title']}' not found. Available: {', '.join(available)}"
        return await asyncio.to_thread(
            _web_write_mod.upload_file_content, pid, args["file_path"], new_content
        )

    if name == "web_status_summary":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        pid = args["project_id"]

        natural_name: str | None = None
        try:
            web_projects = await asyncio.to_thread(_compile_mod.list_projects_web)
            for wp in web_projects:
                if wp.get("id") == pid:
                    natural_name = wp.get("name")
                    break
        except Exception as e:
            logger.warning("web_status_summary: could not resolve natural name: %s", e)

        all_files = await asyncio.to_thread(_web_write_mod.list_files_web, pid)
        tex_files = [f for f in all_files if f.lower().endswith(".tex")]

        title = natural_name if natural_name else pid
        url = project_url(pid)
        summary = [f"📄 Project: {title} [{pid}]"]
        if url:
            summary.append(f"   URL: {url}")
        summary += [
            f"   Total files: {len(all_files)}",
            f"   .tex files: {len(tex_files)}",
        ]

        if tex_files:
            main_file = next((f for f in tex_files if "main" in f.lower()), tex_files[0])
            content = await asyncio.to_thread(_compile_mod.read_file_web, pid, main_file)
            sections = parse_sections(content)
            summary.append(f"\n📋 Structure of {main_file} ({len(sections)} sections):")
            for i, s in enumerate(sections):
                indent = "  " * s["level"]
                summary.append(f"   {indent}{i + 1}. [{s['type']}] {s['title']}")

        return "\n".join(summary)

    if name == "web_verify_citations":
        if not _HAS_WEB_WRITE:
            return "Error: compile extras + websockets required. pip install 'overleaf-mcp[compile]' websockets"
        ok, why = _verify_mod.verify_available()
        if not ok:
            return f"Error: {why}"
        pid = args["project_id"]
        explicit = (args.get("bib_path") or "").strip()
        if explicit:
            bib_paths = [explicit]
        else:
            all_files = await asyncio.to_thread(_web_write_mod.list_files_web, pid)
            bib_paths = [f for f in all_files if f.endswith(_verify_mod._BIB_EXT)]
        if not bib_paths:
            return ("No .bib file found in the project. Pass bib_path explicitly "
                    "if your bibliography lives elsewhere.")

        def _read(p):
            return _compile_mod.read_file_web(pid, p)

        combined, read_paths = await asyncio.to_thread(
            _verify_mod.collect_bibtex, _read, bib_paths)
        if not combined.strip():
            return f"Bibliography file(s) {', '.join(bib_paths)} were empty or unreadable."
        result = await asyncio.to_thread(_verify_mod.run_verification, combined)
        return _verify_mod.format_report(result, read_paths)

    return f"Unknown tool: {name}"


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
