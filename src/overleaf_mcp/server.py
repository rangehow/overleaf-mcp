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

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import git_client
from .config import get_project
from .credentials import get_git_token, get_session
from .latex import get_section_content, parse_sections, update_section

logger = logging.getLogger("overleaf-mcp")

# Optional compile module (gracefully missing)
try:
    from . import compile as _compile_mod

    _HAS_COMPILE = True
except ImportError:
    _HAS_COMPILE = False

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
        "These tools operate on the REMOTE Overleaf repo; if you already have "
        "the project downloaded locally, use read_files / grep_search instead. "
        "Always call list_projects first when unsure."
    ),
}

# ══════════════════════════════════════════════════════════════════════════
# Tool definitions
# ══════════════════════════════════════════════════════════════════════════

_TOOLS: list[Tool] = [
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
                    "type": "boolean",
                    "description": "If true, extract even if output_dir is non-empty. Default false.",
                },
            },
            "required": ["project_id", "output_dir"],
        },
    ),
]


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

server = Server("overleaf-mcp", instructions=_SERVER_INSTRUCTIONS)


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=f"Error: {e}")]


_COOKIE_ONLY_TOOLS = {
    "list_projects",
    "compile_project",
    "download_pdf",
    "download_log",
    "download_source_zip",
    "download_source",
    "create_project",
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


def _auto_setup_guard(name: str) -> str | None:
    has_session = bool(get_session())
    has_token = bool(get_git_token())

    if name in _COOKIE_ONLY_TOOLS:
        if has_session:
            return None
        return (
            f"❌ `{name}` requires the OVERLEAF_SESSION environment variable, "
            "but it is not set.\n\n" + _MISSING_CREDENTIALS_HINT
        )

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
    guard = _auto_setup_guard(name)
    if guard is not None:
        return guard

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
        return json.dumps(result, indent=2)

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
            lines.append(f"  • {wp['name']}  [{wp['id']}]")
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
        summary = [
            f"📄 Project: {title}  [{project.project_id}]",
            f"   Total files: {len(all_files)}",
            f"   .tex files: {len(files)}",
        ]

        if files:
            main_file = next((f for f in files if "main" in f.lower()), files[0])
            content = await asyncio.to_thread(git_client.read_file, project, main_file)
            sections = parse_sections(content)
            summary.append(f"\n📋 Structure of {main_file} ({len(sections)} sections):")
            for i, s in enumerate(sections):
                indent = "  " * s["level"]
                summary.append(f"   {indent}{i + 1}. [{s['type']}] {s['title']}")

        return "\n".join(summary)

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

    if name == "delete_file":
        project = get_project(args["project_id"])
        return await asyncio.to_thread(
            git_client.delete_file,
            project,
            args["file_path"],
            args.get("commit_message"),
        )

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
        from .compile import _headers, OVERLEAF_BASE_URL  # noqa: F811

        pid = args["project_id"]
        compile_result = await asyncio.to_thread(_compile_mod.compile_project, pid)
        output_files = compile_result.get("output_files", [])
        log_file = next((f for f in output_files if f.get("path", "").endswith(".log")), None)
        if not log_file:
            return f"No .log file in compile output. Status: {compile_result.get('status')}"
        log_path = log_file.get("url") or f"/project/{pid}/output/{log_file['path']}"
        log_url = log_path if log_path.startswith("http") else f"{OVERLEAF_BASE_URL}{log_path}"
        r = httpx.get(log_url, headers=_headers(), follow_redirects=True, timeout=30)
        r.raise_for_status()
        text = r.text
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
            bool(args.get("overwrite", False)),
        )

    return f"Unknown tool: {name}"


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
